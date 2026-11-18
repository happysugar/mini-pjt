# TASK-11 LCEL 입고 파싱 체인 (Pydantic 구조화 출력)

- 단계: M3
- 예상 시간: 0.5h (추정)
- 선행 작업: TASK-04
- 관련 개념: #1 LCEL + Pydantic
- 관련 리뷰 결정: 6장 #1 (R6 결정에 따라 추천 결과 쪽 설명은 바뀔 수 있음)

## 목표
"한우 600g, 두부 1모, 우유 1L 샀어" 같은 자연어 입고 문장을 구조화된 입고 목록으로 바꾼다.

## 작업 내용
- [ ] Pydantic 모델 `InventoryInput`(품목, 수량, 단위, S/N 선택, 유효기간 선택, 상대 날짜 토큰)
- [ ] `prompt | llm.with_structured_output(list[InventoryInput])` 체인
- [ ] 날짜 계산은 하지 않고 상대 토큰("어제" → `-1`)만 넘긴다
- [ ] 도구 `add_items_from_text`: 체인 결과를 TASK-04의 입고 함수로 처리 (S/N 발번, 유효기간 계산, 단위 환산)

## 대상 파일
- `src/chains.py`, `src/tools.py`

## 완료 기준
- 평가 문항 P01, E01이 통과한다(S/N 형식, 유효기간, 1모→1개, 중복 S/N 안내).

## 참고
- SERVICE.md 5.1, 6장 #1
