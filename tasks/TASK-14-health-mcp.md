# TASK-14 헬스케어 MCP 서버와 워치 기록

- 단계: M4
- 예상 시간: 1h (추정)
- 선행 작업: TASK-06, TASK-07
- 관련 개념: #5 MCP, #4 도구 다중 (외부 API 역할)
- 관련 리뷰 결정: R9 (D14), 리뷰 작업 T11

## 목표
Galaxy Watch 헬스케어 모듈을 흉내 내는 MCP 서버를 만들고, 식사 기록을 순서·중복 없이 보낸다.

## 작업 내용
- [ ] FastMCP 서버: `record_meal`(멱등 키 `meal_id`), `get_daily_intake`, `get_calorie_budget`. 날짜는 `FRIDGE_TODAY` 공유
- [ ] 에이전트에는 조회 도구(`get_calorie_budget`, `get_daily_intake`)만 `langchain-mcp-adapters`로 연결
- [ ] 식사 확정 도구가 DB 커밋 후 **코드에서** MCP 클라이언트로 `record_meal` 호출, 성공 시 `watch_synced = 1`
- [ ] 실패 시 `WATCH_SYNC_FAILED` 안내, 같은 사용자의 다음 `/query` 시작 때 미동기화 식사를 같은 키로 재전송
- [ ] MCP 호출을 trace에 기록 (P04에서 확인)

## 대상 파일
- `src/health_mcp_server.py`, `src/tools.py`, `src/api.py`

## 완료 기준
- P04 승인 후 trace에 `record_meal` 호출이 1회 기록된다.
- MCP 서버를 끈 상태에서 식사 확정 → `watch_synced = 0` → 서버를 켜고 다음 요청에서 한 번만 재전송된다.

## 참고
- SERVICE.md 5.3 실행 순서, 6장 #5
