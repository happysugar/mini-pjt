# mini-pjt: 냉장고 관리 Agent "구출 저녁"

가족이 함께 쓰는 냉장고의 식재료를 입고부터 폐기까지 관리하고, 유효기간이 임박한 재료를 살리는 한식·일식·이탈리아식·중식 요리를 추천하는 Agent입니다. 식사 후 평가를 받아 다음 추천에 반영하고, 섭취 칼로리를 Galaxy Watch 헬스케어 기록(mock)으로 남깁니다.

> 개발 중인 교육 과정 미니 프로젝트입니다. 지금 동작하는 기능과 구현 예정 기능은 아래 [진행 상황](#진행-상황)에 나눠 적었습니다. 상세 설계는 [SERVICE.md](SERVICE.md), 작업 목록은 [tasks/README.md](tasks/README.md)에 있습니다.

## 주요 기능

### 재고 관리
- "한우 600g, 두부 1모 샀어" 같은 자연어 문장으로 입고합니다.
- S/N을 `YYYY-MM-NNN` 형식(투입 연-월 + 그 달의 순번)으로 자동 발번합니다. 직접 준 S/N은 형식과 중복을 검사합니다.
- 유효기간을 모르면 [보관 가이드](data/storage_guide.md)의 품목별 권장 보관일수로 계산합니다. 가이드에 없는 품목은 "유효기간 미상"으로 둡니다.
- 단위는 `g`, `L`, `개`로 통일합니다(`kg`→`g`, `ml`→`L`, `모·팩·봉·알`→`개`). "한우"는 "소고기"처럼 별칭으로도 찾습니다.
- 출고는 유효기간이 빠른 재고부터 차감합니다(FEFO).
- 유효기간이 0~3일 남으면 임박, 지나면 만료 경고를 응답의 `trace.alerts`에 담습니다.

### 요리 추천
- 한식·일식·이탈리아식·중식 슬롯에 하나씩 추천합니다. 다른 카테고리 요청은 정중히 거절하고 대안을 제시합니다.
- 유효기간이 임박한 재료를 쓰는 요리를 먼저 고르고, 요청자가 3점 이상 준 요리를 우선합니다. 신메뉴도 2개 함께 보여주고, 부족한 재료를 알려줍니다.
- 요청자의 알레르기 재료가 든 요리는 어떤 경우에도 추천하지 않습니다.
- 레시피와 보관 팁은 하이브리드 검색(BM25 + 벡터, LLM 리랭킹)으로 찾고, 근거 문서를 응답의 `contexts`로 돌려줍니다.
- "엄마랑 지민이랑 같이 먹을 거"처럼 가족 합의 저녁도 추천합니다. 이때 다른 가족의 개별 평점이나 칼로리 수치는 보여주지 않습니다.

### 평가와 식사 기록
- "규동 2인분 만들어서 1인분 먹었어, 4점" 한 문장으로 평가 저장, 재료 차감, 워치 칼로리 기록을 함께 처리합니다.
- 저장하기 전에 반드시 사용자 승인(HITL)을 받습니다. 폐기와 대량 출고도 같습니다.
- 인분 정보가 없으면 추측하지 않고 되묻습니다.

### 폐기 회피량
- 유효기간 임박(0~3일) 상태에서 먹거나 쓴 양을 모아 "이번 주에 살린 식재료 g"으로 보여줍니다.

### 안전장치
- 사용자 토큰으로 신원을 확인하고, 다른 가족의 평가 원문·건강 기록·대화에는 접근할 수 없습니다.
- 프롬프트 인젝션을 차단하고, 전화번호·주민번호 같은 개인정보는 응답에서 마스킹합니다.

## 구성

| 위치 | 내용 |
|---|---|
| `src/agent.py` | Supervisor와 하위 에이전트(재고, 요리추천, 평가·헬스) |
| `src/tools.py` | 결정적 도메인 도구: 날짜, S/N, 단위 환산, FEFO 차감, 추천 정렬, DB |
| `src/retriever.py` | RAG 파이프라인 |
| `src/api.py` | FastAPI `POST /query` |
| `data/` | 레시피 36개, 보관 가이드, 시드 재고·가족 데이터 |
| `evaluation/` | 평가 문항 `test_queries.csv`와 평가 리포트 |

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
```

| 키 | 설명 |
|---|---|
| `BEDROCK_MODEL_ID` | 에이전트가 쓰는 채팅 모델 |
| `BEDROCK_EMBED_MODEL_ID` | 레시피 검색용 임베딩 모델 |
| `EVAL_MODEL_ID` | 평가(LLM-as-Judge, RAGAS)용 모델. 채팅 모델과 다르게 두면 자기 채점 편향이 줄어듭니다 |
| `USER_TOKENS` | 가족별 토큰. 요청 헤더 `X-User-Token`과 대조해 사용자를 확인합니다 |
| `DEFAULT_USER` | 토큰도 `user_id`도 없는 요청을 처리할 사용자. 비우면 조회만 가능한 `guest`로 동작합니다 |
| `FRIDGE_TODAY` | "오늘" 날짜를 고정합니다. 시연과 평가는 `2026-11-20`으로 실행해 결과를 재현합니다. 비우면 실제 오늘 날짜를 씁니다 |

모델 ID는 계정에서 호출 권한이 있는 모델로 바꿔도 됩니다. 목록에 보여도 권한이 없으면 `AccessDeniedException`이 납니다.

## 사용법

### DB 초기화 (지금 사용 가능)

시드 데이터로 `data/fridge.db`를 새로 만듭니다. 시드의 날짜는 `FRIDGE_TODAY` 기준 상대 일수라서, `2026-11-20`이면 한우 600g(S/N `2026-11-003`)의 유효기간이 `2026-11-22`(D-day 2)가 됩니다.

```bash
python -c "from dotenv import load_dotenv; load_dotenv(); from src.tools import reset_db; print(reset_db())"
```

### API 서버 (구현 예정)

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

승인이 필요한 요청은 `trace.pending_interrupt`가 함께 옵니다. 같은 `thread_id`로 `resume`을 보내면 이어서 처리합니다.

```bash
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -H "X-User-Token: <dad의 토큰>" \
  -d '{"question": "승인", "user_id": "dad", "thread_id": "<이전 응답의 trace.thread_id>", "resume": "approve"}'
```

### 터미널 데모 (구현 예정)

대표 시나리오 "만료 임박 구출 저녁"을 터미널에서 진행합니다. 화면 위에 임박 경고를, 아래에 이번 주 폐기 회피량을 보여줍니다.

### 테스트 (지금 사용 가능)

```bash
pytest                                  # 결정적 로직 테스트 (Bedrock 불필요)
```

재고 로직(`tests/test_tools.py`)과 추천 로직(`tests/test_recommend.py`)을 임시 DB에서 검증합니다. API 계약 테스트는 M2에서 추가합니다.

### 평가 (구현 예정)

평가 스크립트는 `evaluation/test_queries.csv`의 15문항을 실행하고 결과를 `evaluation/report.md`에 남깁니다.

## 진행 상황

| 단계 | 내용 | 상태 |
|---|---|---|
| M0 | 환경 점검 (Bedrock 채팅·임베딩 호출 확인) | 완료 |
| M1 | 레시피·보관 가이드·시드 데이터 | 완료 |
| M1 | DB 스키마와 시드 로더 | 완료 |
| M1 | 재고·추천 결정적 로직과 pytest | 완료 |
| M2 | 단일 에이전트, HITL, FastAPI `/query`, CLI, 평가 하네스 | 예정 |
| M3 | 하이브리드 RAG, 입고 파싱 체인, 가드레일, 미들웨어 | 예정 |
| M4 | Supervisor 분할, 헬스케어 MCP 서버 | 예정 |
| M5 | 장기 메모리, 가족 합의 저녁, 3일 식단 | 예정 |
| M6 | Trace 마무리, 최종 평가와 리포트 | 예정 |

시드 데이터(`data/seed_*.json`)는 지금 예시 데이터입니다. 실제 냉장고 목록과 가족 선호로 바꿀 예정입니다.