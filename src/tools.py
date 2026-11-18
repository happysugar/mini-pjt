"""냉장고 관리 도메인 도구.

재고·평가·식사 데이터를 담는 SQLite DB(`data/fridge.db`)와, 모든 날짜 계산의
기준이 되는 `today()`를 제공한다. 날짜·단위·S/N 같은 계산은 LLM이 아니라
이 모듈의 결정적 함수가 맡는다.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

from langchain.tools import ToolRuntime, tool
from langgraph.types import interrupt
from pydantic import BaseModel, Field

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
FRIDGE_DB = DATA_DIR / "fridge.db"
# 체크포인트(AsyncSqliteSaver)와 장기 메모리(AsyncSqliteStore)는 도메인 DB와 파일을 나눈다.
CHECKPOINT_DB = DATA_DIR / "checkpoints.db"
SEED_INVENTORY = DATA_DIR / "seed_inventory.json"
SEED_USERS = DATA_DIR / "seed_users.json"
STORAGE_GUIDE = DATA_DIR / "storage_guide.md"
# 카테고리 순서가 곧 추천 슬롯 순서다.
RECIPE_FILES = {
    "한식": DATA_DIR / "recipes_korean.md",
    "일식": DATA_DIR / "recipes_japanese.md",
    "이탈리아식": DATA_DIR / "recipes_italian.md",
}
CUISINES = tuple(RECIPE_FILES)

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

-- 승인 도구의 멱등 기록: 같은 키(thread_id:tool_call_id)는 한 번만 저장한다.
CREATE TABLE applied_actions (
    idem_key   TEXT PRIMARY KEY,
    action     TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    result     TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_inventory_item ON inventory(item, status);
CREATE INDEX idx_deductions_created ON deductions(created_at);
CREATE INDEX idx_ratings_user_dish ON ratings(user_id, dish);
CREATE INDEX idx_meals_user_synced ON meals(user_id, watch_synced);
"""

TABLES = ("applied_actions", "deductions", "ratings", "meals", "threads", "item_units", "inventory", "users")


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


def now_iso() -> str:
    """기록 시각 문자열을 돌려준다. 날짜는 `today()`, 시각은 실제 현재 시각을 쓴다."""
    return f"{today().isoformat()}T{datetime.now().strftime('%H:%M:%S')}"


# ── 보관 가이드와 품목명 매칭 ─────────────────────────────────────


def _name_key(name: str) -> str:
    """품목명 비교용 키. 공백을 없애고 영문은 소문자로 바꾼다."""
    return re.sub(r"\s+", "", name).lower()


@lru_cache(maxsize=4)
def load_storage_guide(path: str = str(STORAGE_GUIDE)) -> dict[str, dict]:
    """`storage_guide.md`의 품목 기준표를 읽는다.

    품목명을 키로, 별칭 목록·기준 단위·권장 보관일수·개당 평균 무게 g(없으면 None)를
    값으로 하는 사전을 돌려준다.
    """
    text = Path(path).read_text(encoding="utf-8")
    table = text.split("## 품목 기준표", 1)[1].split("\n## ", 1)[0]
    guide: dict[str, dict] = {}
    for line in table.splitlines():
        line = line.strip()
        if not line.startswith("|") or re.match(r"^\|\s*-", line):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells[0] == "품목":
            continue
        name, aliases, unit, days, weight = cells
        guide[name] = {
            "aliases": [] if aliases == "-" else [a.strip() for a in aliases.split(",")],
            "base_unit": unit,
            "storage_days": int(days),
            "avg_weight_g": None if weight == "-" else float(weight),
        }
    return guide


@lru_cache(maxsize=4)
def _alias_index(path: str = str(STORAGE_GUIDE)) -> dict[str, str]:
    """품목명과 별칭을 품목명으로 잇는 색인을 만든다. 품목명이 별칭보다 우선한다."""
    guide = load_storage_guide(path)
    index: dict[str, str] = {}
    for name, info in guide.items():
        for alias in info["aliases"]:
            index.setdefault(_name_key(alias), name)
    for name in guide:
        index[_name_key(name)] = name
    return index


def resolve_item(name: str) -> str | None:
    """품목명을 보관 가이드의 품목명으로 바꾼다.

    정확 일치를 먼저, 별칭 일치를 다음으로 찾는다(공백 무시). 가이드에 없으면 None.
    예: "한우" → "소고기", "달걀" → "계란", "망고" → None.
    """
    return _alias_index().get(_name_key(name))


def guide_info(item: str) -> dict | None:
    """품목의 보관 가이드 정보를 돌려준다. 가이드에 없으면 None."""
    canonical = resolve_item(item)
    return load_storage_guide()[canonical] if canonical else None


def item_key(item: str) -> str:
    """같은 품목인지 비교하는 키. 가이드 품목이면 가이드 품목명, 아니면 입력한 이름."""
    return resolve_item(item) or item.strip()


# ── 단위 ─────────────────────────────────────────────────────────

# 입력 단위 → (기본 단위, 곱할 값)
UNIT_TABLE: dict[str, tuple[str, float]] = {
    "g": ("g", 1), "그램": ("g", 1),
    "kg": ("g", 1000), "킬로": ("g", 1000), "킬로그램": ("g", 1000),
    "l": ("L", 1), "리터": ("L", 1),
    "ml": ("L", 0.001), "밀리리터": ("L", 0.001),
    "개": ("개", 1), "모": ("개", 1), "팩": ("개", 1), "봉": ("개", 1),
    "알": ("개", 1), "단": ("개", 1), "대": ("개", 1), "캔": ("개", 1),
}


class UnitMismatchError(ValueError):
    """기준 단위로 환산할 수 없을 때 내는 오류 ("재고 단위 불일치")."""

    def __init__(self, item: str, unit: str, base_unit: str):
        """품목, 들어온 단위, 기준 단위를 기록한다."""
        self.item, self.unit, self.base_unit = item, unit, base_unit
        super().__init__(f"재고 단위 불일치: {item}의 기준 단위는 {base_unit}인데 {unit}(으)로 들어왔어요")


def _round(x: float) -> float:
    """부동소수점 오차를 없애려고 소수 여섯째 자리에서 반올림한다."""
    return round(x, 6)


def normalize_unit(qty: float, unit: str) -> tuple[float, str]:
    """수량과 단위를 `g`, `L`, `개` 중 하나로 바꾼다.

    `kg`→`g`, `ml`→`L`, `모·팩·봉·알·단·대·캔`→`개`. 모르는 단위면 ValueError.
    """
    key = unit.strip()
    key = key.lower() if key.isascii() else key
    if key not in UNIT_TABLE:
        raise ValueError(f"지원하지 않는 단위예요: {unit}")
    base, factor = UNIT_TABLE[key]
    return _round(float(qty) * factor), base


def base_unit_of(item: str, conn: sqlite3.Connection | None = None) -> str | None:
    """품목의 기준 단위를 돌려준다.

    가이드 품목은 가이드 표의 단위, 아니면 `item_units`에 기록된 첫 등록 단위.
    둘 다 없으면(처음 보는 품목) None.
    """
    info = guide_info(item)
    if info:
        return info["base_unit"]
    if conn is not None:
        row = conn.execute("SELECT base_unit FROM item_units WHERE item = ?", (item.strip(),)).fetchone()
        if row:
            return row["base_unit"]
    return None


def convert_to_base(item: str, qty: float, unit: str, conn: sqlite3.Connection | None = None) -> tuple[float, str]:
    """수량을 품목의 기준 단위로 바꾼다.

    단위가 기준 단위와 다르면 개당 평균 무게로 `개`↔`g`를 환산한다. 평균 무게가 없거나
    `L`처럼 환산할 길이 없으면 UnitMismatchError. 처음 보는 품목은 들어온 단위를 그대로 쓴다.
    """
    q, u = normalize_unit(qty, unit)
    base = base_unit_of(item, conn)
    if base is None or u == base:
        return q, u
    info = guide_info(item)
    weight = info["avg_weight_g"] if info else None
    if weight:
        if u == "개" and base == "g":
            return _round(q * weight), "g"
        if u == "g" and base == "개":
            return _round(q / weight), "개"
    raise UnitMismatchError(item, u, base)


