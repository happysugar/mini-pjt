# TASK-13 미들웨어 조립 (요약·마스킹·재시도)

- 단계: M3
- 예상 시간: 0.25h (추정)
- 선행 작업: TASK-06
- 관련 개념: #8 미들웨어
- 관련 리뷰 결정: R8 (D13), 리뷰 작업 T12

## 목표
langchain 1.x 내장 미들웨어를 조립해 에이전트에 붙인다. 직접 구현하지 않는다.

## 작업 내용
- [x] `SummarizationMiddleware`: M2 단일 에이전트에 부착, 토큰 임계값 설정
- [x] `PIIMiddleware`: 내장 탐지기 + 한국 전화번호·주민번호 정규식 (모델 입력 마스킹)
- [x] `ModelRetryMiddleware`, `ToolRetryMiddleware`: 지수 백오프. 최종 실패 시 `trace.errors`에 `MODEL_UNAVAILABLE`
- [x] 미들웨어 목록을 만드는 함수 하나로 정리 (M4에서 서브 에이전트별로 재사용)

## 대상 파일
- `src/middleware.py`, `src/agent.py`

## 완료 기준
- 긴 대화에서 trace에 요약 이벤트가 남는다.
- 모델 입력에 전화번호가 마스킹되어 들어간다.

## 참고
- SERVICE.md 6장 #8

## 결과 (2026-11-19)

### 바꾼 것
- `src/middleware.py`(새 파일): `build_middleware(model, *, summarize=True, summarizer=None, trigger_tokens, keep_tokens, max_retries, initial_delay)`가 내장 미들웨어 목록을 만든다. 직접 구현한 미들웨어는 없다.
  - 순서: PII 마스킹 → 요약 → 모델 재시도 → 도구 재시도. M4 서브 에이전트는 `summarize=False`로 같은 함수를 부르면 된다.
  - `PIIMiddleware`: 내장 `email` + 정규식 `kr_phone`(`KR_PHONE_PATTERN`), `kr_rrn`(`KR_RRN_PATTERN`). 마지막 사용자 메시지를 `[REDACTED_KR_PHONE]`처럼 바꿔 모델에 넘기고, 체크포인트에도 가린 값으로 남는다.
    앞뒤에 숫자·하이픈이 붙으면 잡지 않아 S/N(`2026-11-001`)과 날짜(`2026-11-20`)는 가리지 않는다.
  - `SummarizationMiddleware`: `trigger=("tokens", 16000)`, `keep=("tokens", 2000)`. 요약 모델은 에이전트 모델(Bedrock).
  - `ModelRetryMiddleware`: 재시도 2번, 지수 백오프(1초, 2초, jitter). 최종 실패면 `ModelUnavailableError`를 올린다.
  - `ToolRetryMiddleware`: 재시도 2번, 최종 실패는 오류 문구로 모델에 돌려준다. 승인 대기(interrupt)는 다시 실행하지 않는다(내장 동작).
- `src/agent.py`: `build_agent(..., middleware=None)`. 없으면 `build_middleware(model)`을 붙이고 그 뒤에 기존 `one_tool_call_at_a_time`을 둔다.
- `src/api.py`(적용 지점): `ModelUnavailableError`를 잡아 `trace.errors`에 `MODEL_UNAVAILABLE`, 답변 `MSG_MODEL_UNAVAILABLE`.
  `StepRecorder.on_chat_model_start`가 요약 모델 호출(`metadata.lc_source == "summarization"`)을 보면 `trace.steps`에 `{"event": "summary"}`를 남긴다.
- 테스트 `tests/test_middleware.py`(가짜 모델 17건): 전화·주민번호 정규식과 S/N·날짜 비탐지, 모델 입력의 전화번호·이메일 마스킹, 긴 대화의 summary 이벤트와 요약 뒤 입력, 짧은 대화엔 요약 없음, 모델 3회 실패 시 `MODEL_UNAVAILABLE`.

### 요약 임계값을 16000으로 둔 이유
- 처음 6000으로 두었더니 실제 Bedrock에서 2번째 턴부터 **모델 호출마다 요약**했다.
- 원인: 내장 요약 미들웨어는 근사 토큰 수 외에, 마지막 AI 응답의 `usage_metadata.total_tokens`(Bedrock 실제 입력)가 임계값을 넘어도 요약한다.
  실제 입력에는 시스템 프롬프트와 도구 스키마가 늘 들어가 첫 호출부터 약 5,300토큰이다(측정). 그래서 6000은 금방 넘고 요약 뒤에도 계속 넘는다.
- 그래서 실제 입력 기준 16000으로 올리고, 남기는 이력을 메시지 수 대신 토큰(2000)으로 바꿨다. `keep_tokens >= trigger_tokens`면 `ValueError`.

### 실제 Bedrock 확인 (임시 DB, `FRIDGE_TODAY=2026-11-20`)
- 12턴 대화(재고, 레시피, 추천, 보관 기간 질문 반복): 5번째, 8번째 턴에서 `trace.steps`에 `{"event": "summary"}`가 한 번씩 남았다. 요약 뒤 상태 첫 메시지는 "Here is a summary of the conversation to date: …"다.
- 첫 질문 "엄마 전화번호 010-1234-5678도 같이 기억해줘…"의 번호는 모델 입력과 체크포인트 어디에도 남지 않았다(`[REDACTED_KR_PHONE]`).
- 12문항 평가(`run-20261119-141228.json`): 통과 9/12, 금지 위반 0, 도구 일치 10/12, faithfulness 0.911, answer relevancy 0.788.
  - E02 실패(confirm_meal을 부르지 않고 칼로리를 먼저 물음)는 미들웨어를 끈 상태로 두 번 더 돌려도 같아서 TASK-13 때문이 아니다. 이전 12문항 실행(`run-20261119-095251.json`) 뒤 main에 들어온 변경의 영향으로 보이며 따로 살펴봐야 한다.
  - E03(`get_calorie_budget` 미구현, M5 대상), G01은 이전 실행과 같은 실패다.
- `.venv/Scripts/python -m pytest` 167개 통과.
