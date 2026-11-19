# TASK-14 헬스케어 MCP 서버와 워치 기록

- 단계: M4
- 예상 시간: 1h (추정)
- 선행 작업: TASK-06, TASK-07
- 관련 개념: #5 MCP, #4 도구 다중 (외부 API 역할)
- 관련 리뷰 결정: R9 (D14), 리뷰 작업 T11

## 목표
Galaxy Watch 헬스케어 모듈을 흉내 내는 MCP 서버를 만들고, 식사 기록을 순서·중복 없이 보낸다.

## 작업 내용
- [x] FastMCP 서버: `record_meal`(멱등 키 `meal_id`), `get_daily_intake`, `get_calorie_budget`. 날짜는 `FRIDGE_TODAY` 공유
- [x] 에이전트에는 조회 도구(`get_calorie_budget`, `get_daily_intake`)만 `langchain-mcp-adapters`로 연결
- [x] 식사 확정 도구가 DB 커밋 후 **코드에서** MCP 클라이언트로 `record_meal` 호출, 성공 시 `watch_synced = 1`
- [x] 실패 시 `WATCH_SYNC_FAILED` 안내, 같은 사용자의 다음 `/query` 시작 때 미동기화 식사를 같은 키로 재전송
- [x] MCP 호출을 trace에 기록 (P04에서 확인)

## 대상 파일
- `src/health_mcp_server.py`, `src/tools.py`, `src/api.py`

## 완료 기준
- P04 승인 후 trace에 `record_meal` 호출이 1회 기록된다.
- MCP 서버를 끈 상태에서 식사 확정 → `watch_synced = 0` → 서버를 켜고 다음 요청에서 한 번만 재전송된다.

## 참고
- SERVICE.md 5.3 실행 순서, 6장 #5

## 결과 (2026-11-19)
- `src/health_mcp_server.py` (새 파일, `python -m src.health_mcp_server`, 기본 `http://127.0.0.1:8765/mcp`)
  - FastMCP 도구 `record_meal(meal_id, user_id, dish, kcal, eaten_at)`, `get_daily_intake(user_id)`, `get_calorie_budget(user_id)`.
  - 기록은 별도 SQLite(`HEALTH_DB`, 기본 `data/health.db`)의 `watch_meals`. `meal_id`가 기본 키라 같은 키는 `duplicate`로 돌려주고 다시 쓰지 않는다.
  - 오늘 날짜는 `src.tools.today()`(같은 `FRIDGE_TODAY`). 하루 목표 kcal은 mock 프로필(dad 2400, mom 1900, jimin 2000, 그 외 2000).
- `src/tools.py`
  - `meals.meal_id` 열과 고유 인덱스 추가. 값은 `confirm_meal` 멱등 키(`thread_id:tool_call_id`)라 평가처럼 도메인 DB를 새로 만들어도 워치 쪽 키가 겹치지 않는다. 기존 DB는 `connect()`에서 열을 더한다.
  - `confirm_meal`: 커밋 뒤 코드에서 `sync_meals_to_watch()`로 `record_meal`을 부른다(fastmcp `Client`, 10초 제한). 성공(recorded/duplicate)이면 `watch_synced = 1`, 실패면 0으로 두고 결과 문구에 "워치 기록 실패, 다음 요청 때 다시 시도할게요". kcal이 없으면 보내지 않는다.
  - 호출 기록은 `FridgeContext.mcp_calls`에 `{tool: record_meal, via: mcp, caller, meal_id, event, ms}`로 쌓는다. `HEALTH_MCP_URL`이 없으면 워치 연동을 끄고 예전처럼 대기 문구만 남긴다.
- `src/api.py`: 신원·스레드 확인 직후 `resend_unsynced_meals()`로 그 사용자의 `watch_synced = 0` 식사를 같은 키로 다시 보낸다(`caller: resend`). trace.steps는 재전송 → 도구 호출 → 식사 확정의 MCP 호출 순서다. 실패가 있으면 `WATCH_SYNC_FAILED`, 답변에 실패 안내가 없으면 덧붙인다.
- `src/agent.py`: `load_health_tools()`가 `langchain-mcp-adapters`(`MultiServerMCPClient`, streamable HTTP)로 `get_daily_intake`·`get_calorie_budget`만 불러온다. 인터셉터 `inject_requester`가 `user_id`를 런타임 컨텍스트의 요청자로 덮어쓰고, 모델에게 보이는 스키마에서는 `user_id`를 뺀다(남의 기록 조회 차단). 서버에 연결하지 못하면 경고만 남기고 조회 도구 없이 뜬다.
- `.env`에 `HEALTH_MCP_URL=http://127.0.0.1:8765/mcp` 추가. 테스트는 `tests/conftest.py`에서 이 값을 지우고, 필요한 테스트만 같은 프로세스의 서버를 `set_health_target()`으로 넣는다.
- `evaluation/run_eval.py`: 등록 도구에 워치 조회 도구를 더하고, 코드가 부른 MCP 호출(`via: mcp`)은 도구 일치 비교에서 뺀다.
- 검증
  - pytest 159개 통과(`tests/test_health_mcp.py` 8개 추가: 멱등, 남은 칼로리, 인터셉터, 스키마, 승인 후 1회 기록, 서버 꺼짐 → 재전송 1회, kcal 없는 식사 미전송).
  - 실제 Bedrock + 별도 프로세스 서버: P04 승인 → trace에 `record_meal`(caller confirm_meal, recorded) 1회, `watch_synced = 1`. "오늘 내 남은 칼로리" → `get_calorie_budget`로 2400/680/1720. "엄마 칼로리" → 도구 없이 거절.
  - 서버를 끈 채 식사 확정 → `record_meal` error, `WATCH_SYNC_FAILED`, `watch_synced = 0` → 서버를 켜고 다음 요청에서 `caller: resend` 1회 recorded, 그다음 요청은 재전송 없음. 워치 DB에도 한 건만 남았다.
  - 평가 `run-20261119-140034.json`: P04 3/3 통과, 도구 일치, 승인 턴 trace에 `record_meal` 1회와 `rescued_g_week` 300.
- 남은 점
  - E03은 아직 `get_calorie_budget`을 부르지 않는다. 가족 저녁 추천에서 참여자별 남은 칼로리를 `rank_recipes(calorie_budgets=...)`로 넘기는 연결은 TASK-17에서 한다. 지금 조회 도구는 요청자 본인 것만 읽는다.
  - 워치 조회 도구는 서버 시작 때 한 번 불러온다. API를 먼저 띄우고 워치 서버를 나중에 켜면 API를 다시 시작해야 조회 도구가 붙는다(`record_meal` 동기화는 요청마다 연결하므로 영향 없음).
  - M4 Supervisor 분할(TASK-15) 뒤에도 `FridgeContext.mcp_calls`가 서브 에이전트까지 같은 객체로 전달되는지 확인이 필요하다.