def to_grams(item: str, qty: float, unit: str) -> float | None:
    """기준 단위 수량을 g으로 바꾼다 (폐기 회피량 집계용).

    `g`는 그대로, `L`는 1L = 1,000g, `개`는 개당 평균 무게. 평균 무게가 없으면 None.
    """
    if unit == "g":
        return _round(qty)
    if unit == "L":
        return _round(qty * 1000)
    info = guide_info(item)
    if unit == "개" and info and info["avg_weight_g"]:
        return _round(qty * info["avg_weight_g"])
    return None


# ── 날짜 ─────────────────────────────────────────────────────────

RELATIVE_DAY_WORDS = {"오늘": 0, "금일": 0, "어제": -1, "그제": -2, "그저께": -2, "엊그제": -2, "내일": 1}


def resolve_date(value: date | int | str | None = None) -> date:
    """상대 날짜 토큰이나 날짜 문자열을 날짜로 바꾼다.

    None·빈 값은 `today()`. 정수나 "-1"·"+2" 같은 토큰은 `today()` 기준 상대 일수.
    "어제"·"그저께"·"내일" 같은 말과 "YYYY-MM-DD"도 받는다. 해석할 수 없으면 ValueError.
    """
    if value is None:
        return today()
    if isinstance(value, date):
        return value
    if isinstance(value, int):
        return offset_date(value)
    text = str(value).strip()
    if not text:
        return today()
    if text in RELATIVE_DAY_WORDS:
        return offset_date(RELATIVE_DAY_WORDS[text])
    if re.fullmatch(r"[+-]?\d+", text):
        return offset_date(int(text))
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"날짜를 해석할 수 없어요: {value}") from None


def default_expiry(item: str, input_date: date) -> tuple[date | None, str]:
    """유효기간 기본값을 계산한다.

    가이드 품목은 `투입일자 + 권장 보관일수`와 출처 `guide`.
    가이드에 없으면 `(None, "unknown")`.
    """
    info = guide_info(item)
    if info is None:
        return None, "unknown"
    return input_date + timedelta(days=info["storage_days"]), "guide"


def d_day(expiry: date | str | None, base: date | None = None) -> int | None:
    """D-day(유효기간 - 오늘, 정수 일수)를 계산한다. 유효기간 미상이면 None."""
    if expiry is None:
        return None
    if isinstance(expiry, str):
        expiry = date.fromisoformat(expiry)
    return (expiry - (base or today())).days


def alert_level(dday: int | None) -> str | None:
    """D-day로 경고 수준을 정한다. 0~3은 `imminent`, 음수는 `expired`, 4 이상·미상은 None."""
    if dday is None or dday > 3:
        return None
    return "expired" if dday < 0 else "imminent"


def list_alerts(conn: sqlite3.Connection) -> list[dict]:
    """보관 중인 재고의 임박·만료 경고를 D-day 순으로 돌려준다 (`trace.alerts`용)."""
    alerts = []
    rows = conn.execute(
        "SELECT sn, item, qty, unit, expiry_date FROM inventory WHERE status = 'stored' AND expiry_date IS NOT NULL"
    ).fetchall()
    for row in rows:
        dd = d_day(row["expiry_date"])
        level = alert_level(dd)
        if level:
            alerts.append({"sn": row["sn"], "item": row["item"], "qty": row["qty"], "unit": row["unit"],
                           "d_day": dd, "level": level})
    return sorted(alerts, key=lambda a: (a["d_day"], a["sn"]))


# ── S/N ──────────────────────────────────────────────────────────

SN_PATTERN = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])-(\d{3})$")


def next_sn(conn: sqlite3.Connection, input_date: date) -> str:
    """투입일자의 연-월에서 다음 S/N(`YYYY-MM-NNN`)을 발번한다.

    그 달에 이미 쓴 가장 큰 순번 + 1이다. 소진·폐기된 재고의 번호도 다시 쓰지 않는다.
    """
    prefix = input_date.strftime("%Y-%m")
    rows = conn.execute("SELECT sn FROM inventory WHERE sn LIKE ?", (f"{prefix}-%",)).fetchall()
    used = [int(m.group(3)) for r in rows if (m := SN_PATTERN.match(r["sn"]))]
    seq = max(used, default=0) + 1
    if seq > 999:
        raise ValueError(f"{prefix}의 S/N 순번이 999를 넘었어요")
    return f"{prefix}-{seq:03d}"


def check_sn(conn: sqlite3.Connection, sn: str) -> dict:
    """사용자가 준 S/N의 형식(`YYYY-MM-NNN`)과 중복 여부를 검사한다.

    `{"valid": bool, "reason": str | None}`을 돌려준다.
    """
    m = SN_PATTERN.match(sn.strip())
    if not m or m.group(3) == "000":
        return {"valid": False, "reason": "S/N 형식이 YYYY-MM-NNN이 아니에요"}
    if conn.execute("SELECT 1 FROM inventory WHERE sn = ?", (sn.strip(),)).fetchone():
        return {"valid": False, "reason": f"이미 있는 S/N이에요: {sn.strip()}"}
    return {"valid": True, "reason": None}


# ── 입고 ─────────────────────────────────────────────────────────


def add_inventory(
    conn: sqlite3.Connection,
    item: str,
    qty: float,
    unit: str,
    input_date: date | int | str | None = None,
    expiry_date: date | int | str | None = None,
    sn: str | None = None,
) -> dict:
    """재고 한 건을 입고한다. 커밋은 호출한 쪽이 한다.

    - 투입일자: 상대 토큰("어제", "-1")이나 날짜. 없으면 오늘.
    - 단위: 기준 단위로 환산. 환산할 수 없으면 저장하지 않고 `status="unit_mismatch"`.
    - S/N: 주면 형식·중복을 검사하고, 어긋나면 저장하지 않고 `status="sn_invalid"`와
      자동 발번 S/N(`suggested_sn`)을 돌려준다. 없으면 자동 발번.
    - 유효기간: 주면 출처 `user`, 없으면 가이드로 계산(`guide`), 가이드에도 없으면 미상(`unknown`).
    - 가이드에 없는 품목은 첫 등록 단위를 `item_units`에 기준 단위로 기록한다.
    """
    in_date = resolve_date(input_date)
    try:
        q, u = convert_to_base(item, qty, unit, conn)
    except UnitMismatchError as e:
        return {"status": "unit_mismatch", "item": item, "unit": e.unit, "base_unit": e.base_unit, "message": str(e)}

    if sn:
        check = check_sn(conn, sn)
        if not check["valid"]:
            return {"status": "sn_invalid", "item": item, "sn": sn, "reason": check["reason"],
                    "suggested_sn": next_sn(conn, in_date)}
        sn = sn.strip()
    else:
        sn = next_sn(conn, in_date)

    if expiry_date is not None:
        expiry, source = resolve_date(expiry_date), "user"
    else:
        expiry, source = default_expiry(item, in_date)

    if resolve_item(item) is None and base_unit_of(item, conn) is None:
        conn.execute("INSERT INTO item_units (item, base_unit) VALUES (?, ?)", (item.strip(), u))

    conn.execute(
        "INSERT INTO inventory (sn, item, qty, unit, expiry_date, expiry_source, input_date, status)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'stored')",
        (sn, item.strip(), q, u, expiry.isoformat() if expiry else None, source, in_date.isoformat()),
    )
    return {
        "status": "added",
        "sn": sn,
        "item": item.strip(),
        "qty": q,
        "unit": u,
        "input_date": in_date.isoformat(),
        "expiry_date": expiry.isoformat() if expiry else None,
        "expiry_source": source,
        "d_day": d_day(expiry),
    }


# ── 차감 (FEFO) ──────────────────────────────────────────────────

REASON_MEAL = "meal"
REASON_OUTFLOW = "outflow"
REASON_DISCARD = "discard"


def stock_rows(conn: sqlite3.Connection, item: str) -> list[sqlite3.Row]:
    """품목(별칭 포함)의 보관 중 재고를 FEFO 순서로 돌려준다.

    유효기간이 빠른 순, 유효기간 미상은 맨 뒤, 같으면 투입일자·S/N 순.
    """
    key = item_key(item)
    rows = conn.execute(
        "SELECT * FROM inventory WHERE status = 'stored'"
        " ORDER BY expiry_date IS NULL, expiry_date, input_date, sn"
    ).fetchall()
    return [r for r in rows if item_key(r["item"]) == key]


