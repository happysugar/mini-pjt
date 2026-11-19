"""Plan-Execute 식단 그래프 테스트 (TASK-18, SERVICE.md 6장 #10).

Bedrock 없이 확인한다: 결정적 실행(임박 재료 사용, 요리 중복 없음, 가족 안전 필터), 계획 검사,
가짜 구조화 출력 모델로 그래프 전체 실행과 trace의 계획·실행 이벤트, Supervisor 연결.
"""

import asyncio
import sys
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from langgraph.checkpoint.memory import InMemorySaver

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import api, planner, tools  # noqa: E402
from src.agent import build_agent  # noqa: E402

HANWOO_KEY = "소고기"  # 시드 한우 600g(D-2)의 기준표 품목명


@pytest.fixture
def db(tmp_path, monkeypatch):
    """`FRIDGE_TODAY=2026-11-20` 시드 상태의 임시 도메인 DB 경로."""
    monkeypatch.setenv("FRIDGE_TODAY", "2026-11-20")
    path = str(tmp_path / "fridge.db")
    tools.reset_db(path)
    return path


def run_plan(db: str, plan: planner.Plan | None = None, user: str = "dad") -> planner.MealPlan:
    """계획을 결정적으로 실행해 최종 식단을 만든다(그래프·모델 없이)."""
    plan = plan or planner.default_plan()
    conn = tools.connect(db)
    try:
        work = planner.start_work(conn, tools.FridgeContext(user_id=user, db_path=db), None, plan.servings)
    finally:
        conn.close()
    for step in plan.steps:
        planner.run_step(work, step)
    return planner.build_meal_plan(work)


# ── 결정적 실행 ──────────────────────────────────────────────────


def test_three_day_plan_uses_all_imminent_items(db):
    result = run_plan(db)
    assert [d.day for d in result.days] == [1, 2, 3]
    assert [d.date for d in result.days] == ["2026-11-20", "2026-11-21", "2026-11-22"]
    assert all(d.dish for d in result.days)
    assert len({d.dish for d in result.days}) == 3  # 같은 요리를 두 번 넣지 않는다
    assert [i.item for i in result.imminent] == [HANWOO_KEY]  # 시금치는 이미 만료라 빠진다
    assert result.imminent_used == [HANWOO_KEY] and result.imminent_unused == []
    assert HANWOO_KEY in result.days[0].uses_imminent  # 임박 재료를 쓰는 요리를 먼저 배정


def test_family_servings_and_safety_filter(db):
    result = run_plan(db)
    assert result.servings == 3  # 인분을 말하지 않으면 가족 3명 기준
    assert any("가족 3명" in n for n in result.notes)
    recipes = tools.parse_recipes()
    for day in result.days:
        recipe = recipes[day.dish]
        assert "갑각류" not in recipe.allergens  # 지민 알레르기
        assert day.dish != "가지 파르미자나"  # 아빠 비선호
    # 한우 600g을 하루 3인분으로 쓰면 남지 않는다(소고기 무국 80g·규동 150g 등).
    assert result.imminent_left == [] or result.imminent_left[0].qty < 600


def test_servings_from_plan_and_shortage(db):
    result = run_plan(db, planner.default_plan(days=2, servings=1))
    assert result.servings == 1 and len(result.days) == 2
    assert not any("가족" in n and "인분" in n for n in result.notes)
    for item in result.shortage:
        assert item.qty > 0 and item.unit


def test_plan_does_not_change_inventory(db):
    before = tools.connect(db).execute("SELECT sn, qty, status FROM inventory ORDER BY sn").fetchall()
    run_plan(db)
    after = tools.connect(db).execute("SELECT sn, qty, status FROM inventory ORDER BY sn").fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after]


def test_render_answer_lists_days_and_shortage(db):
    text = planner.render_answer(run_plan(db))
    assert text.startswith("3일 저녁 식단이에요 (하루 3인분).")
    assert "1일차 (2026-11-20)" in text and "임박 재료를 모두 식단에 넣었어요." in text and "부족 재료:" in text


# ── 계획 검사 ────────────────────────────────────────────────────


def test_normalize_plan_keeps_valid_llm_plan():
    llm = planner.default_plan(days=4, servings=2)
    plan, from_llm = planner.normalize_plan(llm)
    assert from_llm and plan.servings == 2 and len([s for s in plan.steps if s.kind == "assign"]) == 4


@pytest.mark.parametrize("steps", [
    [],
    [{"kind": "assign", "day": 1}],
    [{"kind": "imminent"}, {"kind": "assign", "day": 2}, {"kind": "shortage"}],  # 1일차가 없음
    [{"kind": "imminent"}, {"kind": "shortage"}, {"kind": "assign", "day": 1}],  # 순서가 틀림
])
def test_normalize_plan_falls_back_to_default(steps):
    plan, from_llm = planner.normalize_plan(planner.Plan(steps=steps))
    assert not from_llm
    assert plan.steps[0].kind == "imminent" and plan.steps[-1].kind == "shortage"


def test_normalize_plan_caps_days():
    long = planner.Plan(steps=[{"kind": "imminent"}, *[{"kind": "assign", "day": d} for d in range(1, 11)], {"kind": "shortage"}])
    plan, from_llm = planner.normalize_plan(long)
    assert not from_llm and len([s for s in plan.steps if s.kind == "assign"]) == planner.MAX_DAYS


# ── 그래프 ───────────────────────────────────────────────────────


