"""자연어 입고 문장을 입고 목록으로 바꾸는 LCEL 파싱 체인.

`prompt | llm.with_structured_output(...)` 체인으로 "한우 600g, 두부 1모 샀어" 같은 문장을
`InventoryInput` 목록으로 바꾼다. 체인은 문장에 나온 값을 옮기기만 하고, 날짜는 "어제" → `-1`
같은 상대 토큰으로만 넘긴다. S/N 발번, 유효기간, 단위 환산은 `src.tools`의 입고 함수가 계산한다.
"""

from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import BaseModel, Field

from src.tools import InventoryInput

load_dotenv()

PARSE_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            """너는 냉장고 입고 문장에서 품목 목록을 뽑는 파서다. 문장에 나온 값만 옮기고 아무것도 계산하지 않는다.

규칙
- 품목마다 항목 하나를 만든다. 품목명은 사용자가 말한 그대로 쓴다(예: 한우, 두부).
- qty와 unit은 사용자가 말한 그대로 쓴다. 단위를 바꾸지 않는다(1모는 qty=1, unit="모").
- 수량이나 단위를 말하지 않은 품목은 qty=1, unit="개"로 둔다.
- input_date는 상대 일수 토큰으로만 쓴다: 오늘 "0", 어제 "-1", 그제·그저께 "-2", "3일 전" "-3".
  날짜를 말하지 않았으면 비운다. 연-월-일을 모두 말했을 때만 YYYY-MM-DD로 쓴다. 날짜를 계산하지 않는다.
- expiry_date는 사용자가 유효기간을 연-월-일로 말했을 때만 YYYY-MM-DD로 쓴다. 아니면 비운다.
- sn은 사용자가 S/N(시리얼 번호)을 직접 말했을 때만 그 문자열 그대로 쓴다. 아니면 비운다.
- 입고할 품목이 없으면 빈 목록을 돌려준다.""",
        ),
        ("human", "{text}"),
    ]
)


class InventoryList(BaseModel):
    """파싱 체인의 구조화 출력. 입고 문장에 나온 품목 목록."""

    items: list[InventoryInput] = Field(default_factory=list, description="입고할 품목 목록. 문장에 나온 순서대로")


def build_parse_model() -> Any:
    """`.env`의 `BEDROCK_MODEL_ID`로 파싱용 Bedrock 채팅 모델을 만든다."""
    from langchain_aws import ChatBedrockConverse

    return ChatBedrockConverse(
        model=os.environ["BEDROCK_MODEL_ID"],
        region_name=os.environ.get("AWS_DEFAULT_REGION"),
        temperature=0,
        max_tokens=1024,
    )


def _items(result: InventoryList | dict | None) -> list[InventoryInput]:
    """구조화 출력에서 `InventoryInput` 목록만 꺼낸다. dict로 와도 검증해서 바꾼다."""
    if result is None:
        return []
    if isinstance(result, dict):
        result = InventoryList.model_validate(result)
    return list(result.items)


def build_parse_chain(llm: Any = None) -> Runnable:
    """입고 문장 → `list[InventoryInput]` LCEL 체인을 만든다. `llm`이 없으면 Bedrock 모델을 쓴다."""
    llm = llm or build_parse_model()
    return PARSE_PROMPT | llm.with_structured_output(InventoryList) | RunnableLambda(_items)


def parse_inventory_text(text: str, llm: Any = None) -> list[InventoryInput]:
    """자연어 입고 문장을 `InventoryInput` 목록으로 파싱한다."""
    return build_parse_chain(llm).invoke({"text": text})
