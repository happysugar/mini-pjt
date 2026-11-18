# TASK-15 Supervisor 분할 (3개 서브 에이전트)

- 단계: M4
- 예상 시간: 1h (추정)
- 선행 작업: TASK-06, TASK-13, TASK-14
- 관련 개념: #9 Multi-Agent Supervisor
- 관련 리뷰 결정: 아키텍처 Shape 1, R8 (D13), R7 (D11)

## 목표
단일 에이전트를 재고·요리추천·평가/헬스 서브 에이전트로 나누고 `create_supervisor`로 연결하되, 승인 흐름과 신원 전달이 그대로 유지되게 한다.

## 작업 내용
- [ ] 서브 에이전트 3개를 `create_agent`로 생성 (자체 체크포인터 없이)
  - 재고: 입고·출고·조회·폐기
  - 요리추천: 검색·추천·칼로리, 워치 조회 MCP 도구
  - 평가/헬스: 식사 확정(승인), 평가 조회
- [ ] `create_supervisor(agents=[...], model=...)` → `AsyncSqliteSaver`로 컴파일
- [ ] 신원 컨텍스트가 서브 에이전트 도구까지 전달되는지 테스트
- [ ] 상위 대화 이력 테스트: 서브 에이전트 요약 미들웨어가 상위 이력을 지우거나 중복시키는지 확인 → 통과하면 유지, 실패하면 API에서 이력 축약으로 대체
- [ ] trace에 handoff 이벤트 기록
- [ ] 대안 준비: 서브 에이전트 안의 interrupt가 재개되지 않으면 승인 도구를 Supervisor 수준 승인 노드로 옮김

## 대상 파일
- `src/agent.py`, `src/middleware.py`

## 완료 기준
- `tests/test_api.py`와 curl 두 번 승인 재개가 분할 후에도 통과한다.
- trace에 handoff와 MCP 호출이 기록된다.

## 참고
- SERVICE.md 6장 #9, 7장 HITL과 Supervisor
