"""헬스케어 MCP 서버 (Galaxy Watch mock, SERVICE.md 5.3·6장 #5).

워치 헬스케어 모듈을 흉내 내는 FastMCP 서버다. 실서비스에서는 이 MCP 인터페이스가 교체 지점이다
(Samsung Health 데이터는 휴대폰 SDK로 접근하므로 휴대폰 앱이 이 인터페이스를 중계해야 한다).

- `record_meal`: 식사 kcal 기록. `meal_id`가 멱등 키라 같은 키가 다시 오면 중복 기록하지 않는다.
- `get_daily_intake`, `get_calorie_budget`: 오늘 섭취량과 남은 칼로리 조회.
- 오늘 날짜는 냉장고 서버와 같은 `FRIDGE_TODAY` 환경변수로 정한다(`src.tools.today`).
- 기록은 별도 SQLite(`HEALTH_DB`, 기본 `data/health.db`)에 둔다. 워치 쪽 저장소 역할이다.

실행: `python -m src.health_mcp_server` (기본 `http://127.0.0.1:8765/mcp`, `HEALTH_MCP_PORT`로 포트 변경)
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastmcp import FastMCP

from src.tools import DATA_DIR, today

load_dotenv()

# 사용자별 하루 목표 kcal (mock 프로필). 실서비스에서는 워치 앱의 목표값을 읽는다.
DAILY_TARGET_KCAL = {"dad": 2800, "mom": 2300, "jimin": 2400}  # 2026-11-20 모두 +400kcal 상향
DEFAULT_TARGET_KCAL = 2400

HEALTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS watch_meals (
    meal_id     TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    dish        TEXT NOT NULL,
    kcal        REAL NOT NULL,
    eaten_at    TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_watch_meals_user_day ON watch_meals(user_id, eaten_at);
"""


def health_db_path() -> Path:
    """워치 기록 DB 경로. `HEALTH_DB` 환경변수가 없으면 `data/health.db`."""
    return Path(os.environ.get("HEALTH_DB", "").strip() or DATA_DIR / "health.db")


def _connect() -> sqlite3.Connection:
    """워치 기록 DB를 열고 테이블이 없으면 만든다."""
    path = health_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(HEALTH_SCHEMA)
    return conn


def _intake(conn: sqlite3.Connection, user_id: str, day: str) -> tuple[float, list[dict[str, Any]]]:
    """하루 섭취 kcal 합계와 식사 목록을 읽는다. `eaten_at`의 날짜 부분으로 고른다."""
    rows = conn.execute(
        "SELECT meal_id, dish, kcal, eaten_at FROM watch_meals WHERE user_id = ? AND substr(eaten_at, 1, 10) = ? "
        "ORDER BY eaten_at",
        (user_id, day),
    ).fetchall()
    meals = [dict(r) for r in rows]
    return round(sum(m["kcal"] for m in meals), 1), meals


mcp = FastMCP("health-watch-mock")


@mcp.tool
def record_meal(meal_id: str, user_id: str, dish: str, kcal: float, eaten_at: str) -> dict[str, Any]:
    """식사 kcal을 워치에 기록한다. `meal_id`가 같으면 다시 기록하지 않고 duplicate를 돌려준다."""
    if kcal is None or kcal < 0:
        raise ValueError("kcal은 0 이상이어야 해요")
    conn = _connect()
    try:
        with conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO watch_meals (meal_id, user_id, dish, kcal, eaten_at, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (meal_id, user_id, dish, float(kcal), eaten_at, datetime.now().isoformat(timespec="seconds")),
            )
        status = "recorded" if cur.rowcount == 1 else "duplicate"
        return {"meal_id": meal_id, "status": status}
    finally:
        conn.close()


@mcp.tool
def get_daily_intake(user_id: str) -> dict[str, Any]:
    """요청자의 오늘 섭취 kcal 합계와 워치에 기록된 식사 목록을 돌려준다."""
    day = today().isoformat()
    conn = _connect()
    try:
        total, meals = _intake(conn, user_id, day)
    finally:
        conn.close()
    return {"user_id": user_id, "date": day, "intake_kcal": total,
            "meals": [{"dish": m["dish"], "kcal": m["kcal"], "eaten_at": m["eaten_at"]} for m in meals]}


@mcp.tool
def get_calorie_budget(user_id: str) -> dict[str, Any]:
    """요청자의 오늘 목표 kcal, 섭취 kcal, 남은 kcal(목표 - 섭취, 0 미만이면 0)을 돌려준다."""
    day = today().isoformat()
    conn = _connect()
    try:
        total, _ = _intake(conn, user_id, day)
    finally:
        conn.close()
    target = DAILY_TARGET_KCAL.get(user_id, DEFAULT_TARGET_KCAL)
    return {"user_id": user_id, "date": day, "target_kcal": target, "intake_kcal": total,
            "remaining_kcal": round(max(target - total, 0), 1)}


def main() -> None:
    """streamable HTTP로 서버를 띄운다. 포트는 `HEALTH_MCP_PORT`(기본 8765)."""
    port = int(os.environ.get("HEALTH_MCP_PORT", "8765"))
    mcp.run(transport="http", host="127.0.0.1", port=port, path="/mcp")


if __name__ == "__main__":
    main()
