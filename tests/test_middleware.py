"""미들웨어 조립 테스트 (TASK-13, TASK-15, SERVICE.md 6장 #8).

가짜 채팅 모델로 Bedrock 없이 확인한다: 모델 입력의 전화번호 마스킹, 긴 대화의 요약 이벤트,
모델 재시도 최종 실패 시 `trace.errors`의 `MODEL_UNAVAILABLE`.
M4부터 마스킹은 Supervisor `pre_model_hook`, 요약은 턴 시작 전 상위 이력(`HistoryCompactor`), 재시도는 서브 에이전트 미들웨어가 한다.
대본이 답뿐인 가짜 모델은 Supervisor가 에이전트에 넘기지 않고 바로 답한다.
"""

import asyncio
import re
import sys
from pathlib import Path

import botocore.exceptions
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import api, middleware, tools  # noqa: E402
from src.agent import build_agent, run_turn  # noqa: E402


class RecordingModel(GenericFakeChatModel):
    """받은 모델 입력을 기록하고 정해진 답을 차례로 돌려주는 가짜 채팅 모델."""

    seen: list = []

    def bind_tools(self, tools, **kwargs):
        """도구 바인딩 요청을 받아도 자기 자신을 돌려준다."""
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        """입력 메시지를 기록한 뒤 다음 답을 돌려준다."""
        self.seen.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


THROTTLED = botocore.exceptions.ClientError(
    {"Error": {"Code": "ThrottlingException", "Message": "Too many tokens per day"}}, "Converse"
)


class FailingModel(RecordingModel):
    """호출할 때마다 Bedrock 한도 오류를 내는 가짜 채팅 모델 (Supervisor 모델 장애 흉내)."""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        """입력을 기록하고 항상 실패한다."""
        self.seen.append(list(messages))
        raise THROTTLED


class AgentFailingModel(RecordingModel):
    """Supervisor 자리에서는 재고 에이전트로 넘기고, 서브 에이전트 자리에서는 항상 실패하는 가짜 모델."""

    role: str = "agent"

    def bind_tools(self, tools, **kwargs):
        """핸드오프 도구가 있으면 Supervisor 자리로 본다."""
        names = [getattr(t, "name", "") for t in tools]
        return self.model_copy(update={"role": "supervisor" if any(n.startswith("transfer_to_") for n in names) else "agent"})

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        """Supervisor면 핸드오프, 서브 에이전트면 실패."""
        self.seen.append((self.role, list(messages)))
        if self.role == "supervisor":
            call = {"name": "transfer_to_inventory_agent", "args": {}, "id": f"h-{len(messages)}"}
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[call]))])
        raise ConnectionError("bedrock down")


def answers(*texts: str):
    """정해진 답 메시지 반복자를 만든다."""
    return iter([AIMessage(content=t) for t in texts])


def text_of(messages) -> str:
    """메시지 목록의 본문을 한 문자열로 잇는다."""
    return "\n".join(str(m.content) for m in messages)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """오늘 날짜, 기본 사용자, 임시 도메인 DB를 준비한다."""
    monkeypatch.setenv("FRIDGE_TODAY", "2026-11-20")
    monkeypatch.setenv("DEFAULT_USER", "dad")
    db = str(tmp_path / "fridge.db")
    tools.reset_db(db)
    return db


def ask(agent, question: str, db: str, thread_id: str = "t1", recorder=None) -> dict:
    """에이전트 한 턴을 dad로 실행한다."""
    ctx = tools.FridgeContext(user_id="dad", db_path=db)
    return asyncio.run(run_turn(agent, question, ctx, thread_id, callbacks=[recorder] if recorder else None))


# ── 정규식 ──────────────────────────────────────────────────────


@pytest.mark.parametrize("phone", ["010-1234-5678", "01012345678", "011-123-4567", "02-123-4567", "031 123 4567"])
def test_kr_phone_matches(phone):
    assert re.search(middleware.KR_PHONE_PATTERN, f"번호는 {phone}이야")


@pytest.mark.parametrize("text", ["S/N 2026-11-001", "2026-11-20에 넣었어", "한우 600g", "2026-11-014로 저장", "1000원"])
def test_kr_phone_ignores_sn_dates_and_amounts(text):
    assert not re.search(middleware.KR_PHONE_PATTERN, text)
    assert not re.search(middleware.KR_RRN_PATTERN, text)


