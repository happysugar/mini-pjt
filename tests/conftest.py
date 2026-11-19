"""테스트 공통 준비: Bedrock 없이 도는 검색 인덱스.

검색 도구와 API lifespan은 `src.retriever`의 서버 단위 인덱스를 쓴다. 테스트에서는 가짜 임베딩과
LLM 없는(별칭 확장 + RRF 순서) 인덱스를 세션마다 한 번 만들어 끼워 넣는다.
"""

import sys
from pathlib import Path

import pytest
from langchain_core.embeddings import DeterministicFakeEmbedding

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import retriever  # noqa: E402


@pytest.fixture(scope="session")
def offline_index():
    """가짜 임베딩으로 만든 하이브리드 인덱스(LLM 확장·리랭킹 없음)."""
    return retriever.HybridIndex(embeddings=DeterministicFakeEmbedding(size=64), use_llm=False)


@pytest.fixture(autouse=True)
def _use_offline_index(offline_index):
    """모든 테스트에서 서버 단위 인덱스를 오프라인 인덱스로 바꿔 둔다."""
    previous = retriever._INDEX
    retriever.set_index(offline_index)
    yield
    retriever.set_index(previous)
