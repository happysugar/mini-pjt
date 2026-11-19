"""헬스케어 MCP 서버와 워치 기록 동기화 테스트 (TASK-14, SERVICE.md 5.3).

워치 서버는 같은 프로세스의 FastMCP 서버 객체를 메모리 전송으로 연결하고, 기록 DB는 임시 파일을 쓴다.
API 흐름은 `tests/test_api.py`의 가짜 모델과 임시 DB 픽스처를 그대로 쓴다.
"""

import asyncio
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastmcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import agent as agent_module  # noqa: E402
from src import health_mcp_server as health  # noqa: E402
from src import tools  # noqa: E402
from tests.test_api import DAD, MEAL, call, codes, env, make_client, say  # noqa: E402,F401

DOWN_URL = "http://127.0.0.1:9/mcp"  # 아무도 듣지 않는 포트: 워치 서버가 꺼진 상태


@pytest.fixture
def health_db(tmp_path, monkeypatch):
    """워치 기록 DB를 임시 파일로 바꾸고 날짜를 고정한다."""
    monkeypatch.setenv("FRIDGE_TODAY", "2026-11-20")
    path = tmp_path / "health.db"
    monkeypatch.setenv("HEALTH_DB", str(path))
    return path


@pytest.fixture
def watch_on(health_db):
    """식사 확정 도구가 같은 프로세스의 워치 서버로 record_meal을 보내게 한다."""
    tools.set_health_target(health.mcp)
    return health_db


def mcp_call(name: str, args: dict) -> dict:
    """워치 서버 도구를 MCP 클라이언트로 부르고 구조화 결과를 돌려준다."""

    async def run():
        async with Client(health.mcp) as client:
            return (await client.call_tool(name, args)).structured_content

    return asyncio.run(run())


