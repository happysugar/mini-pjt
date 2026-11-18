# TASK-07 FastAPI `/query`, 신원·스레드 규칙, 터미널 데모

- 단계: M2
- 예상 시간: 1h (추정)
- 선행 작업: TASK-06
- 관련 개념: 제출 규약 (POST /query), #6 가드레일, #7 HITL, #11 Trace(기본)
- 관련 리뷰 결정: R5 (D9), 리뷰 작업 T3, T4, T9

## 목표
제출 규약대로 `question`을 받아 `answer`, `contexts`, `trace` 세 키로 답하는 API와, 승인 질문에 y/n으로 답하는 터미널 데모를 만든다.

## 작업 내용
- [ ] `POST /query` (`async def`, 그래프 `ainvoke`): 요청 `question`(필수), `user_id`, `thread_id`, `resume`(선택)
- [ ] 신원 확인
  - `X-User-Token` ↔ `USER_TOKENS` 대조 (`hmac.compare_digest`)
  - 토큰도 `user_id`도 없으면 `DEFAULT_USER`, 그것도 없으면 `guest`(조회 전용)
  - `user_id`만 있고 토큰이 없거나 불일치하면 `IDENTITY_MISMATCH`
- [ ] 스레드 소유권: `threads` 테이블, 남의 `thread_id`면 `THREAD_OWNER_MISMATCH`
- [ ] 특수 상황 5종 응답 (SERVICE.md 8장 표), `trace.errors` 목록 `{code, message}`
- [ ] `trace` 기본 필드: `thread_id`, `user_id`, `alerts`(D-day 0~3), `pending_interrupt`, `steps`, `rescued_g_week`, `errors`
- [ ] 서버 시작(lifespan)에서 체크포인터 열기
- [ ] `src/cli.py`: 질문 입력, 경고를 화면 맨 위에, 승인 질문에 y/n, 폐기 회피량을 화면 아래에 표시

## 대상 파일
- `src/api.py`, `src/cli.py`

## 완료 기준
- `uvicorn src.api:app` 실행 후 curl 두 번(질문 → `resume: approve`)으로 승인이 완료된다.
- `question`만 보낸 요청이 `dad`로 처리된다.
- CLI로 대표 시나리오 9단계를 끝까지 시연할 수 있다.

## 참고
- SERVICE.md 4장 대표 시나리오, 8장 API 계약
