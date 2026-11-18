"""재고 결정적 로직 테스트 (TASK-04, SERVICE.md 5.1·5.4).

모든 테스트는 `FRIDGE_TODAY=2026-11-20`(금요일)과 임시 DB의 시드 상태에서 시작한다.
"""

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import tools  # noqa: E402

TODAY = date(2026, 11, 20)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """시드를 넣은 임시 도메인 DB 연결을 연다."""
    monkeypatch.setenv("FRIDGE_TODAY", TODAY.isoformat())
    db = tmp_path / "fridge.db"
    tools.reset_db(db)
    c = tools.connect(db)
    yield c
    c.close()


def _row(conn, sn):
    """S/N으로 재고 한 행을 읽는다."""
    return conn.execute("SELECT * FROM inventory WHERE sn = ?", (sn,)).fetchone()


# ── today, 시드 ─────────────────────────────────────────────────


def test_today_uses_env(monkeypatch):
    monkeypatch.setenv("FRIDGE_TODAY", "2026-11-20")
    assert tools.today() == TODAY
    monkeypatch.delenv("FRIDGE_TODAY")
    assert tools.today() == date.today()


def test_seed_dates_follow_today(conn):
    hanwoo = _row(conn, "2026-11-003")
    assert hanwoo["expiry_date"] == "2026-11-22"
    assert hanwoo["input_date"] == "2026-11-08"


def test_reset_db_is_repeatable(tmp_path, monkeypatch):
    monkeypatch.setenv("FRIDGE_TODAY", "2026-11-20")
    db = tmp_path / "fridge.db"

    def snapshot():
        c = tools.connect(db)
        data = [c.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in tools.TABLES]
        c.close()
        return [[tuple(r) for r in rows] for rows in data]

    tools.reset_db(db)
    first = snapshot()
    c = tools.connect(db)
    with c:
        tools.deduct_fefo(c, "한우", 100, "g")
    c.close()
    tools.reset_db(db)
    assert snapshot() == first


# ── S/N ──────────────────────────────────────────────────────────


def test_next_sn_monthly_sequence(conn):
    assert tools.next_sn(conn, TODAY) == "2026-11-014"
    assert tools.next_sn(conn, date(2026, 10, 25)) == "2026-10-003"
    assert tools.next_sn(conn, date(2026, 12, 1)) == "2026-12-001"


def test_next_sn_skips_consumed_numbers(conn):
    conn.execute("UPDATE inventory SET status = 'consumed' WHERE sn = '2026-11-013'")
    assert tools.next_sn(conn, TODAY) == "2026-11-014"


@pytest.mark.parametrize("sn", ["2026-13-001", "26-11-001", "2026-11-1", "2026-11-000", "2026/11/001"])
def test_check_sn_rejects_bad_format(conn, sn):
    assert tools.check_sn(conn, sn)["valid"] is False


def test_check_sn_rejects_duplicate(conn):
    result = tools.check_sn(conn, "2026-11-001")
    assert result["valid"] is False
    assert "이미 있는" in result["reason"]
    assert tools.check_sn(conn, "2026-11-099")["valid"] is True


# ── 품목명 매칭 ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name, expected",
    [
        ("소고기", "소고기"),
        ("한우", "소고기"),
        ("차돌", "소고기"),
        ("달걀", "계란"),
        ("파르메산치즈", "파르메산 치즈"),
        ("대하", "새우"),
        ("망고", None),
    ],
)
def test_resolve_item_exact_then_alias(name, expected):
    assert tools.resolve_item(name) == expected


def test_storage_guide_has_no_mango():
    assert "망고" not in tools.load_storage_guide()


# ── 단위 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "qty, unit, expected",
    [
        (1.5, "kg", (1500, "g")),
        (500, "ml", (0.5, "L")),
        (1, "L", (1, "L")),
        (1, "모", (1, "개")),
        (2, "팩", (2, "개")),
        (3, "알", (3, "개")),
    ],
)
def test_normalize_unit(qty, unit, expected):
    assert tools.normalize_unit(qty, unit) == expected


def test_normalize_unit_rejects_unknown():
    with pytest.raises(ValueError):
        tools.normalize_unit(1, "컵")


def test_convert_to_base_uses_avg_weight():
    assert tools.convert_to_base("두부", 1, "모") == (1, "개")
    assert tools.convert_to_base("두부", 300, "g") == (1, "개")
    assert tools.convert_to_base("양파", 500, "g") == (2.5, "개")
    assert tools.convert_to_base("무", 1, "개") == (1500, "g")
    assert tools.convert_to_base("한우", 0.6, "kg") == (600, "g")


def test_convert_to_base_mismatch_without_avg_weight():
    with pytest.raises(tools.UnitMismatchError):
        tools.convert_to_base("우유", 500, "g")
    with pytest.raises(tools.UnitMismatchError):
        tools.convert_to_base("소고기", 1, "개")


