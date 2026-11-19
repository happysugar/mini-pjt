# TASK-11 LCEL 입고 파싱 체인 (Pydantic 구조화 출력)

- 단계: M3
- 예상 시간: 0.5h (추정)
- 선행 작업: TASK-04
- 관련 개념: #1 LCEL + Pydantic
- 관련 리뷰 결정: 6장 #1 (R6 결정에 따라 추천 결과 쪽 설명은 바뀔 수 있음)

## 목표
"한우 600g, 두부 1모, 우유 1L 샀어" 같은 자연어 입고 문장을 구조화된 입고 목록으로 바꾼다.

## 작업 내용
- [x] Pydantic 모델 `InventoryInput`(품목, 수량, 단위, S/N 선택, 유효기간 선택, 상대 날짜 토큰)
- [x] `prompt | llm.with_structured_output(InventoryList) | 목록 꺼내기` 체인 (Bedrock 구조화 출력은 list를 바로 받지 않아 `items` 감싼 모델 사용)
- [x] 날짜 계산은 하지 않고 상대 토큰("어제" → `-1`)만 넘긴다
- [x] 도구 `add_items_from_text`: 체인 결과를 TASK-04의 입고 함수로 처리 (S/N 발번, 유효기간 계산, 단위 환산)

## 대상 파일
- `src/chains.py`, `src/tools.py`

## 완료 기준
- 평가 문항 P01, E01이 통과한다(S/N 형식, 유효기간, 1모→1개, 중복 S/N 안내).

## 참고
- SERVICE.md 5.1, 6장 #1

## 결과 (2026-11-19)

### 바꾼 것
- `src/chains.py`(새 파일): `PARSE_PROMPT | llm.with_structured_output(InventoryList) | _items` 체인. `parse_inventory_text(text, llm=None)`.
  프롬프트에 오늘 날짜를 넣지 않아 파서는 날짜를 계산할 수 없고, "어제" → `"-1"` 같은 상대 토큰만 넘긴다. 모델은 `.env`의 `BEDROCK_MODEL_ID`.
- `src/tools.py`
  - `InventoryInput.input_date` 설명을 상대 일수 토큰 기준으로 바꿨다.
  - 도구 `add_items_from_text(text: str)`: 인자가 `items: list[InventoryInput]`에서 사용자 문장 `text`로 바뀌었다.
    도구 안에서 파싱 체인을 돌리고, 새 함수 `add_parsed_items`가 TASK-04 `add_inventory`(S/N 발번, `resolve_date`, 유효기간, `convert_to_base`)로 입고한다.
    guest는 파싱 전에 막는다. 품목이 없으면 `NO_ITEMS_FOUND` 안내.
  - `AGENT_TOOLS`, `search_recipes`, `get_recipe`는 건드리지 않았다.
- `src/agent.py`: 입고 규칙을 "문장 그대로 text로 넘김, 투입일자·기준 단위 수량·S/N·유효기간 안내, sn_invalid면 저장 안 함을 알리고 suggested_sn 제안, 동의하면 S/N만 바꾼 문장으로 다시 호출"로 바꿨다.
- 테스트: `tests/test_chains.py`(가짜 구조화 출력 모델로 체인·입고 처리), `tests/test_api.py`에 도구 2건(체인 경유 입고, guest 차단).
  `.venv/Scripts/python -m pytest` 115개 통과.

### 중복 S/N 판단
SERVICE.md 5.1은 S/N을 `YYYY-MM-NNN` 유일 식별자로 두고 "미입력 시 자동 발번"만 정한다. 중복을 어떻게 할지는 적혀 있지 않다.
E01 기준은 "어제로 기록", "1모→1개 환산", "중복을 알리고 자동 발번 S/N 제안"이고 금지는 "중복 S/N 그대로 저장"이다.
처음에는 "쓸 수 없는 S/N은 미입력으로 보고 1턴에 자동 발번 S/N으로 저장"(`sn_replaced`)으로 구현했다.
사용자 결정에 따라 **TASK-09 제안대로 2턴을 유지**하도록 바꿨다(같은 날).
- 1턴: 저장하지 않고 `sn_invalid`, `reason`, `suggested_sn`을 돌려준다. `add_parsed_items`가 안내용 투입일자(`resolve_date`)와 기준 단위 수량(`convert_to_base`)을 덧붙인다. 답변은 중복을 알리고 제안 S/N으로 저장할지 묻는다.
- 2턴: "제안한 S/N으로 저장해줘"에 에이전트가 원래 문장의 S/N만 바꿔 `add_items_from_text`를 다시 부른다.
- 이유: S/N은 사용자가 정한 값이라 다른 번호로 바꿔 저장하는 일은 사용자 확인을 거친다. TASK-04 `add_inventory`의 `sn_invalid` 동작과도 같다.
- 이 규칙은 SERVICE.md 5.1 "중복 S/N 처리"와 9장 "S/N 직접 입력"에 반영했다. SERVICE.md 10.1과 `evaluation/test_queries.csv`의 E01 note(2턴 진행)는 TASK-09가 적은 그대로 둔다.

### 실제 Bedrock 확인 (임시 DB, `FRIDGE_TODAY=2026-11-20`)
- 체인 "한우 600g, 두부 1모, 우유 1L 샀어" → 한우 600 g / 두부 1 모 / 우유 1 L (단위는 말한 그대로, 환산은 도구가 함)
- P01 "한우 600g 넣었어" → S/N `2026-11-014`, 투입일자 2026-11-20, 600g, 유효기간 2026-11-23(guide, D-3). 답변에 모두 표시.
- E01 "어제 산 두부 1모 넣었어, S/N은 2026-11-001" → 파서 `input_date="-1"`, `sn="2026-11-001"`.
  (1턴 저장 방식일 때 확인한 값: 2026-11-015, 두부 1개, 투입일자 2026-11-19, 유효기간 2026-11-26. 2턴 방식 확인은 아래 평가 결과)
- 2턴 방식 평가 (`evaluation/run_eval.py --only P01,E01`, `run-20261119-104427.json`): P01 4/4, E01 3/3 통과, 금지 위반 0, 도구 일치 2/2.
  - E01 1턴: "저장하지 못했어요. 2026-11-001은 이미 존재하는 S/N이에요. 제안 S/N 2026-11-014로 저장할까요?" (저장 없음)
  - E01 2턴 "제안한 S/N으로 저장해줘": 에이전트가 `text="어제 산 두부 1모 넣었어, S/N은 2026-11-014"`로 다시 호출. 투입일자 2026-11-19, 1개, S/N 2026-11-014, 유효기간 2026-11-26으로 저장.
- `.venv/Scripts/python -m pytest` 129개 통과 (main의 TASK-09 테스트 포함).
