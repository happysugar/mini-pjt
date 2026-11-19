"""Supervisor 분할 테스트 (TASK-15, SERVICE.md 6장 #9, 7장 HITL과 Supervisor).

가짜 모델(`tests.test_api.FakeModel`)로 Bedrock 없이 확인한다: handoff·승인 대기 trace,
서브 에이전트 안 승인의 재개, 상위 대화 이력 요약(지우거나 중복시키지 않음).
신원 전달과 guest 제한은 `tests/test_api.py`의 기존 테스트가 분할 뒤에도 그대로 확인한다.
"""

import asyncio
import sys
from pathlib import Path

import pytest

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import middleware, tools  # noqa: E402
from src.agent import SUB_AGENTS, build_agent, run_turn  # noqa: E402
from tests.test_api import DAD, MEAL, FakeModel, call, counts, env, make_client, say  # noqa: E402,F401


def handoffs(response: dict) -> list[str]:
    """응답 trace의 handoff 대상 에이전트 목록."""
    return [s["to"] for s in response["trace"]["steps"] if s.get("event") == "handoff"]


def test_sub_agents_have_separate_tools():
    # 승인 도구는 맡은 에이전트에만 있다: 출고·폐기는 재고, 식사 확정은 평가·헬스.
    names = {agent: {t.name for t in agent_tools} for agent, (_, agent_tools) in SUB_AGENTS.items()}
    assert {"withdraw_item", "discard_item", "add_items_from_text"} <= names["inventory_agent"]
    assert {"rank_recipes", "search_recipes", "search_docs", "get_storage_days"} <= names["recommend_agent"]
    assert "confirm_meal" in names["rating_health_agent"]
    assert "confirm_meal" not in names["inventory_agent"] | names["recommend_agent"]


@pytest.mark.usefixtures("allow_free_text_writes")
def test_meal_approval_inside_sub_agent_resumes_once(make_client, env):
    _, ask = make_client(call("confirm_meal", MEAL), say("저장했어요."))
    r1 = ask({"question": "규동 2인분 만들어서 1인분 먹었어, 4점"}, DAD)
    assert handoffs(r1) == ["rating_health_agent"]
    assert {"tool": "confirm_meal", "event": "interrupt"}.items() <= next(
        s for s in r1["trace"]["steps"] if s.get("tool") == "confirm_meal"
    ).items()
    assert r1["trace"]["pending_interrupt"]["type"] == "meal_confirm" and counts(env)["meals"] == 0
    r2 = ask({"question": "승인", "thread_id": r1["trace"]["thread_id"], "resume": "approve"}, DAD)
    assert r2["answer"] == "저장했어요." and r2["trace"]["pending_interrupt"] is None
    assert counts(env)["meals"] == 1 and counts(env)["ratings"] == 16


def test_greeting_needs_no_handoff(make_client):
    _, ask = make_client(say("안녕하세요."))
    r = ask({"question": "안녕"}, DAD)
    assert r["answer"] == "안녕하세요." and handoffs(r) == []


def test_parent_history_is_summarized_once_without_duplicates(tmp_path, monkeypatch):
    monkeypatch.setenv("FRIDGE_TODAY", "2026-11-20")
    db = str(tmp_path / "fridge.db")
    tools.reset_db(db)
    script = []
    for i in range(4):
        script += [call("list_inventory", {}, f"c{i}"), say(f"재고 답 {i} " + "설명 " * 150)]
    model = FakeModel(messages=iter(script))
    summarizer = GenericFakeChatModel(messages=iter([AIMessage(content=f"요약 {i}") for i in range(10)]))
    compactor = middleware.HistoryCompactor(model, summarizer=summarizer, trigger_tokens=600, keep_tokens=200)
    agent = build_agent(checkpointer=InMemorySaver(), model=model, history_compactor=compactor)
    ctx = tools.FridgeContext(user_id="dad", db_path=db)

    async def run() -> tuple[list, list]:
        """4턴을 실행하고 (턴별 결과, 마지막 상위 이력)을 돌려준다."""
        outs = [await run_turn(agent, f"질문 {i} 재고 알려줘", ctx, "t") for i in range(4)]
        state = await agent.aget_state({"configurable": {"thread_id": "t"}})
        return outs, state.values["messages"]

    outs, messages = asyncio.run(run())
    assert any(o["summarized"] for o in outs)
    assert [o["answer"][:6] for o in outs] == [f"재고 답 {i}" for i in range(4)]  # 요약 뒤에도 답이 그대로
    summaries = [m for m in messages if (m.additional_kwargs or {}).get("lc_source") == "summarization"]
    assert len(summaries) == 1 and messages[0] is summaries[0]  # 요약은 하나, 이력 맨 앞
    assert len({m.id for m in messages}) == len(messages)  # 같은 메시지가 두 번 들어가지 않는다
    questions = [m.content for m in messages if m.type == "human" and str(m.content).startswith("질문")]
    assert questions == ["질문 3 재고 알려줘"]  # 앞선 질문은 요약으로 대체됐다


