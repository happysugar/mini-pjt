# TASK-01 환경 점검 (M0)

- 단계: M0
- 예상 시간: 0.25h (추정)
- 선행 작업: 없음
- 관련 개념: 전체 (모든 개념의 실행 전제)
- 관련 리뷰 결정: R11 (D16), R2 (D6), 리뷰 작업 T1

## 목표
코드를 쓰기 전에 Bedrock 모델 접근과 누락 패키지를 확인해, 개발 도중 환경 문제로 막히는 일을 없앤다.

## 작업 내용
- [ ] `requirements.txt` 수정 (SERVICE.md 18장)
  - `langchain>=1.0`
  - 추가: `langgraph-checkpoint-sqlite`, `aiosqlite`, `fastapi`, `uvicorn`, `ragas`
- [ ] `.venv`에 설치. `ragas` 설치가 Python 3.14에서 실패하면 기록만 하고 넘어간다(TASK-09에서 대안 사용).
- [ ] `.env`에 키 추가 (값은 커밋하지 않음)
  - `BEDROCK_MODEL_ID` (채팅), `BEDROCK_EMBED_MODEL_ID` (임베딩), `EVAL_MODEL_ID` (평가 판정)
  - `USER_TOKENS=dad:...,mom:...,jimin:...`
  - `DEFAULT_USER=dad`
  - `FRIDGE_TODAY=2026-11-20` (시연·평가용)
- [ ] Bedrock 채팅 모델 1회 호출, 임베딩 1회 호출 스모크 테스트
- [ ] 채팅 응답 시간 측정해 기록

## 대상 파일
- `requirements.txt`, `.env`

## 완료 기준
- 채팅·임베딩 호출이 모두 성공한다.
- 채팅 1회 응답 시간과 `ragas` 설치 성공 여부를 기록했다.

## 참고
- SERVICE.md 14장 M0, 18장 의존성 변경
