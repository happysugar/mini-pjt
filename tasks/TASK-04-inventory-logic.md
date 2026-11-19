# TASK-04 재고 결정적 로직과 pytest

- 단계: M1
- 예상 시간: 1.5h (추정)
- 선행 작업: TASK-03
- 관련 개념: #2 ReAct 도구의 본체, #4 도구 다중 (계산기)
- 관련 리뷰 결정: 아키텍처 red flag 5 (차감 함수 하나로 통일)

## 목표
LLM이 계산하지 않도록 재고 관련 규칙을 모두 Python 함수로 만들고 테스트로 고정한다.

## 작업 내용
- [x] S/N 발번 `YYYY-MM-NNN` (투입일자의 연-월 + 월별 순번), 직접 입력 S/N의 형식·중복 검사
- [x] 품목명 매칭: 정확 일치 → 별칭 일치. 표에 없는 품목은 첫 등록 단위를 `item_units`에 기록
- [x] 단위 환산: `kg→g`, `ml→L`, `모·팩·봉·알→개`, 기준 단위가 다르면 개당 평균 무게로 환산, 불가하면 "재고 단위 불일치"
- [x] 유효기간 기본값: `투입일자 + 권장 보관일수`, 없으면 `null` + `unknown`
- [x] 상대 날짜 변환 ("어제" → `-1` → 날짜)
- [x] D-day와 알람: 0~3 `imminent`, 음수 `expired`, 4 이상·미상은 없음
- [x] 차감 함수 하나 `deduct_fefo()`: 유효기간 빠른 순 차감, 부족분 보고, 0이면 `consumed`, `deductions` 기록(차감 시점 D-day, g 환산)
- [x] 폐기 회피량 `rescued_g_week()`: D-day 0~3 차감분, 가족 합계, 월요일 시작 주, L=1000g, 개=평균 무게
- [x] 칼로리 계산: 먹은 인분 × 1인분 kcal
- [x] `tests/test_tools.py`

## 대상 파일
- `src/tools.py`, `tests/test_tools.py`

## 완료 기준 (pytest, `FRIDGE_TODAY` 고정)
- S/N 형식과 월별 순번, 중복 S/N 거부
- 유효기간 기본값과 미상 처리
- D-day 경계: 3 → imminent, 0 → imminent, -1 → expired, 4 → 없음
- `1모`→`1개`, 기준 단위 불일치, 표에 없는 품목의 첫 등록 단위 고정
- 재고 부족 시 부분 차감과 부족분 보고
- 폐기 회피량 (L, 개, 평균 무게 없는 품목 제외)

## 참고
- SERVICE.md 5.1, 5.4

## 결과 (2026-11-18)
- `src/tools.py`에 추가: 보관 가이드 파서 `load_storage_guide()`, 품목명 매칭 `resolve_item()`·`item_key()`, 단위 `normalize_unit()`·`convert_to_base()`·`to_grams()`·`UnitMismatchError`, 날짜 `resolve_date()`·`default_expiry()`·`d_day()`·`alert_level()`·`list_alerts()`, S/N `next_sn()`·`check_sn()`, 입고 `add_inventory()`, 차감 `deduct_fefo()`, 폐기 회피량 `week_start()`·`rescued_g_week()`, `calc_kcal()`.
- 규칙 해석
  - 품목명 비교는 공백을 무시한다("파르메산치즈" = "파르메산 치즈"). 재고에는 사용자가 쓴 이름(예: 한우)을 그대로 저장하고, 같은 품목인지는 가이드 품목명으로 비교한다.
  - S/N 순번은 그 달에 쓴 가장 큰 번호 + 1. 소진·폐기된 재고의 번호도 다시 쓰지 않는다.
  - `deduct_fefo()`는 출고·식사·폐기가 함께 쓰는 유일한 차감 함수다. 유효기간 미상 재고는 FEFO 순서 맨 뒤. 폐기(`reason="discard"`)로 0이 되면 `discarded`, 그 외는 `consumed`.
  - `add_inventory()`와 `deduct_fefo()`는 커밋하지 않는다. 식사 확정처럼 여러 쓰기를 한 트랜잭션으로 묶을 수 있게, 커밋은 호출한 쪽이 한다.
  - 입고 중 단위 불일치나 S/N 오류가 나면 저장하지 않고 상태(`unit_mismatch`, `sn_invalid`)와 제안값을 돌려준다. 되묻기는 에이전트가 한다.
- 검증: `tests/test_tools.py` 61개 통과 (`tests/test_recommend.py` 포함 전체 88개 통과). 테스트는 임시 DB만 쓴다.
- 함께 작업한 점: TASK-05가 다른 세션에서 동시에 진행돼 같은 파일 아래쪽에 추천 로직이 붙었다. 그 세션이 이 task의 함수를 재사용했고 겹치는 함수는 없다.

