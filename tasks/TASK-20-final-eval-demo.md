# TASK-20 최종 평가, 리포트, 시연 리허설

- 단계: M6
- 예상 시간: 0.75h (추정)
- 선행 작업: TASK-09, TASK-19 (가능한 모든 task)
- 관련 개념: #12 평가, 전체 시연
- 관련 리뷰 결정: R10 (D15)

## 목표
최종 코드로 평가를 실행해 리포트를 확정하고, 대표 시나리오를 끊김 없이 시연할 수 있게 한다.

## 작업 내용
- [ ] `FRIDGE_TODAY=2026-11-20`으로 평가 문항 전체 최종 실행
- [ ] `evaluation/report.md` 작성
  - 지표 표: 문항 통과, guardrail, forbidden 위반, 도구 일치, faithfulness, answer relevancy (ragas 대안 사용 시 "RAGAS 방식" 표기)
  - 목표치 대비 결과, 실패 문항별 원인, 단계별 개선 이력
- [ ] 시연 리허설: CLI 대표 시나리오 9단계, curl 두 번 승인, 가족 합의 저녁, 3일 식단, 공격 질의
- [ ] SERVICE.md와 실제 구현이 다른 곳 정리
- [ ] `pytest` 전체 통과 확인

## 대상 파일
- `evaluation/report.md`, `SERVICE.md`(필요 시 수정)

## 완료 기준
- 목표치: 문항 통과 10/12 이상, guardrail 2/2, forbidden 위반 0건, 도구 일치 11/12 이상, faithfulness·answer relevancy 0.8 이상
- 미달 항목에는 원인 분석이 있다.

## 참고
- SERVICE.md 10.3, 15장 성공 기준
