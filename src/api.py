"""냉장고 관리 Agent HTTP API.

제출 규약대로 `POST /query`로 `question`을 받아 `answer`, `contexts`, `trace` 세 키로 답한다.
요청자 신원(`X-User-Token`)과 스레드 소유권을 확인한 뒤 에이전트 한 턴을 실행하고,
특수 상황(SERVICE.md 8장)은 `trace.errors`에 `{code, message}`로 담는다.

실행: `uvicorn src.api:app`
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, Header
from langchain_core.callbacks import BaseCallbackHandler
from pydantic import BaseModel, Field

from src import guardrails, retriever
from src.agent import open_agent, pending_interrupt, run_turn
from src.middleware import is_model_unavailable
from src.tools import (
    GUEST_DENIED,
    NOT_IN_GUIDE,
    REJECT_ANSWERS,
    STORAGE_GUIDE,
    GUEST_USER,
    FridgeContext,
    connect,
    list_alerts,
    now_iso,
    rescued_g_week,
    reset_db,
    resend_unsynced_meals,
    resolve_item,
    WATCH_FAILED,
)

load_dotenv()

# 특수 상황 응답 문구 (SERVICE.md 8장 표)
MSG_NO_PENDING = "대기 중인 승인 요청이 없어요"
MSG_THREAD_MISMATCH = "이 대화에 접근할 수 없어요"
MSG_IDENTITY_MISMATCH = "사용자 확인에 실패했어요"
MSG_AGENT_ERROR = "요청을 처리하지 못했어요. 잠시 후 다시 시도해 주세요"
MSG_MODEL_UNAVAILABLE = "지금 AI 모델에 연결할 수 없어요. 잠시 후 다시 시도해 주세요"

# 답변 근거(contexts)를 돌려주는 도구
CONTEXT_TOOLS = {"list_inventory", "search_recipes", "search_docs", "get_recipe", "rank_recipes", "get_storage_days"}


class QueryRequest(BaseModel):
    """`POST /query` 요청 본문."""

    question: str = Field(description="사용자 질문. resume 요청에서도 필수지만 trace에만 기록한다")
    user_id: str | None = Field(default=None, description="요청자. 주면 X-User-Token과 같아야 한다")
    thread_id: str | None = Field(default=None, description="대화 스레드. 없으면 서버가 만든다")
    resume: Literal["approve", "reject"] | None = Field(default=None, description="대기 중인 승인에 대한 답")


class QueryResponse(BaseModel):
    """`POST /query` 응답. 세 키 고정 (제출 규약)."""

    answer: str
    contexts: list[str]
    trace: dict[str, Any]


class IdentityError(Exception):
    """토큰과 `user_id`로 요청자를 확인하지 못했을 때 낸다."""


def parse_user_tokens(raw: str | None = None) -> dict[str, str]:
    """`.env`의 `USER_TOKENS`(`dad:토큰1,mom:토큰2`)를 `{user_id: 토큰}`으로 읽는다."""
    raw = os.environ.get("USER_TOKENS", "") if raw is None else raw
    tokens: dict[str, str] = {}
    for pair in raw.split(","):
        user, sep, token = pair.strip().partition(":")
        if sep and user.strip() and token.strip():
            tokens[user.strip()] = token.strip()
    return tokens


def resolve_identity(token: str | None, user_id: str | None) -> str:
    """요청자 `user_id`를 정한다 (SERVICE.md 8장 신원 확인).

    - 토큰이 있으면 `USER_TOKENS`와 상수 시간 비교로 대조한다. 본문 `user_id`가 다르면 실패.
    - 토큰 없이 `user_id`만 있으면 실패한다(guest로 낮추지 않음).
    - 둘 다 없으면 `DEFAULT_USER`, 그것도 비어 있으면 조회 전용 `guest`.
    실패하면 IdentityError를 낸다.
    """
    token = (token or "").strip()
    user_id = (user_id or "").strip() or None
    if token:
        matched = None
        for user, expected in parse_user_tokens().items():
            if hmac.compare_digest(token.encode(), expected.encode()):
                matched = user
        if matched is None or (user_id is not None and user_id != matched):
            raise IdentityError
        return matched
    if user_id is not None:
        raise IdentityError
    return os.environ.get("DEFAULT_USER", "").strip() or GUEST_USER


def claim_thread(db_path: str | None, thread_id: str, user_id: str) -> bool:
    """스레드 소유자를 확인한다. 처음 보는 스레드면 요청자를 소유자로 기록한다.

    다른 사용자의 스레드면 False를 돌려준다.
    """
    conn = connect(db_path)
    try:
        with conn:
            row = conn.execute("SELECT user_id FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO threads (thread_id, user_id, created_at) VALUES (?, ?, ?)",
                    (thread_id, user_id, now_iso()),
                )
                return True
            return row["user_id"] == user_id
    finally:
        conn.close()


def new_thread_id(user_id: str) -> str:
    """새 스레드 ID(`user_id-YYYYMMDD-8자리`)를 만든다."""
    return f"{user_id}-{time.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"


HANDOFF_PREFIX = "transfer_to_"  # langgraph-supervisor 핸드오프 도구 이름 앞부분


class StepRecorder(BaseCallbackHandler):
    """한 턴 동안 실행된 도구 이름·소요 시간과 도구 결과를 모은다 (`trace.steps`, `contexts`용)."""

    def __init__(self) -> None:
        """기록을 비운다."""
        self.steps: list[dict[str, Any]] = []
        self.outputs: list[tuple[str, str]] = []
        self._started: dict[Any, tuple[str, float]] = {}

    def on_tool_start(self, serialized: dict, input_str: str, *, run_id: Any, **kwargs: Any) -> None:
        """도구 시작 시각을 기록한다. Supervisor의 `transfer_to_*` 호출은 handoff 이벤트로 남긴다."""
        name = (serialized or {}).get("name") or kwargs.get("name") or "tool"
        if name.startswith(HANDOFF_PREFIX):
            self.steps.append({"agent": "supervisor", "event": "handoff", "to": name[len(HANDOFF_PREFIX):]})
            return
        self._started[run_id] = (name, time.perf_counter())

    def on_tool_end(self, output: Any, *, run_id: Any, **kwargs: Any) -> None:
        """도구 소요 시간과 결과 문자열을 기록한다."""
        if run_id not in self._started:
            return  # handoff
        name, ms = self._finish(run_id)
        self.steps.append({"tool": name, "ms": ms})
        self.outputs.append((name, str(getattr(output, "content", output))))

    def on_tool_error(self, error: BaseException, *, run_id: Any, **kwargs: Any) -> None:
        """승인 대기(interrupt)나 오류로 끝난 도구를 기록한다."""
        if run_id not in self._started:
            return  # handoff (Supervisor로 돌아가는 신호도 예외로 올라온다)
        name, ms = self._finish(run_id)
        event = "interrupt" if type(error).__name__ == "GraphInterrupt" else "error"
        self.steps.append({"tool": name, "ms": ms, "event": event})

    def on_custom_event(self, name: str, data: Any, *, run_id: Any, **kwargs: Any) -> None:
        """식단 Plan-Execute 그래프의 계획(`plan`)·실행 단계(`plan_step`)를 남긴다."""
        if name in ("plan", "plan_step"):
            self.steps.append({"agent": "meal_planner_agent", "event": name, **(data or {})})

    def on_chat_model_start(self, serialized: dict, messages: Any, *, run_id: Any, metadata: dict | None = None,
                            **kwargs: Any) -> None:
        """대화 이력 요약 미들웨어가 요약 모델을 부르면 `summary` 이벤트를 남긴다."""
        if (metadata or {}).get("lc_source") == "summarization":
            self.steps.append({"event": "summary"})

    def _finish(self, run_id: Any) -> tuple[str, int]:
        """시작 기록을 꺼내 (도구 이름, 경과 ms)를 돌려준다."""
        name, start = self._started.pop(run_id, ("tool", time.perf_counter()))
        return name, int((time.perf_counter() - start) * 1000)


def recommendation_context(item: dict[str, Any]) -> str:
    """추천 요리 한 개를 근거 문자열로 만든다.

    요약뿐 아니라 슬롯, 카테고리, 주재료, 평점 또는 신메뉴 여부, 1인분 kcal, 임박 재료(D-day),
    부족 재료까지 담는다. 모두 `rank_recipes`가 계산한 값이라 답의 주장을 그대로 뒷받침한다.
    가족 추천의 평점은 그룹 평점이라 다른 가족의 개별 기록은 들어가지 않는다.
    """
    if item.get("is_new"):
        rating = "신메뉴(평가 없음)"
    else:
        rating = f"평점 {item.get('rating')}({item.get('rating_count', 0)}회 평가)"
    imminent = ", ".join(f"{u['item']} D-{u['d_day']}" for u in item.get("imminent_ingredients", [])) or "없음"
    missing = ", ".join(item.get("missing_ingredients", [])) or "없음"
    facts = [
        f"{item.get('slot') or item['cuisine']} 슬롯",
        item["cuisine"],
        f"주재료 {item['main_ingredient']}",
        rating,
        f"1인분 {item['kcal_per_serving']:g}kcal",
        f"임박 재료 {imminent}",
        f"부족 재료 {missing}",
    ]
    return f"[{item['source']}#{item['dish']}] {' / '.join(facts)}\n{item.get('summary', '')}"


def inventory_context(row: dict[str, Any]) -> str:
    """재고 한 건을 근거 문자열로 만든다.

    답은 "한우 D-2"처럼 재고 품목명을 쓰고 추천 결과는 "소고기"처럼 보관 가이드 품목명을 쓰므로,
    두 이름을 함께 적어 어느 쪽 주장도 근거를 찾을 수 있게 한다. D-day는 도구가 계산한 값이다.
    """
    guide = resolve_item(row["item"])
    name = f"{row['item']}(보관 가이드 품목 {guide})" if guide and guide != row["item"] else row["item"]
    expiry = row.get("expiry_date") or "미상"
    d_day = row.get("d_day")
    d_text = "D-day 미상" if d_day is None else ("D-day" if d_day == 0 else f"D{'-' if d_day > 0 else '+'}{abs(d_day)}")
    level = {"imminent": " / 임박", "expired": " / 만료"}.get(row.get("level") or "", "")
    return f"[재고#{row['sn']}] {name} {row['qty']:g}{row['unit']} / 유효기간 {expiry} / {d_text}{level}"


def extract_contexts(outputs: list[tuple[str, str]]) -> list[str]:
    """근거 도구 결과에서 `contexts` 문자열 목록을 만든다. 같은 문자열은 한 번만 넣는다."""
    contexts: list[str] = []

    def add(text: str) -> None:
        text = text.strip()
        if text and text not in contexts:
            contexts.append(text)

    for name, output in outputs:
        if name not in CONTEXT_TOOLS:
            continue
        try:
            data = json.loads(output)
        except (ValueError, TypeError):
            if output.startswith("["):  # get_recipe: "[recipes_x.md#요리] ..."
                add(output)
            elif name == "get_storage_days" and output.startswith(NOT_IN_GUIDE):
                # 가이드에 없는 품목: "망고는 가이드에 없다"는 답의 근거로 담는다
                item = output.removeprefix(NOT_IN_GUIDE).lstrip(": ").split(".", 1)[0].strip()
                add(f"[{STORAGE_GUIDE.name}#{item}] {output}")
            continue
        if name == "list_inventory":
            for row in data:
                add(inventory_context(row))
        elif name in ("search_recipes", "search_docs"):
            for hit in data.get("results", []):
                add(hit)
        elif name == "rank_recipes":
            # 답에 쓰이는 도구 안내·빈 슬롯·요리별 결정적 값을 모두 근거로 담는다
            for note in data.get("notes", []):
                add(f"[rank_recipes#안내] {note}")
            for empty in data.get("empty_slots", []):
                add(f"[rank_recipes#{empty['slot']}] 추천 없음: {empty['reason']}")
            for item in data.get("items", []):
                add(recommendation_context(item))
        elif name == "get_storage_days" and data.get("source"):
            # 답에 그대로 옮길 수 있게 문장으로 담는다(RAGAS가 JSON 키보다 문장을 근거로 잘 찾는다)
            add(
                f"[{data['source']}#{data.get('item', '')}] {data.get('item', '')}의 권장 보관일수는 "
                f"{data.get('basis', '')} {data['storage_days']}일이에요 (보관 가이드 품목 {data.get('guide_item', '')})"
            )
    return contexts


def _domain_trace(db_path: str | None) -> dict[str, Any]:
    """도메인 DB에서 `trace.alerts`와 `trace.rescued_g_week`를 읽는다."""
    conn = connect(db_path)
    try:
        return {"alerts": list_alerts(conn), "rescued_g_week": rescued_g_week(conn)["total_g"]}
    finally:
        conn.close()


def _watch_failed(steps: list[dict]) -> bool:
    """MCP `record_meal` 호출 기록 중 실패가 있는지 본다."""
    return any(s.get("tool") == "record_meal" and s.get("event") == "error" for s in steps)


def _error(code: str, message: str) -> dict[str, str]:
    """`trace.errors` 항목을 만든다."""
    return {"code": code, "message": message}


async def _pending(agent: Any, thread_id: str) -> dict | None:
    """스레드에 대기 중인 승인 요청이 있으면 그 값(type, summary)을 돌려준다."""
    snapshot = await agent.aget_state({"configurable": {"thread_id": thread_id}})
    interrupts = list(getattr(snapshot, "interrupts", ()) or ())
    if not interrupts:
        for task in getattr(snapshot, "tasks", ()) or ():
            interrupts.extend(task.interrupts or ())
    return pending_interrupt({"__interrupt__": interrupts})


async def handle_query(agent: Any, req: QueryRequest, token: str | None, db_path: str | None) -> QueryResponse:
    """`/query` 한 건을 처리한다. 신원 → 스레드 소유권 → 승인 상태 → 입력 가드레일 → 에이전트 실행 순서다.

    모든 응답은 출력 가드레일(개인정보·다른 가족 칼로리 마스킹)을 거친다. trace의 질문도 같은 방식으로 가린다.
    """
    trace: dict[str, Any] = {
        "thread_id": req.thread_id,
        "user_id": None,
        "question": guardrails.mask_output(req.question, "")[0],
        "resume": req.resume,
        "alerts": [],
        "pending_interrupt": None,
        "steps": [],
        "rescued_g_week": 0,
        "errors": [],
        "index": dict(retriever.BUILD_STATS),
        "guardrail": {"input": None, "output": []},
    }

    def reply(answer: str, contexts: list[str] | None = None) -> QueryResponse:
        """현재 trace로 응답을 만든다. 답은 출력 가드레일로 개인정보를 가린 뒤 돌려준다."""
        masked, kinds = guardrails.mask_output(answer, trace["user_id"] or "")
        trace["guardrail"]["output"] = kinds
        return QueryResponse(answer=masked, contexts=contexts or [], trace=trace)

    try:
        user_id = resolve_identity(token, req.user_id)
    except IdentityError:
        trace["errors"].append(_error("IDENTITY_MISMATCH", "토큰과 user_id가 맞지 않거나 토큰이 없어요"))
        return reply(MSG_IDENTITY_MISMATCH)
    trace["user_id"] = user_id

    thread_id = req.thread_id or new_thread_id(user_id)
    trace["thread_id"] = thread_id
    if not claim_thread(db_path, thread_id, user_id):
        trace["errors"].append(_error("THREAD_OWNER_MISMATCH", "다른 사용자의 thread_id예요"))
        return reply(MSG_THREAD_MISMATCH)

    # 지난 요청에서 워치 기록에 실패한 식사를 같은 멱등 키로 다시 보낸다 (SERVICE.md 5.3 4단계).
    resend_steps = await asyncio.to_thread(resend_unsynced_meals, db_path, user_id)
    trace["steps"] = list(resend_steps)
    if _watch_failed(resend_steps):
        trace["errors"].append(_error("WATCH_SYNC_FAILED", "지난 식사의 워치 재전송에 실패했어요. 다음 요청 때 다시 시도해요"))

    trace.update(_domain_trace(db_path))
    pending = await _pending(agent, thread_id)
    if req.resume and not pending:
        trace["errors"].append(_error("NO_PENDING_INTERRUPT", "이 스레드에 대기 중인 승인 요청이 없어요"))
        return reply(MSG_NO_PENDING)
    if not req.resume and pending:
        trace["pending_interrupt"] = pending
        trace["errors"].append(_error("INTERRUPT_PENDING", "승인 또는 거절 후 다시 질문해 주세요"))
        return reply(f"{pending['summary']} (approve/reject)")

    if not req.resume:
        verdict = guardrails.check_input(req.question, user_id)
        trace["guardrail"]["input"] = verdict.as_trace()
        if verdict.blocked:
            trace["errors"].append(_error(verdict.code, "입력 가드레일에서 막은 요청이에요"))
            return reply(verdict.message)

    recorder = StepRecorder()
    context = FridgeContext(user_id=user_id, db_path=db_path)
    try:
        turn = await run_turn(agent, req.question, context, thread_id, resume=req.resume, callbacks=[recorder])
    except Exception as exc:  # noqa: BLE001 - 어떤 실패든 세 키 형식으로 돌려준다
        trace["steps"] = resend_steps + recorder.steps + context.mcp_calls
        if is_model_unavailable(exc):  # 서브 에이전트 재시도 끝 실패, 또는 Supervisor 모델의 Bedrock 오류
            trace["errors"].append(_error("MODEL_UNAVAILABLE", f"{type(exc).__name__}: {exc}"[:300]))
            return reply(MSG_MODEL_UNAVAILABLE)
        trace["errors"].append(_error("AGENT_ERROR", f"{type(exc).__name__}: {exc}"[:300]))
        return reply(MSG_AGENT_ERROR)

    answer = turn["answer"]
    # 도구 호출 기록 뒤에, 식사 확정 도구가 코드에서 부른 MCP record_meal 기록을 붙인다.
    trace["steps"] = resend_steps + recorder.steps + context.mcp_calls
    if _watch_failed(context.mcp_calls):
        trace["errors"].append(_error("WATCH_SYNC_FAILED", "워치 기록에 실패했어요. 다음 요청 때 같은 키로 다시 보내요"))
        if not ("워치" in answer and "실패" in answer):  # 모델이 안내를 빼먹어도 답변에 남긴다 (SERVICE.md 5.3 3단계)
            answer = f"{answer}\n{WATCH_FAILED}"
    trace["pending_interrupt"] = turn["pending_interrupt"]
    if any(output.strip() == GUEST_DENIED for _, output in recorder.outputs):
        trace["errors"].append(_error("GUEST_READ_ONLY", "guest는 조회만 할 수 있어요"))
        answer = GUEST_DENIED
    # 거절로 재개해 승인 도구가 취소 문장을 돌려줬으면, 모델이 바꿔 쓴 답 대신 그 문장을 그대로 쓴다
    rejected = next((o.strip() for _, o in recorder.outputs if o.strip() in REJECT_ANSWERS), None)
    if req.resume and rejected and not turn["pending_interrupt"]:
        answer = rejected
    trace.update(_domain_trace(db_path))
    trace["index"] = dict(retriever.BUILD_STATS)  # 질문 처리 중에 인덱스를 다시 만들지 않았는지 확인용
    return reply(answer, extract_contexts(recorder.outputs))


def ensure_domain_db(db_path: str | None = None) -> None:
    """도메인 DB에 테이블이 없으면 스키마와 시드로 만든다. 있으면 그대로 둔다."""
    conn = connect(db_path)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'threads'"
        ).fetchone()
    finally:
        conn.close()
    if not exists:
        reset_db(db_path)


def create_app(model: Any = None, db_path: str | None = None) -> FastAPI:
    """API 앱을 만든다. 테스트에서는 가짜 모델과 임시 도메인 DB 경로를 넣는다.

    서버 시작(lifespan) 때 도메인 DB를 확인하고, 검색 인덱스(BM25·Chroma)를 한 번 만들고,
    체크포인터(`data/checkpoints.db`)를 연 에이전트를 한 번 만들어 모든 요청이 같이 쓴다.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """체크포인터를 연 에이전트를 서버가 떠 있는 동안 유지한다."""
        ensure_domain_db(db_path)
        retriever.warm_up()
        async with open_agent(model=model) as agent:
            app.state.agent = agent
            yield

    app = FastAPI(title="냉장고 관리 Agent", lifespan=lifespan)

    @app.post("/query", response_model=QueryResponse)
    async def query(req: QueryRequest, x_user_token: str | None = Header(default=None)) -> QueryResponse:
        """질문 한 건을 처리해 `answer`, `contexts`, `trace`를 돌려준다."""
        return await handle_query(app.state.agent, req, x_user_token, db_path)

    return app


app = create_app()
