# TASK-01 환경 점검 (M0)

- 단계: M0
- 예상 시간: 0.25h (추정)
- 선행 작업: 없음
- 관련 개념: 전체 (모든 개념의 실행 전제)
- 관련 리뷰 결정: R11 (D16), R2 (D6), 리뷰 작업 T1

## 목표
코드를 쓰기 전에 Bedrock 모델 접근과 누락 패키지를 확인해, 개발 도중 환경 문제로 막히는 일을 없앤다.

## 작업 내용
- [x] `requirements.txt` 수정 (SERVICE.md 18장)
  - `langchain>=1.0`
  - 추가: `langgraph-checkpoint-sqlite`, `aiosqlite`, `fastapi`, `uvicorn`, `ragas`
- [x] `.venv`에 설치. `ragas` 설치가 Python 3.14에서 실패하면 기록만 하고 넘어간다(TASK-09에서 대안 사용).
- [x] `.env`에 키 추가 (값은 커밋하지 않음)
  - `BEDROCK_MODEL_ID` (채팅), `BEDROCK_EMBED_MODEL_ID` (임베딩), `EVAL_MODEL_ID` (평가 판정)
  - `USER_TOKENS=dad:...,mom:...,jimin:...`
  - `DEFAULT_USER=dad`
  - `FRIDGE_TODAY=2026-11-20` (시연·평가용)
- [x] Bedrock 채팅 모델 1회 호출, 임베딩 1회 호출 스모크 테스트
- [x] 채팅 응답 시간 측정해 기록

## 대상 파일
- `requirements.txt`, `.env`

## 완료 기준
- 채팅·임베딩 호출이 모두 성공한다.
- 채팅 1회 응답 시간과 `ragas` 설치 성공 여부를 기록했다.

## 참고
- SERVICE.md 14장 M0, 18장 의존성 변경

## 결과 (2026-11-18)
- 환경: Python 3.14.8, `langchain` 1.5.0, `langgraph` 1.2.22, `langchain-aws` 1.9.3, 리전 `us-east-1`
- `ragas` 설치 **성공** (0.4.4). 의존성으로 `langchain-community`가 함께 설치됨. 18장 결정대로 코드에서는 직접 쓰지 않는다.
- 사용 모델 (`.env`)
  - 채팅 `BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-6`
  - 임베딩 `BEDROCK_EMBED_MODEL_ID=amazon.titan-embed-text-v2:0` (1024차원)
  - 평가 `EVAL_MODEL_ID=us.anthropic.claude-sonnet-4-5-20250929-v1:0` (채팅과 다른 모델로 자기 채점 편향 완화)
- 계정 접근 권한: Claude 4.5·4.6 계열과 Nova Pro만 호출 가능. Claude 5.x와 Opus 4.8은 추론 프로필 목록에는 있으나 호출 시 `AccessDeniedException`. Cohere 임베딩도 호출 불가.
- 스모크 테스트 (`ChatBedrockConverse`, `BedrockEmbeddings`): 모두 성공
  - 채팅 응답 시간 (한국어 한 문장, 출력 40~63토큰): 1.74s / 1.55s / 1.48s
  - 임베딩 1회 1.05s, 평가 모델 1회 1.50s
