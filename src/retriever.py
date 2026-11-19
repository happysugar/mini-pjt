"""레시피·보관 가이드 RAG 파이프라인 (SERVICE.md 6장 #3, 7장 검색 인덱스).

- 청킹: 레시피는 요리 하나, 보관 가이드는 품목 하나(기준표 행 + 보관 팁)와 문서 규칙 한 덩어리.
  메타데이터에 `source`(파일명), `title`(요리명·품목명), `kind`(recipe·storage)를 담는다.
- 하이브리드 검색: Chroma 벡터(Bedrock 임베딩) + kiwipiepy 형태소 BM25(`rank-bm25`)를
  RRF(Reciprocal Rank Fusion)로 합친다. RRF는 직접 구현한다.
- 쿼리 확장: 보관 가이드 별칭 표(소고기 → 한우, 우둔, 차돌)와 LLM이 만든 질의 2~3개.
- LLM 리랭킹: RRF 상위 후보를 Bedrock 채팅 모델이 질문 관련도로 다시 매겨 상위 k개를 남긴다.

인덱스는 `warm_up()`으로 서버 시작 때 한 번 만들고(임베딩은 `data/chroma/`에 저장해 다음 시작 때 다시 쓴다), 검색 도구는 `get_index()`로 같은 인덱스를 쓴다.
쿼리 확장과 리랭킹은 `HybridIndex.search()` 안에서만, 즉 검색 도구가 불릴 때만 실행한다.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import chromadb
from dotenv import load_dotenv
from kiwipiepy import Kiwi
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from rank_bm25 import BM25Okapi

load_dotenv()

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STORAGE_GUIDE = DATA_DIR / "storage_guide.md"
# 임베딩을 저장해 두는 Chroma 폴더. 문서나 임베딩 모델이 바뀌면 지문이 달라져 다시 만든다
CHROMA_DIR = DATA_DIR / "chroma"
COLLECTION = "fridge-docs"
RECIPE_GLOB = "recipes_*.md"

KIND_RECIPE = "recipe"
KIND_STORAGE = "storage"

RRF_K = 60  # RRF 상수. 순위 1과 2의 점수 차이를 완만하게 만든다
CANDIDATES = 8  # 리랭커에 넘기는 RRF 상위 후보 수
MIN_RELEVANCE = 5  # 리랭커 관련도(0~10)가 이 값보다 낮으면 버린다
# 보관 가이드 범위·규칙 청크. 가이드에 없는 품목 질문의 근거가 되므로 보관 검색 후보에 항상 넣는다
SCOPE_TITLE = "보관 가이드 범위와 단위 규칙"
BM25_KEEP_TAGS = ("NNG", "NNP", "NR", "VV", "VA", "XR", "SL", "SH", "SN")


# ── 청킹 ─────────────────────────────────────────────────────────


def _split_sections(text: str, level: str) -> list[str]:
    """마크다운을 `level`(예: "## ") 제목 단위로 나눈다. 첫 제목 앞부분은 버린다."""
    return [b for b in re.split(rf"(?m)^(?={re.escape(level)})", text) if b.startswith(level)]


def _recipe_chunks(path: Path) -> list[Document]:
    """레시피 문서 하나를 요리 단위 청크로 나눈다. '문서 형식' 절은 빼고, 카테고리는 메타 표에서 읽는다."""
    docs = []
    for block in _split_sections(path.read_text(encoding="utf-8"), "## "):
        title = block.splitlines()[0].lstrip("#").strip()
        if title.startswith("문서 형식"):
            continue
        cuisine = re.search(r"\|\s*카테고리\s*\|\s*([^|]+?)\s*\|", block)
        docs.append(
            Document(
                page_content=block.strip(),
                metadata={
                    "source": path.name,
                    "title": title,
                    "kind": KIND_RECIPE,
                    "cuisine": cuisine.group(1) if cuisine else "",
                },
            )
        )
    return docs


def _guide_rows(text: str) -> dict[str, str]:
    """보관 가이드 품목 기준표를 `{품목: "별칭 ... / 권장 보관일수 N일 ..."}` 문장으로 바꾼다."""
    table = text.split("## 품목 기준표", 1)[1].split("\n## ", 1)[0]
    rows: dict[str, str] = {}
    for line in table.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 5 or cells[0] == "품목" or set(cells[0]) <= set("-: "):
            continue
        name, aliases, unit, days, weight = cells
        rows[name] = (
            f"별칭: {aliases if aliases != '-' else '없음'} / 기준 단위: {unit} / 권장 보관일수: {days}일"
            + (f" / 개당 평균 무게: {weight}g" if weight != "-" else "")
        )
    return rows


def _storage_chunks(path: Path) -> list[Document]:
    """보관 가이드를 품목 단위(기준표 행 + 보관 팁) 청크와 문서 규칙 청크로 나눈다."""
    text = path.read_text(encoding="utf-8")
    rows = _guide_rows(text)
    meta = {"source": path.name, "kind": KIND_STORAGE}
    head = text.split("## 품목 기준표", 1)[0]
    docs = [Document(page_content=head.strip(), metadata={**meta, "title": SCOPE_TITLE})]
    tips = text.split("## 품목별 보관 팁", 1)[1]
    for block in _split_sections(tips, "### "):
        lines = block.splitlines()
        name = lines[0].lstrip("#").strip()
        body = "\n".join(lines[1:]).strip()
        row = rows.get(name, "")
        content = f"{name} 보관\n{row}\n{body}" if row else f"{name} 보관\n{body}"
        docs.append(Document(page_content=content, metadata={**meta, "title": name}))
    return docs


def load_chunks(data_dir: Path = DATA_DIR) -> list[Document]:
    """레시피 문서 전체와 보관 가이드를 청크 목록으로 읽는다."""
    docs: list[Document] = []
    for path in sorted(data_dir.glob(RECIPE_GLOB)):
        docs.extend(_recipe_chunks(path))
    docs.extend(_storage_chunks(data_dir / STORAGE_GUIDE.name))
    return docs


def alias_table(path: Path = STORAGE_GUIDE) -> dict[str, list[str]]:
    """보관 가이드 기준표에서 쿼리 확장용 별칭 표를 만든다.

    품목명과 별칭 어느 쪽으로 찾아도 같은 묶음(품목명 + 별칭들)을 돌려준다.
    예: "소고기" → ["소고기", "한우", "우둔", "차돌", ...], "한우" → 같은 목록.
    """
    rows = _guide_rows(path.read_text(encoding="utf-8"))
    table: dict[str, list[str]] = {}
    for name, desc in rows.items():
        aliases = desc.split(" / ", 1)[0].removeprefix("별칭: ")
        group = [name] + ([a.strip() for a in aliases.split(",")] if aliases != "없음" else [])
        for word in group:
            table.setdefault(word, group)
    return table


# ── 형태소 BM25와 RRF ─────────────────────────────────────────────


def tokenize(kiwi: Kiwi, text: str) -> list[str]:
    """kiwipiepy 형태소 분석으로 명사·용언 어간·외국어 토큰만 남긴다(BM25용)."""
    return [t.form.lower() for t in kiwi.tokenize(text) if t.tag in BM25_KEEP_TAGS]


def rrf(rankings: list[list[str]], k: int = RRF_K) -> list[tuple[str, float]]:
    """여러 순위 목록을 RRF로 합친다. 점수는 목록마다 1/(k + 순위)의 합이다.

    점수가 같으면 먼저 나온 목록의 순서를 따른다. `(문서 ID, 점수)`를 점수 높은 순으로 돌려준다.
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, 1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: -kv[1])