class FakePlannerModel:
    """`with_structured_output(Plan)`에 정해진 계획을 돌려주는(또는 실패하는) 가짜 모델."""

    def __init__(self, plan: planner.Plan | None):
        self.plan = plan
        self.prompts: list[str] = []

    def with_structured_output(self, schema, **kwargs):
        def run(prompt):
            self.prompts.append(str(prompt))
            if self.plan is None:
                raise ConnectionError("bedrock down")
            return self.plan
        return RunnableLambda(run)


def run_graph(db: str, model, question: str = "이번 주 임박 재료 다 쓰는 3일 저녁 식단 짜줘") -> tuple[dict, list[dict]]:
    """식단 그래프를 바로 실행하고 (최종 상태, trace 단계)를 돌려준다."""
    graph = planner.build_planner(model)
    recorder = api.StepRecorder()
    state = asyncio.run(graph.ainvoke(
        {"messages": [{"role": "user", "content": question}]},
        config={"callbacks": [recorder]},
        context=tools.FridgeContext(user_id="dad", db_path=db),
    ))
    return state, recorder.steps


def test_graph_plans_executes_and_traces(db):
    model = FakePlannerModel(planner.default_plan(days=3, servings=2))
    state, steps = run_graph(db, model)
    assert "3일 저녁 식단" in model.prompts[0]  # 계획 프롬프트에 사용자 요청이 들어간다
    plan_events = [s for s in steps if s["event"] == "plan"]
    step_events = [s for s in steps if s["event"] == "plan_step"]
    assert len(plan_events) == 1 and plan_events[0]["source"] == "llm"
    assert [s["kind"] for s in step_events] == ["imminent", "assign", "assign", "assign", "shortage"]
    assert [s["index"] for s in step_events] == [1, 2, 3, 4, 5]
    meal_plan = planner.MealPlan.model_validate(state["meal_plan"])
    assert meal_plan.servings == 2 and meal_plan.imminent_unused == []
    last = state["messages"][-1]
    assert isinstance(last, AIMessage) and last.content.startswith("3일 저녁 식단이에요 (하루 2인분).")


def test_graph_uses_default_plan_when_model_fails(db):
    state, steps = run_graph(db, FakePlannerModel(None))
    plan_event = next(s for s in steps if s["event"] == "plan")
    assert plan_event["source"] == "default" and plan_event["error"] == "ConnectionError"
    assert len(planner.MealPlan.model_validate(state["meal_plan"]).days) == 3


def test_graph_with_async_sqlite_store(db, tmp_path):
    """실제 서버처럼 `AsyncSqliteStore`를 붙여도 실행 단계가 이벤트 루프를 막지 않고 Store의 비선호를 쓴다."""
    from src import memory

    async def run() -> dict:
        async with memory.open_store(tmp_path / "checkpoints.db", tools.SEED_USERS) as store:
            item = await store.aget(("users", "dad"), "profile")
            profile = item.value if item else memory.empty_profile()
            await store.aput(("users", "dad"), "profile", {**profile, "dislikes": [*profile["dislikes"], "규동"]})
            graph = planner.build_planner(FakePlannerModel(planner.default_plan()), store=store)
            return await graph.ainvoke(
                {"messages": [{"role": "user", "content": "3일 저녁 식단 짜줘"}]},
                context=tools.FridgeContext(user_id="dad", db_path=db),
            )

    state = asyncio.run(run())
    dishes = [d["dish"] for d in state["meal_plan"]["days"]]
    assert len(dishes) == 3 and "규동" not in dishes  # Store에 저장한 비선호를 뺀다


# ── Supervisor 연결 ───────────────────────────────────────────────


class RoutingModel(GenericFakeChatModel):
    """Supervisor 자리에서 식단 에이전트로 넘기고, 돌아오면 그 답을 그대로 돌려주는 가짜 모델."""

    role: str = "agent"

    def bind_tools(self, tools, **kwargs):
        names = [getattr(t, "name", "") for t in tools]
        return self.model_copy(update={"role": "supervisor" if any(n.startswith("transfer_to_") for n in names) else "agent"})

    def with_structured_output(self, schema, **kwargs):
        return RunnableLambda(lambda _: planner.default_plan())

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        last = messages[-1]
        if isinstance(last, ToolMessage) and str(last.name).startswith("transfer_back_to_"):
            answer = next(m for m in reversed(messages) if isinstance(m, AIMessage) and not m.tool_calls)
            msg = AIMessage(content=answer.content)
        else:
            call = {"name": f"transfer_to_{planner.PLANNER_AGENT}", "args": {}, "id": f"h-{len(messages)}"}
            msg = AIMessage(content="", tool_calls=[call])
        return ChatResult(generations=[ChatGeneration(message=msg)])


def test_supervisor_hands_meal_plan_to_planner(db, monkeypatch):
    monkeypatch.setenv("DEFAULT_USER", "dad")
    model = RoutingModel(messages=iter([]))
    agent = build_agent(checkpointer=InMemorySaver(), model=model)
    resp = asyncio.run(api.handle_query(agent, api.QueryRequest(question="3일 저녁 식단 짜줘"), None, db))
    assert resp.trace["errors"] == []
    assert resp.trace["steps"][0] == {"agent": "supervisor", "event": "handoff", "to": planner.PLANNER_AGENT}
    events = [s["event"] for s in resp.trace["steps"][1:]]
    assert events[0] == "plan" and events.count("plan_step") == 5
    assert resp.answer.startswith("3일 저녁 식단이에요")
