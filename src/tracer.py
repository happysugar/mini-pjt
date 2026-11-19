"""로컬 트레이서 (SERVICE.md 6장 #11 Observability·Trace).

요청 한 건의 실행 이벤트를 모아 `trace.jsonl`(기본 `data/trace.jsonl`, `TRACE_FILE`로 변경)에 한 줄씩 남기고,
같은 이벤트로 응답의 `trace.steps`와 개념별 이벤트 수(`trace.concepts`)를 만든다.

이벤트 이름과 개념 번호 (리포트·시연에서 개념별로 찾는 기준)
- `tool`(#2, 검색 도구는 #3, 워치 조회 도구는 #5): 서브 에이전트가 고른 도구 호출과 소요 시간
- `multi_tool`(#4): 한 요청에서 성격이 다른 도구(재고 DB·계산기·검색·외부 시스템)를 2종류 이상 쓴 경우
- `mcp`(#5): 코드가 부른 워치 MCP 호출(`record_meal`)
- `guardrail.input`, `guardrail.output`(#6): 입력 검사 결과, 출력 마스킹 결과
- `interrupt`, `resume`(#7): 승인 대기와 승인·거절 재개
- `middleware`(#8): 서브 에이전트 미들웨어 노드(PII 마스킹, 도구 호출 하나만 남기기 등) 실행, `summary`(#8): 이력 요약
- `handoff`, `agent`(#9): Supervisor 핸드오프, 서브 에이전트 실행 시작
- `node`: 그 밖의 그래프 노드(모델 호출 등), `request`·`response`: 요청 시작과 끝
서브 에이전트(서브그래프) 이벤트에는 `agent`(예: recommend_agent)와 `ns`(checkpoint 네임스페이스)를 붙인다.

`.env`에 LangSmith 키(`LANGSMITH_API_KEY`)가 있으면 `configure_langsmith()`가 LangChain 기본 트레이싱을 켜서
같은 실행이 LangSmith에도 전송된다.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

TRACE_FILE_DEFAULT = Path(__file__).resolve().parent.parent / "data" / "trace.jsonl"
HANDOFF_PREFIX = "transfer_to_"
SUB_AGENT_NAMES = ("inventory_agent", "recommend_agent", "rating_health_agent")

CONCEPTS = {
    2: "ReAct",
    3: "RAG",
    4: "도구 다중",
    5: "MCP",
    6: "가드레일",
    7: "HITL",
    8: "미들웨어",
    9: "Supervisor",
    11: "Trace",
}

# 도구 성격 (#4 도구 다중 판정). 없는 도구는 "db"로 본다.
TOOL_KINDS = {
    "search_recipes": "retrieval",
    "search_docs": "retrieval",
    "calc_kcal": "calculator",
    "get_storage_days": "calculator",
    "get_daily_intake": "external",
    "get_calorie_budget": "external",
    "record_meal": "external",
}
RETRIEVAL_TOOLS = {"search_recipes", "search_docs"}
MCP_QUERY_TOOLS = {"get_daily_intake", "get_calorie_budget"}
MIDDLEWARE_MARKERS = ("Middleware", ".before_model", ".after_model", "pre_model_hook", "post_model_hook")

_WRITE_LOCK = threading.Lock()


def trace_file() -> Path:
    """이벤트를 남길 파일 경로. `TRACE_FILE` 환경변수가 없으면 `data/trace.jsonl`."""
    return Path(os.environ.get("TRACE_FILE", "").strip() or TRACE_FILE_DEFAULT)


def configure_langsmith() -> bool:
    """LangSmith 키가 있으면 LangChain 트레이싱을 켠다. 켰는지(또는 이미 켜져 있는지)를 돌려준다.

    키는 `.env`의 `LANGSMITH_API_KEY`(또는 예전 이름 `LANGCHAIN_API_KEY`)에서 읽고 코드에 적지 않는다.
    프로젝트 이름은 `LANGSMITH_PROJECT`가 없으면 `mini-pjt`.
    """
    if not (os.environ.get("LANGSMITH_API_KEY") or os.environ.get("LANGCHAIN_API_KEY")):
        return False
    os.environ.setdefault("LANGSMITH_TRACING", "true")
    os.environ.setdefault("LANGSMITH_PROJECT", "mini-pjt")
    return os.environ["LANGSMITH_TRACING"].lower() == "true"


def _agent_of(metadata: dict | None) -> tuple[str, str]:
    """콜백 metadata의 checkpoint 네임스페이스에서 (에이전트 이름, 네임스페이스)를 꺼낸다."""
    ns = (metadata or {}).get("checkpoint_ns") or (metadata or {}).get("langgraph_checkpoint_ns") or ""
    first = ns.split("|", 1)[0].split(":", 1)[0]
    return (first if first in SUB_AGENT_NAMES else "supervisor"), ns


def tool_concepts(name: str) -> list[int]:
    """도구 이벤트가 보여 주는 개념 번호."""
    concepts = [2]
    if name in RETRIEVAL_TOOLS:
        concepts.append(3)
    if name in MCP_QUERY_TOOLS:
        concepts.append(5)
    return concepts


class RequestTracer(BaseCallbackHandler):
    """요청 한 건의 이벤트를 모으는 콜백 핸들러.

    - `events`: `trace.jsonl`에 남길 이벤트 목록
    - `steps`: 응답 `trace.steps` (도구·핸드오프·interrupt·요약·MCP 호출 요약)
    - `outputs`: (도구 이름, 결과 문자열) 목록. API가 `contexts`를 만들 때 쓴다.
    """

    def __init__(self, request_id: str | None = None, thread_id: str | None = None, user_id: str | None = None) -> None:
        """빈 기록으로 시작한다. 요청 정보는 나중에 `bind()`로 채울 수 있다."""
        self.request_id = request_id or uuid.uuid4().hex[:12]
        self.thread_id = thread_id
        self.user_id = user_id
        self.events: list[dict[str, Any]] = []
        self.steps: list[dict[str, Any]] = []
        self.outputs: list[tuple[str, str]] = []
        self._started: dict[Any, tuple[str, float, str, str]] = {}
        self._last_node: tuple[str, str] | None = None
        self._t0 = time.perf_counter()

    # ── 공통 ──────────────────────────────────────────────────

    def bind(self, thread_id: str | None = None, user_id: str | None = None) -> None:
        """신원 확인 뒤 알게 된 thread_id·user_id를 붙인다."""
        self.thread_id = thread_id or self.thread_id
        self.user_id = user_id or self.user_id

    def emit(self, event: str, concepts: list[int] | None = None, step: dict | None = None, **fields: Any) -> dict:
        """이벤트 하나를 기록한다. `step`을 주면 응답 `trace.steps`에도 그대로 넣는다."""
        record = {"seq": len(self.events) + 1, "t_ms": int((time.perf_counter() - self._t0) * 1000),
                  "event": event, "concepts": concepts or [], **{k: v for k, v in fields.items() if v is not None}}
        self.events.append(record)
        if step is not None:
            self.steps.append(step)
        return record

    def concept_counts(self) -> dict[str, int]:
        """개념 번호별 이벤트 수 (`trace.concepts`). 키는 "#2 ReAct" 형식."""
        counts: dict[str, int] = {}
        for record in self.events:
            for c in record["concepts"]:
                key = f"#{c} {CONCEPTS.get(c, '')}".strip()
                counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: int(kv[0][1:].split()[0])))

    # ── API가 직접 남기는 이벤트 ────────────────────────────────

    def request(self, question: str, resume: str | None) -> None:
        """요청 시작. resume이면 HITL 재개 이벤트도 남긴다."""
        self.emit("request", question=question, resume=resume)
        if resume:
            self.emit("resume", [7], decision=resume)

    def guardrail(self, stage: str, detail: Any) -> None:
        """입력 검사(`input`) 또는 출력 마스킹(`output`) 결과를 남긴다."""
        self.emit(f"guardrail.{stage}", [6], detail=detail)

    def mcp_calls(self, calls: list[dict[str, Any]]) -> None:
        """코드가 부른 워치 MCP 호출(`record_meal`)을 이벤트와 steps에 남긴다."""
        for call in calls:
            self.emit("mcp", [5], step=dict(call), name=call.get("tool"), status=call.get("event"),
                      caller=call.get("caller"), ms=call.get("ms"))

    def finish(self, status: str, errors: list[dict[str, Any]] | None = None) -> None:
        """요청 끝: 도구 다중(#4) 판정 이벤트와 응답 이벤트를 남기고, 전체를 `trace.jsonl`에 쓴다."""
        kinds = sorted({TOOL_KINDS.get(e["name"], "db") for e in self.events if e["event"] in ("tool", "mcp") and e.get("name")})
        if len(kinds) >= 2:
            self.emit("multi_tool", [4], kinds=kinds)
        self.emit("response", [11], status=status, errors=[e.get("code") for e in errors or []] or None)
        self.write()

    def write(self, path: Path | None = None) -> Path:
        """이벤트를 JSON Lines로 덧붙여 쓴다. 줄마다 요청 정보(request_id, thread_id, user_id, ts)를 붙인다."""
        path = Path(path or trace_file())
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().isoformat(timespec="seconds")
        base = {"ts": ts, "request_id": self.request_id, "thread_id": self.thread_id, "user_id": self.user_id}
        lines = [json.dumps({**base, **record}, ensure_ascii=False, default=str) for record in self.events]
        with _WRITE_LOCK, path.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return path

    # ── LangChain 콜백 ────────────────────────────────────────

    def on_chain_start(self, serialized: dict, inputs: Any, *, run_id: Any, metadata: dict | None = None,
                       **kwargs: Any) -> None:
        """그래프 노드 시작: 미들웨어(#8), 서브 에이전트 실행(#9), 그 밖의 노드로 나눠 남긴다."""
        node = (metadata or {}).get("langgraph_node")
        name = kwargs.get("name") or (serialized or {}).get("name")
        if not node or name != node:
            return  # 노드 안쪽 Runnable은 건너뛴다
        agent, ns = _agent_of(metadata)
        if (node, ns) == self._last_node:
            return  # 서브그래프 노드는 바깥·안쪽에서 두 번 시작된다
        self._last_node = (node, ns)
        if any(marker in node for marker in MIDDLEWARE_MARKERS):
            self.emit("middleware", [8], name=node, agent=agent, ns=ns)
        elif node in SUB_AGENT_NAMES:
            self.emit("agent", [9], name=node, ns=ns)
        else:
            self.emit("node", name=node, agent=agent, ns=ns)

    def on_tool_start(self, serialized: dict, input_str: str, *, run_id: Any, metadata: dict | None = None,
                      **kwargs: Any) -> None:
        """도구 시작. Supervisor의 `transfer_to_*` 호출은 handoff(#9)로 남긴다."""
        name = (serialized or {}).get("name") or kwargs.get("name") or "tool"
        agent, ns = _agent_of(metadata)
        if name.startswith(HANDOFF_PREFIX):
            to = name[len(HANDOFF_PREFIX):]
            self.emit("handoff", [9], step={"agent": "supervisor", "event": "handoff", "to": to}, name=name, to=to)
            return
        self._started[run_id] = (name, time.perf_counter(), agent, ns)

    def on_tool_end(self, output: Any, *, run_id: Any, **kwargs: Any) -> None:
        """도구 끝: 소요 시간과 결과 문자열을 기록한다."""
        if run_id not in self._started:
            return  # handoff
        name, ms, agent, ns = self._finish(run_id)
        self.emit("tool", tool_concepts(name), step={"tool": name, "ms": ms}, name=name, agent=agent, ns=ns, ms=ms)
        self.outputs.append((name, str(getattr(output, "content", output))))

    def on_tool_error(self, error: BaseException, *, run_id: Any, **kwargs: Any) -> None:
        """승인 대기(interrupt, #7)나 오류로 끝난 도구를 기록한다."""
        if run_id not in self._started:
            return  # handoff (Supervisor로 돌아가는 신호도 예외로 올라온다)
        name, ms, agent, ns = self._finish(run_id)
        if type(error).__name__ == "GraphInterrupt":
            self.emit("interrupt", [7], step={"tool": name, "ms": ms, "event": "interrupt"}, name=name, agent=agent,
                      ns=ns, ms=ms)
        else:
            self.emit("tool", tool_concepts(name), step={"tool": name, "ms": ms, "event": "error"}, name=name,
                      agent=agent, ns=ns, ms=ms, status="error", error=f"{type(error).__name__}: {error}"[:200])

    def on_chat_model_start(self, serialized: dict, messages: Any, *, run_id: Any, metadata: dict | None = None,
                            **kwargs: Any) -> None:
        """대화 이력 요약 미들웨어가 요약 모델을 부르면 `summary`(#8) 이벤트를 남긴다."""
        if (metadata or {}).get("lc_source") == "summarization":
            self.emit("summary", [8], step={"event": "summary"})

    def _finish(self, run_id: Any) -> tuple[str, int, str, str]:
        """시작 기록을 꺼내 (도구 이름, 경과 ms, 에이전트, 네임스페이스)를 돌려준다."""
        name, start, agent, ns = self._started.pop(run_id, ("tool", time.perf_counter(), "supervisor", ""))
        return name, int((time.perf_counter() - start) * 1000), agent, ns
