"""Plan-Execute 그래프: 임박 재료를 다 쓰는 여러 날 저녁 식단 (SERVICE.md 6장 #10).

계획(plan) → 실행(execute, 단계마다 반복) → 정리(finish) 순서의 LangGraph 그래프다.
- 계획: LLM이 사용자 요청을 보고 단계 목록(`Plan`)을 구조화 출력으로 만든다. 규칙에 맞지 않거나
  모델이 실패하면 기본 계획(임박 재료 확인 → 날짜별 요리 배정 → 부족 재료)을 쓴다.
- 실행: 단계를 하나씩 Python이 결정적으로 처리한다(재고 조회, 안전 필터를 거친 추천 후보, 가상 차감).
  남은 임박 재료를 쓰는 요리를 먼저 배정해, 계획한 날짜 안에 임박 재료를 다 쓰도록 한다.
- 정리: Pydantic `MealPlan`과 답변 문장을 만든다. 날짜·수량·부족 재료 계산은 LLM이 하지 않는다.
계획과 실행 단계는 커스텀 이벤트(`plan`, `plan_step`)로 내보내 API trace에 남긴다.
"""

from __future__ import annotations

import asyncio
import copy
import sqlite3
from datetime import timedelta
from typing import Annotated, Any, Literal

from langchain_core.callbacks.manager import adispatch_custom_event
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from src import memory
from src.tools import (
    FridgeContext,
    UnitMismatchError,
    UnitUnknownError,
    _check_recipe,
    alert_level,
    connect,
    convert_to_base,
    has_allergen,
    item_key,
    parse_recipes,
    seed_profile,
    today,
    usable_stock,
)

PLANNER_AGENT = "meal_planner_agent"
DEFAULT_DAYS = 3
MAX_DAYS = 7

PLAN_PROMPT = """너는 냉장고 식단 계획 단계를 만든다. 사용자 요청을 보고 실행할 단계 목록만 만든다.
요리 이름, 수량, 날짜는 정하지 않는다(실행 단계에서 도구가 계산한다).

규칙
- 첫 단계는 kind="imminent"(임박 재료 확인)다.
- 그다음 날짜마다 kind="assign" 단계를 하나씩 둔다. day는 1부터 시작한다. 사용자가 날 수를 말하지 않으면 3일이다.
- 마지막 단계는 kind="shortage"(부족 재료 정리)다.
- servings: 사용자가 하루 몇 인분인지 말했을 때만 숫자로 넣는다. 말하지 않았으면 비운다.
- goal: 단계마다 무엇을 하는지 한국어 한 문장.

사용자 요청: {request}"""


class PlanStep(BaseModel):
    """계획 한 단계."""

    kind: Literal["imminent", "assign", "shortage"] = Field(description="imminent=임박 재료 확인, assign=그날 요리 배정, shortage=부족 재료 정리")
    day: int | None = Field(default=None, description="assign 단계의 날짜 번호(1부터). 다른 단계는 비운다")
    goal: str = Field(default="", description="이 단계에서 할 일 한 문장")


class Plan(BaseModel):
    """LLM이 만드는 계획. 실행 순서대로의 단계 목록."""

    servings: float | None = Field(default=None, description="하루 인분. 사용자가 말하지 않았으면 비운다")
    steps: list[PlanStep]


class ImminentItem(BaseModel):
    """D-day 0~3인 재고 품목."""

    item: str
    qty: float
    unit: str
    d_day: int


class DayMeal(BaseModel):
    """하루 저녁 요리."""

    day: int
    date: str
    dish: str | None
    cuisine: str | None = None
    uses_imminent: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    note: str = ""


class ShortageItem(BaseModel):
    """식단 전체에서 모자란 재료와 양."""

    item: str
    qty: float
    unit: str


class MealPlan(BaseModel):
    """실행 결과를 모은 최종 식단."""

    days: list[DayMeal]
    servings: float
    imminent: list[ImminentItem]
    imminent_used: list[str]
    imminent_unused: list[str]
    imminent_left: list[ShortageItem] = Field(default_factory=list, description="식단을 다 만든 뒤에도 남는 임박 재료 양")
    shortage: list[ShortageItem]
    notes: list[str] = Field(default_factory=list)


