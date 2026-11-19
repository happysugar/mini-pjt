"""냉장고 관리 메인 에이전트 그래프.

M2 단계의 단일 에이전트다. `langchain.agents.create_agent`로 만들고, 신원은
`FridgeContext` 런타임 컨텍스트로, 대화 상태는 `AsyncSqliteSaver`(`data/checkpoints.db`)로
관리한다. 승인 도구가 `interrupt()`로 멈추면 같은 `thread_id`와 `Command(resume=...)`로 재개한다.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.middleware import AgentState, after_model
from langchain_aws import ChatBedrockConverse
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.runtime import Runtime
from langgraph.types import Command

from src.tools import AGENT_TOOLS, CHECKPOINT_DB, FridgeContext, today

load_dotenv()

SYSTEM_PROMPT = """너는 가족이 함께 쓰는 냉장고를 관리하는 비서다. 한국어 존댓말로 짧고 정확하게 답한다.

오늘 날짜: {today}

규칙
- 날짜, D-day, S/N, 단위 환산, 칼로리는 직접 계산하지 않는다. 항상 도구 결과를 그대로 쓴다.
- 도구는 한 번에 하나씩 부른다.
- 요리 카테고리는 한식·일식·이탈리아식·중식만 지원한다. 다른 카테고리(멕시코식 등)의 요리나 레시피를 요청하면
  rank_recipes를 category=요청한 카테고리(예: "멕시코식")로 부른다. 답변 첫 문장에서 결과 notes의 "지원하지 않아요"
  안내를 먼저 말하고, 이어서 결과를 지금 재고로 만들 수 있는 대안으로 제시한다. 지원하지 않는 요리의 레시피는 만들지 않는다.
- "뭐 먹지", "추천해줘" 같은 요청은 다음 순서로 도구를 하나씩 부른다.
  1. list_inventory로 지금 재고와 임박 재료를 확인한다.
  2. rank_recipes로 추천을 받는다.
  3. search_recipes를 한 번 부르고, query에 추천된 요리명을 모두 넣어 레시피 출처를 확인한다.
  답변은 rank_recipes 결과의 요리·슬롯·신메뉴 여부·부족 재료를 바꾸지 말고 그대로 설명한다.
- 추천 결과의 요리 수를 세거나 여러 요리를 묶어 설명하지 않는다. 임박 재료를 쓴다는 말은 그 요리의
  imminent_ingredients에 재료가 있을 때만 하고, 비어 있는 요리에는 하지 않는다.
- d_day는 남은 날 수다. "D-2"처럼 D 뒤에 빼기 기호로 쓰고(0이면 "D-day"), "D+2"로 쓰지 않는다.
- 레시피를 알려 달라는 질문은 요리명을 알아도 search_recipes를 부른다(get_recipe는 식사 기록 전 확인용).
  search_recipes 결과에 있는 재료·양·1인분 kcal·조리 순서만 말하고 출처 파일을 밝힌다. 문서에 없는 내용을 덧붙이지 않는다.
- 보관 질문은 search_docs로 보관 가이드를 찾고, 이어서 get_storage_days로 권장 보관일수를 확인한다.
  일수는 get_storage_days 결과를, 보관 팁은 search_docs 결과를 근거로 쓰고 출처(storage_guide.md)를 밝힌다.
  가이드에 없는 품목이면 없다고 말하고 유효기간을 직접 알려 달라고 한다. 다른 품목 정보를 대신 쓰지 않는다.
- 식사 평가("~ 먹었어, 4점")는 confirm_meal을 부른다. 만든 인분과 먹은 인분을 사용자가 말하지 않았으면
  추측하지 말고 비워서 부른다. 도구가 승인을 요청하면 그 요약을 그대로 보여주고 승인 여부를 묻는다.
