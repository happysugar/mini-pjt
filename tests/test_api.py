"""`POST /query` API 계약 테스트 (TASK-08, SERVICE.md 8장).

정해진 도구 호출만 하는 가짜 채팅 모델로 에이전트를 만들어 Bedrock·AWS 자격 증명 없이 돈다.
테스트마다 임시 도메인 DB와 임시 체크포인트 DB를 쓰므로 `data/` 파일을 건드리지 않는다.
M4 Supervisor 분할 뒤에도 같은 테스트로 승인·권한 규칙의 회귀를 잡는다.
"""

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import agent as agent_module  # noqa: E402
from src import tools  # noqa: E402
from src.api import create_app  # noqa: E402

HANWOO = "2026-11-003"  # 시드 한우 600g, D-day 2
ONION = "2026-11-001"  # 시드 양파 3개
DAD, MOM = "tok-dad", "tok-mom"


class FakeModel(GenericFakeChatModel):
    """도구 목록을 무시하고 정해진 메시지를 차례로 돌려주는 가짜 채팅 모델."""

    def bind_tools(self, tools, **kwargs):
        """도구 바인딩 요청을 받아도 자기 자신을 돌려준다."""
        return self


def call(name: str, args: dict, call_id: str = "call-1") -> AIMessage:
    """도구 하나를 부르는 AI 메시지를 만든다."""
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def say(text: str) -> AIMessage:
    """도구 호출 없이 답하는 AI 메시지를 만든다."""
    return AIMessage(content=text)


MEAL = {"dish": "규동", "score": 4, "servings_made": 2, "servings_eaten": 1}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """오늘 날짜, 사용자 토큰, 임시 도메인·체크포인트 DB를 준비한다."""
    monkeypatch.setenv("FRIDGE_TODAY", "2026-11-20")
    monkeypatch.setenv("USER_TOKENS", f"dad:{DAD},mom:{MOM}")
    monkeypatch.setenv("DEFAULT_USER", "dad")
    monkeypatch.setattr(agent_module, "CHECKPOINT_DB", tmp_path / "checkpoints.db")
    db = str(tmp_path / "fridge.db")
    tools.reset_db(db)
    return db


@pytest.fixture
def make_client(env):
    """가짜 모델 대본으로 앱을 띄우고 `(client, ask)`를 돌려주는 함수를 만든다."""
    clients = []

    def _make(*script: AIMessage):
        """대본을 받아 lifespan이 실행된 TestClient와 요청 함수를 돌려준다."""
        app = create_app(model=FakeModel(messages=iter(script)), db_path=env)
        client = TestClient(app)
        client.__enter__()
        clients.append(client)

        def ask(body: dict, token: str | None = None) -> dict:
            """`/query`를 보내고 응답 JSON을 돌려준다."""
            headers = {"X-User-Token": token} if token else {}
            response = client.post("/query", json=body, headers=headers)
            assert response.status_code == 200
            return response.json()

        return client, ask

    yield _make
    for client in clients:
        client.__exit__(None, None, None)


def codes(response: dict) -> list[str]:
    """응답 `trace.errors`의 코드 목록."""
    return [e["code"] for e in response["trace"]["errors"]]


def counts(db: str) -> dict:
    """평가·식사·차감 행 수와 한우·양파 재고량을 읽는다."""
    conn = tools.connect(db)
    try:
        out = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("ratings", "meals", "deductions")}
        for name, sn in (("hanwoo", HANWOO), ("onion", ONION)):
            out[name] = conn.execute("SELECT qty FROM inventory WHERE sn = ?", (sn,)).fetchone()[0]
        return out
    finally:
        conn.close()


def tool_outputs(client: TestClient, thread_id: str) -> list[str]:
    """체크포인트에 저장된 스레드의 도구 결과 문자열 목록."""
    agent = client.app.state.agent
    state = client.portal.call(agent.aget_state, {"configurable": {"thread_id": thread_id}})
    return [str(m.content) for m in state.values["messages"] if isinstance(m, ToolMessage)]


# ── 응답 형식과 신원 ─────────────────────────────────────────────


def test_question_only_uses_default_user(make_client):
    _, ask = make_client(say("안녕하세요."))
    r = ask({"question": "안녕"})
    assert set(r) == {"answer", "contexts", "trace"}
    assert r["trace"]["user_id"] == "dad" and r["trace"]["thread_id"]
    assert r["answer"] == "안녕하세요." and not codes(r)