class PlannerState(TypedDict, total=False):
    """그래프 상태. `messages`는 Supervisor와 주고받는 대화, 나머지는 이 그래프 안에서만 쓴다."""

    messages: Annotated[list[AnyMessage], add_messages]
    plan: dict
    cursor: int
    work: dict
    meal_plan: dict


# ── 계획 ─────────────────────────────────────────────────────────


def default_plan(days: int = DEFAULT_DAYS, servings: float | None = None) -> Plan:
    """기본 계획: 임박 재료 확인 → 날짜별 요리 배정 → 부족 재료 정리."""
    steps = [PlanStep(kind="imminent", goal="D-day 0~3 임박 재료를 확인한다")]
    steps += [PlanStep(kind="assign", day=d, goal=f"{d}일차 저녁 요리를 배정한다") for d in range(1, days + 1)]
    steps.append(PlanStep(kind="shortage", goal="식단 전체의 부족 재료를 정리한다"))
    return Plan(servings=servings, steps=steps)


def normalize_plan(plan: Plan | None) -> tuple[Plan, bool]:
    """LLM 계획을 검사한다. 규칙(임박 확인 → 1..N일 배정 → 부족 정리)에 맞으면 그대로, 아니면 기본 계획.

    날 수는 1~7일로 자른다. (계획, LLM 계획을 그대로 썼는지)를 돌려준다.
    """
    if plan is None or not plan.steps:
        return default_plan(), False
    servings = plan.servings if plan.servings and plan.servings > 0 else None
    days = [s.day for s in plan.steps if s.kind == "assign"]
    ok = (
        plan.steps[0].kind == "imminent"
        and plan.steps[-1].kind == "shortage"
        and days == list(range(1, len(days) + 1))
        and 1 <= len(days) <= MAX_DAYS
        and len(plan.steps) == len(days) + 2
    )
    if ok:
        return Plan(servings=servings, steps=plan.steps), True
    return default_plan(min(max(len(days), 1), MAX_DAYS) if days else DEFAULT_DAYS, servings), False


def _request_text(messages: list[AnyMessage]) -> str:
    """대화에서 마지막 사용자 질문을 꺼낸다."""
    for message in reversed(messages or []):
        if isinstance(message, HumanMessage):
            return str(message.content)
    return ""


# ── 실행 (결정적) ─────────────────────────────────────────────────


def family_members(conn: sqlite3.Connection, family_id: str) -> list[str]:
    """가족 user_id 목록."""
    return [r["user_id"] for r in conn.execute("SELECT user_id FROM users WHERE family_id = ? ORDER BY user_id", (family_id,))]


def safety_filters(store: Any, members: list[str]) -> tuple[list[str], list[str]]:
    """가족 전원의 알레르기·비선호를 합친다. Store에 없으면 시드를 쓴다. 값은 결과에 내보내지 않는다."""
    profiles = memory.filter_profiles(store, members) if store is not None else {}
    allergies: set[str] = set()
    dislikes: set[str] = set()
    for member in members:
        profile = profiles.get(member) or seed_profile(member)
        allergies |= set(profile.get("allergies", []))
        dislikes |= set(profile.get("dislikes", []))
    return sorted(allergies), sorted(dislikes)


