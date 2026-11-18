# TASK-06 단일 에이전트, 승인(HITL) 도구, 런타임 신원 주입

- 단계: M2
- 예상 시간: 1.5h (추정)
- 선행 작업: TASK-04, TASK-05
- 관련 개념: #2 ReAct, #7 HITL, #6 가드레일(신원)
- 관련 리뷰 결정: R1 (D12), R1b (D5), R7 (D11), R6 조사 (D10), 리뷰 작업 T2, T5, T15

## 목표
`create_agent` 단일 에이전트로 "구출 저녁" 시나리오를 끝까지 실행한다. 승인이 필요한 도구는 재개 시 다시 실행돼도 한 번만 저장되게 만든다.

## 작업 내용
- [x] `ChatBedrockConverse` 모델 생성 (`.env`의 모델 ID)
- [x] 도구 래핑 (`@tool`, 한국어 docstring): 입고, 출고, 재고 조회, 폐기, 레시피 조회, 추천, 칼로리 계산, 식사 확정
- [x] **런타임 신원 주입**: `user_id`, `family_id`는 런타임 컨텍스트에서 읽고 도구 인자에는 두지 않는다
- [x] **승인 도구 공통 규칙** (식사 확정, 폐기, 대량 출고 50% 초과)
  - 읽기만 해서 요약을 만든다 → 쓰기 전에 `interrupt(요약)`
  - 승인 후 한 트랜잭션으로 한 번만 저장, 멱등 키 `thread_id + tool_call_id`
  - 거절 시 도구가 정해진 문구를 돌려준다 ("취소했어요. 평가도 저장하지 않았어요")
  - 승인 도구를 가진 에이전트는 병렬 도구 호출 끔
- [x] 승인 대상 도구 이름은 `src/tools.py` 한 곳에 상수로 정의
- [x] `AsyncSqliteSaver`(`data/checkpoints.db`) 연결
- [x] **R6 조사**: P02·E03을 (a) 결정적 함수 결과 + LLM 설명, (b) LLM 구조화 출력으로 각각 실행해 규칙 위반을 비교하고 결과를 기록 → 결정 요청

## 대상 파일
- `src/agent.py`, `src/tools.py`

## 완료 기준
- CLI에서 추천 → 평가 → 인분 확인 → 승인 → 저장까지 진행된다.
- 승인 후 재고 차감과 평가 저장이 정확히 한 번 일어난다.
- 도구 스키마에 `user_id` 인자가 없다.
- R6 비교 결과가 기록되어 있다.

## 참고
- SERVICE.md 5.3 승인 구현 규칙, 6장 #2·#6·#7, `.gstack/reviews/eng-review-20261118-151021.md` R1·R1b·R7