# ── LLM 쿼리 확장과 리랭킹 ───────────────────────────────────────

EXPAND_PROMPT = """냉장고 관리 앱의 문서 검색 질의를 늘린다.
문서 종류: {kind_desc}
사용자 질문: {question}

같은 뜻의 검색 질의를 2~3개 만든다. 재료 이름은 다른 표현(예: 소고기 → 한우, 차돌)을 써 본다.
질문에 없는 요리나 품목을 새로 지어내지 않는다.
JSON 배열만 출력한다. 예: ["질의1", "질의2"]"""

RERANK_PROMPT = """사용자 질문에 답하는 근거로 각 문서가 얼마나 관련 있는지 0~10점으로 매긴다.
- 10: 질문의 요리·품목을 직접 다룬다. 0: 관련 없다.
- 질문의 요리·품목이 아닌 다른 요리·품목 문서는 비슷해 보여도 3점 이하로 준다.
- 질문의 품목이 어느 품목 문서에도 없으면, 표에 없는 품목을 어떻게 다루는지 적힌 문서에 높은 점수를 준다.

사용자 질문: {question}

문서 목록
{candidates}

JSON 배열만 출력한다. 예: [{{"id": 0, "score": 9}}, {{"id": 3, "score": 2}}]"""

KIND_DESC = {
    KIND_RECIPE: "한식·일식·이탈리아식·중식 레시피(요리 하나가 문서 하나)",
    KIND_STORAGE: "식재료 보관 가이드(품목 하나가 문서 하나, 권장 보관일수와 보관 팁)",
}