def base_stock(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """쓸 수 있는 재고를 기준 단위로 바꾼 가상 재고. 실행 단계는 이 사본에서만 뺀다(DB는 바꾸지 않음)."""
    stock = usable_stock(conn)
    out: dict[str, list[dict]] = {}
    for key, lots in stock.items():
        for lot in lots:
            try:
                qty, unit = convert_to_base(key, lot["qty"], lot["unit"], conn)
            except UnitUnknownError:
                continue  # 단위 정보를 확인할 수 없는 재고는 계산하지 않는다(식단 가상 재고에서 뺀다, SERVICE.md 5.1)
            except UnitMismatchError:
                qty, unit = lot["qty"], lot["unit"]
            out.setdefault(key, []).append({**lot, "qty": qty, "unit": unit})
        if key in out:
            out[key].sort(key=lambda lot: (lot["d_day"] is None, lot["d_day"] if lot["d_day"] is not None else 0))
    return out


def imminent_items(stock: dict[str, list[dict]]) -> list[ImminentItem]:
    """가상 재고에서 D-day 0~3 품목을 모은다."""
    items = []
    for key, lots in stock.items():
        near = [lot for lot in lots if alert_level(lot["d_day"]) == "imminent"]
        if near:
            items.append(ImminentItem(item=key, qty=round(sum(l["qty"] for l in near), 3), unit=near[0]["unit"],
                                      d_day=min(l["d_day"] for l in near)))
    return sorted(items, key=lambda i: (i.d_day, i.item))


def _consume(stock: dict[str, list[dict]], recipe, servings: float) -> dict[str, tuple[float, str]]:
    """가상 재고에서 레시피 재료를 FEFO로 뺀다. 모자란 양을 {품목: (양, 단위)}로 돌려준다."""
    short: dict[str, tuple[float, str]] = {}
    for ing in recipe.ingredients:
        key = item_key(ing.item)
        try:
            need, unit = convert_to_base(ing.item, ing.qty * servings, ing.unit)
        except UnitMismatchError:
            need, unit = ing.qty * servings, ing.unit
        for lot in stock.get(key, []):
            if need <= 1e-9:
                break
            if lot["unit"] != unit:
                continue
            take = min(lot["qty"], need)
            lot["qty"] -= take
            need -= take
        if need > 1e-9:
            short[ing.item] = (round(need, 3), unit)
    for key in list(stock):
        stock[key] = [lot for lot in stock[key] if lot["qty"] > 1e-9]
        if not stock[key]:
            del stock[key]
    return short


def assign_day(work: dict, day: int) -> DayMeal:
    """그날 요리를 고른다. 남은 임박 재료를 많이 쓰는 요리 → 임박 재료 사용 → 부족 재료가 적은 요리 →
    아직 안 쓴 카테고리 → 요리명 순. 이미 고른 요리와 안전 필터(알레르기·비선호)에 걸린 요리는 뺀다.
    """
    stock = work["stock"]
    remaining = {i for i, qty in work["imminent_left"].items() if qty > 1e-9}
    dislike_set = set(work["dislikes"])
    dislike_items = {item_key(d) for d in dislike_set}
    scored = []
    for recipe in parse_recipes().values():
        ing_keys = {item_key(i.item) for i in recipe.ingredients}
        if recipe.name in work["chosen"] or has_allergen(recipe, work["allergies"]):
            continue
        if recipe.name in dislike_set or dislike_items & ing_keys:
            continue
        candidate = _check_recipe(recipe, stock, work["servings"])
        if candidate is None:
            continue
        uses = sorted(ing_keys & remaining)
        scored.append(((-len(uses), -len(ing_keys & set(work["imminent_all"])), len(candidate.missing),
                        recipe.cuisine in work["cuisines"], recipe.name), recipe, uses, candidate.missing))
    date = (today() + timedelta(days=day - 1)).isoformat()
    if not scored:
        return DayMeal(day=day, date=date, dish=None, note="재고로 만들 수 있는 요리가 더 없어요")
    scored.sort(key=lambda s: s[0])
    _, recipe, uses, missing = scored[0]
    before = {k: sum(l["qty"] for l in lots) for k, lots in stock.items()}
    short = _consume(stock, recipe, work["servings"])
    for key in work["imminent_left"]:
        work["imminent_left"][key] = max(work["imminent_left"][key] - (before.get(key, 0) - sum(l["qty"] for l in stock.get(key, []))), 0)
    for item, (qty, unit) in short.items():
        prev = work["shortage"].get(item, (0.0, unit))
        work["shortage"][item] = (round(prev[0] + qty, 3), unit)
    work["chosen"].append(recipe.name)
    work["cuisines"].append(recipe.cuisine)
    return DayMeal(day=day, date=date, dish=recipe.name, cuisine=recipe.cuisine, uses_imminent=uses, missing=sorted(short))


def start_work(conn: sqlite3.Connection, ctx: FridgeContext, store: Any, servings: float | None) -> dict:
    """실행 단계가 함께 쓰는 작업 상태(가상 재고, 안전 필터, 인분)를 만든다."""
    members = family_members(conn, ctx.family_id) or [ctx.user_id]
    allergies, dislikes = safety_filters(store, members)
    return {
        "stock": base_stock(conn),
        "servings": servings or float(len(members)),
        "servings_default": servings is None,
        "members": len(members),
        "allergies": allergies,
        "dislikes": dislikes,
        "chosen": [],
        "cuisines": [],
        "days": [],
        "imminent": [],
        "imminent_all": [],
        "imminent_left": {},
        "shortage": {},
    }


def _prepare_work(ctx: FridgeContext, store: Any, servings: float | None) -> dict:
    """도메인 DB를 열어 작업 상태를 만든다. 동기 함수라 그래프에서는 스레드에서 부른다."""
    conn = connect(getattr(ctx, "db_path", None))
    try:
        return start_work(conn, ctx, store, servings)
    finally:
        conn.close()


def run_step(work: dict, step: PlanStep) -> dict:
    """계획 한 단계를 실행하고 trace용 요약을 돌려준다."""
    if step.kind == "imminent":
        items = imminent_items(work["stock"])
        work["imminent"] = [i.model_dump() for i in items]
        work["imminent_all"] = [i.item for i in items]
        work["imminent_left"] = {i.item: i.qty for i in items}
        return {"kind": "imminent", "items": [f"{i.item} {i.qty:g}{i.unit} D-{i.d_day}" for i in items]}
    if step.kind == "assign":
        meal = assign_day(work, step.day or len(work["days"]) + 1)
        work["days"].append(meal.model_dump())
        return {"kind": "assign", "day": meal.day, "dish": meal.dish, "uses_imminent": meal.uses_imminent}
    return {"kind": "shortage", "items": [f"{k} {v[0]:g}{v[1]}" for k, v in sorted(work["shortage"].items())]}


def build_meal_plan(work: dict) -> MealPlan:
    """작업 상태를 최종 `MealPlan`으로 묶는다."""
    used = sorted({i for d in work["days"] for i in d["uses_imminent"]})
    imminent = [ImminentItem(**i) for i in work["imminent"]]
    units = {i.item: i.unit for i in imminent}
    notes = []
    if work["servings_default"]:
        notes.append(f"인분을 말하지 않아 가족 {work['members']}명 기준 하루 {work['servings']:g}인분으로 계산했어요")
    if work["allergies"] or work["dislikes"]:
        notes.append("가족의 알레르기와 싫어하는 음식은 빼고 골랐어요")
    return MealPlan(
        days=[DayMeal(**d) for d in work["days"]],
        servings=work["servings"],
        imminent=imminent,
        imminent_used=used,
        imminent_unused=[i.item for i in imminent if i.item not in used],
        imminent_left=[ShortageItem(item=k, qty=round(v, 3), unit=units[k]) for k, v in sorted(work["imminent_left"].items()) if v > 1e-9],
        shortage=[ShortageItem(item=k, qty=v[0], unit=v[1]) for k, v in sorted(work["shortage"].items())],
        notes=notes,
    )


def render_answer(plan: MealPlan) -> str:
    """식단을 사용자에게 보여 줄 한국어 답변으로 만든다(템플릿, LLM 없음)."""
    lines = [f"{len(plan.days)}일 저녁 식단이에요 (하루 {plan.servings:g}인분)."]
    if plan.imminent:
        lines.append("임박 재료: " + ", ".join(f"{i.item} {i.qty:g}{i.unit}(D-{i.d_day})" for i in plan.imminent))
    else:
        lines.append("D-day 0~3 임박 재료가 없어요.")
    for d in plan.days:
        if d.dish is None:
            lines.append(f"- {d.day}일차 ({d.date}): {d.note}")
            continue
        extra = f" · 임박 재료 {', '.join(d.uses_imminent)} 사용" if d.uses_imminent else ""
        extra += f" · 부족: {', '.join(d.missing)}" if d.missing else ""
        lines.append(f"- {d.day}일차 ({d.date}): {d.dish} ({d.cuisine}){extra}")
    if plan.imminent:
        if plan.imminent_unused:
            lines.append("식단에 넣지 못한 임박 재료: " + ", ".join(plan.imminent_unused))
        else:
            lines.append("임박 재료를 모두 식단에 넣었어요.")
        if plan.imminent_left:
            lines.append("다 쓰고도 남는 양: " + ", ".join(f"{s.item} {s.qty:g}{s.unit}" for s in plan.imminent_left))
    lines.append("부족 재료: " + (", ".join(f"{s.item} {s.qty:g}{s.unit}" for s in plan.shortage) or "없어요"))
    lines += plan.notes
    return "\n".join(lines)


# ── 그래프 ───────────────────────────────────────────────────────


def build_planner(model: Any, store: Any = None):
    """Plan-Execute 그래프를 만든다. `model`은 계획 단계에서만 쓴다(구조화 출력).

    Supervisor 아래에서는 상위 그래프의 Store가 그대로 전해지므로 `store`를 넘기지 않는다.
    이 그래프만 따로 돌릴 때(테스트) 장기 메모리 Store를 붙이려면 `store`를 준다.
    """

    async def plan_node(state: PlannerState, runtime: Runtime[FridgeContext]) -> dict:
        """사용자 요청으로 계획을 만든다. 모델이 실패하거나 규칙에 어긋나면 기본 계획."""
        request = _request_text(state.get("messages", []))
        raw: Plan | None = None
        error = None
        try:
            raw = await model.with_structured_output(Plan).ainvoke(PLAN_PROMPT.format(request=request))
        except Exception as exc:  # noqa: BLE001 - 계획 실패는 기본 계획으로 넘긴다
            error = f"{type(exc).__name__}"
        plan, from_llm = normalize_plan(raw)
        await adispatch_custom_event("plan", {
            "source": "llm" if from_llm else "default", "error": error, "servings": plan.servings,
            "steps": [s.model_dump() for s in plan.steps],
        })
        return {"plan": plan.model_dump(), "cursor": 0, "work": {}}

    async def execute_node(state: PlannerState, runtime: Runtime[FridgeContext]) -> dict:
        """계획의 다음 단계 하나를 실행한다."""
        plan = Plan.model_validate(state["plan"])
        work = copy.deepcopy(state.get("work") or {})
        ctx = runtime.context
        if not work:
            # Store(AsyncSqliteStore)와 SQLite를 동기로 읽으므로 이벤트 루프 밖 스레드에서 준비한다.
            work = await asyncio.to_thread(_prepare_work, ctx, runtime.store, plan.servings)
        cursor = state.get("cursor", 0)
        summary = run_step(work, plan.steps[cursor])
        await adispatch_custom_event("plan_step", {"index": cursor + 1, "of": len(plan.steps), **summary})
        return {"work": work, "cursor": cursor + 1}

    def route(state: PlannerState) -> str:
        """남은 단계가 있으면 실행을 계속하고, 없으면 정리로 간다."""
        return "execute" if state.get("cursor", 0) < len(state["plan"]["steps"]) else "finish"

    def finish_node(state: PlannerState) -> dict:
        """최종 식단을 만들고 답변 메시지를 남긴다."""
        meal_plan = build_meal_plan(state["work"])
        return {"meal_plan": meal_plan.model_dump(),
                "messages": [AIMessage(content=render_answer(meal_plan), name=PLANNER_AGENT)]}

    builder = StateGraph(PlannerState, context_schema=FridgeContext)
    builder.add_node("plan", plan_node)
    builder.add_node("execute", execute_node)
    builder.add_node("finish", finish_node)
    builder.add_edge(START, "plan")
    builder.add_edge("plan", "execute")
    builder.add_conditional_edges("execute", route, {"execute": "execute", "finish": "finish"})
    builder.add_edge("finish", END)
    return builder.compile(name=PLANNER_AGENT, store=store)
