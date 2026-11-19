# TASK-15 Supervisor 분할 (3개 서브 에이전트)

- 단계: M4
- 예상 시간: 1h (추정)
- 선행 작업: TASK-06, TASK-13, TASK-14
- 관련 개념: #9 Multi-Agent Supervisor
- 관련 리뷰 결정: 아키텍처 Shape 1, R8 (D13), R7 (D11)

## 목표
단일 에이전트를 재고·요리추천·평가/헬스 서브 에이전트로 나누고 `create_supervisor`로 연결하되, 승인 흐름과 신원 전달이 그대로 유지되게 한다.

## 작업 내용
- [x] 서브 에이전트 3개를 `create_agent`로 생성 (자체 체크포인터 없이)
  - 재고: 입고·출고·조회·폐기
  - 요리추천: 검색·추천·칼로리, 워치 조회 MCP 도구
  - 평가/헬스: 식사 확정(승인), 평가 조회
- [x] `create_supervisor(agents=[...], model=...)` → `AsyncSqliteSaver`로 컴파일
- [x] 신원 컨텍스트가 서브 에이전트 도구까지 전달되는지 테스트
- [x] 상위 대화 이력 테스트: 서브 에이전트 요약 미들웨어가 상위 이력을 지우거나 중복시키는지 확인 → 통과하면 유지, 실패하면 API에서 이력 축약으로 대체
- [x] trace에 handoff 이벤트 기록
- [x] 대안 준비: 서브 에이전트 안의 interrupt가 재개되지 않으면 승인 도구를 Supervisor 수준 승인 노드로 옮김 (재개가 동작해 대안은 쓰지 않음)

## 대상 파일
- `src/agent.py`, `src/middleware.py`

## 완료 기준
- `tests/test_api.py`와 curl 두 번 승인 재개가 분할 후에도 통과한다.
- trace에 handoff와 MCP 호출이 기록된다.

## 참고
- SERVICE.md 6장 #9, 7장 HITL과 Supervisor

## 결과 (2026-11-19)

### 구조 (`src/agent.py`)
- 서브 에이전트 3개를 `create_agent`로 만들고 자체 체크포인터는 두지 않는다. 프롬프트는 공통 규칙(`COMMON_RULES`) + 에이전트별 규칙으로 나눴다.
  - `inventory_agent`: `add_items_from_text`, `list_inventory`, `withdraw_item`(승인), `discard_item`(승인)
  - `recommend_agent`: `list_inventory`, `rank_recipes`, `search_recipes`, `search_docs`, `get_recipe`, `get_storage_days`, `calc_kcal` + 워치 MCP 조회 도구(`get_daily_intake`, `get_calorie_budget`)
  - `rating_health_agent`: `get_recipe`, `confirm_meal`(승인, 코드에서 MCP `record_meal`), `calc_kcal`
  - 평가 조회 전용 도구는 아직 없어 평가·헬스 에이전트는 식사 확정만 맡는다.
- `create_supervisor(agents, model, prompt=SUPERVISOR_PROMPT, pre_model_hook, post_model_hook, context_schema=FridgeContext, output_mode="full_history")`를 `AsyncSqliteSaver`로 컴파일한다(`build_agent`).
  - `full_history`: 서브 에이전트의 도구 호출·결과가 상위 이력에 남아 기존 테스트·평가(`tool_calls`, contexts)가 그대로 동작한다.
  - Supervisor는 에이전트 답을 그대로 최종 답으로 쓴다(프롬프트). 핸드오프는 한 번에 하나만 남긴다(`post_model_hook`).
- 승인: 서브 에이전트 안의 `interrupt()`가 상위 체크포인트에 저장되고 같은 `thread_id` + `Command(resume=...)`로 재개됐다. Supervisor 수준 승인 노드(대안)는 필요 없었다.
- 신원: `FridgeContext`가 서브 에이전트 도구까지 전달된다. `tests/test_api.py`의 신원·guest·스레드 소유권 테스트와 워치 MCP `inject_requester`가 분할 뒤에도 통과한다.

### 미들웨어 (`src/middleware.py`)
- 서브 에이전트: `build_middleware(model, summarize=False)` → PII 마스킹 + 모델·도구 재시도.
- Supervisor: `langgraph-supervisor`가 만든 그래프라 미들웨어를 받지 않는다. `build_supervisor_hook()`이 내장 PII 미들웨어 훅을 `pre_model_hook`에서 불러 마지막 질문을 가린다(같은 id로 바뀌어 상위 이력에 반영).
- Supervisor 모델 오류는 재시도 미들웨어가 없어 Bedrock SDK 재시도 끝의 `botocore` 오류로 올라온다. `is_model_unavailable`로 API가 이것도 `MODEL_UNAVAILABLE`로 표시한다.