def watch_rows(path: Path) -> list[tuple]:
    """워치 기록 DB의 (meal_id, user_id, kcal) 목록."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(health.HEALTH_SCHEMA)  # 한 번도 기록하지 않았으면 테이블이 아직 없다
        return conn.execute("SELECT meal_id, user_id, kcal FROM watch_meals ORDER BY recorded_at").fetchall()
    finally:
        conn.close()


def synced_flags(db: str) -> list[int]:
    """도메인 DB meals의 watch_synced 값 목록."""
    conn = tools.connect(db)
    try:
        return [r[0] for r in conn.execute("SELECT watch_synced FROM meals ORDER BY id")]
    finally:
        conn.close()


def mcp_steps(response: dict) -> list[dict]:
    """응답 trace.steps 중 코드가 부른 MCP record_meal 기록."""
    return [s for s in response["trace"]["steps"] if s.get("via") == "mcp"]


# ── 서버 도구 ────────────────────────────────────────────────────


def test_record_meal_is_idempotent_by_meal_id(health_db):
    meal = {"meal_id": "t1:c1", "user_id": "dad", "dish": "규동", "kcal": 680, "eaten_at": "2026-11-20T19:00:00"}
    assert mcp_call("record_meal", meal)["status"] == "recorded"
    assert mcp_call("record_meal", meal)["status"] == "duplicate"
    assert watch_rows(health_db) == [("t1:c1", "dad", 680.0)]


def test_calorie_budget_counts_only_today_and_own_meals(health_db):
    base = {"dish": "규동", "kcal": 680}
    mcp_call("record_meal", {**base, "meal_id": "a", "user_id": "dad", "eaten_at": "2026-11-20T12:00:00"})
    mcp_call("record_meal", {**base, "meal_id": "b", "user_id": "dad", "eaten_at": "2026-11-19T19:00:00"})
    mcp_call("record_meal", {**base, "meal_id": "c", "user_id": "mom", "eaten_at": "2026-11-20T19:00:00"})
    budget = mcp_call("get_calorie_budget", {"user_id": "dad"})
    assert budget == {"user_id": "dad", "date": "2026-11-20", "target_kcal": 2800, "intake_kcal": 680.0,
                      "remaining_kcal": 2120.0}
    intake = mcp_call("get_daily_intake", {"user_id": "dad"})
    assert intake["intake_kcal"] == 680.0 and len(intake["meals"]) == 1


# ── 에이전트 조회 도구 연결 ──────────────────────────────────────


def test_interceptor_forces_requester_user_id():
    seen = {}

    async def handler(request):
        seen.update(request.args)
        return "ok"

    from langchain_mcp_adapters.interceptors import MCPToolCallRequest

    runtime = SimpleNamespace(context=tools.FridgeContext(user_id="dad"))
    request = MCPToolCallRequest(name="get_calorie_budget", args={"user_id": "mom"}, server_name="health", runtime=runtime)
    asyncio.run(agent_module.inject_requester(request, handler))
    assert seen["user_id"] == "dad"

    no_ctx = MCPToolCallRequest(name="get_calorie_budget", args={}, server_name="health", runtime=None)
    with pytest.raises(ValueError):
        asyncio.run(agent_module.inject_requester(no_ctx, handler))


def test_hide_user_id_from_model_schema():
    tool = SimpleNamespace(args_schema={"type": "object", "properties": {"user_id": {"type": "string"}},
                                        "required": ["user_id"]})
    agent_module._hide_user_id(tool)
    assert tool.args_schema["properties"] == {} and tool.args_schema["required"] == []


def test_no_health_url_means_no_query_tools():
    assert asyncio.run(agent_module.load_health_tools()) == []


# ── 식사 확정 → record_meal (API 흐름) ───────────────────────────


def test_meal_approve_calls_record_meal_once(make_client, env, watch_on):
    _, ask = make_client(call("confirm_meal", MEAL), say("저장했어요."))
    r1 = ask({"question": "규동 2인분 만들어서 1인분 먹었어, 4점"}, DAD)
    assert mcp_steps(r1) == []  # 승인 전에는 워치에 보내지 않는다
    r2 = ask({"question": "승인", "thread_id": r1["trace"]["thread_id"], "resume": "approve"}, DAD)

    steps = mcp_steps(r2)
    assert len(steps) == 1
    assert steps[0]["tool"] == "record_meal" and steps[0]["caller"] == "confirm_meal" and steps[0]["event"] == "recorded"
    assert not codes(r2)
    assert synced_flags(env) == [1]
    assert [(row[1], row[2]) for row in watch_rows(watch_on)] == [("dad", 680.0)]


def test_watch_down_then_resent_once_on_next_query(make_client, env, health_db):
    _, ask = make_client(call("confirm_meal", MEAL), say("저장했어요."), say("네."), say("네."))
    tools.set_health_target(DOWN_URL)
    r1 = ask({"question": "규동 2인분 만들어서 1인분 먹었어, 4점"}, DAD)
    r2 = ask({"question": "승인", "thread_id": r1["trace"]["thread_id"], "resume": "approve"}, DAD)
    assert codes(r2) == ["WATCH_SYNC_FAILED"]
    assert "워치 기록 실패" in r2["answer"]
    assert [s["event"] for s in mcp_steps(r2)] == ["error"]
    assert synced_flags(env) == [0]

    tools.set_health_target(health.mcp)  # 워치 서버를 켠다
    r3 = ask({"question": "오늘 뭐 남았어?"}, DAD)
    steps = mcp_steps(r3)
    assert [(s["caller"], s["event"]) for s in steps] == [("resend", "recorded")]
    assert not codes(r3)
    assert synced_flags(env) == [1]

    r4 = ask({"question": "고마워"}, DAD)
    assert mcp_steps(r4) == []  # 이미 보낸 식사는 다시 보내지 않는다
    assert len(watch_rows(health_db)) == 1


def test_meal_without_kcal_is_not_sent(make_client, env, watch_on):
    _, ask = make_client(call("confirm_meal", {"dish": "김치찌개", "score": 3}), say("저장했어요."))
    r1 = ask({"question": "외식으로 김치찌개 먹었어, 3점"}, DAD)
    r2 = ask({"question": "승인", "thread_id": r1["trace"]["thread_id"], "resume": "approve"}, DAD)
    assert mcp_steps(r2) == [] and not codes(r2)
    assert watch_rows(watch_on) == []