def deduct_fefo(conn: sqlite3.Connection, item: str, qty: float, unit: str, reason: str = REASON_OUTFLOW) -> dict:
    """재고를 유효기간이 빠른 것부터(FEFO) 차감한다. 커밋은 호출한 쪽이 한다.

    출고·식사 차감·폐기가 모두 이 함수 하나를 쓴다. S/N마다 `deductions`에
    g 환산량(평균 무게가 없으면 NULL)과 차감 시점 D-day를 남기고, 0이 된 재고는
    `consumed`(폐기면 `discarded`)로 바꾼다.

    돌려주는 `status`:
    - `ok`: 요청량을 모두 차감
    - `partial`: 있는 만큼만 차감, `shortage`에 모자란 양
    - `not_in_stock`: 재고 없음, 차감하지 않음
    - `unit_mismatch`: 기준 단위로 환산할 수 없음, 차감하지 않음
    """
    try:
        need, u = convert_to_base(item, qty, unit, conn)
    except UnitMismatchError as e:
        return {"status": "unit_mismatch", "item": item, "unit": e.unit, "base_unit": e.base_unit, "message": str(e)}

    rows = stock_rows(conn, item)
    if not rows:
        return {"status": "not_in_stock", "item": item, "requested": need, "deducted": 0.0,
                "shortage": need, "unit": u, "details": []}

    remaining = need
    details = []
    done_status = "discarded" if reason == REASON_DISCARD else "consumed"
    created_at = now_iso()
    for row in rows:
        if remaining <= 0:
            break
        take = _round(min(remaining, row["qty"]))
        left = _round(row["qty"] - take)
        conn.execute(
            "UPDATE inventory SET qty = ?, status = ? WHERE sn = ?",
            (left, done_status if left <= 0 else "stored", row["sn"]),
        )
        dd = d_day(row["expiry_date"])
        conn.execute(
            "INSERT INTO deductions (sn, qty_g, d_day_at_deduction, reason, created_at) VALUES (?, ?, ?, ?, ?)",
            (row["sn"], to_grams(item, take, u), dd, reason, created_at),
        )
        details.append({"sn": row["sn"], "qty": take, "d_day": dd})
        remaining = _round(remaining - take)

    deducted = _round(need - remaining)
    return {
        "status": "ok" if remaining <= 0 else "partial",
        "item": item,
        "requested": need,
        "deducted": deducted,
        "shortage": max(remaining, 0.0),
        "unit": u,
        "details": details,
    }


# ── 폐기 회피량 ─────────────────────────────────────────────────


def week_start(base: date | None = None) -> date:
    """기준일(기본값 `today()`)이 속한 주의 월요일을 돌려준다."""
    base = base or today()
    return base - timedelta(days=base.weekday())


def rescued_g_week(conn: sqlite3.Connection) -> dict:
    """이번 주(월요일 시작) 가족 전체의 폐기 회피량을 g으로 계산한다.

    출고·식사 차감 중 차감 시점 D-day가 0~3이었던 양만 센다. 만료 품목과 폐기는 빼고,
    평균 무게가 없어 g으로 바꿀 수 없던 차감은 집계에서 빼고 `excluded`에 적는다.
    """
    start = week_start()
    rows = conn.execute(
        "SELECT d.sn, d.qty_g, i.item FROM deductions d JOIN inventory i ON i.sn = d.sn"
        " WHERE d.reason != ? AND d.d_day_at_deduction BETWEEN 0 AND 3"
        " AND substr(d.created_at, 1, 10) BETWEEN ? AND ?",
        (REASON_DISCARD, start.isoformat(), today().isoformat()),
    ).fetchall()
    total = sum(r["qty_g"] for r in rows if r["qty_g"] is not None)
    excluded = [{"sn": r["sn"], "item": r["item"]} for r in rows if r["qty_g"] is None]
    return {"total_g": _round(total), "week_start": start.isoformat(), "excluded": excluded}


# ── 칼로리 ──────────────────────────────────────────────────────


def calc_kcal(servings_eaten: float, kcal_per_serving: float | None) -> float | None:
    """섭취 kcal = 먹은 인분 × 1인분 kcal. 1인분 kcal을 모르면 None."""
    if servings_eaten <= 0:
        raise ValueError("먹은 인분은 0보다 커야 해요")
    if kcal_per_serving is None:
        return None
    return _round(servings_eaten * kcal_per_serving)


# ── 레시피 파서 ─────────────────────────────────────────────────


@dataclass(frozen=True)
class Ingredient:
    """레시피 재료 표의 한 행. 양은 1인분 기준이고 단위는 품목 기준 단위다."""

    item: str
    qty: float
    unit: str


@dataclass(frozen=True)
class Recipe:
    """레시피 문서에서 읽은 요리 하나."""

    name: str
    cuisine: str
    main: str
    kcal: float
    allergens: tuple[str, ...]
    ingredients: tuple[Ingredient, ...]
    summary: str
    steps: tuple[str, ...]
    source: str


def _table_rows(lines: list[str]) -> list[list[str]]:
    """마크다운 표 줄에서 머리글과 구분선을 뺀 데이터 행의 칸 목록을 돌려준다."""
    rows = [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in lines
        if line.strip().startswith("|")
    ]
    return [row for row in rows[1:] if not re.fullmatch(r"[-: ]+", "".join(row))]


def _parse_recipe_block(block: str, cuisine: str, source: str) -> Recipe:
    """`## 요리명`으로 시작하는 레시피 한 덩어리를 `Recipe`로 바꾼다. 형식이 어긋나면 ValueError."""
    lines = block.splitlines()
    name = lines[0].lstrip("#").strip()
    body = lines[1:]

    def section(title: str) -> list[str]:
        start = next((i for i, line in enumerate(body) if line.startswith(f"### {title}")), None)
        if start is None:
            raise ValueError(f"{source} '{name}': '### {title}' 절이 없습니다")
        rest = body[start + 1:]
        end = next((i for i, line in enumerate(rest) if line.startswith("### ")), len(rest))
        return rest[:end]

    head = body[: next((i for i, line in enumerate(body) if line.startswith("### ")), len(body))]
    summary = next((line.strip() for line in head if line.strip() and not line.startswith("|")), "")
    meta = {row[0]: row[1] for row in _table_rows(head) if len(row) >= 2}
    for key in ("카테고리", "주재료", "1인분 kcal", "알레르기"):
        if key not in meta:
            raise ValueError(f"{source} '{name}': 메타 표에 '{key}' 행이 없습니다")
    if meta["카테고리"] != cuisine:
        raise ValueError(f"{source} '{name}': 카테고리 '{meta['카테고리']}'가 파일과 다릅니다")

    ingredients = tuple(
        Ingredient(item=row[0], qty=float(row[1]), unit=row[2]) for row in _table_rows(section("재료"))
    )
    if meta["주재료"] not in {ing.item for ing in ingredients}:
        raise ValueError(f"{source} '{name}': 주재료가 재료 표에 없습니다")
    allergens = tuple(a.strip() for a in meta["알레르기"].split(",") if a.strip() and a.strip() != "없음")
    steps = tuple(
        re.sub(r"^\s*\d+\.\s*", "", line) for line in section("조리 순서") if re.match(r"^\s*\d+\.", line)
    )
    return Recipe(
        name=name,
        cuisine=cuisine,
        main=meta["주재료"],
        kcal=float(meta["1인분 kcal"]),
        allergens=allergens,
        ingredients=ingredients,
        summary=summary,
        steps=steps,
        source=source,
    )


@lru_cache(maxsize=1)
def parse_recipes() -> dict[str, Recipe]:
    """레시피 문서 3개를 읽어 요리명별 `Recipe` 사전을 돌려준다.

    형식이 어긋난 레시피나 겹치는 요리명이 있으면 ValueError를 낸다.
    """
    recipes: dict[str, Recipe] = {}
    for cuisine, path in RECIPE_FILES.items():
        text = path.read_text(encoding="utf-8")
        for block in re.split(r"(?m)^(?=## )", text):
            if not block.startswith("## ") or block.startswith("## 문서 형식"):
                continue
            recipe = _parse_recipe_block(block, cuisine, path.name)
            if recipe.name in recipes:
                raise ValueError(f"요리명 '{recipe.name}'이 두 번 나옵니다")
            recipes[recipe.name] = recipe
    return recipes


