"""가족 합의 저녁 테스트 (TASK-17, SERVICE.md 5.5).

워치 서버는 같은 프로세스의 FastMCP 서버 객체를 쓰고(TASK-14 방식), 기록 DB는 임시 파일을 쓴다.
API 흐름은 `tests/test_api.py`의 가짜 모델과 임시 도메인 DB 픽스처를 그대로 쓴다.
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import health_mcp_server as health  # noqa: E402
from src import tools  # noqa: E402
from tests.test_api import DAD, call, env, make_client, say  # noqa: E402,F401
from tests.test_health_mcp import mcp_call  # noqa: E402

FAMILY = {"diners": ["엄마", "지민"]}


@pytest.fixture
def watch_on(tmp_path, monkeypatch):
    """rank_recipes가 같은 프로세스의 워치 서버에서 남은 칼로리를 읽게 한다."""
    monkeypatch.setenv("FRIDGE_TODAY", "2026-11-20")
    monkeypatch.setenv("HEALTH_DB", str(tmp_path / "health.db"))
    tools.set_health_target(health.mcp)
    return tmp_path / "health.db"


def rank_output(client, thread_id: str) -> dict:
    """체크포인트에서 rank_recipes 도구 결과(JSON)를 꺼낸다."""
    from tests.test_api import tool_outputs

    return next(json.loads(o) for o in tool_outputs(client, thread_id) if o.startswith("{") and '"diners"' in o)


def family_ask(make_client, question: str = "오늘 저녁 엄마랑 지민이랑 같이 먹을 거 추천해줘"):
    """가족 추천 대본으로 질문하고 (응답, rank_recipes 결과)를 돌려준다."""
    client, ask = make_client(call("rank_recipes", FAMILY), say("추천해요."))
    r = ask({"question": question, "thread_id": "dad-t-family"}, DAD)
    return r, rank_output(client, "dad-t-family")


# ── 워치에서 남은 칼로리 읽기 ─────────────────────────────────────


def test_fetch_budgets_reads_every_member_without_numbers_in_trace(watch_on):
    budgets, steps = tools.fetch_calorie_budgets(["dad", "mom", "jimin"], caller="rank_recipes")
    assert budgets == {"dad": 2400.0, "mom": 1900.0, "jimin": 2000.0}
    assert steps[0]["tool"] == "get_calorie_budget" and steps[0]["via"] == "mcp" and steps[0]["event"] == "ok"
    assert "1900" not in json.dumps(steps)


def test_fetch_budgets_off_or_down():
    assert tools.fetch_calorie_budgets(["dad"], caller="t") == (None, [])  # 워치 연동 꺼짐
    tools.set_health_target("http://127.0.0.1:9/mcp")  # 아무도 듣지 않는 포트
    budgets, steps = tools.fetch_calorie_budgets(["dad"], caller="t")
    assert budgets is None and steps[0]["event"] == "error"


# ── rank_recipes 가족 추천 ────────────────────────────────────────


def test_family_recommendation_within_budget_and_safe(make_client, watch_on):
    r, out = family_ask(make_client)
    assert set(out["diners"]) == {"dad", "mom", "jimin"}  # 요청자 자동 포함, "지민이" 같은 호칭도 풀린다
    assert out["within_calorie_budget"] is True
    slots = [i["slot"] for i in out["items"]]
    assert slots.count(tools.SLOT_GROUP_RATED) == 1 and slots.count(tools.SLOT_GROUP_NEW) == 2
    recipes = tools.parse_recipes()
    assert all("갑각류" not in recipes[i["dish"]].allergens for i in out["items"])  # 지민 갑각류 알레르기
    # 워치 조회는 코드에서 한 번, 결과에는 개인별 남은 칼로리가 없다
    mcp_steps = [s for s in r["trace"]["steps"] if s.get("via") == "mcp"]
    assert [s["tool"] for s in mcp_steps] == ["get_calorie_budget"] and mcp_steps[0]["caller"] == "rank_recipes"
    raw = json.dumps(out, ensure_ascii=False)
    assert "remaining" not in raw and "1900" not in raw and "budget\": {" not in raw


def test_family_recommendation_low_budget_picks_lightest(make_client, watch_on):
    # 엄마가 오늘 1890kcal를 먹어 남은 칼로리가 10kcal이 되게 한다(가장 가벼운 레시피도 70kcal라 안에 들지 않음)
    mcp_call("record_meal", {"meal_id": "m-1", "user_id": "mom", "dish": "외식", "kcal": 1890, "eaten_at": "2026-11-20T12:00:00"})
    _, out = family_ask(make_client)
    assert out["within_calorie_budget"] is False
    assert any("가장 가벼운 요리" in n for n in out["notes"])
    kcals = {i["kcal_per_serving"] for i in out["items"]}
    assert len(kcals) == 1  # 1인분 kcal이 가장 낮은 요리만 남긴다


def test_family_recommendation_without_watch(make_client):
    _, out = family_ask(make_client)
    assert out["within_calorie_budget"] is None
    assert tools.CALORIE_UNKNOWN in out["notes"]


def test_family_answer_masks_other_member_kcal(make_client, watch_on):
    client, ask = make_client(call("rank_recipes", FAMILY), say("엄마는 오늘 1,250kcal 남았어요."))
    r = ask({"question": "오늘 저녁 엄마랑 지민이랑 같이 먹을 거 추천해줘"}, DAD)
    assert "1,250" not in r["answer"]  # 모델이 실수로 써도 출력 가드레일이 가린다


# ── 식사 확정은 요청자 본인 기록만 ────────────────────────────────


def test_meal_with_family_records_only_requester(make_client, env):
    meal = {"dish": "소고기 무국", "score": 4, "servings_made": 3, "servings_eaten": 1}
    _, ask = make_client(call("confirm_meal", meal), say("저장했어요."))
    r1 = ask({"question": "소고기 무국 엄마랑 지민이랑 같이 3인분 먹었어, 4점"}, DAD)
    ask({"question": "승인", "thread_id": r1["trace"]["thread_id"], "resume": "approve"}, DAD)
    conn = sqlite3.connect(env)
    try:
        users = {row[0] for row in conn.execute("SELECT user_id FROM meals WHERE dish = '소고기 무국'")}
        raters = {row[0] for row in conn.execute("SELECT user_id FROM ratings WHERE dish = '소고기 무국' AND created_at LIKE '2026-11-20%'")}
    finally:
        conn.close()
    assert users == {"dad"} and raters == {"dad"}