### 상위 대화 이력 테스트 → 서브 에이전트 요약 탈락, API(턴 시작 전) 이력 축약으로 대체
- 실험(가짜 모델, 4턴, 임계값 낮춤):
  - A. 서브 에이전트에 요약 미들웨어: 상위 이력 40개로 줄지 않고 요약 메시지 4개가 쌓였다. 서브 에이전트는 상위 그래프 안의 하위 그래프라 그 안의 `RemoveMessage`가 상위 상태로 전해지지 않는다. → 실패
  - Supervisor `pre_model_hook`에서 요약해도 같은 이유로 상위 이력이 줄지 않았다. 또 서브 에이전트가 답하고 돌아온 직후 요약하면 Supervisor가 전달할 그 답까지 요약에 묻혔다.
  - B. 턴 시작 전 상위 그래프 상태를 요약(`HistoryCompactor`): 상위 이력 13개, 요약 1개, 최근 질문만 원문으로 남았고 메시지 id 중복 없음. → 채택
- `HistoryCompactor.compact`: 내장 `SummarizationMiddleware`(16000/2000토큰)로 요약을 계산하고 `aupdate_state(..., as_node="supervisor")`로 적용한다. `run_turn`이 새 질문일 때만 부르고, 승인 대기 중이면 건드리지 않는다. 요약 모델 호출은 trace의 `{"event": "summary"}`로 남는다.

### trace (`src/api.py`)
- `transfer_to_*` 도구 시작을 `{"agent": "supervisor", "event": "handoff", "to": "<에이전트>"}`로 남긴다. 예: P04 1턴 `handoff → rating_health_agent`, `get_recipe`, `confirm_meal`(interrupt).
- MCP 호출은 TASK-14대로 `{"tool": "record_meal", "via": "mcp", ...}`로 붙는다.

### 테스트
- `tests/test_api.py`: 테스트 본문과 대본은 그대로 두고 가짜 모델만 Supervisor를 알게 바꿨다. Supervisor 자리에서는 다음 대본 도구를 가진 에이전트로 핸드오프하고, 돌아오면 에이전트 답을 그대로 돌려준다.
  바꾼 단언은 두 곳: 도구 순서 단언에서 handoff 단계를 거르고(`if "tool" in s`), 추천 테스트에 handoff 단언을 더했다. `tool_outputs` 도우미는 핸드오프 결과를 뺀다.
- `tests/test_supervisor.py`(새 파일): 에이전트별 도구 분리, 서브 에이전트 안 식사 승인 1회 저장, 인사는 handoff 없음, 상위 이력 요약 1번·중복 없음, 승인 대기 중 요약 안 함.
- `tests/test_middleware.py`: 요약은 `HistoryCompactor`, 재시도 실패는 서브 에이전트(`AgentFailingModel`)와 Supervisor Bedrock 오류 두 경우로 나눴다.
- `.venv/Scripts/python -m pytest` 208개 통과.

### 실제 Bedrock 확인 (임시 DB, `FRIDGE_TODAY=2026-11-20`)
- 기본 모델(`BEDROCK_MODEL_ID`)과 `global.anthropic.claude-sonnet-4-6`은 하루 토큰 한도(`ThrottlingException`)에 걸려 모든 문항이 `MODEL_UNAVAILABLE`로 끝났다(Supervisor 모델 오류 표시 확인).
  그래서 에이전트 모델을 `BEDROCK_MODEL_ID=global.anthropic.claude-sonnet-4-5-20250929-v1:0`으로 덮어써 돌렸다.
- 12문항(`run-20261119-145959.json`): 통과 11/12(목표 10), guardrail 2/2, 금지 위반 0, 도구 일치 10/12, faithfulness 0.936, answer relevancy 0.872.
  - E02 통과(기준 4/4)했지만 도구 불일치(`get_recipe` 없이 `confirm_meal`). E03은 `get_calorie_budget` 미호출로 실패(M5 대상, 이전과 같음).
  - 이 실행은 워치 MCP 서버를 켜지 않아 P04 승인 뒤 `record_meal`이 연결 실패로 기록되고 `WATCH_SYNC_FAILED`가 남았다.
- 워치 MCP 서버(`python -m src.health_mcp_server`)를 켜고 P04·E03(`run-20261119-150125.json`): P04 통과.
  1턴(승인 요청): `handoff → rating_health_agent`, `get_recipe`, `confirm_meal`(interrupt). 2턴(`resume=approve`, 같은 thread_id): `confirm_meal`, `record_meal`(`via: mcp`, `event: recorded`), 답 "…워치에도 기록했어요."
  API 요청 두 번으로 승인 재개가 동작함을 확인했다(curl 두 번과 같은 경로).