# ── 요리 추천 (SERVICE.md 5.2, 5.5) ───────────────────────────────

MIN_GOOD_RATING = 3.0
MAX_NEW_IN_SLOTS = 2
SLOT_EXTRA_NEW = "추가 신메뉴"
SLOT_GROUP_RATED = "그룹 평점"
SLOT_GROUP_NEW = "그룹 신메뉴"


class ImminentUse(BaseModel):
    """요리가 쓰는 만료 임박 재료."""

    item: str = Field(description="보관 가이드 품목명")
    d_day: int = Field(description="그 품목 재고 중 가장 작은 D-day (0~3)")


class RecommendationItem(BaseModel):
    """추천 요리 하나."""

    slot: str = Field(default="", description="한식·일식·이탈리아식 슬롯 또는 '추가 신메뉴'. 가족 추천은 '그룹 평점'·'그룹 신메뉴'")
    dish: str = Field(description="요리명")
    cuisine: str = Field(description="카테고리 (한식·일식·이탈리아식)")
    main_ingredient: str = Field(description="주재료")
    missing_ingredients: list[str] = Field(default_factory=list, description="재고에 없거나 모자란 재료")
    rating: float | None = Field(default=None, description="평균 평점(소수 첫째 자리). 미평가면 null. 가족 추천은 그룹 평점")
    rating_count: int = Field(default=0, description="평가 횟수. 가족 추천은 평가한 참여자 수")
    is_new: bool = Field(description="신메뉴(미평가) 여부")
    imminent_ingredients: list[ImminentUse] = Field(default_factory=list, description="사용하는 만료 임박 재료")
    kcal_per_serving: float = Field(description="1인분 kcal")
    summary: str = Field(default="", description="레시피 요약")
    source: str = Field(description="출처 문서 파일명")


class EmptySlot(BaseModel):
    """채우지 못한 슬롯과 이유."""

    slot: str
    reason: str


class RecommendationResult(BaseModel):
    """단일 사용자 요리 추천 결과 (SERVICE.md 5.2)."""

    user_id: str
    category: str | None = Field(default=None, description="사용자가 지정한 카테고리. 없으면 null")
    items: list[RecommendationItem] = Field(default_factory=list)
    empty_slots: list[EmptySlot] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list, description="신메뉴가 없을 때 같은 안내 문구")


class FamilyRecommendationResult(BaseModel):
    """가족 합의 저녁 추천 결과 (SERVICE.md 5.5). 다른 가족의 개별 평점·칼로리는 담지 않는다."""

    diners: list[str]
    items: list[RecommendationItem] = Field(default_factory=list)
    empty_slots: list[EmptySlot] = Field(default_factory=list)
    within_calorie_budget: bool | None = Field(default=None, description="모두의 남은 칼로리 안인지. 칼로리 정보가 없으면 null")
    notes: list[str] = Field(default_factory=list)


@dataclass
class _Candidate:
    """재고 조건을 통과한 레시피와 부족·임박 재료."""

    recipe: Recipe
    missing: list[str]
    imminent: dict[str, int] = field(default_factory=dict)


@dataclass
class _Exclusions:
    """후보에서 빠진 레시피 수를 이유별로 센다."""

    no_stock: int = 0
    allergy: int = 0
    dislike: int = 0


def _round1(value: float) -> float:
    """소수 첫째 자리에서 사사오입한다 (4.25 → 4.3)."""
    return int(value * 10 + 0.5 + 1e-9) / 10