@pytest.mark.usefixtures("allow_free_text_writes")
def test_pending_approval_is_not_summarized(make_client, env):
    # 승인 대기 중에는 임계값을 넘어도 이력을 건드리지 않는다(재개할 체크포인트 보호).
    client, ask = make_client(call("confirm_meal", MEAL), say("저장했어요."))
    r1 = ask({"question": "규동 먹었어, 4점"}, DAD)
    assert r1["trace"]["pending_interrupt"] is not None
    agent = client.app.state.agent
    config = {"configurable": {"thread_id": r1["trace"]["thread_id"]}}
    compactor = middleware.HistoryCompactor(GenericFakeChatModel(messages=iter([])), trigger_tokens=2, keep_tokens=1)
    before = client.portal.call(agent.aget_state, config).values["messages"]
    assert client.portal.call(compactor.compact, agent, config) is False
    assert client.portal.call(agent.aget_state, config).values["messages"] == before


# ── 평가 입력 Form 확정은 confirm_meal_form 호출을 강제한다 ──────────


def test_form_confirm_request_forces_confirm_meal_form():
    from langchain_core.messages import HumanMessage, ToolMessage

    from src.agent import form_confirm_tool_choice

    ask = HumanMessage(f"{tools.FORM_CONFIRM_REQUEST}: 규동 2인분, dad 1인분 4점, mom 1인분 5점")
    assert form_confirm_tool_choice([ask]) == "confirm_meal_form"
    # 이미 불렀으면 강제를 풀어 결과 요약은 평소대로 답하게 한다
    done = [ask, call("confirm_meal_form", {}), ToolMessage("저장했어요", tool_call_id="call-1")]
    assert form_confirm_tool_choice(done) is None
    # 다른 요청이나 이전 턴의 Form 요청은 강제하지 않는다
    assert form_confirm_tool_choice([HumanMessage("규동 먹었어, 4점")]) is None
    assert form_confirm_tool_choice([ask, say("확정했어요."), HumanMessage("재고 알려줘")]) is None


def test_form_confirm_middleware_overrides_tool_choice():
    from langchain_core.messages import HumanMessage

    from src.agent import FormConfirmToolChoice

    class Req:
        """tool_choice를 바꾼 복사본만 확인하는 최소 요청 객체."""

        def __init__(self, messages, tool_choice=None):
            self.messages, self.tool_choice = messages, tool_choice

        def override(self, **kw):
            return Req(self.messages, kw.get("tool_choice"))

    mw = FormConfirmToolChoice()
    seen = []
    form = Req([HumanMessage(f"{tools.FORM_CONFIRM_REQUEST}: 규동 2인분, dad 1인분 4점")])
    mw.wrap_model_call(form, lambda r: seen.append(r.tool_choice))
    asyncio.run(mw.awrap_model_call(Req([HumanMessage("규동 먹었어")]), _async_seen(seen)))
    assert seen == ["confirm_meal_form", None]


def _async_seen(seen: list):
    """비동기 handler: 받은 요청의 tool_choice를 기록한다."""

    async def handler(request):
        seen.append(request.tool_choice)

    return handler


def test_rating_agent_uses_form_confirm_middleware(make_client, env, monkeypatch):
    """API의 Form 확정 흐름에서 평가 에이전트의 모델 호출이 미들웨어를 거친다."""
    from src import agent as agent_mod
    from tests.test_api import FORM_TWO

    picked = []
    original = agent_mod.form_confirm_tool_choice
    monkeypatch.setattr(agent_mod, "form_confirm_tool_choice", lambda msgs: picked.append(original(msgs)) or picked[-1])
    _, ask = make_client(call("confirm_meal_form", {}), say("저장했어요."))
    r = ask({"question": FORM_TWO, "thread_id": "dad-form-mw"}, DAD)
    assert r["trace"]["pending_interrupt"]["type"] == "meal_form_confirm"
    assert picked and picked[0] == "confirm_meal_form"


def test_inventory_form_request_forces_its_own_tool_only_where_bound():
    from langchain_core.messages import HumanMessage

    from src.agent import FormConfirmToolChoice, form_confirm_tool_choice

    ask = HumanMessage(f"{tools.INVENTORY_FORM_REQUEST}: 입고 두부 2개")
    assert form_confirm_tool_choice([ask]) == "confirm_inventory_form"

    class Req:
        """도구 목록과 메시지만 가진 최소 요청 객체."""

        def __init__(self, names):
            self.messages = [ask]
            self.tools = [type("T", (), {"name": n})() for n in names]

    assert FormConfirmToolChoice._choice(Req(["list_inventory", "confirm_inventory_form"])) == "confirm_inventory_form"
    assert FormConfirmToolChoice._choice(Req(["get_recipe", "confirm_meal_form"])) is None  # 그 에이전트에 없는 도구는 강제하지 않는다
