"""RAG 파이프라인 테스트 (TASK-10): 청킹, 별칭 표, RRF, 하이브리드 검색, LLM 확장·리랭킹, 검색 도구.

Bedrock을 부르지 않는다. 임베딩은 가짜, LLM은 정해진 JSON을 돌려주는 가짜 모델을 쓴다.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from langchain_core.embeddings import DeterministicFakeEmbedding

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import retriever, tools  # noqa: E402
from src.api import extract_contexts  # noqa: E402
from src.retriever import KIND_RECIPE, KIND_STORAGE, rrf  # noqa: E402


class FakeLLM:
    """프롬프트 종류(확장·리랭킹)에 따라 정해진 답을 돌려주는 가짜 채팅 모델."""

    def __init__(self, expand: str = "[]", rerank=None):
        """확장 답 문자열과, 후보 제목 목록을 받아 점수 사전을 돌려주는 리랭킹 함수를 받는다."""
        self.expand_answer = expand
        self.rerank = rerank or (lambda titles: {})
        self.prompts: list[str] = []

    def invoke(self, prompt: str):
        """프롬프트를 기록하고 답을 `.content`에 담아 돌려준다."""
        self.prompts.append(prompt)
        if "0~10점" in prompt:
            titles = [line.split("] ", 1)[1].rsplit(" (", 1)[0] for line in prompt.splitlines() if line.startswith("[")]
            scores = self.rerank(titles)
            return SimpleNamespace(content=json.dumps([{"id": n, "score": scores.get(t, 0)} for n, t in enumerate(titles)]))
        return SimpleNamespace(content=self.expand_answer)


def with_llm(index, llm):
    """같은 인덱스에 LLM만 바꿔 끼운 얕은 복사본을 만든다(인덱스를 다시 만들지 않는다)."""
    clone = object.__new__(type(index))
    clone.__dict__.update(index.__dict__)
    clone.llm = llm
    return clone


# ── 청킹과 별칭 ───────────────────────────────────────────────────


def test_chunks_have_source_and_title():
    docs = retriever.load_chunks()
    recipes = [d for d in docs if d.metadata["kind"] == KIND_RECIPE]
    storage = [d for d in docs if d.metadata["kind"] == KIND_STORAGE]
    assert {d.metadata["title"] for d in recipes} == set(tools.parse_recipes())
    assert all(d.metadata["source"].startswith("recipes_") for d in recipes)
    titles = {d.metadata["title"] for d in storage}
    assert set(tools.load_storage_guide()) <= titles
    tofu = next(d for d in storage if d.metadata["title"] == "두부")
    assert "권장 보관일수: 7일" in tofu.page_content and "물을 매일 갈아" in tofu.page_content


def test_alias_table_both_ways():
    table = retriever.alias_table()
    assert {"한우", "우둔", "차돌"} <= set(table["소고기"])
    assert table["한우"] == table["소고기"]


def test_rrf_sums_reciprocal_ranks():
    fused = dict(rrf([["a", "b"], ["b", "c"]], k=60))
    assert fused["b"] == 1 / 62 + 1 / 61
    assert list(dict(rrf([["a", "b"], ["b", "c"]])))[0] == "b"


# ── 하이브리드 검색 ───────────────────────────────────────────────


def test_search_finds_recipe_by_name(offline_index):
    result = offline_index.search("규동 레시피 알려줘", KIND_RECIPE)
    assert result.hits[0].title == "규동"
    assert result.hits[0].source == "recipes_japanese.md"
    assert not result.reranked


def test_search_finds_storage_item(offline_index):
    result = offline_index.search("두부는 냉장 며칠 보관해?", KIND_STORAGE)
    assert result.hits[0].title == "두부"
    assert result.hits[0].text.startswith("[storage_guide.md#두부]")


def test_alias_expansion_adds_query(offline_index):
    queries, _ = offline_index.expand("한우 보관", KIND_STORAGE)
    assert queries[0] == "한우 보관"
    assert "소고기" in queries[1]


def test_llm_expansion_and_bad_json_fallback(offline_index):
    good = with_llm(offline_index, FakeLLM(expand='["소고기 덮밥 만드는 법", "차돌 덮밥"]', rerank=lambda t: {x: 8 for x in t}))
    queries, notes = good.expand("규동 레시피", KIND_RECIPE)
    assert "차돌 덮밥" in queries and not notes
    bad = with_llm(offline_index, FakeLLM(expand="모르겠어요"))
    queries, notes = bad.expand("규동 레시피", KIND_RECIPE)
    assert queries == ["규동 레시피"] and notes


def test_rerank_reorders_and_drops_low_scores(offline_index):
    llm = FakeLLM(rerank=lambda titles: {"오야코동": 9, "규동": 6})
    result = with_llm(offline_index, llm).search("덮밥 레시피", KIND_RECIPE)
    assert result.reranked
    assert [h.title for h in result.hits] == [t for t in ("오야코동", "규동") if t in [h.title for h in result.hits]]
    assert all(h.score >= retriever.MIN_RELEVANCE for h in result.hits)


def test_unknown_item_returns_guide_scope_only(offline_index, monkeypatch):
    llm = FakeLLM(rerank=lambda titles: {"보관 가이드 범위와 단위 규칙": 8})
    monkeypatch.setattr(retriever, "_INDEX", with_llm(offline_index, llm))
    data = json.loads(tools.search_docs.invoke({"query": "망고는 냉장고에서 며칠 보관할 수 있어?"}))
    assert [r.split("]")[0] for r in data["results"]] == ["[storage_guide.md#보관 가이드 범위와 단위 규칙"]
    assert "찾지 못했어요" in data["note"]
    assert extract_contexts([("search_docs", json.dumps(data, ensure_ascii=False))]) == data["results"]


def test_unknown_item_storage_days_is_context():
    output = tools.get_storage_days.invoke({"item": "망고"})
    assert extract_contexts([("get_storage_days", output)]) == [f"[storage_guide.md#망고] {output}"]
    assert extract_contexts([("list_inventory", output)]) == []


# ── 검색 도구와 인덱스 수명 ───────────────────────────────────────


def test_search_recipes_tool_returns_sourced_recipe():
    data = json.loads(tools.search_recipes.invoke({"query": "규동 레시피 알려줘"}))
    assert data["results"][0].startswith("[recipes_japanese.md#규동]")
    assert "소고기 150g" in data["results"][0] and "1인분 680kcal" in data["results"][0]
    assert data["search"]["queries"][0] == "규동 레시피 알려줘"


def test_search_docs_tool_returns_tip_with_source():
    data = json.loads(tools.search_docs.invoke({"query": "두부는 냉장 며칠 보관해?"}))
    assert data["results"][0].startswith("[storage_guide.md#두부]")
    assert "note" not in data


def test_index_is_built_once(monkeypatch):
    built = []
    monkeypatch.setattr(retriever, "HybridIndex", lambda **kw: built.append(kw) or SimpleNamespace(built_ms=5, vectors_loaded=False))
    monkeypatch.setattr(retriever, "BUILD_STATS", {"builds": 0, "built_ms": 0})
    retriever.set_index(None)
    retriever.warm_up()
    retriever.get_index()
    assert retriever.warm_up()["builds"] == 1 and len(built) == 1
    assert built[0]["persist_dir"] == retriever.CHROMA_DIR


class CountingEmbedding(DeterministicFakeEmbedding):
    """문서 임베딩 횟수를 세는 가짜 임베딩."""

    embedded: int = 0

    def embed_documents(self, texts):
        """임베딩한 문서 수를 더하고 가짜 벡터를 돌려준다."""
        self.embedded += len(texts)
        return super().embed_documents(texts)


def test_persisted_vectors_are_reused(tmp_path, monkeypatch):
    first = retriever.HybridIndex(embeddings=CountingEmbedding(size=16), use_llm=False, persist_dir=tmp_path)
    assert not first.vectors_loaded and first.vectors._collection.count() == len(first.ids)
    again = CountingEmbedding(size=16)
    second = retriever.HybridIndex(embeddings=again, use_llm=False, persist_dir=tmp_path)
    assert second.vectors_loaded and again.embedded == 0
    assert second.search("규동 레시피 알려줘", KIND_RECIPE).hits[0].title == "규동"

    original = retriever.load_chunks
    monkeypatch.setattr(retriever, "load_chunks", lambda d: original(d)[:-1])  # 문서가 바뀌면 지문이 달라진다
    changed = CountingEmbedding(size=16)
    third = retriever.HybridIndex(embeddings=changed, use_llm=False, persist_dir=tmp_path)
    assert not third.vectors_loaded and changed.embedded == len(third.ids)
    assert third.vectors._collection.count() == len(third.ids)
