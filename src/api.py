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
from pydantic import BaseModel, Field

from src import guardrails, retriever
from src.tracer import RequestTracer, configure_langsmith
from src.agent import open_agent, pending_interrupt, run_turn
from src.middleware import is_model_unavailable
from src.tools import (
    GUEST_DENIED,
    NOT_IN_GUIDE,
    REJECT_ANSWERS,
    STORAGE_ITEMS,
    GUEST_USER,
    FridgeContext,
    check_inventory_form_stock,
    connect,
    list_alerts,
    now_iso,
    rescued_g_week,
    reset_db,
    save_inventory_form,
    save_meal_form,
    resend_unsynced_meals,
    sync_guide_db,
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


# 요청 한 건의 실행 이벤트 기록은 `src.tracer`가 맡는다. 예전 이름도 남겨 둔다(테스트·외부 코드 호환).
StepRecorder = RequestTracer


def recommendation_context(item: dict[str, Any]) -> str:
    """추천 요리 한 개를 근거 문자열로 만든다.

    요약뿐 아니라 슬롯, 카테고리, 주재료, 평점 또는 신메뉴 여부, 1인분 kcal, 임박 재료(D-day),
    부족 재료까지 담는다. 모두 `rank_recipes`가 계산한 값이라 답의 주장을 그대로 뒷받침한다.
    가족 추천의 평점은 그룹 평점이라 다른 가족 구성원의 개별 기록은 들어가지 않는다.
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
                add(f"[{STORAGE_ITEMS.name}#{item}] {output}")
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


def unsupported_category_note(outputs: list[tuple[str, str]]) -> str | None:
    """이번 턴의 `rank_recipes` 결과 notes에서 지원하지 않는 카테고리 안내 문장을 찾는다. 없으면 None."""
    for name, output in outputs:
        if name != "rank_recipes":
            continue
        try:
            notes = json.loads(output).get("notes", [])
        except (ValueError, TypeError, AttributeError):
            continue
        for note in notes:
            if "지원하지 않아요" in note:
                note = note.strip()
                return note if note.endswith((".", "!", "?")) else f"{note}."
    return None


def lead_with_note(answer: str, note: str) -> str:
    """답의 첫 문단을 도구 안내 문장으로 고정한다.

    모델이 안내를 바꿔 쓴 첫 문단("멕시코식 타코 레시피는 지원하지 않아요 …")은 안내 문장으로 바꾸고,
    첫 문단에 지원 안내가 없으면 안내 문장을 맨 앞에 붙인다. 나머지(대안 요리 설명)는 그대로 둔다.
    """
    head, sep, rest = answer.strip().partition("\n")
    if head.strip() == note:
        return answer
    if "지원하지 않" in head or "드릴 수 없" in head or "제공해 드리기 어렵" in head:
        return f"{note}{sep}{rest}" if sep else note
    return f"{note}\n\n{answer.strip()}"


async def _pending(agent: Any, thread_id: str) -> dict | None:
    """스레드에 대기 중인 승인 요청이 있으면 그 값(type, summary)을 돌려준다."""
    snapshot = await agent.aget_state({"configurable": {"thread_id": thread_id}})
    interrupts = list(getattr(snapshot, "interrupts", ()) or ())
    if not interrupts:
        for task in getattr(snapshot, "tasks", ()) or ():
            interrupts.extend(task.interrupts or ())
    return pending_interrupt({"__interrupt__": interrupts})


async def _last_answer_asks(agent: Any, thread_id: str) -> bool:
    """스레드의 마지막 AI 답이 사용자에게 묻는 문장(물음표로 끝남)이었는지 본다.

    재고 에이전트의 S/N 제안("제안한 S/N으로 저장할까요?")처럼 승인 대기(interrupt) 없이 되묻는 흐름에서는
    "그대로 진행해 줘"가 정상적인 답이라, 빈 승인 처리에서 빼려고 쓴다.
    """
    snapshot = await agent.aget_state({"configurable": {"thread_id": thread_id}})
    for message in reversed((snapshot.values or {}).get("messages", [])):
        if getattr(message, "type", "") == "ai" and not getattr(message, "tool_calls", None):
            content = message.content
            if isinstance(content, list):
                content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
            return str(content).strip().rstrip("😊🙂!. ").endswith("?")
    return False


async def handle_query(agent: Any, req: QueryRequest, token: str | None, db_path: str | None) -> QueryResponse:
    """`/query` 한 건을 처리한다. 신원 → 스레드 소유권 → 승인 상태 → 입력 가드레일 → 에이전트 실행 순서다.

    모든 응답은 출력 가드레일(개인정보·다른 가족 구성원 칼로리 마스킹)을 거친다. trace의 질문도 같은 방식으로 가린다.
    실행 이벤트는 `RequestTracer`가 모아 `trace.jsonl`에 남기고, 같은 이벤트로 `trace.steps`·`trace.concepts`를 만든다.
    """
    tracer = RequestTracer(thread_id=req.thread_id)
    masked_question = guardrails.mask_output(req.question, "")[0]
    tracer.request(masked_question, req.resume)
    trace: dict[str, Any] = {
        "thread_id": req.thread_id,
        "user_id": None,
        "question": masked_question,
        "resume": req.resume,
        "alerts": [],
        "pending_interrupt": None,
        "steps": [],
        "rescued_g_week": 0,
        "errors": [],
        "index": dict(retriever.BUILD_STATS),
        "guardrail": {"input": None, "format": None, "output": []},
        "request_id": tracer.request_id,
        "concepts": {},
    }

    def reply(answer: str, contexts: list[str] | None = None) -> QueryResponse:
        """현재 trace로 응답을 만든다. 답은 출력 가드레일로 개인정보를 가린 뒤 돌려준다."""
        masked, kinds = guardrails.mask_output(answer, trace["user_id"] or "")
        pending = trace["pending_interrupt"]
        if pending and pending.get("summary"):  # 승인 대기 요약도 응답의 일부이므로 같은 방식으로 가린다
            summary, pending_kinds = guardrails.mask_output(pending["summary"], trace["user_id"] or "")
            trace["pending_interrupt"] = {**pending, "summary": summary}
            kinds += [k for k in pending_kinds if k not in kinds]
        trace["guardrail"]["output"] = kinds
        tracer.guardrail("output", kinds)
        tracer.finish("error" if trace["errors"] else "ok", trace["errors"])
        trace["steps"] = tracer.steps
        trace["concepts"] = tracer.concept_counts()
        return QueryResponse(answer=masked, contexts=contexts or [], trace=trace)

    try:
        user_id = resolve_identity(token, req.user_id)
    except IdentityError:
        trace["errors"].append(_error("IDENTITY_MISMATCH", "토큰과 user_id가 맞지 않거나 토큰이 없어요"))
        return reply(MSG_IDENTITY_MISMATCH)
    trace["user_id"] = user_id

    thread_id = req.thread_id or new_thread_id(user_id)
    trace["thread_id"] = thread_id
    tracer.bind(thread_id=thread_id, user_id=user_id)
    if not claim_thread(db_path, thread_id, user_id):
        trace["errors"].append(_error("THREAD_OWNER_MISMATCH", "다른 사용자의 thread_id예요"))
        return reply(MSG_THREAD_MISMATCH)

    # 지난 요청에서 워치 기록에 실패한 식사를 같은 멱등 키로 다시 보낸다 (SERVICE.md 5.3 4단계).
    resend_steps = await asyncio.to_thread(resend_unsynced_meals, db_path, user_id)
    tracer.mcp_calls(resend_steps)
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
    if (not req.resume and guardrails.is_bare_approval(req.question)
            and not await _last_answer_asks(agent, thread_id)):
        # resume 없이 문장으로만 승인한 경우도 resume과 같은 특수 상황으로 보고 모델을 부르지 않는다.
        # 직전 답이 되물은 질문이면(S/N 제안 등) 그 답으로 보고 에이전트에 넘긴다.
        trace["errors"].append(_error("NO_PENDING_INTERRUPT", "대기 중인 승인 요청 없이 승인 문장만 왔어요"))
        return reply(guardrails.MSG_NO_PENDING_MEAL)

    if not req.resume:
        verdict = guardrails.check_input(req.question, user_id)
        trace["guardrail"]["input"] = verdict.as_trace()
        tracer.guardrail("input", trace["guardrail"]["input"])
        if verdict.blocked:
            trace["errors"].append(_error(verdict.code, "입력 가드레일에서 막은 요청이에요"))
            return reply(verdict.message)

    # recipe 평가 결과 입력 Form(JSON)이면 모델을 부르기 전에 형식과 값을 검사한다 (SERVICE.md 5.6).
    # 통과하면 Form을 이 스레드에 보관하고 확정 요청 문장을 에이전트에 넘긴다. 평가·헬스 에이전트의
    # confirm_meal_form이 보관된 값만 읽어 요청자 한 사람의 승인으로 평가자마다 저장한다.
    # 걸리면 저장하지 않고 고정 문구로 답한다.
    question = req.question
    if not req.resume:
        form = guardrails.check_rating_form(req.question, user_id)
        if form.is_form:
            trace["form"] = form.as_trace()
            if form.code:
                trace["errors"].append(_error(form.code, "; ".join(form.problems)))
                return reply(form.message)
            await asyncio.to_thread(save_meal_form, db_path, thread_id, user_id, form.form)
            question = form.question

    # 재고 입력 Form(JSON, 입고·출고·폐기)도 같은 방식이다 (SERVICE.md 5.1). 형식 검사 뒤 지금 재고와 맞춰 보고
    # (S/N 중복, 보유량 등), 통과하면 보관한 Form을 재고 에이전트의 confirm_inventory_form이 읽어 처리한다.
    if not req.resume and question == req.question:
        stock_form = guardrails.check_inventory_form(req.question)
        if stock_form.is_form:
            if not stock_form.code:
                problems = await asyncio.to_thread(check_inventory_form_stock, db_path, stock_form.form)
                if problems:
                    stock_form.code, stock_form.problems = "FORM_INVALID", problems
                    stock_form.message = f"{guardrails.MSG_INVENTORY_FORM_INVALID}: " + "; ".join(problems)
            trace["form"] = stock_form.as_trace()
            if stock_form.code:
                trace["errors"].append(_error(stock_form.code, "; ".join(stock_form.problems)))
                return reply(stock_form.message)
            await asyncio.to_thread(save_inventory_form, db_path, thread_id, user_id, stock_form.form)
            question = stock_form.question

    # 재고 입고·출고·폐기와 recipe 평가는 JSON Form으로만 받는다 (SERVICE.md 5.6). Form이 아닌 자연어 쓰기 요청은
    # 모델을 부르지 않고 Form을 쓰라고 안내한다. 조회·추천·보관 질문은 그대로 에이전트에 넘긴다.
    if not req.resume and question == req.question:
        write = guardrails.check_free_text_write(req.question)
        trace["guardrail"]["format"] = {"code": write.code, "reasons": write.problems} if write.code else None
        if write.code:
            tracer.guardrail("format", trace["guardrail"]["format"])
            trace["errors"].append(_error(write.code, "재고·평가 쓰기 요청은 JSON 입력 Form으로만 받아요"))
            return reply(write.message)

    context = FridgeContext(user_id=user_id, db_path=db_path)
    try:
        turn = await run_turn(agent, question, context, thread_id, resume=req.resume, callbacks=[tracer])
    except Exception as exc:  # noqa: BLE001 - 어떤 실패든 세 키 형식으로 돌려준다
        tracer.mcp_calls(context.mcp_calls)
        if is_model_unavailable(exc):  # 서브 에이전트 재시도 끝 실패, 또는 Supervisor 모델의 Bedrock 오류
            trace["errors"].append(_error("MODEL_UNAVAILABLE", f"{type(exc).__name__}: {exc}"[:300]))
            return reply(MSG_MODEL_UNAVAILABLE)
        trace["errors"].append(_error("AGENT_ERROR", f"{type(exc).__name__}: {exc}"[:300]))
        return reply(MSG_AGENT_ERROR)

    answer = turn["answer"]
    # 도구 호출 기록 뒤에, 식사 확정 도구가 코드에서 부른 MCP record_meal 기록을 붙인다.
    tracer.mcp_calls(context.mcp_calls)
    if _watch_failed(context.mcp_calls):
        trace["errors"].append(_error("WATCH_SYNC_FAILED", "워치 기록에 실패했어요. 다음 요청 때 같은 키로 다시 보내요"))
        if not ("워치" in answer and "실패" in answer):  # 모델이 안내를 빼먹어도 답변에 남긴다 (SERVICE.md 5.3 3단계)
            answer = f"{answer}\n{WATCH_FAILED}"
    trace["pending_interrupt"] = turn["pending_interrupt"]
    if any(output.strip() == GUEST_DENIED for _, output in tracer.outputs):
        trace["errors"].append(_error("GUEST_READ_ONLY", "guest는 조회만 할 수 있어요"))
        answer = GUEST_DENIED
    # 거절로 재개해 승인 도구가 취소 문장을 돌려줬으면, 모델이 바꿔 쓴 답 대신 그 문장을 그대로 쓴다
    rejected = next((o.strip() for _, o in tracer.outputs if o.strip() in REJECT_ANSWERS), None)
    if req.resume and rejected and not turn["pending_interrupt"]:
        answer = rejected
    # 지원하지 않는 카테고리 요청이면, 모델이 바꿔 쓴 안내 대신 rank_recipes 안내 문장을 첫 문단에 그대로 쓴다
    note = unsupported_category_note(tracer.outputs)
    if note and not turn["pending_interrupt"]:
        answer = lead_with_note(answer, note)
    trace.update(_domain_trace(db_path))
    trace["index"] = dict(retriever.BUILD_STATS)  # 질문 처리 중에 인덱스를 다시 만들지 않았는지 확인용
    return reply(answer, extract_contexts(tracer.outputs))


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
        sync_guide_db(db_path)  # 등록된 품목 중 기준표에 빠진 것을 덧붙인다(검색 인덱스를 만들기 전에)
        configure_langsmith()  # .env에 LangSmith 키가 있으면 같은 실행을 LangSmith에도 보낸다
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
