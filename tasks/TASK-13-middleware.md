# TASK-13 미들웨어 조립 (요약·마스킹·재시도)

- 단계: M3
- 예상 시간: 0.25h (추정)
- 선행 작업: TASK-06
- 관련 개념: #8 미들웨어
- 관련 리뷰 결정: R8 (D13), 리뷰 작업 T12

## 목표
langchain 1.x 내장 미들웨어를 조립해 에이전트에 붙인다. 직접 구현하지 않는다.

## 작업 내용
- [ ] `SummarizationMiddleware`: M2 단일 에이전트에 부착, 토큰 임계값 설정
- [ ] `PIIMiddleware`: 내장 탐지기 + 한국 전화번호·주민번호 정규식 (모델 입력 마스킹)
- [ ] `ModelRetryMiddleware`, `ToolRetryMiddleware`: 지수 백오프. 최종 실패 시 `trace.errors`에 `MODEL_UNAVAILABLE`
- [ ] 미들웨어 목록을 만드는 함수 하나로 정리 (M4에서 서브 에이전트별로 재사용)

## 대상 파일
- `src/middleware.py`, `src/agent.py`

## 완료 기준
- 긴 대화에서 trace에 요약 이벤트가 남는다.
- 모델 입력에 전화번호가 마스킹되어 들어간다.

## 참고
- SERVICE.md 6장 #8
