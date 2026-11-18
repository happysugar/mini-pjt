# TASK-09 평가 하네스와 test_queries.csv

- 단계: M2 (이후 단계마다 재실행)
- 예상 시간: 1h (추정)
- 선행 작업: TASK-07
- 관련 개념: #12 평가 (RAGAS, LLM-as-Judge)
- 관련 리뷰 결정: R10 (D15), R2 (D6), 리뷰 작업 T6, T7, T13, T14

## 목표
채점 대상인 평가를 마지막이 아니라 M2에서 먼저 만들어, 단계마다 12문항 점수를 확인한다.

## 작업 내용
- [ ] `evaluation/test_queries.csv`: SERVICE.md 10.1의 12문항 그대로 (`id, category, input, expected_traits, forbidden, expected_tools, note`, 세미콜론 구분)
- [ ] 평가 스크립트
  - 문항마다 `FRIDGE_TODAY` 고정, `fridge.db`·`checkpoints.db` 초기화 후 실행
  - P04는 같은 `thread_id`로 `resume: approve`까지 진행
  - `expected_tools` 이름이 등록된 도구가 아니면 즉시 실패
  - 서브 에이전트 도구 호출까지 수집, `transfer_to_*` 핸드오프는 비교에서 제외
  - LLM-as-Judge: trait 충족과 forbidden 위반 판정 (`EVAL_MODEL_ID`)
  - `ragas` faithfulness·answer relevancy (contexts 있는 5문항). 설치 실패 시 Bedrock 판정 프롬프트로 같은 지표 계산, "RAGAS 방식" 표기
- [ ] 결과를 `evaluation/` 아래 실행별 결과 파일로 저장

## 대상 파일
- `evaluation/test_queries.csv`, 평가 스크립트 (`evaluation/` 아래)

## 완료 기준
- M2 시점에 12문항이 끝까지 실행되고 지표가 출력된다(통과 여부와 무관).
- 같은 코드로 두 번 실행하면 도구 일치 결과가 같다.

## 참고
- SERVICE.md 10장 평가 계획
