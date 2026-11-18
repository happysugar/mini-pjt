"""냉장고 관리 도메인 도구.

재고·평가·식사 데이터를 담는 SQLite DB(`data/fridge.db`)와, 모든 날짜 계산의
기준이 되는 `today()`를 제공한다. 날짜·단위·S/N 같은 계산은 LLM이 아니라
이 모듈의 결정적 함수가 맡는다.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
FRIDGE_DB = DATA_DIR / "fridge.db"
# 체크포인트(AsyncSqliteSaver)와 장기 메모리(AsyncSqliteStore)는 도메인 DB와 파일을 나눈다.
CHECKPOINT_DB = DATA_DIR / "checkpoints.db"
SEED_INVENTORY = DATA_DIR / "seed_inventory.json"
SEED_USERS = DATA_DIR / "seed_users.json"

# 시드 평점처럼 날짜만 있는 기록에 붙이는 시각
SEED_TIME = "12:00:00"

SCHEMA = """
CREATE TABLE inventory (
    sn            TEXT PRIMARY KEY,
    item          TEXT NOT NULL,
    qty           REAL NOT NULL CHECK (qty >= 0),
    unit          TEXT NOT NULL CHECK (unit IN ('g', 'L', '개')),
    expiry_date   TEXT,
    expiry_source TEXT NOT NULL CHECK (expiry_source IN ('user', 'guide', 'unknown')),
    input_date    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'stored'
                  CHECK (status IN ('stored', 'consumed', 'discarded'))
);

CREATE TABLE deductions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    sn                 TEXT NOT NULL REFERENCES inventory(sn),
    qty_g              REAL,
    d_day_at_deduction INTEGER,
    reason             TEXT NOT NULL,
    created_at         TEXT NOT NULL
);

CREATE TABLE users (
    user_id   TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    family_id TEXT NOT NULL
);

CREATE TABLE ratings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL REFERENCES users(user_id),
    dish       TEXT NOT NULL,
    cuisine    TEXT,
    score      INTEGER NOT NULL CHECK (score BETWEEN 1 AND 5),
    comment    TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE meals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL REFERENCES users(user_id),
    dish         TEXT NOT NULL,
    servings     REAL NOT NULL,
    kcal         REAL,
    eaten_at     TEXT NOT NULL,
    watch_synced INTEGER NOT NULL DEFAULT 0 CHECK (watch_synced IN (0, 1))
);

CREATE TABLE threads (
    thread_id  TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE item_units (
    item      TEXT PRIMARY KEY,
    base_unit TEXT NOT NULL CHECK (base_unit IN ('g', 'L', '개'))
);

CREATE INDEX idx_inventory_item ON inventory(item, status);
CREATE INDEX idx_deductions_created ON deductions(created_at);
CREATE INDEX idx_ratings_user_dish ON ratings(user_id, dish);
CREATE INDEX idx_meals_user_synced ON meals(user_id, watch_synced);
"""

TABLES = ("deductions", "ratings", "meals", "threads", "item_units", "inventory", "users")


def today() -> date:
    """오늘 날짜를 돌려준다.

    환경변수 `FRIDGE_TODAY`(YYYY-MM-DD)가 있으면 그 날짜를, 없으면 실제 오늘을 쓴다.
    테스트와 평가는 이 값을 고정해 결과를 재현한다. 모든 날짜 계산은 이 함수를 거친다.
    """
    value = os.environ.get("FRIDGE_TODAY", "").strip()
    if value:
        return date.fromisoformat(value)
    return date.today()


def offset_date(days: int, base: date | None = None) -> date:
    """기준일(기본값 `today()`)에서 상대 일수만큼 옮긴 날짜를 돌려준다."""
    return (base or today()) + timedelta(days=days)


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """도메인 DB 연결을 연다.

    행을 이름으로 읽을 수 있게 `sqlite3.Row`를 쓰고 외래 키 검사를 켠다.
    경로를 주지 않으면 `data/fridge.db`를 쓴다.
    """
    path = Path(db_path) if db_path else FRIDGE_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    """기존 테이블을 모두 지우고 스키마를 새로 만든다."""
    conn.execute("PRAGMA foreign_keys = OFF")
    for table in TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)


def _load_json(path: Path) -> dict:
    """시드 JSON 파일을 읽는다."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_seed(conn: sqlite3.Connection, base: date | None = None) -> dict:
    """시드 JSON을 읽어 상대 일수를 기준일 날짜로 바꿔 넣는다.

    기준일을 주지 않으면 `today()`를 쓴다. 넣은 행 수를 테이블별로 돌려준다.
    """
    base = base or today()
    inv = _load_json(SEED_INVENTORY)
    users = _load_json(SEED_USERS)

    inventory_rows = []
    for it in inv["items"]:
        expiry = it["expiry_offset_days"]
        inventory_rows.append((
            it["sn"],
            it["item"],
            it["qty"],
            it["unit"],
            None if expiry is None else offset_date(expiry, base).isoformat(),
            it["expiry_source"],
            offset_date(it["input_offset_days"], base).isoformat(),
            it.get("status", "stored"),
        ))
    conn.executemany(
        "INSERT INTO inventory (sn, item, qty, unit, expiry_date, expiry_source, input_date, status)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        inventory_rows,
    )
    conn.executemany(
        "INSERT INTO item_units (item, base_unit) VALUES (?, ?)",
        [(u["item"], u["base_unit"]) for u in inv.get("item_units", [])],
    )
    conn.executemany(
        "INSERT INTO users (user_id, name, family_id) VALUES (?, ?, ?)",
        [(u["user_id"], u["name"], users["family_id"]) for u in users["users"]],
    )
    conn.executemany(
        "INSERT INTO ratings (user_id, dish, cuisine, score, comment, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                r["user_id"],
                r["dish"],
                r["cuisine"],
                r["score"],
                r.get("comment") or None,
                f"{offset_date(r['created_offset_days'], base).isoformat()}T{SEED_TIME}",
            )
            for r in users["ratings"]
        ],
    )
    return {
        "inventory": len(inventory_rows),
        "item_units": len(inv.get("item_units", [])),
        "users": len(users["users"]),
        "ratings": len(users["ratings"]),
    }


def reset_db(db_path: Path | str | None = None, base: date | None = None) -> dict:
    """도메인 DB를 지우고 스키마와 시드로 새로 만든다.

    평가 문항마다 호출해 앞 문항의 쓰기가 다음 문항에 남지 않게 한다.
    여러 번 호출해도 같은 상태가 된다. 넣은 행 수를 테이블별로 돌려준다.
    """
    conn = connect(db_path)
    try:
        with conn:
            create_schema(conn)
            counts = load_seed(conn, base)
        return counts
    finally:
        conn.close()