def default_llm() -> Any:
    """`.env`의 `BEDROCK_MODEL_ID`로 쿼리 확장·리랭킹용 Bedrock 채팅 모델을 만든다."""
    from langchain_aws import ChatBedrockConverse

    return ChatBedrockConverse(
        model=os.environ["BEDROCK_MODEL_ID"],
        region_name=os.environ.get("AWS_DEFAULT_REGION"),
        temperature=0,
        max_tokens=512,
    )


def default_embeddings() -> Embeddings:
    """`.env`의 `BEDROCK_EMBED_MODEL_ID`로 Bedrock 임베딩 모델을 만든다."""
    from langchain_aws import BedrockEmbeddings

    return BedrockEmbeddings(
        model_id=os.environ["BEDROCK_EMBED_MODEL_ID"],
        region_name=os.environ.get("AWS_DEFAULT_REGION"),
    )


def _llm_json(llm: Any, prompt: str) -> Any:
    """LLM에 프롬프트를 보내고 답에서 첫 JSON 배열을 꺼낸다. 읽지 못하면 ValueError."""
    content = llm.invoke(prompt).content
    if isinstance(content, list):
        content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
    match = re.search(r"\[.*\]", str(content), re.S)
    if not match:
        raise ValueError("JSON 배열이 없어요")
    return json.loads(match.group(0))


# ── 하이브리드 인덱스 ─────────────────────────────────────────────


@dataclass
class SearchHit:
    """검색 결과 한 건. `text`는 출처 꼬리표가 붙은 근거 문자열이다."""

    source: str
    title: str
    kind: str
    content: str
    score: float

    @property
    def text(self) -> str:
        """`[출처#제목] 본문` 형식의 근거 문자열 (`contexts`에 그대로 들어간다)."""
        return f"[{self.source}#{self.title}] {self.content}"


@dataclass
class SearchResult:
    """검색 한 번의 결과와 실행 기록(확장 질의, 후보 수, 리랭킹 여부)."""

    hits: list[SearchHit]
    queries: list[str]
    candidates: int
    reranked: bool
    notes: list[str] = field(default_factory=list)


