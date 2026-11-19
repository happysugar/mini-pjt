# TASK-08 API 계약 테스트 (가짜 모델)

- 단계: M2
- 예상 시간: 0.5h (추정)
- 선행 작업: TASK-07
- 관련 개념: #6 가드레일, #7 HITL 검증
- 관련 리뷰 결정: R3 (D7), 리뷰 작업 T8

## 목표
Bedrock 없이 몇 초 만에 승인·권한 규칙을 반복 확인할 수 있게 한다. M4 Supervisor 분할 뒤에도 같은 테스트로 회귀를 잡는다.

## 작업 내용
- [x] 정해진 도구 호출만 하는 가짜 채팅 모델로 에이전트 생성 (테스트 전용 구성)
- [x] `with TestClient(app)` 로 lifespan 실행
- [x] 테스트 케이스
  - 특수 상황 5종: `NO_PENDING_INTERRUPT`, `THREAD_OWNER_MISMATCH`, `IDENTITY_MISMATCH`, `INTERRUPT_PENDING`, `GUEST_READ_ONLY`(DEFAULT_USER 미설정 시)
  - `question`만 보낸 요청이 `DEFAULT_USER`로 처리
  - 식사 승인 → 평가·식사·차감 정확히 1회 저장
  - 식사 거절 → 아무것도 저장 안 됨, 정해진 거절 문구
  - 대량 출고 재개 → 재고 정확히 1회 차감
  - 다른 사용자 id로 도구가 실행되지 않음

## 대상 파일
- `tests/test_api.py`

## 완료 기준
- AWS 자격 증명 없이 `pytest tests/test_api.py`가 통과한다.

## 참고
- `.gstack/reviews/eng-review-test-plan-20261118-151021.md`

## 결과 (2026-11-19)
- `tests/test_api.py` 16개 통과(약 4.5초). 전체 pytest 106개 통과.
- 구성
  - 가짜 모델: `GenericFakeChatModel`을 상속하고 `bind_tools`가 자기 자신을 돌려준다. 테스트마다 도구 호출 대본을 넘긴다.
  - `create_app(model=가짜 모델, db_path=임시 fridge.db)`를 `TestClient`로 띄워 lifespan을 실행한다.
  - 체크포인트는 `src.agent.CHECKPOINT_DB`를 임시 경로로 바꿔서 `data/checkpoints.db`를 건드리지 않는다(실행 전후 `data/` 파일 수정 시각이 같음을 확인).
  - `build_model()`(Bedrock)을 부르지 않으므로 AWS 자격 증명이 없어도 돈다.
- 케이스
  - 응답 세 키, `question`만 보내면 `DEFAULT_USER`(dad), 토큰만 보내면 그 사용자
  - `IDENTITY_MISMATCH` 3가지: user_id만 있고 토큰 없음, 토큰과 user_id 불일치, 등록되지 않은 토큰
  - 식사 승인: 승인 전 저장 0, 승인 후 평가·식사 +1, 차감 +2(한우 600→300g, 양파 3→2개), `rescued_g_week` 300, 다시 승인하면 `NO_PENDING_INTERRUPT`이고 추가 저장 없음
  - 식사 거절: 저장 0, 체크포인트의 도구 결과가 `REJECT_MEAL` 문구
  - `INTERRUPT_PENDING`(대기 중 새 질문), `NO_PENDING_INTERRUPT`(대기 없이 resume)
  - `THREAD_OWNER_MISMATCH`: mom이 dad의 스레드를 승인하려 하면 차단, 저장 없음
  - 다른 사용자 id: "엄마 이름으로 저장해줘"라고 해도 평가·식사는 dad로 저장. 모든 에이전트 도구 스키마에 `user_id`·`family_id` 인자가 없음
  - `GUEST_READ_ONLY`: `DEFAULT_USER`가 없으면 guest, 쓰기 도구는 저장 없이 거절 문구
  - 대량 출고: 한우 400g(보유량의 67%)은 승인 대기, 승인 후 200g·차감 1회, 다시 승인하면 `NO_PENDING_INTERRUPT`. 100g은 승인 없이 바로 출고
  - 추천: `rank_recipes` 결과가 `contexts`에 레시피 출처로 들어가고 `trace.alerts`에 한우 임박 경고
- TASK-07 때 쓴 일회성 검증 스크립트의 내용을 이 테스트로 옮겼다.

## 후속 변경: JSON Form 전용 처리 테스트 (2026-11-20, PR #1)
- `tests/test_api.py`: 자연어 입고·출고·폐기·평가 4건이 모델 호출 없이 `FORM_REQUIRED`로 거절되고 DB가 바뀌지 않는지, 조회 질문은 그대로 에이전트에 가는지 확인한다.
- 자연어 문장으로 승인·멱등·신원 처리를 검증하던 기존 API 테스트(식사 승인·거절, 대량 출고, guest 쓰기 등)는 `allow_free_text_writes` fixture(`tests/conftest.py`)로 형식 가드레일만 끄고 그대로 둔다. 도구는 등록돼 있으므로 도구 수준 동작은 계속 검증한다.
- `tests/conftest.py`의 autouse fixture `storage_items_copy`: 테스트마다 품목 기준표 복사본을 쓰게 해 새 품목 등록이 원본에 행을 덧붙이지 않게 한다.
- pytest 452개 통과.
