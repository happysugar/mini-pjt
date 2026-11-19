"""에이전트 미들웨어 조립.

langchain 1.x 내장 미들웨어(요약, PII 마스킹, 모델·도구 재시도)를 설정만 해서 붙인다.
직접 구현하지 않는다. `build_middleware`가 목록을 만들고, M4에서는 서브 에이전트마다
이 함수를 다시 부른다(SERVICE.md 6장 #8).
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import (
    AgentMiddleware,
    ModelRetryMiddleware,
    PIIMiddleware,
    SummarizationMiddleware,
    ToolRetryMiddleware,
)

# 한국 휴대전화·지역번호 전화. 숫자·하이픈이 앞뒤로 붙어 있으면 잡지 않아 S/N(`2026-11-001`)과
# 날짜(`2026-11-20`)와 겹치지 않는다. 모든 번호가 0으로 시작한다.
KR_PHONE_PATTERN = r"(?<![\d-])0(?:1[016789]|2|[3-6][1-5]|70)[-. ]?\d{3,4}[-. ]?\d{4}(?![\d-])"
# 주민등록번호: 생년월일 6자리 - 성별 1자리 + 6자리.
KR_RRN_PATTERN = r"(?<![\d-])\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])-?[1-8]\d{6}(?![\d-])"

# 요약 미들웨어는 근사 토큰 수나 마지막 AI 응답의 실제 입력 토큰(`usage_metadata.total_tokens`)이 이 값을
# 넘으면 요약한다. 실제 입력에는 시스템 프롬프트와 도구 스키마 약 5,300토큰이 늘 들어가므로(TASK-13 측정)
# 그보다 충분히 크게 둔다. 작으면 요약 직후에도 넘어서 모델 호출마다 다시 요약한다.
SUMMARY_TRIGGER_TOKENS = 16000
SUMMARY_KEEP_TOKENS = 2000  # 요약 뒤에도 그대로 남기는 최근 이력 토큰 수. 임계값보다 작아야 매 호출 재요약을 피한다
MAX_RETRIES = 2  # 첫 시도 뒤 재시도 횟수 (지수 백오프 1초, 2초)


class ModelUnavailableError(RuntimeError):
    """모델 호출이 재시도 끝에도 실패했다. API는 `trace.errors`에 `MODEL_UNAVAILABLE`로 남긴다."""


def _raise_unavailable(exc: Exception) -> str:
    """모델 재시도가 모두 실패하면 `ModelUnavailableError`로 바꿔 올린다."""
    raise ModelUnavailableError(f"{type(exc).__name__}: {exc}") from exc


def pii_middleware() -> list[AgentMiddleware]:
    """모델 입력(마지막 사용자 메시지)의 이메일·한국 전화번호·주민번호를 `[REDACTED_...]`로 가린다."""
    return [
        PIIMiddleware("email", strategy="redact", apply_to_input=True),
        PIIMiddleware("kr_phone", detector=KR_PHONE_PATTERN, strategy="redact", apply_to_input=True),
        PIIMiddleware("kr_rrn", detector=KR_RRN_PATTERN, strategy="redact", apply_to_input=True),
    ]


def build_middleware(
    model: Any,
    *,
    summarize: bool = True,
    summarizer: Any = None,
    trigger_tokens: int = SUMMARY_TRIGGER_TOKENS,
    keep_tokens: int = SUMMARY_KEEP_TOKENS,
    max_retries: int = MAX_RETRIES,
    initial_delay: float = 1.0,
) -> list[AgentMiddleware]:
    """에이전트에 붙일 내장 미들웨어 목록을 만든다.

    순서: PII 마스킹 → 요약 → 모델 재시도 → 도구 재시도.
    - `model`: 요약에 쓸 채팅 모델(보통 에이전트 모델). `summarizer`를 주면 그것으로 요약한다.
    - `keep_tokens`는 `trigger_tokens`보다 작게 둔다. 크면 요약 직후에도 임계값을 넘어 호출마다 다시 요약한다.
    - `summarize=False`: 요약 없이 마스킹·재시도만 붙인다(M4 서브 에이전트용).
    - 모델 호출이 재시도 끝에도 실패하면 `ModelUnavailableError`를 올린다.
    - 도구 재시도는 승인 대기(interrupt)는 다시 실행하지 않고, 최종 실패는 오류 문구로 모델에 돌려준다.
    """
    if keep_tokens >= trigger_tokens:
        raise ValueError("keep_tokens는 trigger_tokens보다 작아야 해요")
    middleware: list[AgentMiddleware] = pii_middleware()
    if summarize:
        middleware.append(
            SummarizationMiddleware(
                model,
                trigger=("tokens", trigger_tokens),
                keep=("tokens", keep_tokens),
                summarizer=summarizer,
            )
        )
    middleware.append(
        ModelRetryMiddleware(max_retries=max_retries, on_failure=_raise_unavailable, initial_delay=initial_delay)
    )
    middleware.append(ToolRetryMiddleware(max_retries=max_retries, on_failure="continue", initial_delay=initial_delay))
    return middleware