## 후속 변경: 재고 입력 Form (2026-11-20, `design-review` branch, `3be3cde`)
- 입고·출고·폐기를 화면의 고정된 JSON 재고 입력 Form 하나로도 받는다(SERVICE.md 5.1). 요청 종류는 `status`(`stored` 입고, `consumed` 출고, `discarded` 폐기)로 정하고, 비운 칸은 지금 규칙대로 계산한다(S/N 자동 발번, 투입일자 오늘, 유효기간 = 투입일자 + 권장 보관일수).
- `src/tools.py`에 추가
  - `check_inventory_form_stock()`: 지금 재고와 맞춰 보는 검사. 입고 S/N 중복(자동 발번 S/N 제안), 기준 단위 환산, 출고·폐기 S/N이 보관 중 재고인지와 품목이 맞는지, 재고가 있는지, 출고량 ≤ 보유량.
  - `save_inventory_form()`과 `inventory_forms` 테이블: 검사를 통과한 Form을 스레드별로 하나 보관한다.
  - `store_from_form()`: Form 입고. 기준표 품목은 기준표 값을 쓰고 Form 값이 다르면 `notes`로 알린다. 기준표에 없는 품목은 Form의 별칭·개당 평균 무게·권장 보관일수를 `item_units`의 새 열에 저장하고, 유효기간이 비면 그 보관일수로 계산한다(출처 `user`).
  - `item_profile()`: 기준표에 없는 품목의 기준을 품목명 → 별칭 순으로 찾는다. `stock_rows()`, `base_unit_of()`, `convert_to_base()`가 기준표 다음으로 이 값을 쓴다.
  - 확정 도구 `confirm_inventory_form`(인자 없음): 입고는 승인 없이 저장, 출고는 보유량 50% 초과면 승인, 폐기는 항상 승인. 거절하면 보관한 Form을 지운다.
- 기존 함수 변경
  - `deduct_fefo(..., sn=None)`: `sn`을 주면 그 재고 한 건에서만 차감한다(Form 출고). 주지 않으면 지금처럼 FEFO.
  - 폐기 쓰기를 `apply_discard()`, 승인 요약 문구를 `describe_lots()`로 빼서 `discard_item`과 `confirm_inventory_form`이 함께 쓴다.
  - 기존 DB는 열 때 `inventory_forms` 테이블과 `item_units` 열을 더한다(`_migrate_inventory_forms`).
- 자유 문장 입고·출고·폐기(`add_items_from_text`, `withdraw_item`, `discard_item`)는 그대로 둔다.
- 확인: pytest 414개 통과(`tests/test_api.py` 재고 Form 흐름 11개 추가). Bedrock 평가 P08·P09·P11·N07·E14~E19·G10 모두 통과.

## 계획 변경: 기준표 자동 추가 (2026-11-20, 구현 전)
- `item_units`에 새 품목을 등록하려면 권장 보관일수가 반드시 양의 정수이고, 개당 평균 무게는 값이 있으면 양수여야 한다(SERVICE.md 5.1 "새 품목 등록 조건"). 등록하면 그 품목을 `storage_items.csv`에 덧붙이고 검색 인덱스에도 넣는다("기준표 자동 추가"). 파일·인덱스 쓰기는 TASK-10의 함수를 쓴다.
- 할 일 (`src/tools.py`)
  - [ ] `item_units`에 새 행을 넣는 곳을 함수 하나로 모으고, 그 함수가 등록 조건(보관일수 양의 정수, 평균 무게 없음 또는 양수)을 검사한다. 조건에 맞지 않으면 등록도 재고 저장도 하지 않는다.
  - [ ] 자유 문장 입고(`add_inventory`·`add_items_from_text`): 기준표에 없는 새 품목이면 저장하지 않고 권장 보관일수가 필요하다는 상태(예: `storage_days_required`)를 돌려준다. 에이전트가 되묻고, 받은 값으로 다시 부른다.
  - [ ] Form 입고(`check_inventory_form_stock`): 기준표에 없는 새 품목인데 `storageDays`가 비면 `FORM_INVALID`로 거절한다.
  - [ ] 등록 직후(재고 DB 커밋 뒤) 기준표 추가를 부른다.
- 양은 절대값으로 바꿔 양수로만 다룬다(SERVICE.md 5.1, 2026-11-20 계획 변경). 예: `-2` → `2`. 절대값이 0이면 거절한다.
  - [ ] Form 검사(`check_inventory_form`): 음수 `qty`를 "양은 0보다 큰 숫자여야 해요"로 거절하지 않고 절대값으로 바꿔 정규화한 Form에 담는다. 0은 지금처럼 거절한다.
  - [ ] 자유 문장 입고·출고와 식사 차감의 양도 같은 규칙(절대값)으로 맞춘다.
  - [ ] 기준표에 추가된 품목은 이후 기준표 품목으로 다룬다. Form 값보다 기준표 값이 우선이고(다르면 `notes`), 기준표에서 비어 있는 값만 Form 값으로 채운다.
  - [ ] 추가 뒤 기준표 캐시(`guide_info` 등 `lru_cache`)를 비운다. 파일 쓰기가 실패해도 재고 저장은 되돌리지 않는다.
  - [ ] 서버 시작 때 `item_units`에서 보관일수가 있는데 기준표에 없는 품목을 다시 덧붙인다.
  - [ ] Form 검사(`src/guardrails.py`): 기준표가 별칭을 쉼표로 나누므로 쉼표가 든 별칭을 거절한다.
- 바뀌는 동작: 지금은 표에 없는 품목을 유효기간 미상(`expiry_source = unknown`)으로 저장하지만, 앞으로는 보관일수를 받기 전에는 저장하지 않는다. `unknown` 재고는 이 변경 전 데이터(시드의 피클 재고)에만 남는다.
- 바뀌지 않는 것: `item_units` 행은 지우지 않는다.
