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
    assert budgets == {"dad": 2800.0, "mom": 2300.0, "jimin": 2400.0}
    assert steps[0]["tool"] == "get_calorie_budget" and steps[0]["via"] == "mcp" and steps[0]["event"] == "ok"
    assert "2300" not in json.dumps(steps)


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
    assert "remaining" not in raw and "2300" not in raw and "budget\": {" not in raw


def test_family_recommendation_low_budget_picks_lightest(make_client, watch_on):
    # 엄마가 오늘 2290kcal를 먹어 남은 칼로리가 10kcal이 되게 한다(가장 가벼운 레시피도 70kcal라 안에 들지 않음)
    mcp_call("record_meal", {"meal_id": "m-1", "user_id": "mom", "dish": "외식", "kcal": 2290, "eaten_at": "2026-11-20T12:00:00"})
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


@pytest.mark.usefixtures("allow_free_text_writes")
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


# ── 참여자 판단: 가족 구성원 정보에 있는지 (SERVICE.md 5.5) ─────────


def test_names_not_in_family_are_left_out(make_client, watch_on):
    """가족 구성원 정보에 있는 이름(엄마)만 참여자로 고려하고, 없는 이름(철수)은 빼고 알린다. 요청자는 한 번만 센다."""
    client, ask = make_client(call("rank_recipes", {"diners": ["엄마", "철수", "아빠"]}), say("추천해요."))
    ask({"question": "오늘 저녁 엄마랑 철수랑 같이 먹을 거 추천해줘", "thread_id": "dad-t-unknown"}, DAD)
    out = rank_output(client, "dad-t-unknown")
    assert sorted(out["diners"]) == ["dad", "mom"]
    assert any("가족 목록에 없는 이름은 빼고 골랐어요: 철수" in n for n in out["notes"])
    assert "철수" not in json.dumps(out["items"], ensure_ascii=False)


def test_only_unknown_names_fall_back_to_requester_alone(make_client, watch_on):
    """가족 구성원이 아닌 이름만 있으면 가족 합의가 아니라 요청자 혼자 기준의 일반 추천이 된다."""
    from tests.test_api import tool_outputs

    client, ask = make_client(call("rank_recipes", {"diners": ["철수"]}), say("추천해요."))
    ask({"question": "오늘 저녁 철수랑 같이 먹을 거 추천해줘", "thread_id": "dad-t-only-unknown"}, DAD)
    out = next(json.loads(o) for o in tool_outputs(client, "dad-t-only-unknown") if o.startswith("{"))
    assert "diners" not in out
    assert any("가족 목록에 없는 이름은 빼고 골랐어요: 철수" in n for n in out["notes"])


@pytest.mark.parametrize("word", ["가족", "우리 가족", "온 가족", "다 같이"])
def test_whole_family_words_include_every_member_and_requester(make_client, watch_on, word):
    """이름 없이 "우리 가족"이라고 하면 가족 전원이 참여자다. 요청자도 한 번만 들어간다(E07)."""
    client, ask = make_client(call("rank_recipes", {"diners": [word]}), say("추천해요."))
    ask({"question": "오늘 우리 가족이 같이 먹을 거 추천해줘", "thread_id": "dad-t-all"}, DAD)
    out = rank_output(client, "dad-t-all")
    assert sorted(out["diners"]) == ["dad", "jimin", "mom"]
    assert not any("가족 목록에 없는 이름" in n for n in out["notes"])


def test_requester_is_included_unless_explicitly_excluded(make_client, watch_on):
    """요청자는 기본 참여자다. 요청 문장이 명확히 뺄 때(exclude_requester=true)만 다른 참여자들로 추천한다."""
    client, ask = make_client(call("rank_recipes", {"diners": ["엄마", "지민"], "exclude_requester": True}),
                              say("추천해요."))
    ask({"question": "나는 빼고 엄마랑 지민이 먹을 거 추천해줘", "thread_id": "dad-t-excl"}, DAD)
    out = rank_output(client, "dad-t-excl")
    assert sorted(out["diners"]) == ["jimin", "mom"]
    assert tools.REQUESTER_EXCLUDED in out["notes"]
    # 같은 이름 목록이라도 뺀다는 표시가 없으면 요청자가 들어간다
    _, inc = family_ask(make_client)
    assert sorted(inc["diners"]) == ["dad", "jimin", "mom"] and tools.REQUESTER_EXCLUDED not in inc["notes"]


