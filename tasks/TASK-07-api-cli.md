# TASK-07 FastAPI `/query`, 신원·스레드 규칙, 터미널 데모

- 단계: M2
- 예상 시간: 1h (추정)
- 선행 작업: TASK-06
- 관련 개념: 제출 규약 (POST /query), #6 가드레일, #7 HITL, #11 Trace(기본)
- 관련 리뷰 결정: R5 (D9), 리뷰 작업 T3, T4, T9

## 목표
제출 규약대로 `question`을 받아 `answer`, `contexts`, `trace` 세 키로 답하는 API와, 승인 질문에 y/n으로 답하는 터미널 데모를 만든다.

## 작업 내용
- [x] `POST /query` (`async def`, 그래프 `ainvoke`): 요청 `question`(필수), `user_id`, `thread_id`, `resume`(선택)
- [x] 신원 확인
  - `X-User-Token` ↔ `USER_TOKENS` 대조 (`hmac.compare_digest`)
  - 토큰도 `user_id`도 없으면 `DEFAULT_USER`, 그것도 없으면 `guest`(조회 전용)
  - `user_id`만 있고 토큰이 없거나 불일치하면 `IDENTITY_MISMATCH`
- [x] 스레드 소유권: `threads` 테이블, 남의 `thread_id`면 `THREAD_OWNER_MISMATCH`
- [x] 특수 상황 5종 응답 (SERVICE.md 8장 표), `trace.errors` 목록 `{code, message}`
- [x] `trace` 기본 필드: `thread_id`, `user_id`, `alerts`(D-day 0~3), `pending_interrupt`, `steps`, `rescued_g_week`, `errors`
- [x] 서버 시작(lifespan)에서 체크포인터 열기
- [x] `src/cli.py`: 질문 입력, 경고를 화면 맨 위에, 승인 질문에 y/n, 폐기 회피량을 화면 아래에 표시

## 대상 파일
- `src/api.py`, `src/cli.py`

## 완료 기준
- `uvicorn src.api:app` 실행 후 curl 두 번(질문 → `resume: approve`)으로 승인이 완료된다.
- `question`만 보낸 요청이 `dad`로 처리된다.
- CLI로 대표 시나리오 9단계를 끝까지 시연할 수 있다.

## 참고
- SERVICE.md 4장 대표 시나리오, 8장 API 계약

## 결과 (2026-11-18)
- `src/api.py`: `create_app(model=None, db_path=None)`와 모듈 수준 `app`. lifespan에서 도메인 DB가 없으면 시드로 만들고(`ensure_domain_db`), `open_agent()`로 체크포인터(`data/checkpoints.db`)를 연 에이전트를 한 번 만든다. 테스트는 가짜 모델과 임시 DB 경로를 넣는다.
- 처리 순서: 신원(`resolve_identity`) → 스레드 소유권(`claim_thread`, 첫 요청자를 `threads`에 기록) → 승인 상태(`aget_state`의 interrupt) → 에이전트 한 턴.
  - 승인 대기 중 새 질문: 승인 요약을 다시 보여주고 `INTERRUPT_PENDING`. 대기 없는데 resume: `NO_PENDING_INTERRUPT`.
  - 도구가 `GUEST_DENIED`를 돌려주면 답을 그 문구로 바꾸고 `GUEST_READ_ONLY`.
  - 8장 표에 없는 경우: 모델 호출 실패 등 예외는 `AGENT_ERROR`로 세 키 형식을 유지한다. `resume`이 approve/reject가 아니면 FastAPI 기본 422.
- `trace`: `thread_id`, `user_id`, `question`, `resume`, `alerts`, `pending_interrupt`, `steps`(도구 이름·ms, 승인 대기는 `event: interrupt`), `rescued_g_week`, `errors`. 도구 실행 기록은 콜백(`StepRecorder`)으로 모으고, `run_turn()`에 `callbacks` 인자를 추가했다.
- `contexts`: `search_recipes`·`get_recipe`·`rank_recipes`·`get_storage_days` 결과에서 `[출처#항목] 내용` 형식으로 만든다.
- `list_alerts()` 항목에 `qty`·`unit`을 추가했다(시나리오 2단계 "한우 600g" 표시용, `tests/test_tools.py` 기대값도 수정).
- `src/cli.py` (`python -m src.cli --user dad [--url ...] [--thread ...]`): 표준 라이브러리 `urllib`로 API를 부르고, `USER_TOKENS`에서 토큰을 찾아 헤더에 붙인다. 경고를 맨 위, 답변과 사용한 도구를 가운데, 폐기 회피량을 맨 아래에 표시한다. 승인 요청이 오면 y/n을 받아 같은 스레드로 resume을 보낸다.
- 검증 (가짜 모델, 임시 DB, TestClient): 23개 확인 모두 통과. 특수 상황 5종, question만 보내면 dad, 승인 전 DB 변경 없음, 승인 후 평가·식사 1회와 한우 300g, 거절 시 저장 없음, contexts의 레시피 출처를 확인했다.
- 검증 (실제 Bedrock, 임시 DB로 띄운 uvicorn, curl): (1) question만 → dad, 추천 소고기 무국 / 규동 / 소고기 라구 파스타(부족 토마토 캔), alerts에 한우 imminent, contexts 3개. (2) "규동 2인분 만들어서 1인분 먹었어, 4점" → `pending_interrupt`(meal_confirm). (3) `resume: approve` → 저장, 한우 600→300g, `rescued_g_week` 300.
- 검증 (CLI, 실제 Bedrock): 추천 → "규동 먹었어, 4점" → 인분 되묻기 → "2인분 만들어서 1인분 먹었어" → 승인 요약 → y → 저장 → 하단 폐기 회피량까지 9단계를 끝까지 진행했다.
- 참고: Windows Git Bash에서 `curl -d '{"question":"한글"}'`은 본문이 UTF-8로 가지 않아 400이 난다. 본문을 파일로 저장해 `--data-binary @q.json`으로 보내야 한다.
- 남은 점: 입력·출력 가드레일은 TASK-12, `watch_synced = 0` 재전송은 TASK-14, 상세 trace는 TASK-19.

## 후속 변경: 재고 Form·JSON Form 전용·기준표 동기화 (2026-11-20, PR #1)
- 요청 처리 순서에 두 단계를 더했다: … → 입력 가드레일 → 평가 입력 Form 검사 → 재고 입력 Form 검사(형식 + 지금 재고) → **형식 가드레일**(`guardrails.check_free_text_write`) → 에이전트.
  - 형식 가드레일은 JSON Form이 아닌 자연어 재고 입고·출고·폐기와 식사 기록·평가 요청을 모델 호출 없이 거절하고 `FORM_REQUIRED`를 남긴다(SERVICE.md 5.6, 8장).
  - 응답 `trace.guardrail`에 `format`을 더했다(걸리지 않으면 null).
- 서버 시작(lifespan) 때 `tools.sync_guide_db()`로 `item_units`에 등록된 품목 중 기준표에 빠진 것을 덧붙인 뒤 검색 인덱스를 만든다(SERVICE.md 5.1 "기준표 자동 추가").
- 터미널 데모는 그대로다. 식사 평가와 재고 쓰기는 질문 칸에 JSON Form을 붙여 넣어야 한다.