class HybridIndex:
    """BM25(kiwipiepy) + Chroma 벡터 인덱스. 만들 때 한 번 임베딩하고 이후에는 검색만 한다."""

    def __init__(self, embeddings: Embeddings | None = None, llm: Any = None, use_llm: bool = True,
                 data_dir: Path = DATA_DIR, persist_dir: Path | None = None):
        """청크를 읽어 BM25와 Chroma 인덱스를 만든다.

        - embeddings: 없으면 Bedrock 임베딩(`BEDROCK_EMBED_MODEL_ID`).
        - persist_dir: 주면 Chroma를 그 폴더에 저장하고, 문서·임베딩 모델 지문이 같으면 임베딩 없이 다시 쓴다.
          없으면 메모리에만 만든다(테스트용).
        - llm: 쿼리 확장·리랭킹 모델. 없고 `use_llm`이면 Bedrock 채팅 모델(`BEDROCK_MODEL_ID`).
          `use_llm=False`면 별칭 확장과 RRF 순서만 쓴다(테스트·오프라인용).
        """
        started = time.perf_counter()
        self.docs = load_chunks(data_dir)
        self.ids = [f"{d.metadata['kind']}:{d.metadata['source']}#{d.metadata['title']}" for d in self.docs]
        self.by_id = dict(zip(self.ids, self.docs))
        self.aliases = alias_table(data_dir / STORAGE_GUIDE.name)
        self.kiwi = Kiwi()
        self.bm25: dict[str, tuple[BM25Okapi, list[str]]] = {}
        for kind in (KIND_RECIPE, KIND_STORAGE):
            ids = [i for i, d in zip(self.ids, self.docs) if d.metadata["kind"] == kind]
            corpus = [tokenize(self.kiwi, self._index_text(self.by_id[i])) for i in ids]
            self.bm25[kind] = (BM25Okapi(corpus), ids)
        self.vectors, self.vectors_loaded = self._open_vectors(embeddings or default_embeddings(), persist_dir)
        self.llm = llm if llm is not None or not use_llm else default_llm()
        self.built_ms = int((time.perf_counter() - started) * 1000)

    def fingerprint(self, embeddings: Embeddings) -> str:
        """청크 ID·본문과 임베딩 모델 이름으로 지문(sha256)을 만든다. 하나라도 바뀌면 값이 달라진다."""
        model = getattr(embeddings, "model_id", None) or getattr(embeddings, "model", None) or type(embeddings).__name__
        digest = hashlib.sha256(str(model).encode())
        for doc_id, doc in zip(self.ids, self.docs):
            digest.update(f"|{doc_id}|{doc.page_content}".encode())
        return digest.hexdigest()

    def _open_vectors(self, embeddings: Embeddings, persist_dir: Path | None) -> tuple[Chroma, bool]:
        """Chroma 컬렉션을 연다. `(벡터 저장소, 저장된 임베딩을 다시 썼는지)`를 돌려준다.

        저장 폴더의 컬렉션 지문과 문서 수가 지금 청크와 같으면 그대로 쓰고, 다르면 지우고 새로 임베딩한다.
        """
        if persist_dir is None:
            client, name = chromadb.EphemeralClient(), f"fridge-{uuid.uuid4().hex[:8]}"
        else:
            persist_dir.mkdir(parents=True, exist_ok=True)
            client, name = chromadb.PersistentClient(path=str(persist_dir)), COLLECTION
        fingerprint = self.fingerprint(embeddings)
        if persist_dir is not None and name in {c.name for c in client.list_collections()}:
            existing = client.get_collection(name)
            if (existing.metadata or {}).get("fingerprint") == fingerprint and existing.count() == len(self.ids):
                return Chroma(collection_name=name, embedding_function=embeddings, client=client), True
            client.delete_collection(name)
        store = Chroma(
            collection_name=name,
            embedding_function=embeddings,
            client=client,
            collection_metadata={"hnsw:space": "cosine", "fingerprint": fingerprint},
        )
        store.add_documents(self.docs, ids=self.ids)
        return store, False

    @staticmethod
    def _index_text(doc: Document) -> str:
        """BM25에 넣을 글. 제목을 한 번 더 넣어 요리명·품목명 일치에 무게를 준다."""
        return f"{doc.metadata['title']}\n{doc.page_content}"

    def expand(self, question: str, kind: str) -> tuple[list[str], list[str]]:
        """질문을 별칭 표와 LLM으로 넓힌다. `(질의 목록, 메모)`를 돌려준다. 첫 질의는 항상 원문이다."""
        queries = [question]
        notes: list[str] = []
        words = set(tokenize(self.kiwi, question)) | set(re.findall(r"[가-힣A-Za-z]+", question))
        extra = []
        for word in sorted(words):
            for alias in self.aliases.get(word, []):
                if alias not in question and alias not in extra:
                    extra.append(alias)
        if extra:
            queries.append(f"{question} {' '.join(extra)}")
        if self.llm is not None:
            try:
                generated = _llm_json(self.llm, EXPAND_PROMPT.format(kind_desc=KIND_DESC[kind], question=question))
                queries.extend(str(q).strip() for q in generated[:3] if str(q).strip() and str(q).strip() not in queries)
            except Exception as exc:  # noqa: BLE001 - 확장이 실패해도 원문과 별칭으로 검색한다
                notes.append(f"LLM 쿼리 확장 생략: {type(exc).__name__}")
        return queries, notes

    def _bm25_ranking(self, query: str, kind: str, n: int) -> list[str]:
        """BM25 점수 상위 n개 문서 ID. 점수 0인 문서는 뺀다."""
        bm25, ids = self.bm25[kind]
        scores = bm25.get_scores(tokenize(self.kiwi, query))
        order = sorted(range(len(ids)), key=lambda i: -scores[i])
        return [ids[i] for i in order[:n] if scores[i] > 0]

    def _vector_ranking(self, query: str, kind: str, n: int) -> list[str]:
        """Chroma 벡터 유사도 상위 n개 문서 ID."""
        hits = self.vectors.similarity_search(query, k=n, filter={"kind": kind})
        return [f"{d.metadata['kind']}:{d.metadata['source']}#{d.metadata['title']}" for d in hits]

    def _title_ranking(self, question: str, kind: str) -> list[str]:
        """질문에 제목(요리명·품목명)이 그대로 들어 있는 문서 ID. 긴 제목을 먼저 둔다."""
        tokens = set(tokenize(self.kiwi, question))
        found = [
            i for i in self.bm25[kind][1]
            if (title := self.by_id[i].metadata["title"]) in question and (len(title) >= 2 or title in tokens)
        ]
        return sorted(found, key=lambda i: -len(self.by_id[i].metadata["title"]))

    def rerank(self, question: str, ids: list[str]) -> list[tuple[str, float]] | None:
        """LLM이 후보를 0~10점으로 다시 매긴다. 점수 높은 순 `(ID, 점수)`. 실패하면 None."""
        if self.llm is None or not ids:
            return None
        lines = []
        for n, doc_id in enumerate(ids):
            doc = self.by_id[doc_id]
            lines.append(f"[{n}] {doc.metadata['title']} ({doc.metadata['source']})\n{doc.page_content[:500]}")
        try:
            rated = _llm_json(self.llm, RERANK_PROMPT.format(question=question, candidates="\n\n".join(lines)))
            scores = {int(r["id"]): float(r["score"]) for r in rated if 0 <= int(r["id"]) < len(ids)}
        except Exception:  # noqa: BLE001 - 리랭킹이 실패하면 RRF 순서를 쓴다
            return None
        order = sorted(range(len(ids)), key=lambda n: (-scores.get(n, 0.0), n))
        return [(ids[n], scores.get(n, 0.0)) for n in order]

    def search(self, question: str, kind: str, k: int = 3) -> SearchResult:
        """하이브리드 검색: 쿼리 확장 → 질의마다 BM25·벡터 순위 → RRF → LLM 리랭킹 → 상위 k개.

        보관 검색은 가이드 범위 청크를 후보 끝에 항상 붙인다(가이드에 없는 품목 질문의 근거).
        """
        queries, notes = self.expand(question, kind)
        rankings = [self._title_ranking(question, kind)]
        for query in queries:
            rankings.append(self._bm25_ranking(query, kind, CANDIDATES))
            rankings.append(self._vector_ranking(query, kind, CANDIDATES))
        fused = rrf([r for r in rankings if r])[:CANDIDATES]
        if kind == KIND_STORAGE:
            scope = f"{KIND_STORAGE}:{STORAGE_GUIDE.name}#{SCOPE_TITLE}"
            if scope in self.by_id and scope not in dict(fused):
                fused.append((scope, 0.0))
        reranked = self.rerank(question, [doc_id for doc_id, _ in fused])
        if reranked is None:
            picked = fused[:k]
        else:
            picked = [(doc_id, score) for doc_id, score in reranked if score >= MIN_RELEVANCE][:k]
        hits = []
        for doc_id, score in picked:
            doc = self.by_id[doc_id]
            hits.append(SearchHit(source=doc.metadata["source"], title=doc.metadata["title"], kind=kind,
                                  content=doc.page_content, score=round(score, 4)))
        return SearchResult(hits=hits, queries=queries, candidates=len(fused), reranked=reranked is not None, notes=notes)


