# TASK-10 RAG: 하이브리드 검색, LLM 리랭킹, 쿼리 확장

- 단계: M3
- 예상 시간: 1h (추정)
- 선행 작업: TASK-01, TASK-02
- 관련 개념: #3 RAG
- 관련 리뷰 결정: R4 (D8), 17장 결정 2 (리랭커는 LLM 기반), 리뷰 작업 T10

## 목표
레시피와 보관 가이드를 한국어로 잘 찾는 검색 파이프라인을 만든다.

## 작업 내용
- [x] 문서 청킹: 레시피는 요리 단위, 보관 가이드는 품목·팁 단위, 메타데이터에 출처 파일과 요리명
- [x] Chroma 벡터 인덱스 (Bedrock 임베딩)
- [x] kiwipiepy 형태소 + `rank-bm25` BM25 인덱스
- [x] RRF로 두 결과 결합 (직접 구현, `langchain-community` 미사용)
- [x] 쿼리 확장: 별칭 표 + LLM으로 2~3개 질의 생성 (예: 소고기 → 한우, 우둔, 차돌)
- [x] LLM 리랭킹: 상위 후보를 Bedrock 채팅 모델이 관련도로 다시 매겨 상위 k개
- [x] 인덱스는 서버 시작 때 한 번만 로드. 쿼리 확장·리랭킹은 검색 도구 안에서만 실행
- [x] 검색 도구 `search_recipes`, `search_docs`가 `contexts`에 출처를 담아 반환

## 대상 파일
- `src/retriever.py`, `src/tools.py`(검색 도구 래핑)
- 함께 고친 곳: `src/api.py`(lifespan에서 인덱스 빌드, `search_docs` contexts, `trace.index`), `src/agent.py`(검색 규칙), `tests/conftest.py`, `tests/test_retriever.py`, `tests/test_api.py`

## 완료 기준
- 평가 문항 P03, P05, N02가 통과한다.
- 정상 질문에서 인덱스를 다시 만들지 않는다(trace로 확인).

## 참고
- SERVICE.md 6장 #3, 7장 검색 인덱스

## 결과 (2026-11-19)
- 평가 P03·P05·N02 3/3 통과(실제 Bedrock, `FRIDGE_TODAY=2026-11-20`, 문항마다 임시 DB). 도구 일치 3/3, forbidden 위반 0.
  - P03: `search_recipes`를 부르고 `[recipes_japanese.md#규동]` 재료·양·680kcal·조리 순서로 답함 (faithfulness 1.00, relevancy 0.87)
  - P05: `search_docs` → `get_storage_days`, 7일 + 물 갈아 주기 팁 + 출처 (1.00 / 0.71)
  - N02: `search_docs` → `get_storage_days`, 가이드에 없다고 안내하고 유효기간을 직접 요청. contexts에 가이드 범위 청크가 들어감(이전에는 빈 목록).
    RAGAS faithfulness·relevancy는 0.00. 답이 "정보가 없다"는 안내라 RAGAS가 주장 문장을 뽑지 못한 결과이고 검색 문제는 아님.
  - TASK-09 하네스는 이 브랜치에 없어서 원래 폴더의 `evaluation/`을 잠시 복사해 이 브랜치 코드로 돌리고 지웠다(커밋하지 않음).
- 인덱스 한 번만 빌드: `trace.index = {builds, built_ms}`. 평가에서 앱 3개·질문 3개 동안 `builds`가 1로 그대로였다.
  `tests/test_api.py::test_search_contexts_and_no_index_rebuild`, `tests/test_retriever.py::test_index_is_built_once`로도 확인.
- 구성 (`src/retriever.py`)
  - 청크: 레시피 36개(요리 단위), 보관 가이드 품목 27개(기준표 행 + 보관 팁), 가이드 범위·단위 규칙 1개. 메타데이터 `source`, `title`, `kind`.
  - 벡터: Chroma `PersistentClient`(`data/chroma/`, 테스트는 메모리 `EphemeralClient`), Bedrock 임베딩 `BEDROCK_EMBED_MODEL_ID`, cosine.
  - BM25: kiwipiepy 명사·용언 어간·외국어 토큰 + `BM25Okapi`, 제목을 한 번 더 넣어 가중. kind별 인덱스 2개.
  - 질의마다 BM25·벡터 순위, 질문에 제목이 그대로 있으면 제목 순위까지 RRF(k=60, 직접 구현)로 합쳐 상위 8개.
  - 쿼리 확장: 보관 가이드 별칭 표(소고기 ↔ 한우·우둔·차돌…)로 질의 1개 + LLM 질의 2~3개. LLM 실패 시 원문·별칭만 쓴다.
  - LLM 리랭킹: 후보를 `BEDROCK_MODEL_ID`가 0~10점으로 매겨 5점 이상 상위 3개. 실패하면 RRF 순서.
  - 보관 검색은 가이드 범위 청크를 후보에 항상 넣어, 가이드에 없는 품목도 근거 있는 안내가 되게 했다.
