# TASK-19 Observability·Trace 마무리

- 단계: M6 (기본 trace 필드는 TASK-07에서 먼저 구현)
- 예상 시간: 0.5h (추정)
- 선행 작업: TASK-15
- 관련 개념: #11 Observability·Trace
- 관련 리뷰 결정: 리뷰 작업 T7 (서브 에이전트 도구 호출 수집)

## 목표
모든 실행을 파일로 남기고, 응답 `trace` 필드에서 12개 개념의 동작을 눈으로 확인할 수 있게 한다.

## 작업 내용
- [ ] 로컬 트레이서: 노드·도구·핸드오프·interrupt·MCP 호출·가드레일·요약 이벤트를 `trace.jsonl`에 기록 (서브그래프 포함)
- [ ] 응답 `trace.steps`를 같은 이벤트에서 요약해 생성
- [ ] `.env`에 LangSmith 키가 있으면 LangSmith에도 전송
- [ ] 이벤트 이름을 정리해 리포트·시연에서 개념별로 찾기 쉽게 함

## 대상 파일
- `src/tracer.py`, `src/api.py`

## 완료 기준
- 대표 시나리오 1회 실행 trace에서 개념 #2·#3·#4·#5·#6·#7·#8·#9가 각각 최소 1개 이벤트로 보인다.

## 참고
- SERVICE.md 6장 #11, day7 `local_tracer.py` 패턴