# ── 상대 날짜, 유효기간 ─────────────────────────────────────────


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, date(2026, 11, 20)),
        ("오늘", date(2026, 11, 20)),
        ("어제", date(2026, 11, 19)),
        ("그저께", date(2026, 11, 18)),
        ("-1", date(2026, 11, 19)),
        ("+2", date(2026, 11, 22)),
        (-3, date(2026, 11, 17)),
        ("2026-11-01", date(2026, 11, 1)),
    ],
)
def test_resolve_date(monkeypatch, value, expected):
    monkeypatch.setenv("FRIDGE_TODAY", "2026-11-20")
    assert tools.resolve_date(value) == expected


def test_resolve_date_rejects_garbage(monkeypatch):
    monkeypatch.setenv("FRIDGE_TODAY", "2026-11-20")
    with pytest.raises(ValueError):
        tools.resolve_date("지난주쯤")


def test_default_expiry_from_guide_or_unknown():
    assert tools.default_expiry("한우", TODAY) == (date(2026, 11, 23), "guide")
    assert tools.default_expiry("두부", date(2026, 11, 19)) == (date(2026, 11, 26), "guide")
    assert tools.default_expiry("망고", TODAY) == (None, "unknown")


# ── 입고 ─────────────────────────────────────────────────────────


def test_add_inventory_p01_hanwoo(conn):
    result = tools.add_inventory(conn, "한우", 600, "g")
    assert result["status"] == "added"
    assert result["sn"] == "2026-11-014"
    assert result["input_date"] == "2026-11-20"
    assert result["expiry_date"] == "2026-11-23"
    assert result["expiry_source"] == "guide"
    assert (result["qty"], result["unit"]) == (600, "g")


def test_add_inventory_e01_duplicate_sn_suggests_new(conn):
    result = tools.add_inventory(conn, "두부", 1, "모", input_date="어제", sn="2026-11-001")
    assert result["status"] == "sn_invalid"
    assert result["suggested_sn"] == "2026-11-014"
    assert _row(conn, "2026-11-014") is None

    added = tools.add_inventory(conn, "두부", 1, "모", input_date="어제", sn=result["suggested_sn"])
    assert added["status"] == "added"
    assert (added["qty"], added["unit"]) == (1, "개")
    assert added["input_date"] == "2026-11-19"
    assert added["expiry_date"] == "2026-11-26"


def test_add_inventory_user_expiry(conn):
    result = tools.add_inventory(conn, "소고기", 300, "g", expiry_date="2026-12-01")
    assert result["expiry_source"] == "user"
    assert result["expiry_date"] == "2026-12-01"


def test_add_inventory_unit_mismatch_not_saved(conn):
    before = conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
    result = tools.add_inventory(conn, "우유", 500, "g")
    assert result["status"] == "unit_mismatch"
    assert result["base_unit"] == "L"
    assert conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0] == before


def test_unknown_item_first_unit_becomes_base(conn):
    first = tools.add_inventory(conn, "망고", 2, "개")
    assert first["status"] == "added"
    assert first["expiry_date"] is None and first["expiry_source"] == "unknown"
    assert tools.base_unit_of("망고", conn) == "개"

    other_unit = tools.add_inventory(conn, "망고", 300, "g")
    assert other_unit["status"] == "unit_mismatch"
    assert tools.deduct_fefo(conn, "망고", 100, "g")["status"] == "unit_mismatch"
    assert tools.deduct_fefo(conn, "망고", 1, "개")["status"] == "ok"


# ── D-day, 알림 ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "dday, level",
    [(4, None), (3, "imminent"), (1, "imminent"), (0, "imminent"), (-1, "expired"), (None, None)],
)
def test_alert_level_boundaries(dday, level):
    assert tools.alert_level(dday) == level


def test_d_day(monkeypatch):
    monkeypatch.setenv("FRIDGE_TODAY", "2026-11-20")
    assert tools.d_day("2026-11-22") == 2
    assert tools.d_day(date(2026, 11, 19)) == -1
    assert tools.d_day(None) is None


def test_list_alerts_seed(conn):
    alerts = tools.list_alerts(conn)
    assert alerts == [
        {"sn": "2026-11-008", "item": "시금치", "d_day": -1, "level": "expired"},
        {"sn": "2026-11-003", "item": "한우", "d_day": 2, "level": "imminent"},
    ]


# ── FEFO 차감 ───────────────────────────────────────────────────