- 입고 문장은 add_items_from_text의 text에 사용자가 말한 문장 그대로 넘긴다. 품목·날짜를 나누거나 바꾸지 않는다.
  답변에는 결과의 투입일자, 기준 단위 수량(예: 두부 1모 → 1개), S/N, 유효기간을 그대로 적는다.
  status가 sn_invalid면 저장하지 않았다고 알리고, 이유와 suggested_sn을 보여 주며 그 S/N으로 저장할지 묻는다.
  사용자가 "제안한 S/N으로 저장해줘"처럼 동의하면, 원래 입고 문장에서 S/N만 suggested_sn으로 바꾼 문장을 text로 다시 넘긴다.
- 재고 유효기간 경고는 답변에 따로 쓰지 않는다(화면에 별도로 표시된다).
- 다른 가족의 평가 원문이나 칼로리 같은 개인 기록은 보여주지 않는다.
"""


def build_model() -> ChatBedrockConverse:
    """`.env`의 `BEDROCK_MODEL_ID`로 Bedrock 채팅 모델을 만든다."""
    return ChatBedrockConverse(
        model=os.environ["BEDROCK_MODEL_ID"],
        region_name=os.environ.get("AWS_DEFAULT_REGION"),
        temperature=0,
        max_tokens=2048,
    )


@after_model
def one_tool_call_at_a_time(state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
    """모델이 도구를 여러 개 동시에 부르면 첫 번째만 남긴다.

    Bedrock Converse에는 병렬 도구 호출을 끄는 옵션이 없어서, 승인 도구가 다른 도구와
    함께 실행되지 않도록 여기서 막는다. 응답 본문의 tool_use 블록도 같이 정리한다.
    """
    last = state["messages"][-1]
    calls = getattr(last, "tool_calls", None) or []
    if len(calls) <= 1:
        return None
    keep_id = calls[0]["id"]
    content = last.content
    if isinstance(content, list):
        content = [
            block
            for block in content
            if not (isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id") != keep_id)
        ]
    return {"messages": [last.model_copy(update={"tool_calls": calls[:1], "content": content})]}


def build_agent(checkpointer: Any = None, model: Any = None):
    """단일 냉장고 에이전트 그래프를 만든다. 테스트에서는 가짜 모델과 메모리 체크포인터를 넣는다."""
    return create_agent(
        model=model or build_model(),
        tools=AGENT_TOOLS,
        system_prompt=SYSTEM_PROMPT.format(today=today().isoformat()),
        middleware=[one_tool_call_at_a_time],
        context_schema=FridgeContext,
        checkpointer=checkpointer,
    )


@asynccontextmanager
async def open_agent(model: Any = None) -> AsyncIterator[Any]:
    """`data/checkpoints.db` 체크포인터를 연결한 에이전트를 연다."""
    CHECKPOINT_DB.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(CHECKPOINT_DB)) as saver:
        yield build_agent(checkpointer=saver, model=model)


def pending_interrupt(result: dict) -> dict | None:
    """실행 결과에서 대기 중인 승인 요청(type, summary)을 꺼낸다. 없으면 None."""
    interrupts = result.get("__interrupt__") or []
    return interrupts[0].value if interrupts else None


async def run_turn(
    agent: Any,
    question: str,
    context: FridgeContext,
    thread_id: str,
    resume: str | None = None,
    callbacks: list | None = None,
) -> dict:
    """한 턴을 실행한다. `resume`이 있으면 대기 중인 승인을 그 값으로 재개한다.

    `callbacks`는 도구 실행 기록(trace) 같은 콜백 핸들러 목록이다.
    `answer`(마지막 AI 답 또는 승인 요약), `pending_interrupt`, 원본 `result`를 돌려준다.
    """
    config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
    if callbacks:
        config["callbacks"] = callbacks
    payload: Any = Command(resume=resume) if resume else {"messages": [{"role": "user", "content": question}]}
    result = await agent.ainvoke(payload, config=config, context=context)
    pending = pending_interrupt(result)
    if pending:
        answer = f"{pending['summary']} (approve/reject)"
    else:
        answer = _text(result["messages"][-1].content)
    return {"answer": answer, "pending_interrupt": pending, "result": result}


def _text(content: Any) -> str:
    """메시지 본문(문자열 또는 블록 목록)에서 텍스트만 꺼낸다."""
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