@pytest.mark.parametrize(
    "body, token",
    [
        ({"question": "x", "user_id": "mom"}, None),  # user_id만 있고 토큰 없음
        ({"question": "x", "user_id": "mom"}, DAD),  # 토큰과 user_id 불일치
        ({"question": "x"}, "wrong-token"),  # 등록되지 않은 토큰
    ],
)
def test_identity_mismatch(make_client, env, body, token):
    _, ask = make_client()
    before = counts(env)
    r = ask(body, token)
    assert codes(r) == ["IDENTITY_MISMATCH"]
    assert r["answer"] == "사용자 확인에 실패했어요" and r["trace"]["user_id"] is None
    assert counts(env) == before


def test_token_alone_identifies_user(make_client):
    _, ask = make_client(say("네."))
    r = ask({"question": "안녕"}, MOM)
    assert r["trace"]["user_id"] == "mom" and not codes(r)


# ── 식사 승인 (HITL) ─────────────────────────────────────────────


def test_meal_approve_saves_once(make_client, env):
    _, ask = make_client(call("confirm_meal", MEAL), say("저장했어요."))
    before = counts(env)

    r1 = ask({"question": "규동 2인분 만들어서 1인분 먹었어, 4점"}, DAD)
    tid = r1["trace"]["thread_id"]
    assert r1["trace"]["pending_interrupt"]["type"]
    assert r1["answer"].endswith("(approve/reject)")
    assert any(s.get("event") == "interrupt" for s in r1["trace"]["steps"])
    assert counts(env) == before  # 승인 전에는 아무것도 저장하지 않는다

    r2 = ask({"question": "승인", "thread_id": tid, "resume": "approve"}, DAD)
    after = counts(env)
    assert r2["trace"]["pending_interrupt"] is None and not codes(r2)
    assert after["ratings"] == before["ratings"] + 1
    assert after["meals"] == before["meals"] + 1
    assert after["deductions"] == before["deductions"] + 2  # 한우, 양파
    assert (after["hanwoo"], after["onion"]) == (300, 2)
    assert r2["trace"]["rescued_g_week"] == 300

    # 같은 스레드로 다시 승인해도 두 번 저장하지 않는다
    r3 = ask({"question": "승인", "thread_id": tid, "resume": "approve"}, DAD)
    assert codes(r3) == ["NO_PENDING_INTERRUPT"]
    assert r3["answer"] == "대기 중인 승인 요청이 없어요"
    assert counts(env) == after


def test_meal_reject_saves_nothing(make_client, env):
    client, ask = make_client(call("confirm_meal", MEAL), say("취소했어요."))
    before = counts(env)
    r1 = ask({"question": "규동 먹었어, 4점"}, DAD)
    tid = r1["trace"]["thread_id"]
    r2 = ask({"question": "거절", "thread_id": tid, "resume": "reject"}, DAD)
    assert not codes(r2) and r2["trace"]["pending_interrupt"] is None
    assert counts(env) == before
    assert tools.REJECT_MEAL in tool_outputs(client, tid)


def test_new_question_while_pending(make_client, env):
    _, ask = make_client(call("confirm_meal", MEAL))
    tid = ask({"question": "규동 먹었어, 4점"}, DAD)["trace"]["thread_id"]
    r = ask({"question": "오늘 뭐 먹지?", "thread_id": tid}, DAD)
    assert codes(r) == ["INTERRUPT_PENDING"]
    assert r["trace"]["pending_interrupt"]["type"]
    assert r["answer"].endswith("(approve/reject)")


def test_resume_without_pending(make_client, env):
    _, ask = make_client(say("네."))
    tid = ask({"question": "안녕"}, DAD)["trace"]["thread_id"]
    r = ask({"question": "승인", "thread_id": tid, "resume": "approve"}, DAD)
    assert codes(r) == ["NO_PENDING_INTERRUPT"]


# ── 스레드 소유권과 다른 사용자 ─────────────────────────────────


def test_other_users_thread_is_blocked(make_client, env):
    _, ask = make_client(call("confirm_meal", MEAL))
    tid = ask({"question": "규동 먹었어, 4점"}, DAD)["trace"]["thread_id"]
    before = counts(env)
    r = ask({"question": "승인", "user_id": "mom", "thread_id": tid, "resume": "approve"}, MOM)
    assert codes(r) == ["THREAD_OWNER_MISMATCH"]
    assert r["answer"] == "이 대화에 접근할 수 없어요"
    assert counts(env) == before  # 엄마가 아빠의 승인을 대신 할 수 없다


