# mini-pjt: 냉장고 관리 Agent "구출 저녁"

가족이 함께 쓰는 냉장고의 식재료를 입고부터 폐기까지 관리하고, 유효기간이 임박한 재료를 살리는 한식·일식·이탈리아식·중식 요리를 추천하는 Agent입니다. 식사 후 평가를 받아 다음 추천에 반영하고, 섭취 칼로리를 Galaxy Watch 헬스케어 기록(mock)으로 남깁니다.

> 개발 중인 교육 과정 미니 프로젝트입니다. 지금 동작하는 기능과 구현 예정 기능은 아래 [진행 상황](#진행-상황)에 나눠 적었습니다. 상세 설계는 [SERVICE.md](SERVICE.md), 작업 목록은 [tasks/README.md](tasks/README.md)에 있습니다.

## 주요 기능

### 재고 관리
- 입고·출고·폐기는 화면의 재고 입력 Form(JSON: 품목, 별칭, S/N, 양, 단위, 개당 평균 무게, 유효기간, 투입일자, 권장 보관일수, 상태)으로만 받습니다. 상태 값(`stored`·`consumed`·`discarded`)으로 입고·출고·폐기를 고르고, 비운 칸은 서버가 계산합니다. "한우 600g 넣었어" 같은 자연어 쓰기 요청은 저장하지 않고 Form을 쓰라고 안내합니다(`FORM_REQUIRED`).
- S/N을 `YYYY-MM-NNN` 형식(투입 연-월 + 그 달의 순번)으로 자동 발번합니다. 직접 준 S/N은 형식과 중복을 검사합니다.
- 유효기간을 모르면 [품목 기준표](data/storage_items.csv)의 품목별 권장 보관일수로 계산합니다. 기준표에 없는 새 품목은 권장 보관일수(양의 정수)가 있어야 등록하고, 등록하면 기준표와 검색 인덱스에 자동으로 덧붙입니다. 개당 평균 무게는 값이 있으면 양수만 받습니다. 품목별 보관 팁은 [보관 가이드](data/storage_guide.md)에 따로 있습니다.
- 단위는 `g`, `L`, `개`로 통일합니다(`kg`→`g`, `ml`→`L`, `모·팩·봉·알`→`개`). "한우"는 "소고기"처럼 별칭으로도 찾습니다. 양은 절대값으로 다룹니다(`-2` → `2`, 0은 거절).
- 출고는 유효기간이 빠른 재고부터 차감합니다(FEFO). S/N을 주면 그 재고에서만 차감합니다. 그 품목 보유량의 50%를 넘는 출고와 모든 폐기는 승인(HITL)을 받습니다.
- 유효기간이 0~3일 남으면 임박, 지나면 만료 경고를 응답의 `trace.alerts`에 담습니다.

### 요리 추천
- 한식·일식·이탈리아식·중식 슬롯에 하나씩 추천합니다. 다른 카테고리 요청은 정중히 거절하고 대안을 제시합니다.
- 유효기간이 임박한 재료를 쓰는 요리를 먼저 고르고, 요청자가 3점 이상 준 요리를 우선합니다. 신메뉴도 2개 함께 보여주고, 부족한 재료를 알려줍니다.
- 요청자의 알레르기 재료가 든 요리는 어떤 경우에도 추천하지 않습니다.
- 레시피와 보관 팁은 하이브리드 검색(BM25 + 벡터, LLM 리랭킹)으로 찾고, 근거 문서를 응답의 `contexts`로 돌려줍니다.
- "엄마랑 지민이랑 같이 먹을 거"처럼 가족 합의 저녁도 추천합니다. 이때 다른 가족 구성원의 개별 평점이나 칼로리 수치는 보여주지 않습니다.

### 평가와 식사 기록
- recipe 평가 결과는 화면의 평가 입력 Form(JSON: recipe 명, 요리한 양 `N 인분`, 평가자별 식사량 `N 인분`과 점수 배열)으로만 받습니다. 요청자 한 사람의 승인으로 평가자마다 평가·식사·워치 칼로리 기록을 남기고, 재료는 요리한 양 기준으로 한 번만 차감합니다. "규동 먹었어, 4점" 같은 자연어 평가는 저장하지 않고 Form을 쓰라고 안내합니다.
- 저장하기 전에 반드시 사용자 승인(HITL)을 받습니다. 폐기와 대량 출고도 같습니다.
- API가 모델을 부르기 전에 Form의 형식과 값(인분 형식, 가족 구성원 여부, 점수 1~5, 식사량 합계)을 검사하고, 틀리면 저장하지 않고 문제를 알려 줍니다.
- 대기 중인 승인 없이 "아까 그 식사 기록 승인할게"처럼 승인만 말하면 "대기 중인 승인 요청이나 기록할 식사가 없어요"라고 답합니다.
- 기록되는 사용자는 토큰으로 확인한 요청자입니다. "아빠 계정으로 기록해줘" 같은 자연어 요청은 형식 가드레일이 먼저 거절합니다.

### 폐기 회피량
- 유효기간 임박(0~3일) 상태에서 먹거나 쓴 양을 모아 "이번 주에 살린 식재료 g"으로 보여줍니다.

### 안전장치
- 사용자 토큰으로 신원을 확인하고, 다른 가족 구성원의 평가 원문·건강 기록·대화에는 접근할 수 없습니다.
- 프롬프트 인젝션을 모델 호출 전에 차단합니다. 단어 하나가 아니라 "이전 지시 + 무시"처럼 동작과 대상의 조합을 보고, 띄어 쓴 글자("무 시 해")나 숫자 치환("1gn0re") 우회도 풀어서 검사합니다.
- 재고 입고·출고·폐기와 recipe 평가는 JSON Form으로만 받습니다. 자연어 쓰기 요청은 모델을 부르기 전에 `FORM_REQUIRED`로 거절합니다. 조회·추천·보관 질문은 자연어로 받습니다.
- 다른 가족 구성원의 기록 조회와 개인정보 저장 요청을 거절하고, 전화번호·주민번호 같은 개인정보는 응답에서 마스킹합니다.

## 구성

| 위치 | 내용 |
|---|---|
| `src/agent.py` | Supervisor와 하위 에이전트(재고, 요리추천, 평가·헬스, 3일 식단) |
| `src/tools.py` | 결정적 도메인 도구: 날짜, S/N, 단위 환산, FEFO 차감, 추천 정렬, DB |
| `src/retriever.py` | RAG 파이프라인 (BM25 + 벡터 RRF, LLM 쿼리 확장·리랭킹) |
| `src/api.py` | FastAPI `POST /query` (신원·스레드·승인 확인, 특수 상황 응답) |
| `src/guardrails.py` | 입력 검사(인젝션, 다른 가족 구성원 기록, 개인정보 저장, 빈 승인 문장), 평가·재고 입력 Form 검사, 자연어 쓰기 차단(`FORM_REQUIRED`), 출력 마스킹 |
| `src/chains.py` | 입고 문장을 파싱하는 LCEL 체인(자연어 입고 도구용. API는 자연어 입고를 받지 않음) |
| `src/middleware.py` | 내장 미들웨어 조립(PII 마스킹, 재시도, 이력 요약) |
| `src/planner.py` | Plan-Execute 3일 저녁 식단 |
| `src/memory.py` | 장기 메모리(선호·비선호·알레르기) |
| `src/health_mcp_server.py` | 워치 헬스케어 MCP 서버(mock) |
| `src/tracer.py` | 로컬 트레이서(`data/trace.jsonl`) |
| `src/cli.py` | 터미널 데모 |
| `data/` | 레시피, 보관 가이드, 품목 기준표(`storage_items.csv`, 새 품목 등록 때 자동으로 덧붙음), 시드 재고·가족 데이터 |
| `evaluation/` | 평가 문항 `test_queries.csv`, 검색 평가 `retrieval_queries.csv`, 평가 스크립트 `run_eval.py`, 결과 `results/` |

- 모델: Amazon Bedrock (`ChatBedrockConverse`)
- 프레임워크: LangChain 1.x (`create_agent`), LangGraph
- 저장소: SQLite (`data/fridge.db` 도메인 데이터, `data/checkpoints.db` 대화 체크포인트·장기 메모리)

## 준비

### 1. 요구 사항
- Python 3.14
- Amazon Bedrock 모델 호출 권한이 있는 AWS 계정 (채팅·임베딩·평가 모델)

### 2. 설치

```bash
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Windows·Python 3.14에서 `ragas` 설치가 scikit-network 빌드 오류로 실패하면 미리 빌드된 패키지를 우선 쓰도록 설치합니다. 이렇게 해도 평가 지표 계산에는 문제가 없습니다.

```bash
pip install --prefer-binary ragas==0.4.4
```

### 3. 환경 변수 (`.env`)

저장소 루트에 `.env` 파일을 만듭니다. 이 파일은 `.gitignore`에 들어 있으며 **절대 커밋하지 않습니다.**

```dotenv
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=us-east-1

BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-6
BEDROCK_EMBED_MODEL_ID=amazon.titan-embed-text-v2:0
EVAL_MODEL_ID=us.anthropic.claude-sonnet-4-5-20250929-v1:0

USER_TOKENS=dad:<토큰>,mom:<토큰>,jimin:<토큰>
DEFAULT_USER=dad
FRIDGE_TODAY=2026-11-20

HEALTH_MCP_URL=http://127.0.0.1:8765/mcp
```

| 키 | 설명 |
|---|---|
| `BEDROCK_MODEL_ID` | 에이전트가 쓰는 채팅 모델 |
| `BEDROCK_EMBED_MODEL_ID` | 레시피 검색용 임베딩 모델 |
| `EVAL_MODEL_ID` | 평가(LLM-as-Judge, RAGAS)용 모델. 채팅 모델과 다르게 두면 자기 채점 편향이 줄어듭니다 |
| `USER_TOKENS` | 가족별 토큰. 요청 헤더 `X-User-Token`과 대조해 사용자를 확인합니다 |
| `DEFAULT_USER` | 토큰도 `user_id`도 없는 요청을 처리할 사용자. 비우면 조회만 가능한 `guest`로 동작합니다 |
| `FRIDGE_TODAY` | "오늘" 날짜를 고정합니다. 시연과 평가는 `2026-11-20`으로 실행해 결과를 재현합니다. 비우면 실제 오늘 날짜를 씁니다 |
| `HEALTH_MCP_URL` | 워치 헬스케어 MCP 서버 주소. 비우거나 서버가 꺼져 있으면 워치 조회 도구(`get_calorie_budget`, `get_daily_intake`) 없이 시작합니다 |
| `HEALTH_MCP_PORT`, `HEALTH_DB` | (선택) 워치 서버 포트(기본 8765)와 기록 DB(기본 `data/health.db`) |
| `TRACE_FILE` | (선택) 로컬 트레이스 파일. 기본 `data/trace.jsonl` |
| `FRIDGE_STORAGE_ITEMS` | (선택) 품목 기준표 경로. 기본 `data/storage_items.csv`. 테스트와 평가는 임시 복사본을 가리키게 해 원본을 바꾸지 않습니다 |
| `LANGSMITH_API_KEY`, `LANGSMITH_TRACING` | (선택) 있으면 LangSmith에도 기록합니다 |
| `FRIDGE_API_URL` | (선택) 터미널 데모가 부를 API 주소 |

모델 ID는 계정에서 호출 권한이 있는 모델로 바꿔도 됩니다. 목록에 보여도 권한이 없으면 `AccessDeniedException`이 납니다.

## 사용법

### DB 초기화

시드 데이터로 `data/fridge.db`를 새로 만듭니다. 시드의 날짜는 `FRIDGE_TODAY` 기준 상대 일수라서, `2026-11-20`이면 한우 600g(S/N `2026-11-003`)의 유효기간이 `2026-11-22`(D-day 2)가 됩니다.

```bash
python -c "from dotenv import load_dotenv; load_dotenv(); from src.tools import reset_db; print(reset_db())"
```

### 워치 MCP 서버

식사 칼로리 기록과 남은 칼로리 조회를 맡는 워치 mock 서버입니다. API 서버보다 먼저 별도 터미널에서 띄웁니다. 기본 주소는 `http://127.0.0.1:8765/mcp`입니다.

```bash
python -m src.health_mcp_server
```

### API 서버

```bash
uvicorn src.api:app
```

`POST /query`에 `question`을 보내면 `answer`, `contexts`, `trace` 세 키로 답합니다.

```bash
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -H "X-User-Token: <dad의 토큰>" \
  -d '{"question": "오늘 저녁 뭐 먹지?", "user_id": "dad"}'
```

재고 입고·출고·폐기와 평가 결과는 `question`에 JSON Form 문자열을 넣어 보냅니다(형식은 [SERVICE.md 5.1](SERVICE.md#51-재고-관리-입고출고유효기간)과 5.6). 예: `{"item": "두부", "aliases": null, "sn": null, "qty": 2, "unit": "개", "avgWeight": null, "expiryDate": null, "inputDate": null, "storageDays": null, "status": "stored"}`

승인이 필요한 요청은 `trace.pending_interrupt`가 함께 옵니다. 같은 `thread_id`로 `resume`을 보내면 이어서 처리합니다.

```bash
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -H "X-User-Token: <dad의 토큰>" \
  -d '{"question": "승인", "user_id": "dad", "thread_id": "<이전 응답의 trace.thread_id>", "resume": "approve"}'
```

대기 중인 승인이 없는데 `resume`을 보내거나 승인 문장만 보내면 `trace.errors`에 `NO_PENDING_INTERRUPT`가 오고 아무것도 바뀌지 않습니다. 그 밖의 특수 상황 응답은 [SERVICE.md 8장](SERVICE.md#8-api-계약)에 있습니다.

Windows Git Bash의 curl은 `-d '{"question":"한글"}'` 본문을 UTF-8로 보내지 않아 400이 날 수 있습니다. 이때는 본문을 파일로 저장해 `--data-binary @q.json`으로 보냅니다.

### 터미널 데모

대표 시나리오 "만료 임박 구출 저녁"을 터미널에서 진행합니다. 실행 중인 API 서버에 질문을 보내고, 화면 위에 임박 경고를, 승인 요청은 y/n 질문으로, 아래에 이번 주 폐기 회피량을 보여줍니다. 식사 평가 단계는 질문 칸에 평가 입력 Form JSON을 붙여 넣습니다(자연어 평가는 받지 않음).

```bash
python -m src.cli --user dad
```

### 테스트

```bash
pytest
```

결정적 로직, API 계약(가짜 모델), 가드레일, 미들웨어, 검색, 평가 하네스를 임시 DB에서 검증합니다. Bedrock이나 AWS 자격 증명 없이 돕니다.

### 평가

평가 문항(`evaluation/test_queries.csv`)을 실제 Bedrock으로 실행하고 LLM-as-Judge, 도구 일치, RAGAS(faithfulness, answer relevancy)로 채점합니다. 문항마다 시드로 만든 임시 DB를 쓰므로 `data/` 아래 DB는 바뀌지 않습니다. 워치 MCP 서버는 평가 스크립트가 평가 전용으로 직접 띄우고 문항마다 기록을 비우므로 따로 켤 필요가 없고, `data/health.db`도 바뀌지 않습니다(`--no-watch`로 끌 수 있음).

```bash
python -m evaluation.run_eval
```

```bash
python -m evaluation.run_eval --only P02,N01 --compare evaluation/results/<이전 결과>.json
```

```bash
python -m evaluation.run_eval --retrieval
```

- `--only`: 일부 문항만 실행합니다. `--compare`: 이전 결과와 도구 일치를 비교합니다.
- `--retrieval`: 검색기만 따로 채점합니다(`evaluation/retrieval_queries.csv`, Precision@3·Recall@3·MRR·Hit rate). `--no-llm`을 붙이면 LLM 쿼리 확장·리랭킹 없이 채점합니다.
- 결과는 `evaluation/results/run-YYYYMMDD-HHMMSS.json`(검색 평가는 `retrieval-…json`)에 저장됩니다. 파일 이름의 시각은 결과를 저장한 시각입니다.
- 최근 전체 평가(`run-20261120-052058.json`, 47문항: positive 8, negative 16, edge 14, guardrail 9): 문항 통과 47/47, guardrail 9/9, forbidden 위반 0, 도구 일치 47/47, faithfulness 0.951, answer relevancy 0.849
- 최근 검색 평가(`retrieval-20261120-051234.json`): Precision@3 0.973, Recall@3 1.0, MRR 1.0, Hit rate 1.0

## 진행 상황

| 단계 | 내용 | 상태 |
|---|---|---|
| M0 | 환경 점검 (Bedrock 채팅·임베딩 호출 확인) | 완료 |
| M1 | 레시피·보관 가이드·시드 데이터 | 완료 |
| M1 | DB 스키마와 시드 로더 | 완료 |
| M1 | 재고·추천 결정적 로직과 pytest | 완료 |
| M2 | 단일 에이전트, HITL, FastAPI `/query`, CLI, 평가 하네스 | 완료 |
| M3 | 하이브리드 RAG, 입고 파싱 체인, 가드레일, 미들웨어 | 완료 |
| M4 | Supervisor 분할, 헬스케어 MCP 서버 | 완료 |
| M5 | 장기 메모리, 가족 합의 저녁, 3일 식단 | 완료 |
| M6 | Trace 마무리 | 완료 |
| M6 | 최종 평가와 리포트(`evaluation/report.md`), 시연 리허설 | 예정 (TASK-20) |

### 구현 계획 변경 (2026-11-20)

- **화면 입력 방식**: "오늘의 추천 요리"와 "가족 합의 추천 요리"는 조회 버튼이 고정 메시지를 보내고, recipe 평가 결과는 고정된 JSON 입력 Form(recipe 명, 요리한 양 `N 인분`, 평가자별 식사량 `N 인분`과 점수 배열)으로 받습니다. 권한 관리, 재고 조정, 가족 구성원 관리의 입력 Form도 JSON 형식입니다. 평가 입력 JSON은 API가 모델을 부르기 전에 형식과 값(인분 형식, 가족 구성원 여부, 점수 1~5, 식사량 합계)을 검사하고, 틀리면 저장하지 않습니다. 통과하면 요청자 한 사람의 승인으로 평가자마다 평가·식사·워치 기록을 남기고, 재료는 요리한 양 기준으로 한 번만 차감합니다.
- **가족은 하나**: 등록하는 가족은 하나뿐입니다(`family_id` 하나). 문서의 "다른 가족 구성원"은 같은 가족의 다른 사람을 뜻합니다. 조회·추천·보관 질문은 자유 문장도 받고, 기존 평가 문항은 비정상 경로(시스템 버그·해킹) 검증용으로 유지합니다. 자세한 내용은 [SERVICE.md 5.6](SERVICE.md#56-화면-입력-방식-구현-계획-변경-2026-11-20).
- **재고 입력 Form**: 입고·출고·폐기를 고정된 JSON 재고 입력 Form 하나로 받습니다. API가 형식과 지금 재고(S/N 중복, 보유량 등)를 먼저 검사하고, 재고 에이전트의 확정 도구(`confirm_inventory_form`)가 보관된 Form만 읽어 처리합니다(SERVICE.md 5.1).
- **재고·평가 쓰기는 JSON Form으로만**: 자연어로 온 입고·출고·폐기와 식사 기록·평가 요청은 모델을 부르기 전에 `FORM_REQUIRED`로 거절합니다. 자연어로 쓴 평가 문항 11개는 거절을 기대하는 negative 문항으로 바꿨습니다(SERVICE.md 5.6, 10.1).
- **새 품목 등록과 기준표 자동 추가**: 기준표에 없는 품목은 권장 보관일수(양의 정수)가 있어야 등록하고, 등록하면 `storage_items.csv`와 검색 인덱스에 덧붙여 보관 기간 질문에도 답합니다. 양은 절대값으로 다룹니다(SERVICE.md 5.1).
- **이번 구현 범위에서 제외**: 권한 관리(admin·guest), 재고 조정 화면, 가족 구성원 관리, 레시피 문서 관리(신메뉴 자동·수동 등록, 삭제). 자세한 내용은 [SERVICE.md 5.7](SERVICE.md#57-이번-구현-범위에서-제외한-기능-구현-계획-변경-2026-11-20).

M6 전에 시스템 프롬프트 중복 정리, 인젝션 규칙 확장, 빈 승인 문장 처리, 지원하지 않는 카테고리 안내 고정을 반영했습니다(각 TASK 문서의 "후속 변경"). 2026-11-20 작업(재고 입력 Form, JSON Form 전용 처리, 기준표 자동 추가)은 `design-review` branch에서 진행해 PR #1로 main에 merge했습니다. pytest 452개가 통과합니다. P02 answer relevancy(0.74, 목표 0.8)는 답변이 질문에 맞는데도 평가 모델의 역질문과 원래 질문의 말투 차이로 낮게 나오는 것으로 진단해 `evaluation/report.md`에 근거를 기록했습니다. 남은 과제는 리포트 나머지(개선 이력)와 시연 리허설입니다.

시드 데이터(`data/seed_*.json`)는 지금 예시 데이터입니다. 실제 냉장고 목록과 가족 선호로 바꿀 예정입니다.