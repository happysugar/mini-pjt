# TASK-19 Observability·Trace 마무리

- 단계: M6 (기본 trace 필드는 TASK-07에서 먼저 구현)
- 예상 시간: 0.5h (추정)
- 선행 작업: TASK-15
- 관련 개념: #11 Observability·Trace
- 관련 리뷰 결정: 리뷰 작업 T7 (서브 에이전트 도구 호출 수집)

## 목표
모든 실행을 파일로 남기고, 응답 `trace` 필드에서 12개 개념의 동작을 눈으로 확인할 수 있게 한다.

## 작업 내용
- [x] 로컬 트레이서: 노드·도구·핸드오프·interrupt·MCP 호출·가드레일·요약 이벤트를 `trace.jsonl`에 기록 (서브그래프 포함)
- [x] 응답 `trace.steps`를 같은 이벤트에서 요약해 생성
- [x] `.env`에 LangSmith 키가 있으면 LangSmith에도 전송
- [x] 이벤트 이름을 정리해 리포트·시연에서 개념별로 찾기 쉽게 함

## 대상 파일
- `src/tracer.py`, `src/api.py`

## 완료 기준
- 대표 시나리오 1회 실행 trace에서 개념 #2·#3·#4·#5·#6·#7·#8·#9가 각각 최소 1개 이벤트로 보인다.

## 참고
- SERVICE.md 6장 #11, day7 `local_tracer.py` 패턴

## 결과 (2026-11-19)
- 작업 브랜치 `task-19-trace` (main `505f138` 기준).
- `src/tracer.py` (새 파일)
  - `RequestTracer`(LangChain 콜백): 요청 한 건의 이벤트를 모아 끝날 때 `trace.jsonl`(기본 `data/trace.jsonl`, `TRACE_FILE`로 변경)에 한 줄씩 덧붙인다. 줄마다 `ts`, `request_id`, `thread_id`, `user_id`, `seq`, `t_ms`, `event`, `concepts`를 붙인다.
  - 이벤트 이름과 개념 번호: `tool`(#2, 검색 도구 #3, 워치 조회 도구 #5), `multi_tool`(#4, 성격이 다른 도구 2종류 이상), `mcp`(#5, 코드가 부른 record_meal), `guardrail.input`·`guardrail.output`(#6), `interrupt`·`resume`(#7), `middleware`·`summary`(#8), `handoff`·`agent`(#9), `response`(#11), 그 밖의 `node`·`request`.
  - 서브그래프 이벤트에는 `agent`(예: recommend_agent)와 `ns`(checkpoint 네임스페이스)를 붙인다.
  - `configure_langsmith()`: `.env`에 `LANGSMITH_API_KEY`(또는 `LANGCHAIN_API_KEY`)가 있으면 `LANGSMITH_TRACING=true`, 프로젝트 `mini-pjt`로 LangChain 기본 트레이싱을 켠다. 서버 시작(lifespan) 때 부른다. 지금 `.env`에는 키가 없어 꺼져 있다.
- `src/api.py`: `StepRecorder`를 `RequestTracer`로 바꾸고(예전 이름은 별칭으로 남김), 요청 시작·resume·입력/출력 가드레일·재전송/식사 확정 MCP 호출·응답 끝을 같은 트레이서에 남긴다. `trace.steps`는 같은 이벤트에서 예전 형식 그대로 만들고, `trace.request_id`와 개념별 이벤트 수 `trace.concepts`(예: `{"#2 ReAct": 3, ...}`)를 더했다. 모든 응답은 `reply()`를 거치므로 신원 실패·가드레일 차단 같은 조기 응답도 기록된다.
- `tests/conftest.py`: 테스트마다 `TRACE_FILE`을 임시 파일로 바꾼다. `.gitignore`에 `/data/trace.jsonl` 추가.
- 검증
  - pytest 232개 통과(`tests/test_tracer.py` 5개 추가: 가짜 모델 대표 시나리오에서 #2~#9 이벤트, 차단 요청 기록과 질문 마스킹, 신원 실패 기록, LangSmith 키 유무, 개념 표기).
  - 실제 Bedrock 대표 시나리오 1회(추천 → 식사 평가 → 승인, 워치 서버 켬): `trace.jsonl` 106줄, 개념별 이벤트 수 #2 5, #3 1, #4 2, #5 1, #6 5, #7 2, #8 57, #9 5, #11 3. #2~#9 모두 1개 이상(완료 기준 충족). 기본 `global.anthropic.claude-sonnet-4-6`이 일일 토큰 한도에 걸려 이 실행만 같은 모델의 `us.anthropic.claude-sonnet-4-6`으로 했다(`.env`는 그대로).
- 남은 점
  - `middleware` 이벤트가 모델 호출마다 6개(PII 3종 × before/after)라 jsonl의 절반 이상을 차지한다. 리포트에서 개념별로 볼 때는 `concepts`로 거르면 되지만, 파일 크기가 문제가 되면 미들웨어 이벤트를 모델 호출당 하나로 묶는다.
  - LangSmith 전송은 키가 없어 실제로 확인하지 못했다. 키를 `.env`에 넣으면 서버 시작 때 켜진다.
  - `trace.jsonl`은 회전 없이 계속 덧붙인다. 시연 전 지우거나 `TRACE_FILE`로 실행별 파일을 지정한다.

## 후속 변경 (2026-11-20, PR #1)
- `guardrail.format`(#6) 이벤트를 더했다. 형식 가드레일이 자연어 재고·평가 쓰기 요청을 막으면 `{"code": "FORM_REQUIRED", "reasons": [...]}`를 남긴다. 응답 `trace.guardrail.format`과 같은 값이다.
