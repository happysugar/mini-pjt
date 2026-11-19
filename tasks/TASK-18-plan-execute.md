# TASK-18 Plan-Execute: 3일 저녁 식단

- 단계: M5
- 예상 시간: 0.75h (추정)
- 선행 작업: TASK-05, TASK-15
- 관련 개념: #10 Plan-Execute
- 관련 리뷰 결정: 없음

## 목표
"이번 주 임박 재료를 다 쓰는 3일 저녁 식단 짜줘"를 계획 → 단계별 실행으로 처리한다.

## 작업 내용
- [x] 계획 단계: 임박 재료 목록 확인 → 날짜별 요리 배정 → 부족 재료 목록, Pydantic `MealPlan` 구조로 계획 생성
- [x] 실행 단계: 계획 단계를 하나씩 실행(추천 함수, 재고 조회), 결과를 모아 최종 식단과 부족 재료 목록 작성
- [x] 계획과 실행 단계를 trace에 기록
- [x] Supervisor가 식단 요청을 이 그래프로 넘기도록 연결

## 대상 파일
- `src/planner.py`, `src/agent.py`

## 완료 기준
- 시드 기준으로 3일 식단이 D-day 0~3 재료를 모두 사용하고, trace에 계획·실행 단계가 보인다.

## 참고
- SERVICE.md 6장 #10, day7 `plan_and_execute.py` 패턴

## 결과 (2026-11-19)

작업 브랜치 `task-18-plan-execute`.

### 구조 (`src/planner.py`, 새 파일)
- LangGraph `StateGraph`: `plan` → `execute`(계획 단계마다 반복) → `finish`. 이름 `meal_planner_agent`로 컴파일해 Supervisor의 네 번째 에이전트로 붙였다(`src/agent.py`).
- 계획: LLM 구조화 출력 `Plan(servings, steps: list[PlanStep])`. 단계는 `imminent`(임박 재료 확인) → `assign`(날짜별 요리 배정, day 1..N) → `shortage`(부족 재료 정리).
  요리 이름·수량·날짜는 계획에 넣지 않는다. 규칙에 어긋나거나(첫·마지막 단계, 날짜 순서, 1~7일) 모델이 실패하면 기본 계획(3일)을 쓴다(`normalize_plan`).
- 실행: 모두 Python이 결정적으로 처리한다. DB는 바꾸지 않고 재고 사본(기준 단위)에서 날마다 쓴 재료를 FEFO로 뺀다.
  - 요리 고르기 순서: 남은 임박 재료를 많이 쓰는 요리 → 임박 재료 사용 → 부족 재료가 적은 요리 → 아직 안 쓴 카테고리 → 요리명. 같은 요리는 두 번 넣지 않는다.
  - 안전 필터: 가족 전원의 알레르기·비선호(장기 메모리 Store, 없으면 시드)를 합쳐 적용한다. 값은 결과에 내보내지 않는다. 추천과 같은 `_check_recipe`·`has_allergen`을 쓴다.
  - 인분: 계획의 `servings`(사용자가 말했을 때)를 쓰고, 없으면 가족 인원 수(시드 3명)로 계산하고 그 사실을 notes에 적는다.
- 정리: Pydantic `MealPlan`(날짜별 `DayMeal`, 임박 재료·사용/미사용·남는 양, 부족 재료 `ShortageItem`, notes)과 템플릿 답변(`render_answer`). LLM은 답변을 쓰지 않는다.
- trace: 계획은 `{"agent": "meal_planner_agent", "event": "plan", "source": "llm"|"default", "steps": [...]}`, 실행 단계는 `{"event": "plan_step", "index", "of", "kind", ...}`.
  그래프가 커스텀 이벤트를 보내고 `src/api.py`의 `StepRecorder.on_custom_event`가 `trace.steps`에 남긴다.
- Supervisor 프롬프트에 "여러 날 식단 짜기 → meal_planner_agent" 라우팅을 더했다. SERVICE.md 7장 그림은 planner를 요리추천 에이전트 안에 두었지만,
  계획·실행 단계를 따로 trace에 남기려고 독립 하위 그래프로 붙였다.

### 시드 기준 결과 (`FRIDGE_TODAY=2026-11-20`)
- D-day 0~3 재료는 한우 600g(D-2, 기준표 품목 소고기) 하나다. 시금치는 이미 만료(D-1)라 빠진다.
- 가족 3명 기준 하루 3인분: 1일차 규동 → 2일차 소고기 굴소스볶음 → 3일차 계란말이. 한우를 1·2일차에 다 쓰고, 2일차에 소고기 150g이 모자란다(부족 재료).
- 하루 1인분: 규동 → 소고기 굴소스볶음 → 소고기 무국. 사흘 모두 한우를 쓰고 270g이 남는다(남는 양으로 안내).
- 지민 갑각류 알레르기와 아빠 비선호(가지 파르미자나)는 식단에 들어가지 않는다.

### 실제 Bedrock 확인 (임시 DB, `us.anthropic.claude-sonnet-4-6`: 기본 `global.` 프로필은 일일 한도)
- "이번 주 임박 재료를 다 쓰는 3일 저녁 식단 짜줘": handoff → `meal_planner_agent`, LLM 계획(5단계, servings 없음) → 실행 5단계 → 위 3인분 식단 그대로. 오류 없음.
- "2일치 저녁 식단 짜줘, 하루 2인분": LLM 계획이 2일·2인분을 읽어 4단계 → 규동, 소고기 굴소스볶음, 소고기 100g 남음.
- Supervisor가 식단 답변을 바꾸지 않고 그대로 전달했다.
- 처음 실행에서 `execute` 노드가 `AsyncSqliteStore`를 이벤트 루프에서 동기로 읽어 `InvalidStateError`가 났다. DB·Store 준비를 `asyncio.to_thread`로 옮겨 고쳤고,
  실제 `AsyncSqliteStore`를 붙인 테스트를 더했다(스레드로 넘기지 않으면 같은 오류로 실패하는 것을 확인).

### 테스트 (`tests/test_planner.py`, 15개)
- 결정적 실행: 3일·날짜, 요리 중복 없음, 임박 재료 모두 사용, 가족 인분·안전 필터, 인분 지정·부족 재료, DB 변경 없음, 답변 문장.
- 계획 검사: 올바른 LLM 계획 유지, 잘못된 계획 4가지는 기본 계획, 7일 상한.
- 그래프: 가짜 구조화 출력 모델로 계획·실행 이벤트 5단계, 모델 실패 시 기본 계획, `AsyncSqliteStore` 비선호 반영.
- Supervisor: handoff → 계획 → 실행 5단계 → 답변.
- `.venv/Scripts/python -m pytest` 242개 통과.

### 남은 점
- 임박 재료 이름이 기준표 품목명(소고기)으로 나온다. 재고 이름(한우)으로 보여 주려면 재고 사본에 원래 이름을 같이 담아야 한다.
- 식단은 계획일 뿐 재고를 차감하거나 저장하지 않는다. 식단을 저장하거나 날마다 식사 기록으로 잇는 기능은 없다.