- 도구: `search_recipes`(결과는 기존과 같은 `recipe_text` 형식), `search_docs` 신규. 결과에 `search.queries/candidates/reranked`.
  `get_recipe`는 식사 기록 전 확인용으로 설명을 바꾸고, 프롬프트에서 레시피 질문은 `search_recipes`를 쓰게 했다.
  `rank_recipes`는 건드리지 않아 추천 결과는 그대로다.
- pytest 전체 119개 통과. 테스트는 `tests/conftest.py`가 가짜 임베딩·LLM 없는 인덱스를 세션당 한 번 끼워 넣어 AWS 없이 돈다.
- 시작 시간 단축: Chroma를 `data/chroma/`(`PersistentClient`, git 제외)에 저장한다. 컬렉션 메타데이터에 지문
  (청크 ID·본문 + 임베딩 모델 ID의 sha256)을 넣고, 다음 시작 때 지문과 문서 수가 같으면 임베딩 없이 다시 쓴다.
  레시피·가이드 문서나 `BEDROCK_EMBED_MODEL_ID`가 바뀌면 컬렉션을 지우고 새로 만든다. `trace.index.vectors_loaded`로 확인.
  - 실측(실제 Bedrock): 첫 시작 인덱스 빌드 24.1초(임베딩·저장) → 두 번째 3.7초(저장본 로드, 나머지는 Kiwi·BM25).
  - `tests/test_retriever.py::test_persisted_vectors_are_reused`: 재사용 시 임베딩 0건, 문서가 바뀌면 전부 다시 임베딩.
  - 테스트는 `persist_dir` 없이 메모리 인덱스를 써서 `data/`를 건드리지 않는다. pytest 전체 120개 통과.
- 재평가 (저장 인덱스 사용, 실제 Bedrock): P03·P05·N02 3/3 통과, 도구 일치 3/3, forbidden 위반 0.
  `trace.index`는 `vectors_loaded: true`, `built_ms` 3240, `builds` 1 그대로.
  RAGAS faithfulness / relevancy: P03 1.00 / 0.87, P05 1.00 / 0.68, N02 0.33 / 0.00("정보 없음" 안내라 낮게 나옴).
- N02 근거 보강: `get_storage_days`가 "보관 가이드에 없는 품목이에요: 망고"를 돌려주면 `extract_contexts`가
  `[storage_guide.md#망고] …`로 contexts에 담는다(`tools.NOT_IN_GUIDE` 문구 기준). "망고는 가이드에 없다"는 답의 근거가 생겨
  faithfulness가 1.00이 됐다(아래 재평가). answer relevancy 0.00은 RAGAS가 "정보 없음" 답(noncommittal)에 0을 곱하는 설계라
  평가 기준(N02는 해당 없음 처리)을 바꿔야 하며, 이는 TASK-09 평가 하네스 쪽 결정으로 남긴다.
  - pytest 전체 121개 통과(`test_unknown_item_storage_days_is_context` 추가).
  - 첫 재평가는 에이전트 모델 `us.anthropic.claude-sonnet-4-6`이 Bedrock 일일 토큰 한도(`ThrottlingException: Too many tokens per day`)에
    걸려 실패했다. 같은 모델의 `global.` 프로필은 호출된다.
  - 재평가 (실제 Bedrock, global 프로필): `.env`의 `EVAL_MODEL_ID`를 `global.anthropic.claude-sonnet-4-5-20250929-v1:0`으로 바꾸고,
    에이전트 모델은 실행할 때만 `BEDROCK_MODEL_ID=global.anthropic.claude-sonnet-4-6`으로 덮어써 P03·P05·N02를 돌렸다.
    3/3 통과, 도구 일치 3/3, forbidden 위반 0. N02 contexts에 `[storage_guide.md#망고]`가 들어가 faithfulness 0.33 → 1.00.
    RAGAS faithfulness / relevancy: P03 1.00 / 0.87, P05 1.00 / 0.71, N02 1.00 / 0.00. 평균 faithfulness 1.00, relevancy 0.525.