def test_wanted_ingredient_blocked_by_diner_allergy_is_noted_without_name(make_client, watch_on):
    """요청한 재료가 참여자 알레르기에 걸리면 누구의 알레르기인지 밝히지 않고 notes로 알린다(E34)."""
    client, ask = make_client(call("rank_recipes", {"diners": ["지민"], "wanted": "새우"}), say("추천해요."))
    ask({"question": "지민이랑 같이 먹을 새우 요리 추천해줘", "thread_id": "dad-t-wanted"}, DAD)
    out = rank_output(client, "dad-t-wanted")
    note = tools.WANTED_ALLERGY.format(wanted="새우", particle="는", allergy="갑각류")
    assert note in out["notes"] and not any("지민" in n for n in out["notes"])
    assert all("새우" not in item["dish"] for item in out["items"])


@pytest.mark.parametrize(("wanted", "hit"), [("새우", "갑각류"), ("새우 요리", "갑각류"), ("대하", "갑각류"),
                                              ("새우 로제 파스타", "갑각류"), ("두부", None), ("소고기", None)])
def test_wanted_allergy_maps_ingredient_to_allergen_class(wanted, hit):
    """요청한 재료·요리가 든 레시피의 알레르기 분류로 판정한다(새우 → 갑각류)."""
    assert tools.wanted_allergy(wanted, ["갑각류"]) == hit


# ── 혼자 먹는 추천도 요청자의 남은 칼로리를 쓴다 (SERVICE.md 5.2) ────


def _solo_rank(make_client, thread_id: str) -> dict:
    """혼자 먹는 추천 대본으로 질문하고 rank_recipes 결과(JSON)를 돌려준다."""
    from tests.test_api import tool_outputs

    client, ask = make_client(call("rank_recipes", {}), say("추천해요."))
    r = ask({"question": "오늘 저녁 뭐 먹지?", "thread_id": thread_id}, DAD)
    out = next(json.loads(o) for o in tool_outputs(client, thread_id) if o.startswith("{"))
    return r, out


def test_solo_recommendation_reads_own_budget(make_client, watch_on):
    r, out = _solo_rank(make_client, "dad-t-solo")
    assert out["within_calorie_budget"] is True and "diners" not in out
    mcp_steps = [s for s in r["trace"]["steps"] if s.get("via") == "mcp"]
    assert [(s["tool"], s["caller"], s["members"]) for s in mcp_steps] == [("get_calorie_budget", "rank_recipes", 1)]
    assert "2800" not in json.dumps(out)  # 남은 칼로리 수치는 결과에 담지 않는다


def test_solo_recommendation_low_budget_picks_lightest(make_client, watch_on):
    # 아빠가 오늘 2790kcal를 먹어 남은 칼로리가 10kcal(가장 가벼운 레시피 80kcal보다 적음)
    mcp_call("record_meal", {"meal_id": "d-1", "user_id": "dad", "dish": "외식", "kcal": 2790, "eaten_at": "2026-11-20T12:00:00"})
    _, out = _solo_rank(make_client, "dad-t-solo-low")
    assert out["within_calorie_budget"] is False
    assert any("가장 가벼운 요리" in n for n in out["notes"])


# Regression: ISSUE-001 — guest(신원 미확인)의 추천이 워치 기본 목표치로 "남은 칼로리 안"이라고 안내함
# Found by /qa on 2026-11-20
# Report: .gstack/qa-reports/qa-report-api-query-2026-11-20.md
def test_guest_recommendation_has_no_calorie_budget(make_client, watch_on, monkeypatch):
    """guest는 가족 구성원이 아니고 워치 기록도 없다. 워치를 조회하지 않고 칼로리 정보를 확인하지 못했다고 알린다."""
    from tests.test_api import tool_outputs

    monkeypatch.delenv("DEFAULT_USER")
    client, ask = make_client(call("rank_recipes", {}), say("추천해요."))
    r = ask({"question": "오늘 저녁 뭐 먹지?", "thread_id": "guest-t-solo"})
    assert r["trace"]["user_id"] == "guest"
    out = next(json.loads(o) for o in tool_outputs(client, "guest-t-solo") if o.startswith("{"))
    assert out["within_calorie_budget"] is None and tools.CALORIE_UNKNOWN in out["notes"]
    assert not [s for s in r["trace"]["steps"] if s.get("via") == "mcp"]


def test_solo_recommendation_without_watch(make_client):
    _, out = _solo_rank(make_client, "dad-t-solo-off")
    assert out["within_calorie_budget"] is None and tools.CALORIE_UNKNOWN in out["notes"]
