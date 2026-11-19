# TASK-09 평가 하네스와 test_queries.csv

- 단계: M2 (이후 단계마다 재실행)
- 예상 시간: 1h (추정)
- 선행 작업: TASK-07
- 관련 개념: #12 평가 (RAGAS, LLM-as-Judge)
- 관련 리뷰 결정: R10 (D15), R2 (D6), 리뷰 작업 T6, T7, T13, T14

## 목표
채점 대상인 평가를 마지막이 아니라 M2에서 먼저 만들어, 단계마다 12문항 점수를 확인한다.

## 작업 내용
- [x] `evaluation/test_queries.csv`: SERVICE.md 10.1의 12문항 그대로 (`id, category, input, expected_traits, forbidden, expected_tools, note`, 세미콜론 구분)
- [x] 평가 스크립트
  - 문항마다 `FRIDGE_TODAY` 고정, `fridge.db`·`checkpoints.db` 초기화 후 실행
  - P04는 같은 `thread_id`로 `resume: approve`까지 진행
  - `expected_tools` 이름이 등록된 도구가 아니면 즉시 실패
  - 서브 에이전트 도구 호출까지 수집, `transfer_to_*` 핸드오프는 비교에서 제외
  - LLM-as-Judge: trait 충족과 forbidden 위반 판정 (`EVAL_MODEL_ID`)
  - `ragas` faithfulness·answer relevancy (contexts 있는 5문항). 설치 실패 시 Bedrock 판정 프롬프트로 같은 지표 계산, "RAGAS 방식" 표기
- [x] 결과를 `evaluation/` 아래 실행별 결과 파일로 저장

## 대상 파일
- `evaluation/test_queries.csv`, 평가 스크립트 (`evaluation/` 아래)

## 완료 기준
- M2 시점에 12문항이 끝까지 실행되고 지표가 출력된다(통과 여부와 무관).
- 같은 코드로 두 번 실행하면 도구 일치 결과가 같다.

## 참고
- SERVICE.md 10장 평가 계획

## 결과 (2026-11-19)
- `evaluation/test_queries.csv`: SERVICE.md 10.1 표 12행을 그대로 옮겼다(콤마가 든 칸은 CSV 따옴표 처리).
- `evaluation/run_eval.py` (`.venv/Scripts/python -m evaluation.run_eval [--only P02,N01] [--compare 이전결과.json]`)
  - 문항마다 임시 폴더에 `fridge.db`를 `reset_db`로 새로 만들고, `src.agent.CHECKPOINT_DB`를 임시 경로로 바꿔 `checkpoints.db`도 새로 쓴다. `FRIDGE_TODAY=2026-11-20` 고정. `data/` 아래 DB는 건드리지 않는다.
  - `create_app(db_path=임시)`를 httpx ASGI로 직접 호출한다. note의 `user_id`로 `USER_TOKENS`에서 토큰을 찾아 헤더에 붙인다. note에 `resume=approve`가 있으면(P04) 같은 `thread_id`로 승인까지 보낸다.
  - 도구 일치: 모든 턴의 `trace.steps` 도구 이름(`transfer_to_*` 제외)에 기대 도구가 다 있으면 일치. `이름(key=value)` 인자 조건은 체크포인트 메시지의 tool_calls 인자로 확인한다(N01). 등록 도구(`AGENT_TOOLS`)에 없는 이름이면 실행과 상관없이 실패로 표시한다(전체 실행은 멈추지 않음).
  - Judge: `EVAL_MODEL_ID`, structured output으로 trait별 충족·forbidden별 위반 판정. 근거로 턴별 answer·contexts·trace와 턴별 DB 행 수를 준다.
  - RAGAS: `ragas` 0.4.4가 설치돼 있어 라이브러리로 계산(Faithfulness, ResponseRelevancy, 임베딩 `BEDROCK_EMBED_MODEL_ID`). 실패하면 같은 정의의 Bedrock 프롬프트 계산으로 바꾸고 "RAGAS 방식"으로 표기한다. ResponseRelevancy 기본 프롬프트가 역질문을 영어로 만들어 점수가 0.05 근처로 나와서, 응답과 같은 언어로 만들라는 지시를 덧붙였다.
  - 두 번째 질문: note의 `같은 thread_id로 "질문"를 보낸` 문구를 읽어 같은 스레드로 보낸다(E01).
  - Judge 근거의 DB 상태는 테이블 행 수와 마지막 S/N 재고 행(item·qty·unit·input_date)이다.
  - 결과: `evaluation/results/run-YYYYMMDD-HHMMSS.json`(요약, 문항별 턴 응답, 도구 호출과 인자, 판정 근거, RAGAS 점수).