# ── 서버 단위 인덱스 (한 번만 만든다) ─────────────────────────────

_INDEX: HybridIndex | None = None
_LOCK = threading.Lock()
BUILD_STATS = {"builds": 0, "built_ms": 0, "vectors_loaded": False}


def get_index() -> HybridIndex:
    """서버가 쓰는 인덱스를 돌려준다. 아직 없으면 그때 한 번 만든다(CLI처럼 lifespan이 없는 실행용)."""
    global _INDEX
    if _INDEX is None:
        with _LOCK:
            if _INDEX is None:
                _INDEX = HybridIndex(persist_dir=CHROMA_DIR)
                BUILD_STATS["builds"] += 1
                BUILD_STATS["built_ms"] = _INDEX.built_ms
                BUILD_STATS["vectors_loaded"] = _INDEX.vectors_loaded
    return _INDEX


def set_index(index: HybridIndex | None) -> None:
    """인덱스를 바꿔 끼운다. 테스트에서 가짜 임베딩 인덱스를 넣을 때 쓴다."""
    global _INDEX
    _INDEX = index


def warm_up() -> dict[str, int]:
    """서버 시작 때 인덱스를 만든다(이미 있으면 그대로 쓴다). 빌드 횟수·시간·저장 임베딩 재사용 여부를 돌려준다."""
    get_index()
    return dict(BUILD_STATS)
