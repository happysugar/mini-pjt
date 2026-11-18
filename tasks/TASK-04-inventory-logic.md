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