def usable_stock(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """추천에 쓸 수 있는 재고(보관 중, 수량 > 0, 만료 전)를 품목 키별로 묶는다.

    만료(D-day 음수) 재고는 빼고, 유효기간 미상은 넣는다. 각 항목은 sn·qty·unit·d_day.
    """
    stock: dict[str, list[dict]] = {}
    rows = conn.execute(
        "SELECT sn, item, qty, unit, expiry_date FROM inventory WHERE status = 'stored' AND qty > 0"
    ).fetchall()
    for row in rows:
        dd = d_day(row["expiry_date"])
        if alert_level(dd) == "expired":
            continue
        stock.setdefault(item_key(row["item"]), []).append(
            {"sn": row["sn"], "qty": row["qty"], "unit": row["unit"], "d_day": dd}
        )
    return stock


def _check_recipe(recipe: Recipe, stock: dict[str, list[dict]], servings: float) -> _Candidate | None:
    """주재료가 재고에 있으면 부족 재료와 임박 재료를 계산해 후보로 돌려주고, 없으면 None."""
    if item_key(recipe.main) not in stock:
        return None
    missing: list[str] = []
    imminent: dict[str, int] = {}
    for ing in recipe.ingredients:
        have = 0.0
        for lot in stock.get(item_key(ing.item), []):
            try:
                qty, _ = convert_to_base(ing.item, lot["qty"], lot["unit"])
                have += qty
            except UnitMismatchError:
                # 양을 비교할 수 없는 재고는 충분하다고 본다.
                have = float("inf")
            if alert_level(lot["d_day"]) == "imminent":
                imminent[ing.item] = min(imminent.get(ing.item, lot["d_day"]), lot["d_day"])
        if have + 1e-9 < ing.qty * servings:
            missing.append(ing.item)
    return _Candidate(recipe=recipe, missing=missing, imminent=imminent)


def has_allergen(recipe: Recipe, allergies: list[str] | tuple[str, ...]) -> bool:
    """레시피에 알레르기 재료가 들어 있는지 본다.

    메타 표의 알레르기 분류(예: 갑각류)나 재료명(예: 새우, 별칭 대하 포함)이 겹치면 True.
    """
    items = {item_key(ing.item) for ing in recipe.ingredients}
    for allergy in allergies:
        allergy = allergy.strip()
        if allergy and (allergy in recipe.allergens or item_key(allergy) in items):
            return True
    return False


def _safe_candidates(
    conn: sqlite3.Connection, allergies: list[str], dislikes: list[str], servings: float
) -> tuple[list[_Candidate], _Exclusions]:
    """안전 필터(알레르기 → 비선호)를 먼저 적용하고, 재고 조건을 통과한 후보를 돌려준다."""
    stock = usable_stock(conn)
    excluded = _Exclusions()
    dislike_set = {d.strip() for d in dislikes}
    candidates: list[_Candidate] = []
    for recipe in parse_recipes().values():
        if has_allergen(recipe, allergies):
            excluded.allergy += 1
        elif recipe.name in dislike_set:
            excluded.dislike += 1
        elif (candidate := _check_recipe(recipe, stock, servings)) is None:
            excluded.no_stock += 1
        else:
            candidates.append(candidate)
    return candidates, excluded


def user_ratings(conn: sqlite3.Connection, user_id: str) -> dict[str, tuple[float, int]]:
    """사용자의 요리별 (평균 평점 원값, 평가 횟수)를 돌려준다."""
    rows = conn.execute(
        "SELECT dish, AVG(score) AS avg, COUNT(*) AS cnt FROM ratings WHERE user_id = ? GROUP BY dish",
        (user_id,),
    ).fetchall()
    return {row["dish"]: (row["avg"], row["cnt"]) for row in rows}


def seed_profile(user_id: str) -> dict:
    """시드 JSON에서 사용자의 선호·비선호·알레르기를 읽는다.

    장기 메모리 Store(TASK-16)가 생기기 전까지 쓰는 임시 출처다. 없는 사용자면 빈 목록.
    """
    for user in _load_json(SEED_USERS)["users"]:
        if user["user_id"] == user_id:
            return {key: list(user.get(key, [])) for key in ("likes", "dislikes", "allergies")}
    return {"likes": [], "dislikes": [], "allergies": []}


def _to_item(candidate: _Candidate, rating: float | None, count: int) -> RecommendationItem:
    """후보를 출력 모델로 바꾼다. 평점이 None이면 신메뉴다."""
    recipe = candidate.recipe
    return RecommendationItem(
        dish=recipe.name,
        cuisine=recipe.cuisine,
        main_ingredient=recipe.main,
        missing_ingredients=candidate.missing,
        rating=None if rating is None else _round1(rating),
        rating_count=count,
        is_new=rating is None,
        imminent_ingredients=[
            ImminentUse(item=item, d_day=dd)
            for item, dd in sorted(candidate.imminent.items(), key=lambda kv: (kv[1], kv[0]))
        ],
        kcal_per_serving=recipe.kcal,
        summary=recipe.summary,
        source=recipe.source,
    )


def rank_key(item: RecommendationItem) -> tuple:
    """동점 정렬 키 (SERVICE.md 5.2).

    임박 재료 사용 여부(사용 우선) → 임박 재료 최소 D-day(작을수록) → 평균 평점(높을수록)
    → 평가 횟수(많을수록) → 요리명 가나다순.
    """
    min_dd = min((use.d_day for use in item.imminent_ingredients), default=None)
    return (
        min_dd is None,
        min_dd if min_dd is not None else 0,
        -(item.rating if item.rating is not None else -1.0),
        -item.rating_count,
        item.dish,
    )


def _good(items: list[RecommendationItem]) -> list[RecommendationItem]:
    """평점 3점 이상인 평가된 요리를 동점 정렬 순서로 돌려준다."""
    return sorted((i for i in items if not i.is_new and i.rating >= MIN_GOOD_RATING), key=rank_key)


def _new(items: list[RecommendationItem]) -> list[RecommendationItem]:
    """신메뉴를 동점 정렬 순서로 돌려준다."""
    return sorted((i for i in items if i.is_new), key=rank_key)


def _slot(item: RecommendationItem, slot: str) -> RecommendationItem:
    """슬롯 이름을 붙인 사본을 돌려준다."""
    return item.model_copy(update={"slot": slot})


def _no_dish_reason(cuisine: str, has_any: bool) -> str:
    """빈 슬롯 이유 문구를 만든다."""
    if not has_any:
        return f"재고로 만들 수 있는 {cuisine} 요리가 없어요"
    return f"재고로 만들 수 있는 {cuisine} 요리 중 평점 3점 이상이거나 새로 시도할 요리가 없어요"


def recommend(
    conn: sqlite3.Connection,
    user_id: str,
    category: str | None = None,
    allergies: list[str] | None = None,
    dislikes: list[str] | None = None,
    servings: float = 1,
) -> RecommendationResult:
    """요청자 한 명의 요리 추천을 슬롯 규칙(SERVICE.md 5.2 규칙 1~6)대로 만든다.

    알레르기·비선호를 주지 않으면 `seed_profile()`에서 읽는다. 부족 재료는 `servings`
    인분 기준으로 계산한다. 카테고리를 지정하면 그 안에서 평점 우선 2개와 신메뉴 1개를
    고른다. 지원하지 않는 카테고리는 ValueError를 낸다.
    """
    if category is not None and category not in CUISINES:
        raise ValueError(f"지원하지 않는 카테고리예요: {category} (한식·일식·이탈리아식만 지원)")
    profile = seed_profile(user_id)
    allergies = profile["allergies"] if allergies is None else allergies
    dislikes = profile["dislikes"] if dislikes is None else dislikes

    candidates, _ = _safe_candidates(conn, allergies, dislikes, servings)
    ratings = user_ratings(conn, user_id)
    pool = [_to_item(c, *ratings.get(c.recipe.name, (None, 0))) for c in candidates]
    result = RecommendationResult(user_id=user_id, category=category)

    # 규칙 6: 카테고리 지정
    if category is not None:
        in_cat = [i for i in pool if i.cuisine == category]
        result.items = [_slot(i, category) for i in _good(in_cat)[:2]]
        new = _new(in_cat)
        if new:
            result.items.append(_slot(new[0], category))
        elif in_cat:
            result.notes.append(f"새로 시도할 수 있는 {category} 요리가 없어요")
        if not result.items:
            result.empty_slots.append(EmptySlot(slot=category, reason=_no_dish_reason(category, bool(in_cat))))
        return result

    # 규칙 1~3: 카테고리별 슬롯
    slots: dict[str, RecommendationItem] = {}
    empty: dict[str, str] = {}
    for cuisine in CUISINES:
        in_cat = [i for i in pool if i.cuisine == cuisine]
        pick = (_good(in_cat) or _new(in_cat) or [None])[0]
        if pick:
            slots[cuisine] = pick
        else:
            empty[cuisine] = _no_dish_reason(cuisine, bool(in_cat))

    # 규칙 4: 슬롯 신메뉴가 3개면 앞선 2개만 남긴다
    new_in_slots = sorted((i for i in slots.values() if i.is_new), key=rank_key)
    for dropped in new_in_slots[MAX_NEW_IN_SLOTS:]:
        del slots[dropped.cuisine]
        empty[dropped.cuisine] = f"신메뉴는 한 번에 {MAX_NEW_IN_SLOTS}개까지만 추천해요"

    result.items = [_slot(slots[c], c) for c in CUISINES if c in slots]
    result.empty_slots = [EmptySlot(slot=c, reason=empty[c]) for c in CUISINES if c in empty]

    # 규칙 4·5: 슬롯에 신메뉴가 없으면 전체에서 1개 추가, 후보가 없으면 이유만 적는다
    if not new_in_slots:
        extra = _new(pool)
        if extra:
            result.items.append(_slot(extra[0], SLOT_EXTRA_NEW))
        else:
            result.notes.append("새로 시도할 수 있는 요리가 없어요")
    return result


def recommend_family(
    conn: sqlite3.Connection,
    requester: str,
    diners: list[str],
    profiles: dict[str, dict] | None = None,
    calorie_budgets: dict[str, float] | None = None,
) -> FamilyRecommendationResult:
    """가족 합의 저녁 추천을 만든다 (SERVICE.md 5.5).

    순서는 안전 필터 → 칼로리 제약 → 자리 채우기다. 요청자는 자동으로 참여자에 들어간다.
    그룹 평점은 참여자별 평균 평점의 평균이고, 아무도 평가하지 않았으면 그룹 신메뉴다.
    `profiles`에 없는 참여자는 `seed_profile()`을 쓰고, `calorie_budgets`(참여자별 오늘
    남은 kcal)가 없으면 칼로리 제약을 건너뛴다. 결과에는 개인별 수치를 담지 않는다.
    """
    members = list(dict.fromkeys([requester, *diners]))
    profiles = profiles or {}
    allergies: list[str] = []
    dislikes: list[str] = []
    for member in members:
        profile = profiles.get(member) or seed_profile(member)
        allergies += profile.get("allergies", [])
        dislikes += profile.get("dislikes", [])

    candidates, excluded = _safe_candidates(conn, allergies, dislikes, 1)
    member_ratings = [user_ratings(conn, m) for m in members]
    pool = []
    for c in candidates:
        averages = [r[c.recipe.name][0] for r in member_ratings if c.recipe.name in r]
        pool.append(_to_item(c, sum(averages) / len(averages) if averages else None, len(averages)))

    result = FamilyRecommendationResult(diners=members)
    budgets = [calorie_budgets[m] for m in members if calorie_budgets and m in calorie_budgets]
    if budgets and pool:
        limit = min(budgets)
        within = [i for i in pool if i.kcal_per_serving <= limit]
        result.within_calorie_budget = bool(within)
        if not within:
            lightest = min(i.kcal_per_serving for i in pool)
            within = [i for i in pool if i.kcal_per_serving == lightest]
            result.notes.append("모두의 남은 칼로리 안에 드는 요리가 없어 가장 가벼운 요리를 골랐어요")
        pool = within

    good, new = _good(pool), _new(pool)
    if not good and not new:
        blocked = [
            label
            for label, count in (("재고", excluded.no_stock), ("알레르기", excluded.allergy), ("비선호", excluded.dislike))
            if count
        ]
        result.notes.append(f"함께 먹을 수 있는 요리를 찾지 못했어요 (걸린 조건: {', '.join(blocked)})")
        return result
    if good:
        result.items.append(_slot(good[0], SLOT_GROUP_RATED))
    else:
        result.empty_slots.append(EmptySlot(slot=SLOT_GROUP_RATED, reason="함께 먹는 사람들의 평점이 3점 이상인 요리가 없어요"))
    if new:
        result.items.append(_slot(new[0], SLOT_GROUP_NEW))
    else:
        result.empty_slots.append(EmptySlot(slot=SLOT_GROUP_NEW, reason="함께 먹는 사람 모두가 이미 평가한 요리뿐이라 새로 시도할 요리가 없어요"))
    return result


# ── 에이전트 도구 (TASK-06) ───────────────────────────────────────
#
# 신원(user_id, family_id)은 도구 인자가 아니라 런타임 컨텍스트(`FridgeContext`)로만 받는다.
# 그래서 LLM이 어떤 말을 들어도 다른 사용자로 도구를 부를 수 없다.
#
# 승인 도구 공통 규칙 (SERVICE.md 5.3):
#   1. 읽기만 해서 승인 요약을 만든다.
#   2. 쓰기 전에 `interrupt(요약)`을 부른다. 재개되면 도구가 처음부터 다시 실행된다.
#   3. 승인되면 한 트랜잭션으로 한 번만 저장하고, 멱등 키(thread_id:tool_call_id)로 중복을 막는다.
#   4. 거절되면 도구가 정해진 문구를 돌려준다.

# 쓰기 전에 사용자 승인을 받는 도구. withdraw_item은 보유량의 50%를 넘게 꺼낼 때만 승인을 받는다.
APPROVAL_TOOLS = frozenset({"confirm_meal", "discard_item", "withdraw_item"})
BULK_WITHDRAW_RATIO = 0.5
GUEST_USER = "guest"

REJECT_MEAL = "취소했어요. 평가도 저장하지 않았어요"
REJECT_DISCARD = "취소했어요. 폐기하지 않았어요"
REJECT_WITHDRAW = "취소했어요. 재고를 바꾸지 않았어요"
GUEST_DENIED = "평가와 재고 변경은 로그인한 가족만 할 수 있어요"
ASK_SERVINGS = "몇 인분 만들었고, 본인은 몇 인분 드셨나요?"
WATCH_PENDING = "워치 기록은 아직 연결 전이라 대기 중이에요"


@dataclass
class FridgeContext:
    """서버가 확인한 요청자 신원. 에이전트 실행 시 `context=`로 넣는다.

    `db_path`는 테스트·평가에서 임시 DB를 쓰려고 둔다. 없으면 `data/fridge.db`.
    """

    user_id: str
    family_id: str = "family-1"
    db_path: str | None = None


class InventoryInput(BaseModel):
    """입고할 재고 한 건."""

    item: str = Field(description="품목명. 사용자가 말한 이름 그대로 (예: 한우, 두부)")
    qty: float = Field(description="수량")
    unit: str = Field(description="단위. 사용자가 말한 그대로 (g, kg, L, ml, 개, 모, 팩, 봉, 알 등)")
    input_date: str | None = Field(
        default=None,
        description="투입일자. '오늘', '어제', '-1' 같은 상대 표현이나 YYYY-MM-DD. 말하지 않았으면 비운다",
    )
    expiry_date: str | None = Field(default=None, description="사용자가 말한 유효기간(YYYY-MM-DD). 말하지 않았으면 비운다")
    sn: str | None = Field(default=None, description="사용자가 직접 준 S/N(YYYY-MM-NNN). 말하지 않았으면 비운다")


def _context(runtime: ToolRuntime[FridgeContext]) -> FridgeContext:
    """런타임 컨텍스트에서 서버가 확인한 신원을 꺼낸다. 없으면 실행을 멈춘다."""
    ctx = runtime.context
    # `tools`와 `src.tools`로 두 번 import돼도 동작하도록 클래스 대신 속성으로 확인한다.
    if not getattr(ctx, "user_id", None):
        raise RuntimeError("런타임 컨텍스트(FridgeContext)가 없어요")
    return ctx


def _open(runtime: ToolRuntime[FridgeContext]) -> sqlite3.Connection:
    """요청자 컨텍스트의 도메인 DB 연결을 연다."""
    return connect(_context(runtime).db_path)


def idempotency_key(runtime: ToolRuntime[FridgeContext]) -> str:
    """승인 도구의 멱등 키 `thread_id:tool_call_id`를 만든다."""
    thread_id = (runtime.config or {}).get("configurable", {}).get("thread_id", "no-thread")
    return f"{thread_id}:{runtime.tool_call_id}"


def is_approved(decision: object) -> bool:
    """재개 값이 승인인지 판단한다. `"approve"` 또는 `{"decision": "approve"}`를 받는다."""
    if isinstance(decision, dict):
        decision = decision.get("decision") or decision.get("resume")
    return str(decision).strip().lower() in {"approve", "approved", "yes", "y", "승인", "true"}


def _fmt_qty(qty: float, unit: str = "") -> str:
    """수량을 보기 좋게 적는다 (300.0 → "300", 0.5 → "0.5")."""
    return f"{qty:.2f}".rstrip("0").rstrip(".") + unit


def _to_json(obj: object) -> str:
    """도구 결과를 한글이 그대로 보이는 JSON 문자열로 바꾼다."""
    return json.dumps(obj, ensure_ascii=False, default=str)


def _applied_result(conn: sqlite3.Connection, key: str) -> str | None:
    """이미 처리한 승인 작업이면 그때 돌려준 문구를, 아니면 None을 돌려준다."""
    row = conn.execute("SELECT result FROM applied_actions WHERE idem_key = ?", (key,)).fetchone()
    return row["result"] if row else None


def _record_applied(conn: sqlite3.Connection, key: str, action: str, user_id: str, message: str) -> None:
    """승인 작업을 처리했다고 기록한다. 저장과 같은 트랜잭션에서 부른다."""
    conn.execute(
        "INSERT INTO applied_actions (idem_key, action, user_id, result, created_at) VALUES (?, ?, ?, ?, ?)",
        (key, action, user_id, message, now_iso()),
    )


def _lot_label(conn: sqlite3.Connection, result: dict) -> str:
    """차감 결과를 보여줄 이름. 실제로 차감한 재고의 이름(예: 한우)을 쓰고, 없으면 요청한 이름."""
    for detail in result.get("details", []):
        row = conn.execute("SELECT item FROM inventory WHERE sn = ?", (detail["sn"],)).fetchone()
        if row:
            return row["item"]
    return result["item"]


def plan_meal_deductions(conn: sqlite3.Connection, recipe: Recipe, servings_made: float) -> list[dict]:
    """레시피 재료를 만든 인분만큼 FEFO로 차감한다. 커밋하지 않는다.

    승인 요약을 만들 때는 이 함수를 부른 뒤 롤백해서 미리보기로 쓰고,
    승인 후에는 같은 함수를 트랜잭션 안에서 다시 불러 실제로 차감한다.
    """
    results = []
    for ing in recipe.ingredients:
        result = deduct_fefo(conn, ing.item, _round(ing.qty * servings_made), ing.unit, reason=REASON_MEAL)
        result["label"] = _lot_label(conn, result)
        results.append(result)
    return results


def describe_deductions(results: list[dict]) -> tuple[str, list[str]]:
    """차감 결과를 '한우 300g·양파 1개' 문구와 부족·건너뜀 안내 목록으로 바꾼다."""
    parts: list[str] = []
    notes: list[str] = []
    for r in results:
        if r["status"] in ("ok", "partial") and r["deducted"] > 0:
            parts.append(f"{r['label']} {_fmt_qty(r['deducted'], r['unit'])}")
        if r["status"] == "partial":
            notes.append(f"{r['label']} {_fmt_qty(r['shortage'], r['unit'])} 부족")
        elif r["status"] == "not_in_stock":
            notes.append(f"{r['item']} 재고 없음")
        elif r["status"] == "unit_mismatch":
            notes.append(f"{r['item']} 재고 단위 불일치로 차감 건너뜀")
    return "·".join(parts), notes


def recipe_text(recipe: Recipe) -> str:
    """레시피를 사용자에게 보여줄 문장으로 정리한다. 레시피 문서에 있는 내용만 쓴다."""
    ingredients = "\n".join(f"- {i.item} {_fmt_qty(i.qty, i.unit)}" for i in recipe.ingredients)
    steps = "\n".join(f"{n}. {s}" for n, s in enumerate(recipe.steps, 1))
    allergens = ", ".join(recipe.allergens) or "없음"
    return (
        f"[{recipe.source}#{recipe.name}] {recipe.name} ({recipe.cuisine})\n{recipe.summary}\n"
        f"주재료: {recipe.main} / 1인분 {_fmt_qty(recipe.kcal)}kcal / 알레르기: {allergens}\n"
        f"재료 (1인분 기준)\n{ingredients}\n조리 순서\n{steps}"
    )


def _resolve_diners(conn: sqlite3.Connection, family_id: str, names: list[str]) -> tuple[list[str], list[str]]:
    """'엄마', '지민이', 'mom' 같은 이름을 같은 가족의 user_id로 바꾼다. (찾은 id, 못 찾은 이름)을 돌려준다."""
    users = conn.execute("SELECT user_id, name FROM users WHERE family_id = ?", (family_id,)).fetchall()
    found: list[str] = []
    unknown: list[str] = []
    for raw in names:
        name = raw.strip()
        candidates = {name, name[:-1] if len(name) > 2 and name.endswith("이") else name}
        match = next((u["user_id"] for u in users if u["user_id"] in candidates or u["name"] in candidates), None)
        if match:
            found.append(match)
        else:
            unknown.append(name)
    return found, unknown


@tool
def add_items_from_text(runtime: ToolRuntime[FridgeContext], items: list[InventoryInput]) -> str:
    """사용자가 말한 식재료를 냉장고 재고로 입고한다.

    문장에 나온 품목마다 하나씩 넣는다. 날짜·S/N·유효기간·단위 환산은 도구가 계산하므로
    직접 계산하지 말고 사용자가 말한 값을 그대로 넘긴다. 결과의 status가
    `sn_invalid`면 중복·형식 오류를 알리고 suggested_sn을 제안하고,
    `unit_mismatch`면 기준 단위로 다시 물어본다. expiry_source가 `unknown`이면
    유효기간 미상으로 저장됐다고 알린다.
    """
    ctx = _context(runtime)
    if ctx.user_id == GUEST_USER:
        return GUEST_DENIED
    conn = _open(runtime)
    try:
        results = []
        with conn:
            for it in items:
                try:
                    results.append(add_inventory(conn, it.item, it.qty, it.unit, it.input_date, it.expiry_date, it.sn))
                except ValueError as e:
                    results.append({"status": "invalid", "item": it.item, "message": str(e)})
        return _to_json(results)
    finally:
        conn.close()


@tool
def list_inventory(runtime: ToolRuntime[FridgeContext]) -> str:
    """냉장고에 보관 중인 재고 목록을 유효기간이 빠른 순으로 돌려준다.

    각 항목에 S/N, 수량, 유효기간(미상이면 null), D-day, 경고 수준(imminent·expired)이 있다.
    """
    conn = _open(runtime)
    try:
        rows = conn.execute(
            "SELECT sn, item, qty, unit, expiry_date, expiry_source, input_date FROM inventory"
            " WHERE status = 'stored' AND qty > 0 ORDER BY expiry_date IS NULL, expiry_date, sn"
        ).fetchall()
        items = []
        for r in rows:
            dd = d_day(r["expiry_date"])
            items.append({**dict(r), "d_day": dd, "level": alert_level(dd)})
        return _to_json(items)
    finally:
        conn.close()


@tool
def withdraw_item(runtime: ToolRuntime[FridgeContext], item: str, qty: float, unit: str) -> str:
    """재고를 꺼내 쓴다(출고). 유효기간이 빠른 것부터 차감한다.

    그 품목 전체 보유량의 50%를 넘게 꺼내면 사용자 승인을 먼저 받는다.
    """
    ctx = _context(runtime)
    if ctx.user_id == GUEST_USER:
        return GUEST_DENIED
    conn = _open(runtime)
    try:
        try:
            need, base = convert_to_base(item, qty, unit, conn)
        except UnitMismatchError as e:
            return f"{e}. 기준 단위({e.base_unit})로 다시 알려 주세요"
        except ValueError as e:
            return str(e)
        total = _round(sum(r["qty"] for r in stock_rows(conn, item)))
        if total <= 0:
            return f"{item} 재고가 없어요"

        key = idempotency_key(runtime)
        if need > total * BULK_WITHDRAW_RATIO:
            done = _applied_result(conn, key)
            if done:
                return done
            summary = (
                f"{item} {_fmt_qty(need, base)} 출고 (보유량 {_fmt_qty(total, base)}의 "
                f"{round(min(need, total) / total * 100)}%). 진행할까요?"
            )
            decision = interrupt({"type": "bulk_withdraw", "tool": "withdraw_item", "summary": summary})
            if not is_approved(decision):
                return REJECT_WITHDRAW

        with conn:
            done = _applied_result(conn, key)
            if done:
                return done
            result = deduct_fefo(conn, item, need, base, reason=REASON_OUTFLOW)
            message = f"{item} {_fmt_qty(result['deducted'], base)} 출고했어요"
            if result["shortage"] > 0:
                message += f" (재고가 {_fmt_qty(result['shortage'], base)} 모자랐어요)"
            _record_applied(conn, key, "withdraw_item", ctx.user_id, message)
        return message
    finally:
        conn.close()


@tool
def discard_item(runtime: ToolRuntime[FridgeContext], item: str, sn: str | None = None) -> str:
    """재고를 폐기한다. 반드시 사용자 승인을 먼저 받는다.

    S/N을 주면 그 재고만, 주지 않으면 그 품목의 보관 중 재고를 모두 폐기한다.
    """
    ctx = _context(runtime)
    if ctx.user_id == GUEST_USER:
        return GUEST_DENIED
    conn = _open(runtime)
    try:
        key = idempotency_key(runtime)
        done = _applied_result(conn, key)
        if done:
            return done
        lots = [r for r in stock_rows(conn, item) if sn is None or r["sn"] == sn.strip()]
        if not lots:
            return f"폐기할 {item} 재고가 없어요"
        desc = ", ".join(
            f"{r['item']} {_fmt_qty(r['qty'], r['unit'])} (S/N {r['sn']}, D-day {d_day(r['expiry_date'])})"
            for r in lots
        )
        decision = interrupt({"type": "discard", "tool": "discard_item", "summary": f"{desc} 폐기. 진행할까요?"})
        if not is_approved(decision):
            return REJECT_DISCARD

        with conn:
            done = _applied_result(conn, key)
            if done:
                return done
            created_at = now_iso()
            for r in lots:
                conn.execute("UPDATE inventory SET qty = 0, status = 'discarded' WHERE sn = ?", (r["sn"],))
                conn.execute(
                    "INSERT INTO deductions (sn, qty_g, d_day_at_deduction, reason, created_at) VALUES (?, ?, ?, ?, ?)",
                    (r["sn"], to_grams(r["item"], r["qty"], r["unit"]), d_day(r["expiry_date"]), REASON_DISCARD, created_at),
                )
            message = f"폐기했어요: {desc}"
            _record_applied(conn, key, "discard_item", ctx.user_id, message)
        return message
    finally:
        conn.close()


@tool
def search_recipes(query: str) -> str:
    """레시피 문서에서 요리를 찾아 재료, 1인분 kcal, 조리 순서, 출처를 돌려준다.

    지원 카테고리는 한식·일식·이탈리아식뿐이다. 찾지 못하면 빈 목록과 안내를 돌려주므로
    레시피를 지어내지 않는다. (키워드 검색. 하이브리드 RAG는 TASK-10에서 바꾼다)
    """
    tokens = [t for t in re.split(r"[\s,]+", query) if len(t) >= 2]
    scored = []
    for recipe in parse_recipes().values():
        score = 0
        for t in tokens:
            key = item_key(t)
            if t in recipe.name or recipe.name in t:
                score += 3
            if key == item_key(recipe.main):
                score += 2
            if any(item_key(i.item) == key for i in recipe.ingredients):
                score += 1
            if t in recipe.cuisine:
                score += 1
        if score:
            scored.append((score, recipe.name, recipe))
    scored.sort(key=lambda x: (-x[0], x[1]))
    hits = [recipe_text(r) for _, _, r in scored[:3]]
    if not hits:
        return _to_json({"results": [], "note": "레시피 문서에서 찾지 못했어요. 지원 카테고리: 한식·일식·이탈리아식"})
    return _to_json({"results": hits})


@tool
def get_recipe(dish: str) -> str:
    """요리명으로 레시피 한 개를 정확히 찾는다. 레시피에 없는 요리(외식 등)면 그렇다고 알려준다."""
    recipe = parse_recipes().get(dish.strip())
    if recipe is None:
        return f"레시피에 없는 요리예요: {dish}"
    return recipe_text(recipe)


@tool
def rank_recipes(runtime: ToolRuntime[FridgeContext], category: str | None = None, diners: list[str] | None = None) -> str:
    """재고·평점·알레르기·비선호 규칙으로 오늘 만들 요리를 추천한다. 결과를 바꾸지 말고 그대로 설명한다.

    - category: 한식·일식·이탈리아식 중 하나. 지정하지 않으면 세 카테고리 슬롯으로 추천한다.
    - diners: 함께 먹는 가족 이름(예: ["엄마", "지민"]). 요청자는 자동 포함이므로 넣지 않는다.
      주면 가족 합의 저녁 규칙으로 메뉴 1개와 신메뉴 1개를 고른다.
    """
    ctx = _context(runtime)
    conn = _open(runtime)
    try:
        notes: list[str] = []
        if category is not None and category not in CUISINES:
            notes.append(f"{category}는 지원하지 않아요. 한식·일식·이탈리아식 중에서 골랐어요")
            category = None
        others: list[str] = []
        if diners:
            found, unknown = _resolve_diners(conn, ctx.family_id, diners)
            others = [d for d in found if d != ctx.user_id]
            if unknown:
                notes.append(f"가족 목록에 없는 이름은 빼고 골랐어요: {', '.join(unknown)}")
        if others:
            result = recommend_family(conn, ctx.user_id, others)
        else:
            result = recommend(conn, ctx.user_id, category=category)
        result.notes = notes + result.notes
        return result.model_dump_json()
    finally:
        conn.close()


@tool("calc_kcal")
def calc_kcal_tool(servings_eaten: float, dish: str | None = None, kcal_per_serving: float | None = None) -> str:
    """먹은 인분으로 섭취 kcal을 계산한다. 레시피에 있는 요리면 레시피의 1인분 kcal을 쓴다."""
    recipe = parse_recipes().get(dish.strip()) if dish else None
    per = recipe.kcal if recipe else kcal_per_serving
    try:
        kcal = calc_kcal(servings_eaten, per)
    except ValueError as e:
        return str(e)
    return _to_json({"dish": dish, "servings_eaten": servings_eaten, "kcal_per_serving": per, "kcal": kcal})


@tool
def get_storage_days(item: str) -> str:
    """보관 가이드에서 품목의 권장 보관일수와 기준 단위를 찾는다. 가이드에 없으면 그렇다고 알려준다."""
    info = guide_info(item)
    if info is None:
        return f"보관 가이드에 없는 품목이에요: {item}. 유효기간을 직접 알려 주세요"
    return _to_json({"item": item, "guide_item": resolve_item(item), "storage_days": info["storage_days"],
                     "base_unit": info["base_unit"], "source": STORAGE_GUIDE.name})


@tool
def confirm_meal(
    runtime: ToolRuntime[FridgeContext],
    dish: str,
    score: int,
    servings_made: float | None = None,
    servings_eaten: float | None = None,
    kcal_per_serving: float | None = None,
    comment: str | None = None,
) -> str:
    """식사 평가를 확정한다: 평가 저장, 레시피 재료 차감, 식사·칼로리 기록. 반드시 사용자 승인을 먼저 받는다.

    - score: 1~5점.
    - servings_made: 만든 인분(재료 차감 기준). servings_eaten: 요청자가 먹은 인분(칼로리 기준).
      레시피에 있는 요리인데 둘 중 하나라도 사용자가 말하지 않았으면 추측하지 말고 비워서 부른다.
      그러면 도구가 되물을 문구를 돌려준다.
    - 레시피에 없는 요리(외식 등)는 재고를 차감하지 않는다. 사용자가 kcal을 말했으면
      kcal_per_serving에 넣고, 모르면 비운다(워치 기록 생략).
    """
    ctx = _context(runtime)
    if ctx.user_id == GUEST_USER:
        return GUEST_DENIED
    if not 1 <= int(score) <= 5:
        return "평가는 1~5점으로 알려 주세요"
    recipe = parse_recipes().get(dish.strip())
    if recipe is not None and (servings_made is None or servings_eaten is None):
        return ASK_SERVINGS
    if recipe is None:
        servings_eaten = servings_eaten or 1  # 외식은 1인분으로 기록한다
    if (servings_made is not None and servings_made <= 0) or servings_eaten <= 0:
        return "인분은 0보다 커야 해요"
    kcal = calc_kcal(servings_eaten, recipe.kcal if recipe else kcal_per_serving)

    conn = _open(runtime)
    try:
        key = idempotency_key(runtime)
        done = _applied_result(conn, key)
        if done:
            return done

        # 1) 읽기 전용 미리보기: 차감해 보고 롤백한다.
        preview = plan_meal_deductions(conn, recipe, servings_made) if recipe else []
        conn.rollback()
        deducted, notes = describe_deductions(preview)
        parts = [f"{dish} 평가 {score}점 저장"]
        if recipe:
            parts.append(f"{_fmt_qty(servings_made)}인분 기준 {deducted} 차감" if deducted else "차감할 재고 없음")
        else:
            parts.append("레시피에 없는 요리라 재고는 차감하지 않음")
        if kcal is None:
            parts.append(f"{ctx.user_id} 식사 {_fmt_qty(servings_eaten)}인분 기록 (kcal을 몰라 워치 기록은 생략)")
        else:
            parts.append(f"{ctx.user_id} 식사 {_fmt_qty(servings_eaten)}인분 {_fmt_qty(kcal)}kcal를 워치에 기록")
        summary = ", ".join(parts) + (f" ({'; '.join(notes)})" if notes else "") + ". 진행할까요?"

        # 2) 쓰기 전에 승인. 재개되면 이 도구가 처음부터 다시 실행된다.
        decision = interrupt({"type": "meal_confirm", "tool": "confirm_meal", "summary": summary})
        if not is_approved(decision):
            return REJECT_MEAL

        # 3) 승인 후 한 트랜잭션으로 한 번만 저장한다.
        with conn:
            done = _applied_result(conn, key)
            if done:
                return done
            now = now_iso()
            conn.execute(
                "INSERT INTO ratings (user_id, dish, cuisine, score, comment, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (ctx.user_id, dish.strip(), recipe.cuisine if recipe else None, int(score), comment, now),
            )
            conn.execute(
                "INSERT INTO meals (user_id, dish, servings, kcal, eaten_at, watch_synced) VALUES (?, ?, ?, ?, ?, 0)",
                (ctx.user_id, dish.strip(), servings_eaten, kcal, now),
            )
            applied = plan_meal_deductions(conn, recipe, servings_made) if recipe else []
            deducted, notes = describe_deductions(applied)
            message = f"저장했어요. {dish} {score}점"
            if deducted:
                message += f", {deducted} 차감"
            message += f", {ctx.user_id} 식사 {_fmt_qty(servings_eaten)}인분"
            message += f" {_fmt_qty(kcal)}kcal 기록." if kcal is not None else " 기록 (kcal 미상)."
            if notes:
                message += f" ({'; '.join(notes)})"
            if kcal is not None:
                message += f" {WATCH_PENDING}."
            _record_applied(conn, key, "confirm_meal", ctx.user_id, message)
        return message
    finally:
        conn.close()


AGENT_TOOLS = [
    add_items_from_text,
    list_inventory,
    withdraw_item,
    discard_item,
    search_recipes,
    get_recipe,
    rank_recipes,
    calc_kcal_tool,
    get_storage_days,
    confirm_meal,
]
