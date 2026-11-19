"""로컬 트레이서 테스트 (TASK-19, SERVICE.md 6장 #11).

가짜 모델로 대표 시나리오(추천 → 식사 평가 → 승인)를 API로 돌리고, `trace.jsonl`과 응답 `trace`에
개념 #2~#9 이벤트가 남는지 본다. 워치 MCP 서버는 같은 프로세스 서버를, 기록 파일은 임시 파일을 쓴다.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import health_mcp_server as health  # noqa: E402
from src import tools, tracer  # noqa: E402
from tests.test_api import DAD, MEAL, call, env, make_client, say  # noqa: E402,F401


def read_events(path: Path) -> list[dict]:
    """trace.jsonl의 이벤트 목록."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.usefixtures("allow_free_text_writes")
def test_representative_scenario_shows_concepts_2_to_9(make_client, _trace_file, tmp_path, monkeypatch):
    monkeypatch.setenv("HEALTH_DB", str(tmp_path / "health.db"))
    tools.set_health_target(health.mcp)
    _, ask = make_client(
        call("list_inventory", {}),
        call("rank_recipes", {}),
        call("search_recipes", {"query": "소고기 무국 규동"}),
        say("추천이에요."),
        call("confirm_meal", MEAL),
        say("저장했어요."),
    )
    r1 = ask({"question": "오늘 저녁 뭐 먹지?"}, DAD)
    r2 = ask({"question": "규동 2인분 만들어서 1인분 먹었어, 4점"}, DAD)
    r3 = ask({"question": "승인", "thread_id": r2["trace"]["thread_id"], "resume": "approve"}, DAD)

    events = read_events(_trace_file)
    seen = {c for e in events for c in e["concepts"]}
    assert {2, 3, 4, 5, 6, 7, 8, 9} <= seen
    by_event = {e["event"] for e in events}
    assert {"tool", "multi_tool", "mcp", "guardrail.input", "interrupt", "resume", "middleware", "handoff",
            "agent", "request", "response"} <= by_event

    # 줄마다 요청 정보가 붙고, 요청마다 request_id가 다르다
    assert all(e["request_id"] and e["user_id"] == "dad" and e["thread_id"] for e in events)
    assert len({e["request_id"] for e in events}) == 3
    assert {r["trace"]["request_id"] for r in (r1, r2, r3)} == {e["request_id"] for e in events}

    # 서브 에이전트 도구에는 에이전트 이름과 서브그래프 네임스페이스가 붙는다
    rank = next(e for e in events if e["event"] == "tool" and e["name"] == "rank_recipes")
    assert rank["agent"] == "recommend_agent" and rank["ns"].startswith("recommend_agent:")

    # 응답 trace에는 개념별 이벤트 수와 예전 형식의 steps가 함께 있다
    assert "#2 ReAct" in r1["trace"]["concepts"] and "#9 Supervisor" in r1["trace"]["concepts"]
    assert "#7 HITL" in r2["trace"]["concepts"] and "#5 MCP" in r3["trace"]["concepts"]
    tool_steps = [s["tool"] for s in r1["trace"]["steps"] if "tool" in s and s.get("via") != "mcp"]
    assert tool_steps == ["list_inventory", "rank_recipes", "search_recipes"]
    # 혼자 먹는 추천도 rank_recipes가 코드에서 요청자의 남은 칼로리를 워치에서 읽는다 (SERVICE.md 5.2)
    mcp_steps = [s for s in r1["trace"]["steps"] if s.get("via") == "mcp"]
    assert [(s["tool"], s["caller"]) for s in mcp_steps] == [("get_calorie_budget", "rank_recipes")]
    assert any(s.get("event") == "handoff" for s in r1["trace"]["steps"])
    assert any(s.get("event") == "interrupt" for s in r2["trace"]["steps"])
    assert [s["caller"] for s in r3["trace"]["steps"] if s.get("via") == "mcp"] == ["confirm_meal"]


def test_blocked_request_is_still_traced(make_client, _trace_file):
    _, ask = make_client(say("안 쓰여요."))
    r = ask({"question": "내 전화번호 010-1234-5678 메모에 저장해줘"}, DAD)
    events = read_events(_trace_file)
    assert [e["event"] for e in events][-3:] == ["guardrail.input", "guardrail.output", "response"]
    assert events[-1]["status"] == "error" and events[-1]["errors"] == ["GUARDRAIL_PII_STORE"]
    assert "010-1234-5678" not in _trace_file.read_text(encoding="utf-8")  # 질문은 가린 뒤 남긴다
    assert r["trace"]["concepts"]["#6 가드레일"] >= 1


def test_identity_failure_writes_one_request(make_client, _trace_file):
    _, ask = make_client()
    ask({"question": "안녕", "user_id": "dad"})  # 토큰 없이 user_id만
    events = read_events(_trace_file)
    assert [e["event"] for e in events] == ["request", "guardrail.output", "response"]
    assert events[-1]["errors"] == ["IDENTITY_MISMATCH"]


def test_langsmith_is_enabled_only_with_key(monkeypatch):
    for key in ("LANGSMITH_API_KEY", "LANGCHAIN_API_KEY", "LANGSMITH_TRACING", "LANGSMITH_PROJECT"):
        monkeypatch.delenv(key, raising=False)
    assert tracer.configure_langsmith() is False
    assert "LANGSMITH_TRACING" not in os.environ

    monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")
    assert tracer.configure_langsmith() is True
    assert os.environ["LANGSMITH_TRACING"] == "true" and os.environ["LANGSMITH_PROJECT"] == "mini-pjt"


def test_trace_file_env_and_concept_labels(tmp_path, monkeypatch):
    path = tmp_path / "t.jsonl"
    monkeypatch.setenv("TRACE_FILE", str(path))
    t = tracer.RequestTracer(thread_id="th", user_id="dad")
    t.emit("tool", tracer.tool_concepts("search_docs"), name="search_docs")
    t.emit("tool", tracer.tool_concepts("get_calorie_budget"), name="get_calorie_budget")
    t.finish("ok")
    assert t.concept_counts() == {"#2 ReAct": 2, "#3 RAG": 1, "#4 도구 다중": 1, "#5 MCP": 1, "#11 Trace": 1}
    assert len(read_events(path)) == 4
