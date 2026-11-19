# TASK-16 장기 메모리 (LangGraph Store)

- 단계: M5
- 예상 시간: 0.5h (추정)
- 선행 작업: TASK-15
- 관련 개념: #10 장기 메모리
- 관련 리뷰 결정: 없음 (설계 그대로)

## 목표
사용자별 선호·비선호·알레르기를 세션이 바뀌어도 기억해 추천에 반영한다.

## 작업 내용
- [x] `AsyncSqliteStore`(`data/checkpoints.db`) 연결
- [x] 네임스페이스 `("users", user_id)`에 선호·비선호·알레르기 저장·조회
- [x] 시드 사용자 정보를 Store에 초기 적재
- [x] 대화 중 "나 매운 거 싫어" 같은 발화를 선호로 저장하는 도구 (신원은 런타임 주입)
- [x] 추천 함수가 Store의 알레르기·비선호를 안전 필터에 사용

## 대상 파일
- `src/memory.py`, `src/tools.py`

## 완료 기준
- 새 `thread_id`로 시작한 다음 세션에서도 저장된 선호가 추천에 반영된다.
- 다른 사용자의 Store 항목은 읽을 수 없다.

## 참고
- SERVICE.md 6장 #10

## 결과 (2026-11-19)
- 작업 브랜치 `task-16-memory` (main `e707769` 기준).
- `src/memory.py` (새 파일)
  - `open_store(db_path, seed_users_path)`: `AsyncSqliteStore`(`data/checkpoints.db`)를 열고 `setup()` 뒤 시드 적재. 체크포인터와 같은 파일, 다른 테이블이다.
  - 네임스페이스 `("users", user_id)`, 키 `profile` 한 항목에 `likes`, `dislikes`, `allergies`, `notes`(필터에 쓰지 않는 자유 메모) 목록을 둔다.
  - `seed_store()`: `seed_users.json` 사용자 중 Store에 프로필이 없는 사용자만 적재한다. 대화로 바뀐 선호는 서버를 다시 띄워도 덮어쓰지 않는다.
  - `read_profile(store, requester, user_id)`는 다른 사용자면 `PermissionError`. 가족 합의 추천의 안전 필터만 `filter_profiles()`로 참여자의 알레르기·비선호를 읽고, 그 값은 도구 결과에 내보내지 않는다.
  - 이 모듈은 `src.tools`를 import하지 않는다(순환 import 방지). 경로는 호출하는 쪽(`src.agent`)이 넘긴다.
- `src/tools.py`
  - 새 도구 `remember_preference(kind, value)`: 요청자(런타임 컨텍스트) 네임스페이스에만 저장한다. guest는 거절. 값이 요리명·재료명(알레르기면 분류도)이면 그 목록에, 아니면 `notes`에 저장하고(예: "매운 거"), `applied` 문장으로 필터 반영 여부를 알려 준다. 알레르기는 이름을 못 찾아도 안전을 위해 알레르기 목록에 남긴다.
  - 새 도구 `get_my_preferences()`: 요청자 본인 프로필만 돌려준다.
  - `rank_recipes`: 알레르기·비선호를 `runtime.store`에서 읽는다(없으면 시드). 가족 추천은 참여자별 Store 프로필을 `recommend_family(profiles=...)`로 넘긴다.
  - 안전 필터: 비선호가 재료명이면(예: 가지) 그 재료가 든 요리도 뺀다. 요리명 비선호는 그대로.
- `src/agent.py`: Supervisor를 `compile(checkpointer, store=store)`로 만들어 서브 에이전트 도구도 같은 Store를 쓴다. `open_agent()`가 체크포인터와 Store를 함께 연다. 요리추천 에이전트에 두 도구와 규칙(취향 발화 → remember_preference, 저장된 취향 질문 → get_my_preferences)을 더하고 Supervisor 라우팅 설명에 추가했다.
- 검증
  - pytest 221개 통과(`tests/test_memory.py` 10개 추가: 시드 1회 적재와 변경 보존, 중복 저장, 다른 사용자 읽기 거절, SQLite 다시 열기 후 유지, 재료 비선호 필터, 새 thread·새 서버에서 비선호 반영, 요청자에게만 저장, 자유 메모, 가족 추천에 참여자 Store 반영, guest 거절).
  - 실제 Bedrock 확인은 못 했다. `global.anthropic.claude-sonnet-4-6`이 일일 토큰 한도(ThrottlingException: Too many tokens per day)에 걸려 첫 추천 질문 말고는 응답하지 않았다. 한도가 풀리면 "나 규동 싫어" → 새 thread "오늘 저녁 뭐 먹지?" → 서버 재시작 후 같은 질문 순서로 다시 확인한다.
- 실제 Bedrock 확인 (2026-11-19 오후, TASK-11 세션, 임시 `fridge.db`·`checkpoints.db`, `FRIDGE_TODAY=2026-11-20`, 워치 서버 없음)
  - 1단계 dad "나 규동 싫어": 확인. Supervisor → `recommend_agent` handoff 뒤 `remember_preference(kind="dislike", value="규동")`를 불렀고,
    도구 결과는 `saved_as: dislikes`, `applied: "요리로 확인돼 다음 추천부터 빼요"`. 에이전트 모델 `global.anthropic.claude-sonnet-4-6`(기본)과
    `global.anthropic.claude-sonnet-4-5-20250929-v1:0` 두 모델 모두 같은 도구 호출이 나왔다.
  - 2~5단계는 확인하지 못했다. 두 모델 모두 1단계 직후 일일 토큰 한도(`ThrottlingException: Too many tokens per day`)에 걸려 `MODEL_UNAVAILABLE`로 끝났다.
    2단계는 `list_inventory` → `rank_recipes` → `search_recipes`까지 실행됐지만 최종 답변 단계에서 막혀 추천 목록을 받지 못했다.
  - 확인 스크립트: 앱을 API로 띄워 질문을 보내고, 3단계는 같은 `checkpoints.db`로 앱을 새로 만들어 서버 재시작을 흉내 낸다.
    답변이 막혀도 도구 결과를 남기도록 `StepRecorder` 출력을 따로 모은다. 한도가 풀리면 같은 순서로 2~5단계를 다시 확인한다.
- 남은 점
  - 선호 삭제("이제 규동 괜찮아") 도구는 없다. 필요하면 `remove_preference`를 더한다.
  - `likes`는 저장만 하고 추천 순위에는 아직 쓰지 않는다(SERVICE.md 5.2 규칙에 좋아함 가중치가 없음).
  - TASK-17 가족 합의 추천이 참여자 칼로리 예산을 붙일 때도 `filter_profiles()`를 그대로 쓰면 된다.