def test_tools_run_as_server_identified_user(make_client, env):
    # 질문에 "엄마 이름으로"라고 해도 도구는 토큰으로 확인한 사용자(dad)로만 실행된다.
    _, ask = make_client(call("confirm_meal", MEAL), say("저장했어요."))
    tid = ask({"question": "엄마 이름으로 규동 4점 저장해줘"}, DAD)["trace"]["thread_id"]
    ask({"question": "승인", "thread_id": tid, "resume": "approve"}, DAD)
    conn = tools.connect(env)
    try:
        rating = conn.execute("SELECT user_id FROM ratings ORDER BY id DESC LIMIT 1").fetchone()[0]
        meal = conn.execute("SELECT user_id FROM meals ORDER BY id DESC LIMIT 1").fetchone()[0]
    finally:
        conn.close()
    assert (rating, meal) == ("dad", "dad")


def test_tool_schemas_have_no_user_id():
    # LLM이 도구 인자로 사용자를 바꿀 수 없도록 어떤 도구에도 user_id 인자가 없다.
    for t in tools.AGENT_TOOLS:
        assert not {"user_id", "family_id"} & set(t.tool_call_schema.model_json_schema().get("properties", {})), t.name


# ── guest ───────────────────────────────────────────────────────


def test_guest_write_is_read_only(make_client, env, monkeypatch):
    monkeypatch.delenv("DEFAULT_USER")
    _, ask = make_client(call("confirm_meal", MEAL), say("저장했어요."))
    before = counts(env)
    r = ask({"question": "규동 먹었어, 4점"})
    assert r["trace"]["user_id"] == "guest"
    assert codes(r) == ["GUEST_READ_ONLY"]
    assert r["answer"] == tools.GUEST_DENIED and r["trace"]["pending_interrupt"] is None
    assert counts(env) == before


# ── 입고 (TASK-11 파싱 체인) ────────────────────────────────────


class FakeParser:
    """입고 파싱 체인용 가짜 구조화 출력 모델. 받은 문장을 기록한다."""

    def __init__(self, rows: list[dict]):
        """돌려줄 품목 목록을 받는다."""
        self.rows, self.texts = rows, []

    def with_structured_output(self, schema, **kwargs):
        """프롬프트의 사용자 문장을 기록하고 정해진 품목을 돌려주는 러너블을 만든다."""
        from langchain_core.runnables import RunnableLambda

        def run(prompt_value):
            """사용자 문장을 기록하고 품목 목록을 스키마로 검증해 돌려준다."""
            self.texts.append(prompt_value.to_messages()[-1].content)
            return schema.model_validate({"items": self.rows})

        return RunnableLambda(run)


def test_add_items_from_text_runs_parse_chain(make_client, env, monkeypatch):
    from src import chains

    text = "어제 산 두부 1모 넣었어, S/N은 2026-11-001"
    parser = FakeParser([{"item": "두부", "qty": 1, "unit": "모", "input_date": "-1", "sn": "2026-11-001"}])
    monkeypatch.setattr(chains, "build_parse_model", lambda: parser)
    client, ask = make_client(call("add_items_from_text", {"text": text}), say("이미 있는 S/N이에요."))
    before = counts(env)
    r = ask({"question": text})
    assert parser.texts == [text]
    [out] = json.loads(tool_outputs(client, r["trace"]["thread_id"])[0])
    # 중복 S/N이면 저장하지 않고 제안한다(2턴째 저장은 test_chains에서 확인).
    assert out["status"] == "sn_invalid" and out["suggested_sn"] != "2026-11-001"
    assert (out["qty"], out["unit"], out["input_date"]) == (1, "개", "2026-11-19")
    assert counts(env) == before and r["trace"]["errors"] == []


def test_guest_cannot_add_items(make_client, env, monkeypatch):
    from src import chains

    monkeypatch.delenv("DEFAULT_USER")
    monkeypatch.setattr(chains, "build_parse_model", lambda: pytest.fail("guest는 파싱 전에 막혀야 해요"))
    _, ask = make_client(call("add_items_from_text", {"text": "두부 1모 샀어"}), say("넣었어요."))
    r = ask({"question": "두부 1모 샀어"})
    assert codes(r) == ["GUEST_READ_ONLY"]


# ── 대량 출고 (HITL) ────────────────────────────────────────────


