# TASK-17 가족 합의 저녁

- 단계: M5
- 예상 시간: 0.75h (추정)
- 선행 작업: TASK-05, TASK-14, TASK-16
- 관련 개념: #9 Supervisor, #10 장기 메모리, #6 가드레일(집계값만)
- 관련 리뷰 결정: 없음 (SERVICE.md 5.5 그대로)

## 목표
"오늘 저녁 엄마랑 지민이랑 같이 먹을 거 추천해줘"에 참여자 모두의 평점·알레르기·남은 칼로리를 반영해 답한다.

## 작업 내용
- [x] 요청 문장에서 참여자 추출 (요청자 자동 포함)
- [x] TASK-05의 가족 합의 규칙 함수 연결: 안전 필터 → 칼로리 제약(워치 MCP) → 출력 2개
- [x] 답변에는 개인별 평점·칼로리 수치를 넣지 않고 "모두의 남은 칼로리 안"인지만 표시
- [x] 후보가 없을 때 이유(재고, 알레르기, 비선호) 안내
- [x] 식사 확정은 요청자 본인 기록만 남김

## 대상 파일
- `src/tools.py`, `src/agent.py`

## 완료 기준
- 평가 문항 E03이 통과한다(갑각류 요리 제외, 개별 수치 비노출).

## 참고
- SERVICE.md 4장 확장 시나리오, 5.5

## 결과 (2026-11-19)
- 작업 브랜치 `task-17-family-dinner`(main `90cfc28`에서 분기). main 병합은 승인 후에 한다.
- `src/tools.py`
  - `fetch_calorie_budgets(members, caller)`: 참여자 전원의 오늘 남은 kcal을 워치 MCP `get_calorie_budget`으로 코드에서 읽는다
    (fastmcp `Client` 하나로 참여자마다 호출, 10초 제한). 값은 칼로리 제약에만 쓰고 에이전트(LLM)에는 넘기지 않는다.
    trace에는 `{tool: get_calorie_budget, via: mcp, caller: rank_recipes, members, event, ms}`만 남기고 수치는 남기지 않는다.
    워치 연동이 꺼져 있거나 한 명이라도 읽지 못하면 `None`을 돌려주고 칼로리 제약을 건너뛴다.
  - `rank_recipes`(diners 있을 때): 위 값을 `recommend_family(calorie_budgets=...)`에 넘긴다. TASK-05 규칙(안전 필터 → 칼로리 제약 →
    그룹 평점 1개 + 그룹 신메뉴 2개, 후보 없을 때 재고·알레르기·비선호 이유)이 끝까지 동작한다. 결과에는 개인별 수치 없이
    `within_calorie_budget`만 담긴다. 칼로리를 못 읽으면 notes에 "워치 칼로리 정보를 확인하지 못해 칼로리 제약 없이 골랐어요".
- `src/agent.py`(`recommend_agent`): 다른 가족과 같이 먹을 추천은 `list_inventory` → `get_calorie_budget`(본인) →
  `rank_recipes(diners=[다른 가족 이름])` → `search_recipes` 순서. 답변에는 그룹 평점 메뉴·그룹 신메뉴와 "모두의 남은 칼로리 안"
  여부만 쓰고, 누구의 칼로리 수치(본인 포함)·요리 kcal·사람별 평점도 쓰지 않는다. empty_slots·notes의 이유는 그대로 알린다.
- 이미 맞게 되어 있던 부분(테스트로 확인만): 참여자 추출(`_resolve_diners`, "지민이" → jimin, 요청자 자동 포함),
  식사 확정은 요청자 본인 기록만(`confirm_meal`이 `ctx.user_id`로만 저장). 다른 가족 칼로리는 출력 가드레일(TASK-12)도 가린다.
- 테스트: `tests/test_family_dinner.py` 7개(참여자 칼로리 조회, 워치 꺼짐·다운, 칼로리 안 추천 + 갑각류 제외 + trace에 수치 없음,
  칼로리 부족 시 가장 가벼운 요리, 워치 없을 때 안내, 다른 가족 칼로리 마스킹, 가족 식사 확정은 본인만). pytest 전체 228개 통과.
- 실제 Bedrock 평가(E03) 2/2 통과 (에이전트 `global.anthropic.claude-sonnet-4-6`, 채점 `global.anthropic.claude-sonnet-4-5-20250929-v1:0`,
  워치 MCP 서버를 임시 DB로 띄움). 결과 `evaluation/results/run-20261119-171040.json`, `run-20261119-171119.json`.
  - 두 번 모두 도구 순서 `list_inventory` → `get_calorie_budget` → `rank_recipes(diners=["엄마", "지민"])` → `search_recipes`, 도구 일치.
  - trace에 코드가 부른 워치 호출 `get_calorie_budget`(caller `rank_recipes`, ok) 1회. 수치는 trace에 남지 않는다.
  - 추천: 그룹 평점 메뉴 소고기 무국(그룹 평점 3.8) 1개, 그룹 신메뉴 소고기 굴소스볶음·소고기 라구 파스타 2개. 갑각류 요리 없음.
  - 답변은 "모두의 남은 칼로리 안에 드는 메뉴들입니다"만 말하고, 사람별 평점·남은 칼로리·요리 kcal 수치는 쓰지 않았다. 3/3 충족, forbidden 위반 0.
  - 처음 시도 때는 Sonnet 4.6·4.5가 일일 토큰 한도(`ThrottlingException`)에 걸려 미뤘다가, 한도가 풀린 뒤 확인했다.
- 최신 main(승인 도구 직접 호출, 장기 메모리 선호 정리)과 병합한 뒤 pytest 234개 통과.

## 후속 변경: 요청자 기본 포함과 "우리 가족" (2026-11-20)
- 계기: 66문항 전체 평가(`run-20261120-104648.json`)에서 E07 "오늘 우리 가족이 같이 먹을 거 추천해줘"가 실패했다. 모델이 "우리 가족"을 참여자로 풀지 못해 `rank_recipes`를 부르지 않고 누구와 먹는지 되물었다.
- 규칙(사용자 결정): 가족 구성원이 포함된 같이 먹을 추천에서는, 요청 문장이 요청자를 명확히 빼지 않는 한 요청자를 항상 포함한다.
- `src/tools.py`
  - `FAMILY_ALL_NAMES`("가족", "우리 가족", "온 가족", "다 같이" 등): `_resolve_diners`가 이 말을 가족 구성원 전원의 user_id로 바꾼다. 결정적 코드라 모델이 이름을 몰라도 전원이 참여자가 된다.
  - `rank_recipes(exclude_requester=False)`: 요청 문장이 "나는 빼고"처럼 요청자를 명확히 뺄 때만 true. true면 나머지 참여자만으로 `recommend_family`를 부르고 notes에 `REQUESTER_EXCLUDED`를 넣는다. 기본값은 false라 요청자는 언제나 들어간다. 참여자 중복은 한 번만 센다.
- `src/agent.py` 추천 에이전트 프롬프트: 이름 없이 가족 전원을 말하면 `diners=["가족"]`, 참여자를 되묻지 않고 반드시 `rank_recipes`를 부른다, `exclude_requester`는 명확히 뺄 때만 넣는다.
- 테스트: `tests/test_family_dinner.py`에 가족 전원 말 4가지와 요청자 제외/기본 포함을 더했다.
