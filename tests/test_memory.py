"""장기 메모리(LangGraph Store) 테스트 (TASK-16, SERVICE.md 6장 #10).

Store 함수는 `InMemoryStore`와 임시 SQLite `AsyncSqliteStore`로, API 흐름은 `tests/test_api.py`의
가짜 모델·임시 DB 픽스처로 확인한다. 체크포인트·Store 파일은 테스트마다 임시 경로를 쓴다.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from langgraph.store.memory import InMemoryStore

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import memory, tools  # noqa: E402
from tests.test_api import DAD, MOM, call, env, make_client, say, tool_outputs  # noqa: E402,F401


def rank_dishes(outputs: list[str]) -> list[str]:
    """도구 결과 중 rank_recipes JSON의 추천 요리명 목록."""
    for out in outputs:
        try:
            data = json.loads(out)
        except ValueError:
            continue
        if isinstance(data, dict) and "items" in data:
            return [item["dish"] for item in data["items"]]
    raise AssertionError("rank_recipes 결과가 없어요")


# ── Store 함수 ───────────────────────────────────────────────────


def test_seed_store_loads_once_and_keeps_changes():
    store = InMemoryStore()
    loaded = asyncio.run(memory.seed_store(store, tools.SEED_USERS))
    assert set(loaded) == {"dad", "mom", "jimin"}
    assert memory.get_profile(store, "jimin")["allergies"] == ["갑각류"]

    memory.add_preference(store, "dad", "dislikes", "규동")
    assert asyncio.run(memory.seed_store(store, tools.SEED_USERS)) == []  # 이미 있으면 덮어쓰지 않는다
    assert "규동" in memory.get_profile(store, "dad")["dislikes"]


def test_add_preference_dedupes_and_validates_field():
    store = InMemoryStore()
    _, added = memory.add_preference(store, "dad", "dislikes", "가지")
    _, again = memory.add_preference(store, "dad", "dislikes", " 가지 ")
    assert added and not again
    assert memory.get_profile(store, "dad")["dislikes"] == ["가지"]
    with pytest.raises(ValueError):
        memory.add_preference(store, "dad", "secrets", "x")


def test_like_and_dislike_are_exclusive():
    store = InMemoryStore()
    asyncio.run(memory.seed_store(store, tools.SEED_USERS))
    assert "규동" in memory.get_profile(store, "dad")["likes"]  # 시드 likes

    profile, added = memory.add_preference(store, "dad", "dislikes", "규동")
    assert added and "규동" in profile["dislikes"] and "규동" not in profile["likes"]
    assert "규동" not in memory.get_profile(store, "dad")["likes"]  # 저장된 값에도 반영

    profile, _ = memory.add_preference(store, "dad", "likes", "규동")
    assert "규동" in profile["likes"] and "규동" not in profile["dislikes"]


def test_cannot_read_other_users_profile():
    store = InMemoryStore()
    memory.add_preference(store, "mom", "allergies", "새우")
    assert memory.read_profile(store, "dad")["allergies"] == []
    with pytest.raises(PermissionError):
        memory.read_profile(store, "dad", "mom")


def test_sqlite_store_persists_across_reopen(tmp_path):
    db = tmp_path / "checkpoints.db"

    async def first():
        async with memory.open_store(db, tools.SEED_USERS) as store:
            await asyncio.to_thread(memory.add_preference, store, "dad", "dislikes", "규동")

    async def second():
        async with memory.open_store(db, tools.SEED_USERS) as store:
            return await asyncio.to_thread(memory.get_profile, store, "dad")

    asyncio.run(first())
    profile = asyncio.run(second())
    assert "규동" in profile["dislikes"] and "가지 파르미자나" in profile["dislikes"]


def test_dislike_ingredient_excludes_recipes_with_it(env):
    conn = tools.connect(env)
    try:
        base = tools.recommend(conn, "dad", allergies=[], dislikes=[])
        assert "규동" in [i.dish for i in base.items]
        no_onion = tools.recommend(conn, "dad", allergies=[], dislikes=["양파"])
    finally:
        conn.close()
    recipes = tools.parse_recipes()
    for item in no_onion.items:
        assert "양파" not in {ing.item for ing in recipes[item.dish].ingredients}


# ── API 흐름: 새 thread·새 서버에서도 반영 ───────────────────────


def test_saved_dislike_applies_in_new_thread_and_new_server(make_client):
    client, ask = make_client(
        call("remember_preference", {"kind": "dislike", "value": "규동"}),
        say("기억했어요."),
        call("rank_recipes", {}),
        say("추천이에요."),
    )
    r1 = ask({"question": "나 규동 싫어"}, DAD)
    saved = json.loads(tool_outputs(client, r1["trace"]["thread_id"])[0])
    assert saved["user_id"] == "dad" and saved["saved_as"] == "dislikes"

    r2 = ask({"question": "오늘 저녁 뭐 먹지?"}, DAD)  # 새 thread
    assert r2["trace"]["thread_id"] != r1["trace"]["thread_id"]
    assert "규동" not in rank_dishes(tool_outputs(client, r2["trace"]["thread_id"]))

    # 서버를 다시 띄워도(같은 checkpoints.db) 그대로 남는다
    client2, ask2 = make_client(call("rank_recipes", {}), say("추천이에요."))
    r3 = ask2({"question": "오늘 저녁 뭐 먹지?"}, DAD)
    assert "규동" not in rank_dishes(tool_outputs(client2, r3["trace"]["thread_id"]))


def test_preference_is_saved_only_for_requester(make_client):
    client, ask = make_client(
        call("remember_preference", {"kind": "allergy", "value": "새우"}),
        say("기억했어요."),
        call("get_my_preferences", {}),
        say("dad의 선호예요."),
    )
    ask({"question": "나 새우 알레르기 있어"}, MOM)
    r = ask({"question": "내 선호 뭐로 기억하고 있어?"}, DAD)
    mine = json.loads(tool_outputs(client, r["trace"]["thread_id"])[0])
    assert mine["user_id"] == "dad" and "새우" not in mine["allergies"]


def test_free_text_preference_becomes_note(make_client):
    client, ask = make_client(call("remember_preference", {"kind": "dislike", "value": "매운 거"}), say("기억했어요."))
    r = ask({"question": "나 매운 거 싫어"}, DAD)
    saved = json.loads(tool_outputs(client, r["trace"]["thread_id"])[0])
    assert saved["saved_as"] == "notes" and "메모" in saved["applied"]


def test_family_dinner_uses_diners_store_profile(make_client):
    client, ask = make_client(
        call("remember_preference", {"kind": "dislike", "value": "소고기 무국"}),
        say("기억했어요."),
        call("rank_recipes", {"diners": ["엄마"]}),
        say("가족 추천이에요."),
    )
    ask({"question": "나 소고기 무국 싫어"}, MOM)
    r = ask({"question": "엄마랑 같이 먹을 거 추천해줘"}, DAD)
    outputs = tool_outputs(client, r["trace"]["thread_id"])
    assert "소고기 무국" not in rank_dishes(outputs)
    assert not any("소고기 무국" in o and "dislike" in o for o in outputs)  # 엄마의 비선호 원문은 내보내지 않는다


def test_guest_cannot_save_preference(make_client, monkeypatch):
    monkeypatch.setenv("DEFAULT_USER", "")
    client, ask = make_client(call("remember_preference", {"kind": "dislike", "value": "규동"}), say("안 돼요."))
    r = ask({"question": "나 규동 싫어"})
    assert tools.GUEST_DENIED in tool_outputs(client, r["trace"]["thread_id"])
