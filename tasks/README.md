# 구현 작업 목록 (냉장고 관리 Agent)

SERVICE.md(APPROVED)와 엔지니어링 리뷰 결정(`.gstack/reviews/eng-review-20261118-151021.md`)을 task 단위로 나눈 목록입니다. 각 파일에 목표, 작업 내용 체크리스트, 대상 파일, 완료 기준이 있습니다.

## 목록

| Task | 단계 | 내용 | 예상 | 선행 | 개념 |
|---|---|---|---|---|---|
| [TASK-01](TASK-01-env-check.md) | M0 | 환경 점검 | 0.25h | — | 전체 |
| [TASK-02](TASK-02-data-authoring.md) | M1 | 레시피·보관 가이드·시드 작성 | 1.5h | — | #3, #12 |
| [TASK-03](TASK-03-db-schema-seed.md) | M1 | DB 스키마와 시드 로더 | 0.5h | 01, 02 | #4 |
| [TASK-04](TASK-04-inventory-logic.md) | M1 | 재고 결정적 로직 + pytest | 1.5h | 03 | #2, #4 |
| [TASK-05](TASK-05-recommendation-logic.md) | M1 | 추천 결정적 로직 + pytest | 1h | 02, 03 | #4, #6 |
| [TASK-06](TASK-06-single-agent-hitl.md) | M2 | 단일 에이전트, 승인 도구, 신원 주입, R6 조사 | 1.5h | 04, 05 | #2, #6, #7 |
| [TASK-07](TASK-07-api-cli.md) | M2 | FastAPI `/query`, 신원·스레드 규칙, CLI | 1h | 06 | #6, #7, #11 |
| [TASK-08](TASK-08-api-contract-tests.md) | M2 | API 계약 테스트 (가짜 모델) | 0.5h | 07 | #6, #7 |
| [TASK-09](TASK-09-eval-harness.md) | M2 | 평가 하네스와 test_queries.csv | 1h | 07 | #12 |
| [TASK-10](TASK-10-rag-retriever.md) | M3 | 하이브리드 RAG, LLM 리랭킹, 쿼리 확장 | 1h | 01, 02 | #3 |
| [TASK-11](TASK-11-lcel-parse-chain.md) | M3 | LCEL 입고 파싱 체인 | 0.5h | 04 | #1 |
| [TASK-12](TASK-12-guardrails.md) | M3 | 가드레일 | 0.75h | 07 | #6 |
| [TASK-13](TASK-13-middleware.md) | M3 | 미들웨어 조립 | 0.25h | 06 | #8 |
| [TASK-14](TASK-14-health-mcp.md) | M4 | 헬스케어 MCP 서버와 워치 기록 | 1h | 06, 07 | #4, #5 |
| [TASK-15](TASK-15-supervisor.md) | M4 | Supervisor 분할 | 1h | 06, 13, 14 | #9 |
| [TASK-16](TASK-16-long-term-memory.md) | M5 | 장기 메모리 (Store) | 0.5h | 15 | #10 |
| [TASK-17](TASK-17-family-dinner.md) | M5 | 가족 합의 저녁 | 0.75h | 05, 14, 16 | #6, #9, #10 |
| [TASK-18](TASK-18-plan-execute.md) | M5 | Plan-Execute 3일 식단 | 0.75h | 05, 15 | #10 |
| [TASK-19](TASK-19-trace.md) | M6 | Trace 마무리 | 0.5h | 15 | #11 |
| [TASK-20](TASK-20-final-eval-demo.md) | M6 | 최종 평가, 리포트, 시연 리허설 | 0.75h | 09, 19 | #12 |

## 일정

- 예상 합계는 **약 16.5시간**입니다(모두 추정치). 예산 14시간보다 2.5시간 많습니다.
- TASK-02(데이터 작성 1.5h)는 TASK-01·03~05와 병렬로 진행할 수 있어, 순차 기준 실제 소요는 약 15시간입니다.
- 범위는 줄이지 않기로 결정했으므로(결정 c95bb12c), 아래 점검 시점에서 진행 상황을 확인합니다.

| 점검 시점 | 끝나 있어야 할 task | 의미 |
|---|---|---|
| 6시간 | TASK-01~09 | CLI 시연, API 승인, 평가 하네스가 동작하는 최소 제출본 |
| 10시간 | TASK-10~15 | RAG·가드레일·MCP·Supervisor까지 완성 |
| 마지막 2시간 | TASK-19~20 | 기능 추가 없이 평가·리포트·리허설만 |

## 남은 결정

- 남은 결정 없음. **R6 (D10)**은 2026-11-18에 (a) 결정적 함수로 확정했습니다. 추천 결과는 `rank_recipes`(결정적 함수)가 만들고 LLM은 설명만 합니다. 근거는 [TASK-06](TASK-06-single-agent-hitl.md)의 비교 표에 있습니다.

## 구현 계획 변경 (2026-11-20)

- 화면 입력 방식(조회 버튼 고정 메시지, recipe 평가 결과 JSON 입력 Form)을 SERVICE.md 5.6에 정리했습니다. 평가 문항은 34개로 늘었습니다(TASK-20 참고).
- 권한 관리, 재고 조정 화면, 가족 구성원 관리, 레시피 문서 관리는 이번 구현 범위에서 제외합니다(SERVICE.md 5.7). 이 기능들의 task는 만들지 않았습니다.
- 평가 입력 Form은 API가 먼저 검사하고, 요청자 한 사람의 승인으로 평가자마다 저장합니다(2026-11-20 결정). 가족은 하나뿐입니다. 남은 결정은 "우리 가족"을 코드에서 해석할지와 guest의 평가 입력 권한입니다(SERVICE.md 17장 열린 질문 2·3).