## 결과 (2026-11-18)
- `src/tools.py`에 에이전트 도구 10개(`AGENT_TOOLS`): `add_items_from_text`, `list_inventory`, `withdraw_item`, `discard_item`, `search_recipes`, `get_recipe`, `rank_recipes`, `calc_kcal`, `get_storage_days`, `confirm_meal`. 이름은 평가 문항의 `expected_tools`와 맞췄다.
- 신원: `FridgeContext(user_id, family_id, db_path)`를 `ToolRuntime[FridgeContext]`로 주입한다. 10개 도구 스키마 어디에도 `user_id`가 없다(스키마 출력으로 확인). `guest`는 쓰기 도구가 거절한다.
- 승인 도구: `APPROVAL_TOOLS = {confirm_meal, discard_item, withdraw_item}`, 대량 출고 기준 `BULK_WITHDRAW_RATIO = 0.5`. 요약은 차감을 실제로 해 본 뒤 롤백해서 만든다(읽기 전용 미리보기). 승인 후에는 같은 함수로 한 트랜잭션 안에서 저장하고, 멱등 키를 새 테이블 `applied_actions`에 같은 트랜잭션으로 남긴다.
- 병렬 도구 호출: Bedrock Converse에 끄는 옵션이 없어, `after_model` 훅(`one_tool_call_at_a_time`)이 첫 번째 호출만 남긴다(본문의 tool_use 블록도 정리).
- `src/agent.py`: `build_agent()`, `open_agent()`(`AsyncSqliteSaver`, `data/checkpoints.db`), `run_turn()`(승인 재개는 `Command(resume=...)`).
- 검증 (가짜 모델, 임시 DB): 인분 없으면 되묻기, 승인 전 DB 변경 없음, 승인 후 평가·식사·차감 각 1회, 거절 시 0회, 대량 출고 승인 후 1회 차감, 소량 출고는 승인 없음, 병렬 호출 시 첫 도구만 실행.
- 검증 (실제 Bedrock, `us.anthropic.claude-sonnet-4-6`): 추천(소고기 무국 / 규동 / 소고기 라구 파스타, 부족 토마토 캔) → "규동 먹었어, 4점" → 인분 되묻기 → 승인 요약 "규동 평가 4점 저장, 2인분 기준 한우 300g·양파 1개 차감, dad 식사 1인분 680kcal를 워치에 기록" → 승인 → 한우 600→300g, 양파 3→2개, 폐기 회피량 300g. 턴당 1.2~5.1초. CLI(`src/cli.py`)는 TASK-07이라 이번에는 스크립트로 같은 흐름을 실행했다.
- 아직 안 된 것: 워치 기록(`record_meal`)은 TASK-14. 지금은 `watch_synced = 0`으로 남기고 "대기 중"이라고 알린다.

### R6 비교 (P02, E03 각 3회, temperature 0)
| 방식 | P02 기대 결과 일치 | E03 기대 결과 일치 | 안전 규칙 위반 | 응답 시간 |
|---|---|---|---|---|
| (a) 결정적 `rank_recipes` + LLM 설명 (현재 에이전트) | 3/3 | 3/3 | 0 | 4.6~7.3초 |
| (b) LLM 구조화 출력 (데이터·규칙을 주고 `RecommendationResult` 생성) | 0/3 | 0/3 | 0 | 8.5~19.1초 |

- 기대 결과: P02 소고기 무국 / 규동 / 신메뉴 소고기 라구 파스타(부족 토마토 캔), E03 그룹 평점 소고기 무국 + 그룹 신메뉴 소고기 라구 파스타.
- (b)는 알레르기·비선호·3점 미만·만료 재료 같은 안전 규칙은 지켰지만, 동점 정렬 1순위("임박 재료 사용")를 매번 어겼다. P02 이탈리아식은 감자 뇨끼(부족: 버터), E03 신메뉴는 표고버섯 솥밥을 골라 D-day 2 한우를 쓰지 않았다. 세 번 모두 같은 오답이라 프롬프트 우연이 아니라 규칙 적용 실패다.
- (a)는 답변 문장에도 금지 요리(갑각류, 비선호, 3점 미만, 만료 재료)나 다른 가족 개별 평점이 나오지 않았다.
- 권장: (a). 결정적 함수가 결과를 만들고 LLM은 설명만 한다. 6장 #1(LCEL + Pydantic)은 입고 파싱 체인(TASK-11)으로 충족한다. → 결정 요청 (D10)

### 다음 task에 넘기는 점
- P02 평가의 `expected_tools`는 `list_inventory; search_recipes; rank_recipes`인데, 지금 에이전트는 `rank_recipes`만 부른다. 결과는 맞지만 도구 일치 점수와 `contexts`(레시피 출처)를 위해 TASK-09에서 프롬프트나 도구 구성을 조정해야 한다.
- **결정 (2026-11-18): (a) 결정적 함수.** 추천 결과는 `rank_recipes`가 만들고 LLM은 설명만 한다. SERVICE.md 6장 #1과 tasks/README.md "남은 결정"에 반영했다.