def test_kr_rrn_matches():
    assert re.search(middleware.KR_RRN_PATTERN, "주민번호 900101-1234567")
    assert not re.search(middleware.KR_RRN_PATTERN, "S/N 2026-11-001")


# ── PII 마스킹 ──────────────────────────────────────────────────


def test_phone_is_masked_in_model_input(env):
    model = RecordingModel(messages=answers("저장하지 않아요."))
    model.seen = []
    agent = build_agent(checkpointer=InMemorySaver(), model=model)
    ask(agent, "엄마 번호 010-1234-5678, 메일 mom@example.com 기억해줘", env)
    sent = text_of(model.seen[0])
    assert "010-1234-5678" not in sent and "[REDACTED_KR_PHONE]" in sent
    assert "mom@example.com" not in sent and "[REDACTED_EMAIL]" in sent


def test_sn_is_not_masked(env):
    model = RecordingModel(messages=answers("확인했어요."))
    model.seen = []
    agent = build_agent(checkpointer=InMemorySaver(), model=model)
    ask(agent, "S/N 2026-11-001 두부 상태 알려줘", env)
    assert "2026-11-001" in text_of(model.seen[0])


# ── 요약 ────────────────────────────────────────────────────────


def test_long_conversation_leaves_summary_event(env):
    model = RecordingModel(messages=answers(*[f"답변 {i}. " + "재고 설명 " * 200 for i in range(4)]))
    model.seen = []
    summarizer = RecordingModel(messages=answers(*["요약: 사용자가 재고를 여러 번 물었다."] * 5))
    compactor = middleware.HistoryCompactor(model, summarizer=summarizer, trigger_tokens=300, keep_tokens=100)
    agent = build_agent(checkpointer=InMemorySaver(), model=model, history_compactor=compactor)
    events = []
    for i in range(3):
        recorder = api.StepRecorder()
        ask(agent, f"질문 {i}: 냉장고에 뭐 있어?", env, recorder=recorder)
        events += [s for s in recorder.steps if s.get("event") == "summary"]
    assert events, "긴 대화에서 summary 이벤트가 trace.steps에 남아야 해요"
    # 요약 뒤 모델 입력은 요약 메시지로 시작하고, 앞선 질문 원문은 빠진다.
    last_input = text_of(model.seen[-1])
    assert "요약: 사용자가 재고를 여러 번 물었다." in last_input and "질문 0" not in last_input


def test_keep_must_be_smaller_than_trigger():
    with pytest.raises(ValueError):
        middleware.build_middleware(object(), trigger_tokens=100, keep_tokens=100)
    with pytest.raises(ValueError):
        middleware.HistoryCompactor(object(), trigger_tokens=100, keep_tokens=100)


def test_short_conversation_has_no_summary(env):
    model = RecordingModel(messages=answers("네."))
    agent = build_agent(checkpointer=InMemorySaver(), model=model)
    recorder = api.StepRecorder()
    ask(agent, "안녕", env, recorder=recorder)
    assert not [s for s in recorder.steps if s.get("event") == "summary"]


# ── 재시도 ──────────────────────────────────────────────────────


def test_sub_agent_model_failure_after_retries_is_model_unavailable(env):
    model = AgentFailingModel(messages=answers())
    model.seen = []
    mw = middleware.build_middleware(model, summarize=False, initial_delay=0)
    agent = build_agent(checkpointer=InMemorySaver(), model=model, middleware=mw)
    resp = asyncio.run(api.handle_query(agent, api.QueryRequest(question="냉장고에 뭐 있어?"), None, env))
    assert [e["code"] for e in resp.trace["errors"]] == ["MODEL_UNAVAILABLE"]
    assert resp.answer == api.MSG_MODEL_UNAVAILABLE
    agent_calls = [role for role, _ in model.seen if role == "agent"]
    assert len(agent_calls) == 1 + middleware.MAX_RETRIES  # 서브 에이전트 첫 시도 + 재시도 2번
    assert resp.trace["steps"][0] == {"agent": "supervisor", "event": "handoff", "to": "inventory_agent"}


def test_supervisor_bedrock_error_is_model_unavailable(env):
    model = FailingModel(messages=answers())
    model.seen = []
    agent = build_agent(checkpointer=InMemorySaver(), model=model)
    resp = asyncio.run(api.handle_query(agent, api.QueryRequest(question="냉장고에 뭐 있어?"), None, env))
    assert [e["code"] for e in resp.trace["errors"]] == ["MODEL_UNAVAILABLE"]
    assert "ThrottlingException" in resp.trace["errors"][0]["message"]