def test_deduct_fefo_earliest_expiry_first(conn):
    tools.add_inventory(conn, "소고기", 200, "g")  # 2026-11-014, 유효기간 11-23
    result = tools.deduct_fefo(conn, "소고기", 700, "g", reason=tools.REASON_MEAL)

    assert result["status"] == "ok"
    assert [(d["sn"], d["qty"], d["d_day"]) for d in result["details"]] == [
        ("2026-11-003", 600, 2),
        ("2026-11-014", 100, 3),
    ]
    assert _row(conn, "2026-11-003")["status"] == "consumed"
    assert _row(conn, "2026-11-003")["qty"] == 0
    assert _row(conn, "2026-11-014")["qty"] == 100

    rows = conn.execute("SELECT sn, qty_g, d_day_at_deduction, reason FROM deductions ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("2026-11-003", 600, 2, "meal"), ("2026-11-014", 100, 3, "meal")]


def test_deduct_fefo_partial_reports_shortage(conn):
    result = tools.deduct_fefo(conn, "한우", 800, "g")
    assert result["status"] == "partial"
    assert result["deducted"] == 600
    assert result["shortage"] == 200
    assert _row(conn, "2026-11-003")["status"] == "consumed"


def test_deduct_fefo_not_in_stock(conn):
    result = tools.deduct_fefo(conn, "토마토 캔", 0.5, "개")
    assert result["status"] == "not_in_stock"
    assert result["shortage"] == 0.5
    assert conn.execute("SELECT COUNT(*) FROM deductions").fetchone()[0] == 0


def test_deduct_fefo_converts_unit_and_records_grams(conn):
    result = tools.deduct_fefo(conn, "양파", 400, "g")  # 2개
    assert result["status"] == "ok"
    assert (result["deducted"], result["unit"]) == (2, "개")
    assert _row(conn, "2026-11-001")["qty"] == 1
    assert conn.execute("SELECT qty_g FROM deductions").fetchone()[0] == 400


def test_deduct_fefo_unit_mismatch_changes_nothing(conn):
    result = tools.deduct_fefo(conn, "우유", 200, "g")
    assert result["status"] == "unit_mismatch"
    assert _row(conn, "2026-11-011")["qty"] == 1


def test_deduct_fefo_discard_marks_discarded(conn):
    tools.deduct_fefo(conn, "시금치", 200, "g", reason=tools.REASON_DISCARD)
    assert _row(conn, "2026-11-008")["status"] == "discarded"


# ── 폐기 회피량 ─────────────────────────────────────────────────


def test_week_start_is_monday():
    assert tools.week_start(TODAY) == date(2026, 11, 16)
    assert tools.week_start(date(2026, 11, 16)) == date(2026, 11, 16)
    assert tools.week_start(date(2026, 11, 22)) == date(2026, 11, 16)


def test_rescued_g_week(conn):
    # 집계 대상: 임박(D-day 0~3) 재고의 출고·식사 차감
    tools.deduct_fefo(conn, "한우", 300, "g", reason=tools.REASON_MEAL)  # g: 300
    tools.add_inventory(conn, "우유", 1, "L", expiry_date="+1")
    tools.deduct_fefo(conn, "우유", 0.5, "L")  # L: 0.5L = 500g
    tools.add_inventory(conn, "두부", 1, "개", expiry_date="+3")
    tools.deduct_fefo(conn, "두부", 1, "개")  # 개: 1개 = 300g

    # 집계 제외: 평균 무게 없는 품목, 만료 재고, 폐기, 임박 아님, 지난주
    tools.add_inventory(conn, "수제 잼", 2, "개", expiry_date="+1")
    tools.deduct_fefo(conn, "수제 잼", 1, "개")
    tools.deduct_fefo(conn, "시금치", 100, "g")
    tools.add_inventory(conn, "당근", 2, "개", expiry_date="0")
    tools.deduct_fefo(conn, "당근", 1, "개", reason=tools.REASON_DISCARD)
    tools.deduct_fefo(conn, "감자", 1, "개")
    conn.execute(
        "INSERT INTO deductions (sn, qty_g, d_day_at_deduction, reason, created_at)"
        " VALUES ('2026-11-003', 999, 2, 'meal', '2026-11-15T20:00:00')"
    )

    result = tools.rescued_g_week(conn)
    assert result["total_g"] == 1100
    assert result["week_start"] == "2026-11-16"
    assert [e["item"] for e in result["excluded"]] == ["수제 잼"]


def test_rescued_g_week_scenario_gyudon(conn):
    tools.deduct_fefo(conn, "소고기", 300, "g", reason=tools.REASON_MEAL)
    tools.deduct_fefo(conn, "양파", 1, "개", reason=tools.REASON_MEAL)
    assert tools.rescued_g_week(conn)["total_g"] == 300


# ── 칼로리 ──────────────────────────────────────────────────────


def test_calc_kcal():
    assert tools.calc_kcal(1, 680) == 680
    assert tools.calc_kcal(2, 680) == 1360
    assert tools.calc_kcal(0.5, 450) == 225
    assert tools.calc_kcal(1, None) is None
    with pytest.raises(ValueError):
        tools.calc_kcal(0, 680)
