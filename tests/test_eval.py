"""평가 하네스의 결정적 로직 테스트 (TASK-09, SERVICE.md 10장).

모델을 부르지 않는 부분만 본다: 문항 CSV 읽기, 기대 도구 표기 해석, 도구 일치 판정.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# run_eval은 불러올 때 FRIDGE_TODAY를 고정한다. 다른 테스트에 남지 않게 원래 값으로 되돌린다.
_saved_today = os.environ.get("FRIDGE_TODAY")
from evaluation import run_eval  # noqa: E402

if _saved_today is None:
    os.environ.pop("FRIDGE_TODAY", None)
else:
    os.environ["FRIDGE_TODAY"] = _saved_today


@pytest.fixture(scope="module")
def queries():
    """`evaluation/test_queries.csv` 문항을 id로 찾을 수 있게 읽는다."""
    return {q["id"]: q for q in run_eval.load_queries()}


def run_with(steps_per_turn, tool_calls=()):
    """턴별 trace.steps 도구 이름과 체크포인트 tool_calls로 가짜 실행 기록을 만든다."""
    turns = [{"trace": {"steps": [{"tool": name, "ms": 1} for name in names]}} for names in steps_per_turn]
    return {"turns": turns, "tool_calls": list(tool_calls)}


def test_csv_has_twelve_queries_in_four_categories(queries):
    """SERVICE.md 10.1대로 positive 5, negative 2, edge 3, guardrail 2건이다."""
    assert len(queries) == 12
    counts = {}
    for q in queries.values():
        counts[q["category"]] = counts.get(q["category"], 0) + 1
    assert counts == {"positive": 5, "negative": 2, "edge": 3, "guardrail": 2}
    assert all(q["user_id"] == "dad" for q in queries.values())


def test_note_multi_turn_parsing(queries):
    """P04는 승인 재개, E01은 같은 스레드로 두 번째 질문, 나머지는 한 턴이다."""
    assert queries["P04"]["resume"] == "approve"
    assert queries["P04"]["followup"] is None
    assert queries["E01"]["followup"] == "제안한 S/N으로 저장해줘"
    assert queries["E01"]["resume"] is None
    assert queries["P01"]["resume"] is None and queries["P01"]["followup"] is None


def test_semicolon_lists_and_tool_args(queries):
    """세미콜론 칸을 목록으로 나누고, `이름(key=value)` 기대 도구는 인자 조건으로 읽는다."""
    assert len(queries["P02"]["traits"]) == 6
    assert [t["name"] for t in queries["P02"]["tools"]] == ["list_inventory", "search_recipes", "rank_recipes"]
    assert queries["N01"]["tools"] == [{"name": "rank_recipes", "args": {"category": "멕시코식"}}]
    assert queries["G01"]["tools"] == []


def test_parse_tool_spec_rejects_bad_args():
    """인자가 key=value 형식이 아니면 바로 오류를 낸다."""
    with pytest.raises(ValueError):
        run_eval.parse_tool_spec("rank_recipes(멕시코식)")


def test_unregistered_expected_tool_fails_regardless_of_run(queries):
    """등록되지 않은 기대 도구가 있으면 실행 기록과 상관없이 불일치다.

    `search_docs`는 TASK-10에서 등록됐으므로, 등록 목록에서 빼서 미등록 상황을 만든다.
    """
    registered = run_eval.registered_tool_names() - {"search_docs"}
    result = run_eval.tool_match(queries["P05"], run_with([["search_docs"]]), registered)
    assert result["match"] is False
    assert "search_docs" in result["reason"]


def test_tool_match_ignores_order_and_handoffs(queries):
    """순서는 보지 않고 여러 턴의 도구를 합치며, transfer_to_* 핸드오프는 뺀다."""
    registered = run_eval.registered_tool_names()
    run = run_with([["transfer_to_inventory", "confirm_meal", "calc_kcal"], ["get_recipe"]])
    result = run_eval.tool_match(queries["P04"], run, registered)
    assert result["match"] is True
    assert "transfer_to_inventory" not in result["called"]


def test_tool_match_reports_missing_tool(queries):
    """기대 도구 하나라도 빠지면 불일치이고 이유에 이름을 적는다."""
    registered = run_eval.registered_tool_names()
    result = run_eval.tool_match(queries["P04"], run_with([["get_recipe"]]), registered)
    assert result["match"] is False
    assert "confirm_meal" in result["reason"]


def test_tool_match_checks_arguments(queries):
    """N01은 rank_recipes를 category=멕시코식으로 불러야 일치다."""
    registered = run_eval.registered_tool_names()
    steps = [["rank_recipes"]]
    good = run_with(steps, [{"name": "rank_recipes", "args": {"category": "멕시코식"}}])
    bad = run_with(steps, [{"name": "rank_recipes", "args": {}}])
    assert run_eval.tool_match(queries["N01"], good, registered)["match"] is True
    assert run_eval.tool_match(queries["N01"], bad, registered)["match"] is False


@pytest.mark.parametrize("path", sorted(run_eval.RESULTS_DIR.glob("run-*.json")), ids=lambda p: p.name)
def test_saved_result_files_have_report_shape(path):
    """저장된 실행별 결과 파일이 리포트에 쓸 요약과 문항별 기록을 갖췄다."""
    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["fridge_today"] == "2026-11-20"
    assert {"passed", "tool_match", "forbidden_violations", "faithfulness", "answer_relevancy"} <= set(data["summary"])
    ids = [r["id"] for r in data["results"]]
    assert len(ids) == data["summary"]["total"] == len(set(ids))
    for r in data["results"]:
        assert {"match", "called", "reason"} <= set(r["tools"])
        assert "passed" in r["judge"]


def test_empty_expected_tools_means_no_calls(queries):
    """기대 도구가 빈 칸이면 도구를 하나도 부르지 않아야 일치다."""
    registered = run_eval.registered_tool_names()
    assert run_eval.tool_match(queries["G01"], run_with([[]]), registered)["match"] is True
    assert run_eval.tool_match(queries["G01"], run_with([["list_inventory"]]), registered)["match"] is False
