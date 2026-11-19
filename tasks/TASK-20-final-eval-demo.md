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
- 목표치 (SERVICE.md 10.3, 지금 문항 24개 기준): 문항 통과 20/24(80%) 이상, guardrail 6/6, forbidden 위반 0건, 도구 일치 22/24(90%) 이상, faithfulness·answer relevancy 0.8 이상. 문항 수가 바뀌면 같은 비율로 계산한다(평가 스크립트 요약 줄의 "목표" 값)
- 미달 항목에는 원인 분석이 있다.

## 참고
- SERVICE.md 10.3, 15장 성공 기준
- 실행 준비 (2026-11-20 확인)
  - 워치 MCP 서버를 켜고 `HEALTH_MCP_URL`을 지정해 실행한다. 없으면 E03이 실패하고 식사 확정에 `WATCH_SYNC_FAILED`가 남는다.
  - `.venv`에 ragas 0.4.4가 있어야 한다(`pip install --prefer-binary ragas==0.4.4`). 없으면 "RAGAS 방식" 대체 채점이 된다.
- 현재 결과 (리포트 초안 재료, TASK-09 후속 변경 참고)
  - `run-20261119-234433.json`: 통과 24/24, guardrail 6/6, forbidden 0, 도구 일치 24/24, faithfulness 0.916, answer relevancy 0.837
  - `retrieval-20261119-231732.json`: Precision@3 0.973, Recall@3 1.0, MRR 1.0, Hit rate 1.0
  - 리포트에 원인 분석이 필요한 항목: P02 answer relevancy 0.74(목표 미달), 실행 사이 faithfulness 편차(같은 답과 근거로 P02 0.893 → 0.733)