- 실행 결과 (실제 Bedrock, 두 번 실행: `run-20261119-092438.json`, `run-20261119-092740.json`)

  | 지표 | 1차 | 2차 | 목표 |
  |---|---|---|---|
  | 문항 통과 | 9/12 | 9/12 | 10/12 |
  | guardrail | 1/2 | 1/2 | 2/2 |
  | forbidden 위반 | 0 | 0 | 0 |
  | 도구 일치 | 5/12 | 5/12 | 11/12 |
  | faithfulness (n=4) | 0.543 | 0.529 | 0.8 |
  | answer relevancy (n=4) | 0.603 | 0.532 | 0.8 |

  - 두 번 실행한 도구 일치 결과(일치 여부와 호출 도구 목록)가 12문항 모두 같다(`--compare`로 확인).
  - 실패 문항: E01(중복 S/N이라 도구가 저장을 거절해 "어제 날짜로 기록"·"1모→1개" trait 미충족), E03(칼로리 언급 없음, `get_calorie_budget` 미구현, M5 대상), G01(거절은 하지만 본인 기록 조회 제안이 없음).
  - 도구 불일치: P02(`list_inventory`·`search_recipes` 없이 `rank_recipes`만), P03(`search_recipes` 대신 `get_recipe`), P04(`calc_kcal` 없이 `confirm_meal`), E02(`get_recipe` 없이 `confirm_meal`), P05·N02(`search_docs` 미등록, TASK-10), E03(`get_calorie_budget` 미등록).
  - 이후 P04 기대 도구에서 `calc_kcal`을 뺐다. `confirm_meal`이 안에서 `calc_kcal` 함수로 섭취 kcal을 계산하므로,
    `calc_kcal` 도구를 따로 부르지 않아도 같은 결정적 계산을 거친다. 기대 도구는 `get_recipe; confirm_meal`이다.
- 남은 문제
  - answer relevancy는 문항마다 실행 간 흔들린다(P03 0.92 → 0.64). ragas 역질문 생성이 temperature 0이 아니다. 지표 비교는 평균이나 여러 번 실행으로 봐야 한다.
  - N01 relevancy가 0.00: 응답이 "지원하지 않아요"로 시작해 ragas가 noncommittal로 판정한다. 의도된 거절 답이라 지표 정의상 한계로 리포트에 적어야 한다.
  - N02는 `get_storage_days`가 가이드에 없는 품목이면 source를 안 줘 contexts가 비어 RAGAS 대상에서 빠진다(n=4). `search_docs`(TASK-10)가 생기면 채워질 것으로 본다.
  - P02 faithfulness 0.2: `rank_recipes` contexts가 요리 요약만 담고 슬롯·평점·부족 재료는 없어 답변 주장 대부분이 근거 없음으로 판정된다. `src/api.py` `extract_contexts` 수정이 필요하다(이 작업 범위 밖이라 고치지 않음).
  - P04 note의 MCP `record_meal` 호출 확인은 TASK-14 이후에 trace에서 볼 수 있다. 지금은 `rescued_g_week`와 DB 행 수만 Judge 근거로 준다.
  - 등록 도구 목록은 `AGENT_TOOLS`만 본다. 서브 에이전트·MCP 도구가 생기면(TASK-14, 15) `registered_tool_names()`와 `trace.steps` 수집 범위를 넓혀야 한다.
  - `evaluation/report.md`(지표 표, 실패 원인, 개선 이력)는 TASK-20에서 이 결과 파일로 만든다.
- E01 2턴 반영 (같은 날): SERVICE.md 10.1과 CSV의 E01 note에 2턴 진행("제안한 S/N으로 저장해줘")을 적고 스크립트가 그 질문을 보내게 했다.
  - E01: 1턴은 중복 알림과 2026-11-014 제안(저장 없음), 2턴은 2026-11-014·두부 1개·투입일 2026-11-19로 저장돼 3/3 통과.
  - 다시 두 번 실행(`run-20261119-094942.json`, `run-20261119-095251.json`): 문항 통과 10/12, guardrail 1/2, forbidden 위반 0, 도구 일치 5/12, faithfulness 0.526/0.515, answer relevancy 0.624/0.603. 두 실행의 도구 일치 결과가 같다.
