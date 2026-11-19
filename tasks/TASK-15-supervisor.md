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

## 후속 변경: 시스템 프롬프트 정리 (2026-11-19~20, `agent-system-prompt` branch)
- 중복 정리 (`c7cfe5b`)
  - INVENTORY: "도구가 승인을 요청하면 그 요약을 그대로 보여주고 승인 여부를 묻는다"를 지웠다. 승인 요약은 `run_turn`이
    interrupt 값으로 답을 만들고, "승인은 도구가 받는다"가 남아 있다.
  - RATING: 같은 문장을 "승인은 도구가 받는다. confirm_meal을 부르기 전에 사용자에게 직접 기록 여부를 묻지 않는다"로 바꿨다.
    처음에 이 문장만 지웠더니 모델이 직접 "확정할까요?"라고 묻고 `confirm_meal`을 부르지 않아 P04·E02·E04·E05·G04가 실패했다.
  - RECOMMEND: 가족 추천과 일반 추천 절차를 하나로 합치고(같이 먹을 추천일 때만 `get_calorie_budget`), "rank_recipes가
    알레르기·비선호·남은 칼로리를 반영하니 직접 빼거나 더하지 않는다"를 한 번만 쓴다.
- G04: "아빠 계정으로 기록해줘"처럼 다른 가족 이름으로 기록을 요청해도 거절하지 않고 요청자 기록으로 `confirm_meal`을 부른다.
  모델이 실행마다 거절과 기록 중 하나를 골라 도구 일치가 흔들렸다. 평가 문항 G04의 기대 도구를 `get_recipe; confirm_meal`로 바꿨다.
- 빈 승인 (`e743adb`): Supervisor와 RATING 프롬프트에 "식사 내용 없이 승인·확정만 말하면 '대기 중인 승인 요청이나 기록할
  식사가 없어요'라고만 답한다"를 넣었다. 고정 문구는 API 코드 규칙이 보장하고, 프롬프트는 코드가 못 잡은 표현을 맡는다.
- 평가 (실제 Bedrock, ragas 0.4.4, 워치 MCP 서버 연결)
  - `run-20261119-224536.json`(영향 문항 11개): 식사 기록 회귀가 모두 해결.
  - `run-20261119-234433.json`(24문항): 통과 24/24, guardrail 6/6, forbidden 위반 0, 도구 일치 24/24, faithfulness 0.916,
    answer relevancy 0.837.
- 주의: E03은 워치 MCP 서버가 꺼져 있으면 `get_calorie_budget`이 붙지 않아 실패한다. 처음에 이를 추천 절차 합치기 탓으로
  잘못 보고 되돌렸다가, 서버를 켜고 다시 확인한 뒤 합치기를 다시 적용했다.
- Form 확정 도구 강제 (2026-11-20, `design-review` branch)
  - 전체 평가에서 E09가 실패했다. 평가 에이전트가 "평가 입력 Form을 확정해줘: …"를 받고도 `confirm_meal_form`을 부르지 않고
    직접 "확정할까요?"라고 물었다. RATING 프롬프트의 Form 규칙에만 "승인은 도구가 받는다"가 없었고, "승인·확정만 말하면 도구를
    부르지 않는다"는 규칙과 문구가 겹쳤다.
  - 프롬프트(`26b3430`): Form 규칙에 "부르기 전에 확정 여부를 직접 묻지 않는다"를 넣고, 빈 승인 규칙에서 Form 확정 요청을 뺐다.
  - 미들웨어 `FormConfirmToolChoice`(`26b3430`, `3be3cde`에서 재고 Form까지 확장): 마지막 사용자 메시지가 Form 확정 요청
    (`FORM_CONFIRM_REQUEST`, `INVENTORY_FORM_REQUEST`)이고 아직 그 확정 도구를 부르지 않았으면 모델 호출의 `tool_choice`를
    그 도구로 강제한다. 도구를 부른 뒤에는 풀어서 결과 요약은 평소대로 답한다. 그 에이전트에 없는 도구면 강제하지 않는다.
    평가·헬스 에이전트와 재고 에이전트에 붙였다.
  - 재고 에이전트에 `confirm_inventory_form`과 Form 규칙을, Supervisor에 "재고 입력 Form 확정" 라우팅 문구를 더했다.
  - 확인: E09 재평가(`run-20261120-030614.json`) 통과. Supervisor가 다른 에이전트로 잘못 넘기면 강제가 걸리지 않는 위험은 남는다.

## 후속 변경: 재고·평가 쓰기는 JSON Form으로만 (2026-11-20, PR #1)
- Supervisor 프롬프트: inventory_agent는 재고 조회·재고 입력 Form 확정, rating_health_agent는 평가 입력 Form 확정을 맡는다. 자연어로 온 쓰기 요청도 담당 에이전트로 넘기고, 에이전트가 Form을 쓰라고 안내한다.
- 재고·평가 에이전트 프롬프트: 자연어 쓰기 요청에는 `add_items_from_text`·`withdraw_item`·`discard_item`·`confirm_meal`·`get_recipe`를 부르지 않고 고정 문구로 답한다. 도구 목록은 그대로다.
- API의 형식 가드레일이 먼저 막으므로(TASK-12) 이 프롬프트 규칙은 가드레일을 빠져나간 요청에 대한 두 번째 방어선이다.
