"""입고 파싱 체인 테스트 (TASK-11, SERVICE.md 5.1).

가짜 구조화 출력 모델로 `prompt | llm.with_structured_output(...)` 체인과,
파싱 결과를 TASK-04 입고 함수로 처리하는 `add_parsed_items`를 Bedrock 없이 확인한다.
"""

import sys
from pathlib import Path

import pytest
from langchain_core.runnables import RunnableLambda

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import chains, tools  # noqa: E402


class FakeStructuredModel:
    """`with_structured_output`에 정해진 출력을 돌려주는 가짜 모델. 받은 프롬프트를 기록한다."""

    def __init__(self, output):
        """돌려줄 출력(dict 또는 None)을 받는다."""
        self.output = output
        self.prompts = []
        self.schema = None

    def with_structured_output(self, schema, **kwargs):
        """스키마를 기록하고, 프롬프트를 받아 출력을 스키마로 검증해 돌려주는 러너블을 만든다."""
        self.schema = schema

        def run(prompt_value):
            """프롬프트를 기록하고 정해진 출력을 돌려준다."""
            self.prompts.append(prompt_value.to_messages())
            return None if self.output is None else schema.model_validate(self.output)

        return RunnableLambda(run)


def items(*rows: dict) -> dict:
    """구조화 출력 dict를 만든다."""
    return {"items": list(rows)}


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """`FRIDGE_TODAY=2026-11-20` 시드 상태의 임시 도메인 DB 연결을 연다."""
    monkeypatch.setenv("FRIDGE_TODAY", "2026-11-20")
    db = tmp_path / "fridge.db"
    tools.reset_db(db)
    c = tools.connect(db)
    yield c
    c.close()


# ── 체인 ────────────────────────────────────────────────────────


def test_chain_parses_items_and_passes_text():
    fake = FakeStructuredModel(items(
        {"item": "한우", "qty": 600, "unit": "g"},
        {"item": "두부", "qty": 1, "unit": "모"},
        {"item": "우유", "qty": 1, "unit": "L"},
    ))
    parsed = chains.parse_inventory_text("한우 600g, 두부 1모, 우유 1L 샀어", llm=fake)
    assert [(p.item, p.qty, p.unit) for p in parsed] == [("한우", 600, "g"), ("두부", 1, "모"), ("우유", 1, "L")]
    assert all(isinstance(p, tools.InventoryInput) for p in parsed)
    assert fake.schema is chains.InventoryList
    system, human = fake.prompts[0]
    assert human.content == "한우 600g, 두부 1모, 우유 1L 샀어"
    # 파서는 오늘 날짜를 모르므로 날짜를 계산할 수 없다. 상대 토큰만 넘긴다.
    assert "2026" not in system.content and '어제 "-1"' in system.content


def test_chain_keeps_relative_token_and_sn():
    fake = FakeStructuredModel(items({"item": "두부", "qty": 1, "unit": "모", "input_date": "-1", "sn": "2026-11-001"}))
    [parsed] = chains.parse_inventory_text("어제 산 두부 1모 넣었어, S/N은 2026-11-001", llm=fake)
    assert parsed.input_date == "-1" and parsed.sn == "2026-11-001" and parsed.expiry_date is None


def test_chain_empty_output():
    assert chains.parse_inventory_text("안녕", llm=FakeStructuredModel(None)) == []
    assert chains.parse_inventory_text("안녕", llm=FakeStructuredModel(items())) == []


# ── 파싱 결과 입고 ──────────────────────────────────────────────


def _inputs(*rows: dict) -> list:
    """dict 목록을 `InventoryInput` 목록으로 바꾼다."""
    return [tools.InventoryInput(**r) for r in rows]


def test_p01_hanwoo_auto_sn_and_guide_expiry(conn):
    [r] = tools.add_parsed_items(conn, _inputs({"item": "한우", "qty": 600, "unit": "g"}))
    assert r["status"] == "added" and tools.SN_PATTERN.match(r["sn"]) and r["sn"].startswith("2026-11-")
    assert (r["qty"], r["unit"], r["input_date"]) == (600, "g", "2026-11-20")
    assert (r["expiry_date"], r["expiry_source"]) == ("2026-11-23", "guide")


def test_e01_duplicate_sn_saved_with_auto_sn(conn):
    [r] = tools.add_parsed_items(
        conn, _inputs({"item": "두부", "qty": 1, "unit": "모", "input_date": "-1", "sn": "2026-11-001"})
    )
    assert r["status"] == "added" and r["sn_replaced"] is True
    assert r["requested_sn"] == "2026-11-001" and "이미 있는 S/N" in r["sn_reason"]
    assert r["sn"] != "2026-11-001" and tools.SN_PATTERN.match(r["sn"])
    assert (r["qty"], r["unit"], r["input_date"]) == (1, "개", "2026-11-19")
    assert r["expiry_date"] == "2026-11-26"  # 어제 + 두부 권장 7일
    # 기존 2026-11-001(양파)은 그대로다.
    row = conn.execute("SELECT item FROM inventory WHERE sn = '2026-11-001'").fetchone()
    assert row["item"] == "양파"


def test_three_items_and_unit_mismatch(conn):
    results = tools.add_parsed_items(conn, _inputs(
        {"item": "한우", "qty": 600, "unit": "g"},
        {"item": "두부", "qty": 1, "unit": "모"},
        {"item": "우유", "qty": 500, "unit": "g"},
    ))
    assert [r["status"] for r in results] == ["added", "added", "unit_mismatch"]
    assert results[0]["sn"] != results[1]["sn"]


def test_bad_unit_is_reported_not_raised(conn):
    [r] = tools.add_parsed_items(conn, _inputs({"item": "두부", "qty": 1, "unit": "그릇"}))
    assert r["status"] == "invalid" and "단위" in r["message"]