- 거절이 정답인 문항의 answer relevancy 제외 (같은 날, TASK-10 세션 요청): ragas ResponseRelevancy는 회피 답이면 0을 곱해, "가이드에 없다"가 정답인 N02가 항상 0.00이 된다.
  - CSV와 SERVICE.md 10.1의 N02 note에 `answer=abstain`을 붙이고, 10.2 RAGAS 설명에 한 문장을 더했다. 문항 ID가 아니라 note 표시로 고른다.
  - 이후 N01에도 `answer=abstain`을 붙였다. 타코 레시피 요청에 대한 정답이 거절("지원하지 않아요")이라, 대안 요리를 함께 제시해도 RAGAS가 회피 답으로 판정해 두 번의 전체 평가에서 모두 relevancy 0.00이 나왔다. 거절과 대안 제시는 Judge가 채점하고 faithfulness는 계속 계산한다.
  - `run_eval.py`: 표시된 문항은 faithfulness만 계산하고 relevancy는 None(`relevancy_skipped`)으로 평균에서 뺀다. 요약에 `faithfulness_n`·`relevancy_n`을 따로 두고 표에는 "제외"로 보인다.
  - 확인: 가짜 점수로 바꿔 넣어 N02만 relevancy가 빠지는 것을 봤다(faithfulness n=3, relevancy n=2). 이 작업 폴더에는 `search_docs`가 아직 없어 실제 실행(`run-20261119-102823.json`)에서는 N02 contexts가 비어 RAGAS 대상에서 빠진다.

## 후속 변경 (2026-11-19~20, `agent-system-prompt` branch)
- ragas 설치: `.venv`에 ragas가 없어 전체 평가 한 번(`run-20261119-223343.json`)이 대체 채점("RAGAS 방식")으로 계산됐다.
  이 점수는 ragas 결과와 비교하지 않는다. Windows·Python 3.14에서는 scikit-network 최신판에 미리 빌드된 패키지가 없어
  `pip install --prefer-binary ragas==0.4.4`로 설치했다(scikit-network 0.12.1). `requirements.txt`는 바꾸지 않았다.
- 워치 MCP 서버: 서버 없이 돌리면 `get_calorie_budget`이 붙지 않아 E03이 도구 불일치로 실패한다. 전체 평가는
  `python -m src.health_mcp_server`를 켜고 `HEALTH_MCP_URL=http://127.0.0.1:8765/mcp`로 실행한다(`.env`는 바꾸지 않음).
- 평가 문항 변경 (`evaluation/test_queries.csv`)
  - G04 기대 도구: 빈 칸 → `get_recipe; confirm_meal` (다른 가족 계정 요청도 요청자 기록으로 처리하도록 정함)
  - N03 trait: "어떤 식사를 기록할지 되물음"을 지움 (고정 안내 문구 하나로 답함)
  - N01 forbidden: "응답에 '타코'라는 단어가 나옴"을 추가. 처음에 쓴 "안내 문장에 사용자가 말한 요리명(타코)을 넣음"은
    채점 모델이 "멕시코식"까지 요리명으로 읽어 맞는 답을 3회 중 2회 위반으로 판정해서 바꿨다.
- 결과 (워치 MCP 서버 연결, ragas 0.4.4, 3회 평균)

  | 결과 파일 | 문항 통과 | guardrail | forbidden | 도구 일치 | faithfulness | answer relevancy |
  |---|---|---|---|---|---|---|
  | `run-20261119-231528.json` | 23/24 | 6/6 | 0 | 24/24 | 0.963 | 0.849 |
  | `run-20261119-234433.json` | 24/24 | 6/6 | 0 | 24/24 | 0.916 | 0.837 |

  - 검색 평가 `retrieval-20261119-231732.json`(k=3, LLM 쿼리 확장·리랭킹): Precision@3 0.973, Recall@3 1.0, MRR 1.0,
    Hit rate 1.0. S10만 Precision 0.333이다(정답 외에 두부·우유 문서도 돌려줌).
  - N01 수정 뒤 N01 3회(`run-20261120-000501`, `000557`, `000914`): 모두 통과, faithfulness 0.917~0.962.
