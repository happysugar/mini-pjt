"""사용자별 장기 메모리 (LangGraph Store, SERVICE.md 6장 #10).

`AsyncSqliteStore`(`data/checkpoints.db`)의 네임스페이스 `("users", user_id)`에 선호·비선호·알레르기를
`profile` 한 항목으로 저장한다. 대화 스레드와 상관없이 남으므로 새 `thread_id`에서도 추천에 반영된다.

- 서버 시작 때 `open_store()`가 테이블을 만들고, 시드 사용자 정보를 아직 없는 사용자에게만 적재한다.
- 읽기·쓰기 함수는 동기 `BaseStore` 메서드를 쓴다. 도구는 워커 스레드에서 돌기 때문에
  `AsyncSqliteStore`의 동기 메서드가 서버 이벤트 루프로 넘겨 실행한다.
- 다른 사용자의 항목은 `read_profile()`로 읽을 수 없다. 가족 합의 추천의 안전 필터만
  `filter_profiles()`로 알레르기·비선호를 읽고, 그 값은 답변에 내보내지 않는다.

이 모듈은 `src.tools`를 import하지 않는다(도구 모듈이 이 모듈을 쓴다).
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from langgraph.store.base import BaseStore
from langgraph.store.sqlite.aio import AsyncSqliteStore

PROFILE_KEY = "profile"
# 도구의 kind 값 → profile 목록 이름
KIND_FIELDS = {"like": "likes", "dislike": "dislikes", "allergy": "allergies"}
PROFILE_FIELDS = ("likes", "dislikes", "allergies", "notes")
# 서로 반대인 목록. 한쪽에 넣으면 다른 쪽의 같은 값은 뺀다.
OPPOSITE_FIELDS = {"likes": "dislikes", "dislikes": "likes"}


def namespace(user_id: str) -> tuple[str, str]:
    """사용자 장기 메모리 네임스페이스 `("users", user_id)`."""
    return ("users", user_id)


def empty_profile() -> dict[str, list[str]]:
    """빈 프로필. `notes`는 추천 필터에 쓰지 않는 자유 메모(예: "매운 거")다."""
    return {field: [] for field in PROFILE_FIELDS}


def _normalize(value: dict | None) -> dict[str, list[str]]:
    """저장된 값을 네 목록이 모두 있는 프로필로 맞춘다."""
    profile = empty_profile()
    for field in PROFILE_FIELDS:
        profile[field] = list((value or {}).get(field, []))
    return profile


def get_profile(store: BaseStore, user_id: str) -> dict[str, list[str]] | None:
    """Store에서 사용자 프로필을 읽는다. 없으면 None."""
    item = store.get(namespace(user_id), PROFILE_KEY)
    return _normalize(item.value) if item else None


def read_profile(store: BaseStore, requester: str, user_id: str | None = None) -> dict[str, list[str]]:
    """요청자 본인의 프로필을 읽는다. 다른 사용자의 항목을 읽으려 하면 PermissionError를 낸다."""
    user_id = user_id or requester
    if user_id != requester:
        raise PermissionError("다른 사용자의 장기 메모리는 읽을 수 없어요")
    return get_profile(store, requester) or empty_profile()


def filter_profiles(store: BaseStore, members: list[str]) -> dict[str, dict[str, list[str]]]:
    """안전 필터용으로 참여자들의 알레르기·비선호만 읽는다. Store에 없는 참여자는 결과에서 빠진다.

    가족 합의 추천에서만 쓰고, 돌려준 값은 답변이나 도구 결과에 그대로 내보내지 않는다.
    """
    out = {}
    for member in members:
        profile = get_profile(store, member)
        if profile is not None:
            out[member] = {"allergies": profile["allergies"], "dislikes": profile["dislikes"]}
    return out


def add_preference(store: BaseStore, user_id: str, field: str, value: str) -> tuple[dict[str, list[str]], bool]:
    """사용자 프로필의 한 목록(likes·dislikes·allergies·notes)에 값을 더한다.

    이미 있으면 그대로 둔다. 좋아함과 싫어함은 반대이므로, 한쪽에 넣으면 다른 쪽에 있던 같은 값은 뺀다
    (예: 시드 likes의 규동을 "나 규동 싫어"로 dislikes에 넣으면 likes에서 빠진다).
    (갱신된 프로필, 새로 더했는지)를 돌려준다.
    """
    if field not in PROFILE_FIELDS:
        raise ValueError(f"알 수 없는 프로필 항목이에요: {field}")
    value = value.strip()
    profile = get_profile(store, user_id) or empty_profile()
    added = bool(value) and value not in profile[field]
    opposite = OPPOSITE_FIELDS.get(field)
    removed = bool(value) and opposite is not None and value in profile[opposite]
    if removed:
        profile[opposite] = [v for v in profile[opposite] if v != value]
    if added:
        profile[field].append(value)
    if added or removed:
        store.put(namespace(user_id), PROFILE_KEY, profile)
    return profile, added


def seed_profiles(seed_users_path: Path) -> dict[str, dict[str, list[str]]]:
    """시드 사용자 JSON에서 사용자별 선호·비선호·알레르기를 읽는다."""
    data = json.loads(Path(seed_users_path).read_text(encoding="utf-8"))
    return {
        user["user_id"]: _normalize({key: user.get(key, []) for key in ("likes", "dislikes", "allergies")})
        for user in data["users"]
    }


async def seed_store(store: BaseStore, seed_users_path: Path) -> list[str]:
    """Store에 아직 프로필이 없는 시드 사용자만 초기 적재한다. 적재한 user_id 목록을 돌려준다.

    이미 있는 프로필(대화로 바뀐 선호 포함)은 덮어쓰지 않는다.
    """
    loaded = []
    for user_id, profile in seed_profiles(seed_users_path).items():
        if await store.aget(namespace(user_id), PROFILE_KEY) is None:
            await store.aput(namespace(user_id), PROFILE_KEY, profile)
            loaded.append(user_id)
    return loaded


@asynccontextmanager
async def open_store(db_path: Path, seed_users_path: Path) -> AsyncIterator[AsyncSqliteStore]:
    """`db_path`의 `AsyncSqliteStore`를 열고 테이블 준비와 시드 적재를 마친 뒤 넘겨준다."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteStore.from_conn_string(str(db_path)) as store:
        await store.setup()
        await seed_store(store, seed_users_path)
        yield store


def profile_or(store: Any, user_id: str, fallback: dict[str, list[str]]) -> dict[str, list[str]]:
    """Store가 있으면 사용자 프로필을, 없거나 항목이 없으면 `fallback`을 돌려준다."""
    if store is None:
        return fallback
    return get_profile(store, user_id) or fallback
