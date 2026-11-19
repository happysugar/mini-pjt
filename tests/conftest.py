"""테스트 공통 준비: Bedrock 없이 도는 검색 인덱스.

검색 도구와 API lifespan은 `src.retriever`의 서버 단위 인덱스를 쓴다. 테스트에서는 가짜 임베딩과
LLM 없는(별칭 확장 + RRF 순서) 인덱스를 세션마다 한 번 만들어 끼워 넣는다.
"""

import os
import shutil
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
def _no_health_mcp(monkeypatch):
    """`.env`의 `HEALTH_MCP_URL`과 상관없이 워치 MCP 연동을 끈 상태로 시작한다.

    워치 서버가 필요한 테스트는 `tools.set_health_target()`으로 같은 프로세스의 서버를 넣는다.
    """
    from src import tools

    monkeypatch.delenv("HEALTH_MCP_URL", raising=False)
    tools.set_health_target(None)
    yield
    tools.set_health_target(None)


@pytest.fixture(autouse=True)
def _trace_file(tmp_path, monkeypatch):
    """요청 이벤트(`trace.jsonl`)를 `data/` 대신 테스트마다 임시 파일에 쓴다."""
    path = tmp_path / "trace.jsonl"
    monkeypatch.setenv("TRACE_FILE", str(path))
    return path


@pytest.fixture
def allow_free_text_writes(monkeypatch):
    """API의 형식 가드레일(자연어 재고·평가 쓰기 차단)을 끈다.

    자연어 문장으로 도구의 승인(HITL)·멱등·신원 처리를 검증하던 테스트용이다. 실제 서비스는 이런 요청을 모델 호출 전에
    막는다(SERVICE.md 5.6). 도구는 그대로 등록돼 있으므로 도구 수준의 동작은 계속 검증한다.
    """
    from src import guardrails

    monkeypatch.setattr(guardrails, "free_text_write_kind", lambda question: None)


@pytest.fixture(autouse=True)
def storage_items_copy(tmp_path, offline_index):
    """품목 기준표를 테스트마다 임시 복사본으로 바꿔 쓴다(새 품목 등록이 원본에 행을 덧붙이지 않게).

    끝나면 원래 경로로 되돌리고, 세션 인덱스의 보관 청크도 원본 기준표에 맞춘다.
    """
    from src import tools

    path = tmp_path / retriever.STORAGE_ITEMS.name
    shutil.copyfile(retriever.STORAGE_ITEMS, path)
    previous = os.environ.get(retriever.STORAGE_ITEMS_ENV)
    os.environ[retriever.STORAGE_ITEMS_ENV] = str(path)
    tools.clear_guide_cache()
    yield path
    if previous is None:
        os.environ.pop(retriever.STORAGE_ITEMS_ENV, None)
    else:
        os.environ[retriever.STORAGE_ITEMS_ENV] = previous
    tools.clear_guide_cache()
    offline_index.sync_storage()


@pytest.fixture(autouse=True)
def _use_offline_index(offline_index):
    """모든 테스트에서 서버 단위 인덱스를 오프라인 인덱스로 바꿔 둔다."""
    previous = retriever._INDEX
    retriever.set_index(offline_index)
    yield
    retriever.set_index(previous)