- 남은 점: P02 answer relevancy가 0.74로 목표 0.8보다 낮다. 추천 답이 요리 설명·임박 재료·출처를 길게 붙여 RAGAS의 역질문이
  원래 질문에서 멀어진다. 같은 근거로 답을 짧게 줄이면 0.84가 나온다(측정만 하고 아직 반영하지 않음).
- 워치 기록 격리 (2026-11-20, `design-review` branch): 평가가 사용자가 띄운 워치 서버(`data/health.db`)를 그대로 써서,
  평가를 돌릴 때마다 P04·E04·G04가 승인한 식사가 쌓였다. 2026-11-20 날짜로 dad 3,480kcal, mom 2,040kcal이 쌓이자
  E03이 "모두의 남은 칼로리 안에 드는 요리가 없어"로 신메뉴를 1개만 내 실패했다(`run-20261120-010100.json`).
  - `run_eval.py`가 실행마다 평가 전용 워치 서버를 임시 `HEALTH_DB`와 빈 포트로 띄우고(`watch_server()`), 문항마다
    `reset_watch_db()`로 기록을 비운다. 실행 중에만 `HEALTH_MCP_URL`을 그 주소로 바꾸고 끝나면 되돌린다. `--no-watch`로 끌 수 있다.
  - 결과 파일에 `watch_server`(어느 방식으로 돌렸는지)를 남긴다.
  - 확인: pytest 349개 통과(초기화, 임시 DB 기록, 환경 변수 복원, 종료 후 포트 닫힘 테스트 추가).
    P04·E03·E04·E06을 두 번 연속 실행(`run-20261120-011824`, `011958`): 두 번 모두 4/4 통과, E03 "세 메뉴 모두 모두의 남은
    칼로리 안", P04·E04 워치 기록 `recorded`. `data/health.db`는 바뀌지 않았다.
  - 참고: 이 확인 직전 전체 평가(`run-20261120-005629.json`)는 N06부터 8개 문항이 Bedrock 하루 토큰 한도(`ThrottlingException`)로
    실패했고, 8개를 `BEDROCK_MODEL_ID=global.anthropic.claude-sonnet-4-6`으로 다시 돌렸다(`run-20261120-010100.json`, 도구 일치 8/8).
- 전체 평가 (2026-11-20, `design-review` branch, 35문항, `run-20261120-025638.json`): 통과 34/35, guardrail 9/9, forbidden 0,
  도구 일치 34/35, faithfulness 0.94(n=5), answer relevancy 0.846(n=3). 검색 평가 `retrieval-20261120-024832.json`:
  Precision@3 0.96, Recall@3 1.0, MRR 0.973, Hit rate 1.0.
  - E09만 실패했다. 평가 에이전트가 `confirm_meal_form`을 부르지 않고 "확정할까요?"라고 직접 되물었다. 미들웨어로 확정 도구 호출을
    강제해 고쳤고(`26b3430`, TASK-15 후속 변경), 다시 돌린 `run-20261120-030614.json`에서 통과했다.
  - E13과 기대 도구를 바꾼 P02·P07은 이 실행에서 처음 돌렸고 모두 통과했다.
- 재고 입력 Form 문항 (2026-11-20, `3be3cde`): P08(입고, 빈 칸 자동 계산), P09(S/N 지정 출고), P11(모든 칸 입력 입고),
  N07(대량 출고 거절), E14(폐기 승인), E15(기준표에 없는 품목), E16(기준표 값 우선), E17(S/N 중복), E18(양 0 + 날짜 순서),
  E19(S/N과 품목 불일치), G10(별칭 칸 인젝션)을 더했다. JSON Form마다 positive 문항을 두려고 평가 입력 Form의 P10도 더했다. 전체 47문항.
  - E17·E18·E19는 API의 Form 검사가, G10은 입력 가드레일이 모델 호출 없이 거절하므로 기대 도구를 비웠다.
  - Bedrock 평가: `run-20261120-033127.json`(10문항) 10/10, `run-20261120-033755.json`(P10)·`run-20261120-034045.json`(P11) 통과.
    47문항 전체를 한 번에 돌린 평가는 아직 없다.
