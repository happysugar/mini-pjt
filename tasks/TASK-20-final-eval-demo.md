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
- 목표치 (SERVICE.md 10.3, 지금 문항 59개 기준, 2026-11-20 검증 Rule 문항 12개 추가): 문항 통과 48/59(80%) 이상, guardrail 9/9, forbidden 위반 0건, 도구 일치 54/59(90%) 이상, faithfulness·answer relevancy 0.8 이상. 문항 수가 바뀌면 같은 비율로 계산한다(평가 스크립트 요약 줄의 "목표" 값)
- 미달 항목에는 원인 분석이 있다.

## 참고
- SERVICE.md 10.3, 15장 성공 기준
- 실행 준비 (2026-11-20 확인)
  - 워치 MCP 서버는 평가 스크립트가 평가 전용으로 직접 띄운다(임시 DB, 문항마다 초기화). 따로 켜거나 `HEALTH_MCP_URL`을 지정할 필요가 없다.
  - `.venv`에 ragas 0.4.4가 있어야 한다(`pip install --prefer-binary ragas==0.4.4`). 없으면 "RAGAS 방식" 대체 채점이 된다.
- 현재 결과 (리포트 초안 재료, TASK-09 후속 변경 참고)
  - `run-20261119-234433.json`: 통과 24/24, guardrail 6/6, forbidden 0, 도구 일치 24/24, faithfulness 0.916, answer relevancy 0.837
  - `retrieval-20261119-231732.json`: Precision@3 0.973, Recall@3 1.0, MRR 1.0, Hit rate 1.0
  - 리포트에 원인 분석이 필요한 항목: P02 answer relevancy 0.74(목표 미달), 실행 사이 faithfulness 편차(같은 답과 근거로 P02 0.893 → 0.733)
- 평가 문항 추가 (2026-11-20, 구현 계획 변경, SERVICE.md 5.5·5.6·10.1): 24개 → 35개.
  - 참여자 판단: E13 "엄마랑 철수랑"(가족 구성원 정보에 없는 철수는 빼고 알림). 아직 실제로 돌려 보지 않았다.
  - 혼자 먹는 추천의 칼로리 제약(2026-11-20): P02·P07의 기대 도구에 `get_calorie_budget`을 더했다(모든 추천 요청에서 호출). 아직 실제로 돌려 보지 않았다.
  - 화면 조회 버튼의 고정 메시지: P07 "오늘의 추천 요리는?", E07 "오늘 우리 가족이 같이 먹을 거 추천해줘"
  - 평가 입력 Form(JSON): E09 평가자 두 명(정상), E08 식사량 0 인분, E10 평가자 점수 빠짐, E11 식사량 합계 > 요리한 양, E12 요리한 양 형식 오류, G07 점수 9점, G08 가족이 아닌 평가자, G09 recipe 명 칸 인젝션
  - E09(평가자 두 명)는 `confirm_meal_form`으로 요청자 한 사람의 승인 후 평가자마다 저장되는지 본다. E07("우리 가족" 해석)은 모델이 `diners`를 채우는지에 달려 실행마다 결과가 다를 수 있다. 리포트에 원인으로 적는다.
  - Form 문항(E08·E10·E11·E12·G07·G08)은 API의 Form 검사가 모델 호출 없이 거절하므로 기대 도구를 비웠다(TASK-12 후속 변경).
  - 확인(`run-20261120-014712`, `014749`): E09 외 Form 문항과 G09, E07 모두 통과. E07은 앞선 실행(`run-20261120-014142`)에서는 `rank_recipes`를 부르지 않고 레시피에 없는 요리를 지어내 실패했다.
- 최신 결과 (2026-11-20, `design-review` branch, 리포트에 쓸 값)
  - 전체 35문항 `run-20261120-025638.json`: 통과 34/35, guardrail 9/9, forbidden 0, 도구 일치 34/35, faithfulness 0.94, answer relevancy 0.846. 실패한 E09는 확정 도구 강제 뒤 `run-20261120-030614.json`에서 통과(TASK-15 후속 변경).
  - 검색 `retrieval-20261120-024832.json`: Precision@3 0.96, Recall@3 1.0, MRR 0.973, Hit rate 1.0. S10만 정답이 3위(Precision·RR 0.333).
  - P02 answer relevancy는 0.751로 여전히 목표 0.8 아래다(위 원인 분석 그대로).
