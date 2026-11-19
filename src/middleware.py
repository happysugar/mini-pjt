"""에이전트 미들웨어 조립.

langchain 1.x 내장 미들웨어(요약, PII 마스킹, 모델·도구 재시도)를 설정만 해서 붙인다.
직접 구현하지 않는다. `build_middleware`가 목록을 만들고, M4에서는 서브 에이전트마다
이 함수를 다시 부른다(SERVICE.md 6장 #8).

M4 Supervisor는 `langgraph-supervisor`가 만든 그래프라 미들웨어를 받지 않는다. 그래서
`build_supervisor_hook`이 내장 PII 미들웨어 훅을 Supervisor의 `pre_model_hook`에서 부르고,
대화 이력 요약은 `HistoryCompactor`가 턴 시작 전에 상위 그래프 상태에 적용한다.
"""

from __future__ import annotations

from typing import Any

import botocore.exceptions
from langchain.agents.middleware import (
    AgentMiddleware,
    ModelRetryMiddleware,
    PIIMiddleware,
    SummarizationMiddleware,
    ToolRetryMiddleware,
)
from langchain_core.runnables import RunnableLambda
from langchain_core.runnables.config import var_child_runnable_config

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


def is_model_unavailable(exc: BaseException) -> bool:
    """모델 호출 실패인지 판단한다.

    서브 에이전트는 `ModelRetryMiddleware`가 `ModelUnavailableError`로 바꿔 올린다. Supervisor 모델은
    미들웨어가 없어 Bedrock SDK 재시도(botocore) 끝의 `ClientError`·`BotoCoreError`가 그대로 올라온다.
    """
    return isinstance(exc, (ModelUnavailableError, botocore.exceptions.ClientError, botocore.exceptions.BotoCoreError))


def build_supervisor_hook() -> RunnableLambda:
    """Supervisor `pre_model_hook`을 만든다. 내장 PII 미들웨어 훅으로 마지막 사용자 메시지를 가린다.

    가린 메시지는 같은 id로 바뀌어 상위 대화 이력에 그대로 남으므로, 서브 에이전트도 가려진 질문을 받는다.
    """
    pii = pii_middleware()

    def hook(state: dict) -> dict:
        """PII 훅을 차례로 적용하고 마지막 갱신을 돌려준다. 바뀐 것이 없으면 빈 갱신."""
        update: dict = {}
        for mw in pii:
            if out := mw.before_model(state, None):
                state, update = {**state, **out}, out
        return update

    return RunnableLambda(hook, name="supervisor_pre_model")


class HistoryCompactor:
    """상위 대화 이력(Supervisor 그래프 상태)을 턴 시작 전에 요약해 줄인다.

    요약 계산은 내장 `SummarizationMiddleware`가 하고, 결과(전체 삭제 + 요약 + 최근 이력)를
    `aupdate_state`로 상위 그래프에 적용한다. Supervisor·서브 에이전트는 상위 그래프 안의 하위 그래프라
    그 안의 미들웨어·훅이 이력을 지워도 상위 상태에는 전해지지 않기 때문이다(TASK-15 실험).
    """

    def __init__(
        self,
        model: Any,
        *,
        summarizer: Any = None,
        trigger_tokens: int = SUMMARY_TRIGGER_TOKENS,
        keep_tokens: int = SUMMARY_KEEP_TOKENS,
    ) -> None:
        """요약 미들웨어를 만든다. `keep_tokens`는 `trigger_tokens`보다 작아야 한다."""
        if keep_tokens >= trigger_tokens:
            raise ValueError("keep_tokens는 trigger_tokens보다 작아야 해요")
        self.summary = SummarizationMiddleware(
            model, trigger=("tokens", trigger_tokens), keep=("tokens", keep_tokens), summarizer=summarizer
        )

    async def compact(self, graph: Any, config: dict, node: str = "supervisor") -> bool:
        """스레드 이력이 임계값을 넘으면 요약해 상위 상태에 적용하고 True를 돌려준다.

        `config`의 콜백으로 요약 모델 호출이 기록된다(trace의 summary 이벤트).
        """
        snapshot = await graph.aget_state(config)
        messages = (snapshot.values or {}).get("messages") or []
        if not messages or snapshot.next:
            return False  # 새 스레드이거나 승인 대기 중
        token = var_child_runnable_config.set({"callbacks": config.get("callbacks")})
        try:
            update = await self.summary.abefore_model({"messages": messages}, None)
        finally:
            var_child_runnable_config.reset(token)
        if not update:
            return False
        await graph.aupdate_state(config, update, as_node=node)
        return True