def test_bulk_withdraw_resume_deducts_once(make_client, env):
    _, ask = make_client(call("withdraw_item", {"item": "한우", "qty": 400, "unit": "g"}), say("출고했어요."))
    before = counts(env)

    r1 = ask({"question": "한우 400g 꺼낼게"}, DAD)
    tid = r1["trace"]["thread_id"]
    assert r1["trace"]["pending_interrupt"]["type"] == "bulk_withdraw"
    assert counts(env) == before

    r2 = ask({"question": "승인", "thread_id": tid, "resume": "approve"}, DAD)
    after = counts(env)
    assert not codes(r2)
    assert after["hanwoo"] == 200 and after["deductions"] == before["deductions"] + 1

    r3 = ask({"question": "승인", "thread_id": tid, "resume": "approve"}, DAD)
    assert codes(r3) == ["NO_PENDING_INTERRUPT"] and counts(env) == after


def test_small_withdraw_needs_no_approval(make_client, env):
    _, ask = make_client(call("withdraw_item", {"item": "한우", "qty": 100, "unit": "g"}), say("출고했어요."))
    r = ask({"question": "한우 100g 꺼낼게"}, DAD)
    assert r["trace"]["pending_interrupt"] is None and not codes(r)
    assert counts(env)["hanwoo"] == 500


# ── 추천 contexts ──────────────────────────────────────────────


def test_recommendation_contexts(make_client):
    _, ask = make_client(call("rank_recipes", {}), say("추천해요."))
    r = ask({"question": "오늘 저녁 뭐 먹지?"}, DAD)
    assert [s["tool"] for s in r["trace"]["steps"]] == ["rank_recipes"]
    assert any(c.startswith("[recipes_korean.md#소고기 무국]") for c in r["contexts"])
    assert any(a["sn"] == HANWOO and a["level"] == "imminent" for a in r["trace"]["alerts"])


def test_recommendation_contexts_carry_answer_facts(make_client):
    """답에 쓰는 평점·신메뉴·kcal·임박·부족 재료와 도구 안내가 contexts에 근거로 들어간다."""
    _, ask = make_client(call("rank_recipes", {"category": "멕시코식"}), say("지원하지 않아요."))
    r = ask({"question": "멕시코식 타코 레시피 알려줘"}, DAD)
    assert any(c.startswith("[rank_recipes#안내]") and "지원하지 않아요" in c for c in r["contexts"])
    soup = next(c for c in r["contexts"] if c.startswith("[recipes_korean.md#소고기 무국]"))
    assert "평점 4.5(2회 평가)" in soup and "1인분 220kcal" in soup and "임박 재료 소고기 D-2" in soup
    pasta = next(c for c in r["contexts"] if c.startswith("[recipes_italian.md#소고기 라구 파스타]"))
    assert "신메뉴(평가 없음)" in pasta and "부족 재료 토마토 캔" in pasta


def test_inventory_contexts_link_item_to_guide_name(make_client):
    """재고 조회 결과가 contexts에 들어가고, 재고 품목명(한우)과 가이드 품목명(소고기)을 함께 적는다."""
    _, ask = make_client(call("list_inventory", {}), say("재고예요."))
    r = ask({"question": "냉장고에 뭐 있어?"}, DAD)
    hanwoo = next(c for c in r["contexts"] if c.startswith(f"[재고#{HANWOO}]"))
    assert "한우(보관 가이드 품목 소고기) 600g" in hanwoo and "D-2 / 임박" in hanwoo


# ── 검색 contexts와 인덱스 (TASK-10) ─────────────────────────────


def test_search_contexts_and_no_index_rebuild(make_client):
    _, ask = make_client(
        call("search_docs", {"query": "두부 냉장 보관"}),
        call("get_storage_days", {"item": "두부"}, "call-2"),
        say("7일이에요."),
        call("search_recipes", {"query": "규동 레시피"}, "call-3"),
        say("규동은 이렇게 만들어요."),
    )
    first = ask({"question": "두부는 냉장 며칠 보관해?", "thread_id": "dad-t-rag"}, DAD)
    assert [s["tool"] for s in first["trace"]["steps"]] == ["search_docs", "get_storage_days"]
    assert any(c.startswith("[storage_guide.md#두부]") for c in first["contexts"])
    second = ask({"question": "규동 레시피 알려줘", "thread_id": "dad-t-rag"}, DAD)
    assert second["contexts"][0].startswith("[recipes_japanese.md#규동]")
    assert second["trace"]["index"] == first["trace"]["index"]