- 재고 입력 Form 문항 추가 (2026-11-20, SERVICE.md 5.1): 35개 → 47개. 재고 Form 11개(P08·P09·P11, N07, E14~E19, G10)와 평가 Form positive P10. 새 문항은 각각 Bedrock 평가에서 모두 통과했다(`run-20261120-033127`, `033755`, `034045`). 리포트 전에 47문항 전체를 한 번에 다시 돌려야 한다.

- 최신 결과 (2026-11-20, PR #1로 main에 merge, 리포트에 쓸 값)
  - 문항 구성 변경: 재고·평가 쓰기는 JSON Form으로만 받기로 해 자연어 쓰기 문항 11개를 negative로 바꿨다(TASK-09·12 계획 변경). 47문항 = positive 8, negative 16, edge 14, guardrail 9.
  - 전체 47문항 `run-20261120-052058.json`: 통과 47/47, guardrail 9/9, forbidden 0, 도구 일치 47/47, faithfulness 0.951, answer relevancy 0.849.
  - 검색 `retrieval-20261120-051234.json`: Precision@3 0.973, Recall@3 1.0, MRR 1.0, Hit rate 1.0. S10만 Precision 0.333(정답은 1위, 나머지 2개가 정답 아님).
  - 리포트에 원인 분석이 필요한 항목: P02 answer relevancy 0.74(목표 0.8 미달, 이전 실행 0.75~0.79).
    - 진단 (2026-11-20): 역질문이 "오늘 저녁 메뉴로 무엇을 추천해줄 수 있나요?"로 뜻은 같지만 원래 질문 "오늘 저녁 뭐 먹지?"와 말투가 달라 코사인 0.742. 답변 첫 문장을 6가지로 바꿔도 0.717~0.785라 코드는 바꾸지 않고 `evaluation/report.md` 2장에 근거를 기록했다.
- 최신 결과 (2026-11-20, 입력 Form 검증 Rule 보강, `validation-rules` branch, merge 전, 리포트에 쓸 값)
  - 문항 구성: 검증 Rule negative 12문항(N17~N28)을 더해 59문항 = positive 8, negative 28, edge 14, guardrail 9. E12(`"2 인분"`)·E18(음수 양)을 새 Rule에 맞춰 바꿨다(TASK-09·12 후속 변경).
  - 전체 59문항 `run-20261120-093823.json`: 통과 59/59, guardrail 9/9, forbidden 0, 도구 일치 59/59, faithfulness 0.951, answer relevancy 0.849(P02 0.741). 토큰 한도 오류 없음.
  - 검색 `retrieval-20261120-094041.json`: Precision@3 0.96, Recall@3 1.0, MRR 1.0, Hit rate 1.0. R06 0.667(리랭커가 규동을 3위로 남김, 이전 실행은 1.0), S10 0.333(이전과 같음).
  - `evaluation/report.md` 1장을 이 결과로 바꾸고, 검색 지표별 분석과 59문항 상세 표(faithfulness·relevancy·RAGAS 점수)를 넣었다.
- 시연 리허설 때 볼 점 (2026-11-20 변경)
  - 대표 시나리오의 식사 평가는 자연어("규동 먹었어, 4점")가 아니라 평가 입력 Form JSON으로 보낸다. 터미널 데모(`src/cli.py`)는 질문 칸에 JSON을 붙여 넣어야 한다(SERVICE.md 4장).
  - 자연어 입고·평가를 시도하면 `FORM_REQUIRED` 안내가 나온다. 이것도 안전장치 시연 장면으로 쓸 수 있다.
- 남은 일: `evaluation/report.md` 나머지(실패 문항 원인은 P02만 해당, 개선 이력), 시연 리허설. 리포트는 최신 결과 표와 P02 원인 분석까지 썼다.
