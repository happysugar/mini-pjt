# TASK-08 API 계약 테스트 (가짜 모델)

- 단계: M2
- 예상 시간: 0.5h (추정)
- 선행 작업: TASK-07
- 관련 개념: #6 가드레일, #7 HITL 검증
- 관련 리뷰 결정: R3 (D7), 리뷰 작업 T8

## 목표
Bedrock 없이 몇 초 만에 승인·권한 규칙을 반복 확인할 수 있게 한다. M4 Supervisor 분할 뒤에도 같은 테스트로 회귀를 잡는다.

## 작업 내용
- [ ] 정해진 도구 호출만 하는 가짜 채팅 모델로 에이전트 생성 (테스트 전용 구성)
- [ ] `with TestClient(app)` 로 lifespan 실행
- [ ] 테스트 케이스
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
