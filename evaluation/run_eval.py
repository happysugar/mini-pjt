"""평가 하네스: `evaluation/test_queries.csv` 15문항을 API로 실행하고 지표를 낸다 (SERVICE.md 10장).

- 문항마다 `FRIDGE_TODAY`를 고정하고 임시 폴더에 `fridge.db`(시드)와 `checkpoints.db`를 새로 만든다.
  `data/` 아래 실제 DB는 건드리지 않는다.
- `POST /query`를 앱에 직접(ASGI) 보내고, 응답 `trace.steps`로 도구 일치를 본다.
  note에 `resume=approve`가 있으면 같은 `thread_id`로 승인까지 진행한다.
- LLM-as-Judge(`EVAL_MODEL_ID`)로 trait 충족과 forbidden 위반을 판정한다.
- contexts 대상 문항은 `ragas` faithfulness·answer relevancy를 계산하고, 실패하면 같은 방식의
  Bedrock 판정 프롬프트로 계산해 "RAGAS 방식"으로 표기한다.
- 결과는 `evaluation/results/run-YYYYMMDD-HHMMSS.json`에 실행별로 저장한다.

실행: `.venv/Scripts/python -m evaluation.run_eval [--only P02,N01] [--compare 이전결과.json]`
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
EVAL_TODAY = "2026-11-20"
os.environ["FRIDGE_TODAY"] = EVAL_TODAY  # 공통 전제: 날짜 고정 (src 모듈을 불러오기 전에)

import httpx  # noqa: E402
from langchain_aws import BedrockEmbeddings, ChatBedrockConverse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

import src.agent as agent_module  # noqa: E402
from src.agent import HEALTH_QUERY_TOOLS  # noqa: E402
from src.api import create_app, parse_user_tokens  # noqa: E402
from src.tools import AGENT_TOOLS, reset_db  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parent
QUERIES_CSV = EVAL_DIR / "test_queries.csv"
RESULTS_DIR = EVAL_DIR / "results"
# contexts로 RAGAS 지표를 계산하는 문항 (SERVICE.md 10.2)
RAGAS_IDS = ("P02", "P03", "P05", "N01", "N02")
# RAGAS 지표를 문항마다 몇 번 채점해 평균 낼지. 같은 답도 채점마다 relevancy가 ±0.1 흔들려
# 한 번의 점수로 0.8 기준을 판단하기 어렵다. 채점 호출은 이 배수만큼 늘어난다(평가 모델만 사용).
RAGAS_REPEATS = 3
HANDOFF_PREFIX = "transfer_to_"
TOOL_SPEC = re.compile(r"^(?P<name>[A-Za-z_][\w]*)\s*(?:\((?P<args>.*)\))?$")


# ── 문항 읽기와 도구 이름 검사 ─────────────────────────────────────


def split_list(value: str) -> list[str]:
    """세미콜론으로 구분한 칸을 목록으로 나눈다. 빈 칸은 빈 목록이다."""
    return [part.strip() for part in (value or "").split(";") if part.strip()]


def parse_tool_spec(spec: str) -> dict[str, Any]:
    """`rank_recipes(category=멕시코식)` 같은 기대 도구 표기를 이름과 인자 조건으로 나눈다."""
    match = TOOL_SPEC.match(spec.strip())
    if not match:
        raise ValueError(f"기대 도구 표기를 읽을 수 없어요: {spec}")
    args: dict[str, str] = {}
    for pair in filter(None, (p.strip() for p in (match["args"] or "").split(","))):
        key, sep, value = pair.partition("=")
        if not sep:
            raise ValueError(f"기대 도구 인자는 key=value 형식이어야 해요: {spec}")
        args[key.strip()] = value.strip()
    return {"name": match["name"], "args": args}


def load_queries(path: Path = QUERIES_CSV) -> list[dict[str, Any]]:
    """평가 문항 CSV를 읽어 목록 칸을 나누고 note에서 user_id와 다단계 진행 방법을 꺼낸다.

    다단계는 두 가지다: `resume=approve|reject`(승인 재개), `같은 thread_id로 "질문"를 보낸`(두 번째 질문).
    """
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    queries = []
    for row in rows:
        note = row.get("note", "")
        user = re.search(r"user_id=(\w+)", note)
        resume = re.search(r"resume=(approve|reject)", note)
        followup = re.search(r'같은 thread_id로 "([^"]+)"를 보낸', note)
        abstain = bool(re.search(r"answer=abstain", note))  # 거절이 정답인 문항: answer relevancy 제외
        queries.append(
            {
                **row,
                "traits": split_list(row["expected_traits"]),
                "forbidden_list": split_list(row["forbidden"]),
                "tools": [parse_tool_spec(s) for s in split_list(row["expected_tools"])],
                "user_id": user.group(1) if user else None,
                "resume": resume.group(1) if resume else None,
                "followup": followup.group(1) if followup else None,
                "abstain": abstain,
            }
        )
    return queries


def registered_tool_names() -> set[str]:
    """에이전트에 등록된 도구 이름 집합. 워치 MCP 조회 도구(TASK-14)를 포함한다. 서브 에이전트 도구가 생기면 더한다."""
    return {t.name for t in AGENT_TOOLS} | set(HEALTH_QUERY_TOOLS)


def unknown_tools(query: dict[str, Any], registered: set[str]) -> list[str]:
    """문항의 기대 도구 중 등록되지 않은 이름을 돌려준다."""
    return [t["name"] for t in query["tools"] if t["name"] not in registered]


# ── 문항 실행 ─────────────────────────────────────────────────────


def db_counts(db_path: Path) -> dict[str, Any]:
    """판정 근거용으로 도메인 DB의 주요 상태(테이블 행 수, 마지막 S/N 재고 행)를 읽는다."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        counts: dict[str, Any] = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("inventory", "ratings", "meals", "deductions")
        }
        last = conn.execute(
            "SELECT sn, item, qty, unit, input_date, expiry_date, status FROM inventory ORDER BY sn DESC LIMIT 1"
        ).fetchone()
        counts["last_inventory"] = dict(last) if last else None
        return counts
    finally:
        conn.close()


def state_tool_calls(messages: list[Any]) -> list[dict[str, Any]]:
    """체크포인트 메시지에서 모델이 부른 도구 이름과 인자를 순서대로 꺼낸다."""
    calls = []
    for message in messages:
        for call in getattr(message, "tool_calls", None) or []:
            calls.append({"name": call["name"], "args": call.get("args", {})})
    return calls


async def run_query(query: dict[str, Any], model: Any = None) -> dict[str, Any]:
    """문항 하나를 격리된 임시 DB로 실행해 턴별 응답과 도구 호출 기록을 돌려준다."""
    with tempfile.TemporaryDirectory(prefix="fridge-eval-", ignore_cleanup_errors=True) as tmp:
        fridge_db = Path(tmp) / "fridge.db"
        agent_module.CHECKPOINT_DB = Path(tmp) / "checkpoints.db"  # open_agent가 읽는 경로를 임시 파일로
        reset_db(fridge_db)
        seed_state = db_counts(fridge_db)

        app = create_app(model=model, db_path=str(fridge_db))
        headers = {}
        token = parse_user_tokens().get(query["user_id"] or "")
        if token:
            headers["X-User-Token"] = token
        turns: list[dict[str, Any]] = []
        started = time.perf_counter()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://eval", timeout=300) as client:
                body: dict[str, Any] = {"question": query["input"]}
                if query["user_id"]:
                    body["user_id"] = query["user_id"]
                requests = [body]
                for i, req in enumerate(requests):
                    resp = await client.post("/query", json=req, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
                    turns.append({"request": req, **data, "db_after": db_counts(fridge_db)})
                    if i == 0 and query["resume"] and data["trace"].get("pending_interrupt"):
                        requests.append({**body, "question": "승인", "thread_id": data["trace"]["thread_id"], "resume": query["resume"]})
                    elif i == 0 and query["followup"]:  # note의 2턴 진행 (예: E01 제안 S/N으로 저장)
                        requests.append({**body, "question": query["followup"], "thread_id": data["trace"]["thread_id"]})
            thread_id = turns[0]["trace"]["thread_id"]
            snapshot = await app.state.agent.aget_state({"configurable": {"thread_id": thread_id}})
            calls = state_tool_calls(snapshot.values.get("messages", []))
        return {
            "turns": turns,
            "seed_state": seed_state,
            "tool_calls": calls,
            "elapsed_s": round(time.perf_counter() - started, 1),
        }


# ── 도구 일치 ─────────────────────────────────────────────────────


def called_tools(turns: list[dict[str, Any]]) -> list[str]:
    """모든 턴의 `trace.steps`에서 도구 이름을 모은다.

    `transfer_to_*` 핸드오프와, 에이전트 도구가 아니라 코드가 부른 MCP 호출(`via: mcp`, 예: record_meal)은 뺀다.
    """
    names = []
    for turn in turns:
        for step in turn["trace"].get("steps", []):
            name = step.get("tool")
            if name and not name.startswith(HANDOFF_PREFIX) and step.get("via") != "mcp":
                names.append(name)
    return names


def tool_match(query: dict[str, Any], run: dict[str, Any], registered: set[str]) -> dict[str, Any]:
    """기대 도구가 모두 호출됐는지 본다(순서 무시). 빈 칸이면 도구 호출이 없어야 일치다.

    인자 조건(`이름(key=value)`)은 체크포인트의 도구 호출 인자로 확인한다.
    등록되지 않은 기대 도구가 있으면 실행 결과와 상관없이 실패다.
    """
    called = called_tools(run["turns"])
    unknown = unknown_tools(query, registered)
    if unknown:
        return {"match": False, "called": called, "reason": f"등록되지 않은 기대 도구: {', '.join(unknown)}"}
    if not query["tools"]:
        ok = not called
        return {"match": ok, "called": called, "reason": "" if ok else "도구를 부르지 않아야 하는 문항"}
    missing = []
    for spec in query["tools"]:
        if spec["name"] not in called:
            missing.append(spec["name"])
            continue
        if spec["args"]:
            ok = any(
                c["name"] == spec["name"] and all(str(c["args"].get(k, "")).strip() == v for k, v in spec["args"].items())
                for c in run["tool_calls"]
            )
            if not ok:
                args = ", ".join(f"{k}={v}" for k, v in spec["args"].items())
                missing.append(f"{spec['name']}({args})")
    return {"match": not missing, "called": called, "reason": f"빠진 호출: {', '.join(missing)}" if missing else ""}


# ── LLM-as-Judge ──────────────────────────────────────────────────


class Verdict(BaseModel):
    """항목 하나에 대한 판정."""

    item: str = Field(description="판정한 항목 원문")
    ok: bool = Field(description="trait이면 충족 여부, forbidden이면 위반 여부(위반이면 true)")
    reason: str = Field(description="한 문장 근거")


class JudgeResult(BaseModel):
    """한 문항의 trait 충족과 forbidden 위반 판정."""

    traits: list[Verdict] = Field(description="expected_traits 각 항목을 같은 순서로 판정")
    forbidden: list[Verdict] = Field(description="forbidden 각 항목을 같은 순서로 판정. ok=true는 위반")


JUDGE_PROMPT = """너는 냉장고 관리 에이전트의 응답을 채점하는 심사관이다. 오늘 날짜는 {today}이다.
아래 근거(사용자 질문, 턴별 응답 answer·contexts·trace, 도메인 DB 상태)만 보고 판정한다.

- expected_traits: 항목마다 응답이 그 특성을 충족하면 ok=true.
  "승인 요청(pending_interrupt) 반환"처럼 trace에 관한 항목은 trace를 근거로 본다.
  "contexts에 ~ 포함"은 contexts를, "DB 변경 없음"은 DB 상태 변화를 근거로 본다.
- forbidden: 항목마다 응답이 그것을 위반했으면 ok=true, 위반하지 않았으면 ok=false.
- 항목 수와 순서를 그대로 지키고 item에 원문을 적는다. 근거가 없으면 trait은 미충족으로 본다.

[질문]
{question}

[expected_traits]
{traits}

[forbidden]
{forbidden}

[시드 직후 DB 상태]
{seed_state}

[턴별 응답]
{turns}
"""


def build_eval_model() -> ChatBedrockConverse:
    """`.env`의 `EVAL_MODEL_ID`로 평가용 Bedrock 모델을 만든다."""
    return ChatBedrockConverse(
        model=os.environ["EVAL_MODEL_ID"],
        region_name=os.environ.get("AWS_DEFAULT_REGION"),
        temperature=0,
        max_tokens=4096,
    )


def build_embeddings() -> BedrockEmbeddings:
    """`.env`의 `BEDROCK_EMBED_MODEL_ID`로 임베딩 모델을 만든다 (answer relevancy용)."""
    return BedrockEmbeddings(model_id=os.environ["BEDROCK_EMBED_MODEL_ID"], region_name=os.environ.get("AWS_DEFAULT_REGION"))


def _turns_for_judge(turns: list[dict[str, Any]]) -> str:
    """판정 프롬프트에 넣을 턴별 응답 요약 JSON을 만든다."""
    keep = ("thread_id", "user_id", "alerts", "pending_interrupt", "steps", "rescued_g_week", "errors")
    slim = [
        {
            "request": t["request"],
            "answer": t["answer"],
            "contexts": t["contexts"],
            "trace": {k: t["trace"].get(k) for k in keep},
            "db_after": t["db_after"],
        }
        for t in turns
    ]
    return json.dumps(slim, ensure_ascii=False, indent=1)


async def judge(model: ChatBedrockConverse, query: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    """LLM-as-Judge로 trait 충족과 forbidden 위반을 판정하고 통과 여부를 정한다."""
    prompt = JUDGE_PROMPT.format(
        today=EVAL_TODAY,
        question=query["input"],
        traits="\n".join(f"- {t}" for t in query["traits"]),
        forbidden="\n".join(f"- {f}" for f in query["forbidden_list"]) or "(없음)",
        seed_state=json.dumps(run["seed_state"], ensure_ascii=False),
        turns=_turns_for_judge(run["turns"]),
    )
    result: JudgeResult = await model.with_structured_output(JudgeResult).ainvoke(prompt)
    traits_met = sum(v.ok for v in result.traits)
    violations = sum(v.ok for v in result.forbidden)
    passed = len(result.traits) == len(query["traits"]) and traits_met == len(query["traits"]) and violations == 0
    return {
        "passed": passed,
        "traits_met": traits_met,
        "traits_total": len(query["traits"]),
        "violations": violations,
        "traits": [v.model_dump() for v in result.traits],
        "forbidden": [v.model_dump() for v in result.forbidden],
    }


# ── RAGAS (faithfulness, answer relevancy) ─────────────────────────


def ragas_scores(samples: list[dict[str, Any]]) -> tuple[list[dict[str, float]], str]:
    """`ragas` 라이브러리로 두 지표를 계산한다. (문항별 점수, 표기) 를 돌려준다."""
    import ragas
    from ragas import EvaluationDataset, evaluate
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import Faithfulness, ResponseRelevancy

    dataset = EvaluationDataset.from_list(
        [{"user_input": s["question"], "response": s["answer"], "retrieved_contexts": s["contexts"]} for s in samples]
    )
    relevancy = ResponseRelevancy()
    # 기본 프롬프트는 역질문을 영어로 만들어 한국어 질문과의 코사인이 0 가까이 나온다. 응답과 같은 언어로 만들게 한다.
    relevancy.question_generation.instruction += " Write the question in the same language as the response (e.g. Korean)."
    result = evaluate(
        dataset,
        metrics=[Faithfulness(), relevancy],
        llm=LangchainLLMWrapper(build_eval_model()),
        embeddings=LangchainEmbeddingsWrapper(build_embeddings()),
        show_progress=False,
    )
    df = result.to_pandas()
    scores = [
        {"faithfulness": _num(row.get("faithfulness")), "answer_relevancy": _num(row.get("answer_relevancy"))}
        for row in df.to_dict("records")
    ]
    return scores, f"ragas {ragas.__version__}"


class Claims(BaseModel):
    """답변을 쪼갠 사실 주장과 contexts 근거 여부."""

    claims: list[str] = Field(description="답변 속 독립적인 사실 주장 목록")
    supported: list[bool] = Field(description="각 주장이 contexts만으로 뒷받침되면 true. claims와 같은 순서")


class GeneratedQuestions(BaseModel):
    """답변만 보고 거꾸로 만든 질문."""

    questions: list[str] = Field(description="이 답변이 대답하는 질문 3개")
    noncommittal: bool = Field(description="답변이 회피·모호하면 true")


def fallback_scores(samples: list[dict[str, Any]]) -> tuple[list[dict[str, float]], str]:
    """`ragas`를 쓸 수 없을 때 같은 정의로 두 지표를 Bedrock 판정 프롬프트로 계산한다.

    - faithfulness: 답변 주장 중 contexts로 뒷받침되는 비율
    - answer relevancy: 답변에서 만든 질문 3개와 원래 질문의 임베딩 코사인 평균(회피 답변은 0)
    """
    model = build_eval_model()
    embeddings = build_embeddings()
    scores = []
    for s in samples:
        claims: Claims = model.with_structured_output(Claims).invoke(
            "답변을 독립적인 사실 주장으로 나누고, 각 주장이 contexts만으로 뒷받침되는지 판정하라.\n\n"
            f"[contexts]\n{chr(10).join(s['contexts'])}\n\n[답변]\n{s['answer']}"
        )
        faith = sum(claims.supported) / len(claims.claims) if claims.claims else math.nan
        gen: GeneratedQuestions = model.with_structured_output(GeneratedQuestions).invoke(
            f"다음 답변만 보고, 이 답변이 대답하는 질문 3개를 한국어로 만들어라.\n\n[답변]\n{s['answer']}"
        )
        if gen.noncommittal or not gen.questions:
            relevancy = 0.0
        else:
            q_vec = embeddings.embed_query(s["question"])
            g_vecs = embeddings.embed_documents(gen.questions)
            relevancy = sum(_cosine(q_vec, g) for g in g_vecs) / len(g_vecs)
        scores.append({"faithfulness": faith, "answer_relevancy": relevancy})
    return scores, "RAGAS 방식"


def _cosine(a: list[float], b: list[float]) -> float:
    """두 벡터의 코사인 유사도."""
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


def _num(value: Any) -> float:
    """점수 값을 float로 바꾼다. 없으면 NaN."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.nan


def average_runs(raw: list[dict[str, float]], count: int) -> list[dict[str, Any]]:
    """`samples * 반복 횟수` 순서로 채점한 결과를 문항별 평균으로 묶는다.

    raw[i + k*count]가 i번째 문항의 k번째 채점이다. 평균은 NaN을 뺀 값으로 내고(없으면 NaN),
    회차별 값은 `faithfulness_runs`, `answer_relevancy_runs`에 남긴다.
    """
    averaged = []
    for i in range(count):
        runs = raw[i::count]
        row: dict[str, Any] = {}
        for key in ("faithfulness", "answer_relevancy"):
            values = [run[key] for run in runs]
            mean = _mean(values)
            row[key] = math.nan if mean is None else mean
            row[f"{key}_runs"] = [round(v, 3) if _is_score(v) else None for v in values]
        averaged.append(row)
    return averaged


def compute_ragas(results: list[dict[str, Any]]) -> str:
    """대상 문항 중 contexts가 있는 문항의 RAGAS 지표를 결과에 채우고 계산 방식 표기를 돌려준다.

    note에 `answer=abstain`인 문항(거절이 정답)은 회피 답이면 relevancy가 0이 되는 지표라
    answer relevancy를 None으로 두고 평균에서 뺀다. faithfulness는 그대로 계산한다.
    문항마다 RAGAS_REPEATS번 채점해 평균을 내고, 회차별 값은 `*_runs`에 남긴다.
    """
    targets = [r for r in results if r["id"] in RAGAS_IDS]
    samples, owners = [], []
    for r in targets:
        last = r["run"]["turns"][-1]
        if not last["contexts"]:
            r["ragas"] = {"skipped": "contexts 없음"}
            continue
        samples.append({"question": r["input"], "answer": last["answer"], "contexts": last["contexts"]})
        owners.append(r)
    if not samples:
        return "없음"
    repeated = samples * RAGAS_REPEATS
    try:
        raw, mode = ragas_scores(repeated)
    except Exception as exc:  # noqa: BLE001 - 설치·호환 문제면 같은 정의의 대체 계산으로
        print(f"[ragas] 라이브러리 계산 실패, RAGAS 방식으로 대체: {type(exc).__name__}: {exc}"[:300])
        raw, mode = fallback_scores(repeated)
    scores = average_runs(raw, len(samples))
    mode = f"{mode}, {RAGAS_REPEATS}회 평균"
    for r, score in zip(owners, scores):
        if r.get("abstain"):
            score = {**score, "answer_relevancy": None, "relevancy_skipped": "거절이 정답인 문항"}
        r["ragas"] = score
    return mode


# ── 집계와 출력 ───────────────────────────────────────────────────


def _mean(values: list[float]) -> float | None:
    """NaN을 뺀 평균. 값이 없으면 None."""
    values = [v for v in values if isinstance(v, float) and not math.isnan(v)]
    return round(sum(values) / len(values), 3) if values else None


def _is_score(value: Any) -> bool:
    """평균에 들어가는 점수(NaN이 아닌 float)인지 본다."""
    return isinstance(value, float) and not math.isnan(value)


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    """문항 결과를 SERVICE.md 10.3 목표치와 같은 지표로 모은다."""
    judged = [r for r in results if "judge" in r and "error" not in r["judge"]]
    guard = [r for r in results if r["category"] == "guardrail"]
    ragas_rows = [r["ragas"] for r in results if isinstance(r.get("ragas"), dict) and "faithfulness" in r["ragas"]]
    return {
        "total": len(results),
        "passed": sum(r["judge"]["passed"] for r in judged),
        "guardrail_passed": sum(r.get("judge", {}).get("passed", False) for r in guard),
        "guardrail_total": len(guard),
        "forbidden_violations": sum(r["judge"]["violations"] for r in judged),
        "tool_match": sum(r["tools"]["match"] for r in results),
        "faithfulness": _mean([s["faithfulness"] for s in ragas_rows]),
        "answer_relevancy": _mean([s["answer_relevancy"] for s in ragas_rows]),
        "ragas_n": len(ragas_rows),
        "faithfulness_n": sum(_is_score(s["faithfulness"]) for s in ragas_rows),
        "relevancy_n": sum(_is_score(s["answer_relevancy"]) for s in ragas_rows),
    }


def print_report(results: list[dict[str, Any]], summary: dict[str, Any], mode: str) -> None:
    """문항별 결과 표와 지표를 터미널에 출력한다."""
    print(f"\n{'id':4} {'cat':9} {'pass':5} {'traits':7} {'viol':4} {'tool':5} {'faith':6} {'relev':6}  called / reason")
    for r in results:
        j = r.get("judge", {})
        rg = r.get("ragas") or {}
        fmt = lambda v: f"{v:.2f}" if _is_score(v) else "-"  # noqa: E731
        traits = f"{j.get('traits_met', '-')}/{j.get('traits_total', '-')}"
        relev = "제외" if rg.get("relevancy_skipped") else fmt(rg.get("answer_relevancy"))
        print(
            f"{r['id']:4} {r['category']:9} {str(j.get('passed', 'ERR')):5} {traits:7} {str(j.get('violations', '-')):4} "
            f"{str(r['tools']['match']):5} {fmt(rg.get('faithfulness')):6} {relev:6}  "
            f"{';'.join(r['tools']['called']) or '(없음)'}{' / ' + r['tools']['reason'] if r['tools']['reason'] else ''}"
        )
    s = summary
    print(
        f"\n문항 통과 {s['passed']}/{s['total']} (목표 10)  guardrail {s['guardrail_passed']}/{s['guardrail_total']} (목표 2)  "
        f"forbidden 위반 {s['forbidden_violations']}건 (목표 0)  도구 일치 {s['tool_match']}/{s['total']} (목표 11)"
    )
    print(
        f"faithfulness {s['faithfulness']} (n={s['faithfulness_n']})  answer relevancy {s['answer_relevancy']} "
        f"(n={s['relevancy_n']}, answer=abstain 문항 제외)  {mode}, 목표 0.8"
    )


def compare_tools(results: list[dict[str, Any]], previous_path: Path) -> bool:
    """이전 실행 결과 파일과 도구 일치 결과(일치 여부, 호출 도구)를 비교해 출력한다. 같으면 True."""
    previous = {r["id"]: r for r in json.loads(previous_path.read_text(encoding="utf-8"))["results"]}
    same = True
    for r in results:
        p = previous.get(r["id"])
        if p is None:
            continue
        if p["tools"]["match"] != r["tools"]["match"] or p["tools"]["called"] != r["tools"]["called"]:
            same = False
            print(f"[비교] {r['id']}: 이전 {p['tools']['match']} {p['tools']['called']} → 이번 {r['tools']['match']} {r['tools']['called']}")
    print(f"[비교] {previous_path.name} 대비 도구 일치 결과 {'같음' if same else '다름'}")
    return same


async def run_all(queries: list[dict[str, Any]], registered: set[str]) -> list[dict[str, Any]]:
    """문항을 차례로 실행하고 도구 일치와 Judge 판정을 붙인다."""
    judge_model = build_eval_model()
    results = []
    for q in queries:
        print(f"[실행] {q['id']} {q['input']}", flush=True)
        record: dict[str, Any] = {k: q[k] for k in ("id", "category", "input", "user_id", "abstain")}
        try:
            record["run"] = await run_query(q)
        except Exception as exc:  # noqa: BLE001 - 한 문항 실패로 전체가 멈추지 않게
            record["run"] = {"turns": [], "tool_calls": [], "seed_state": {}, "error": f"{type(exc).__name__}: {exc}"}
            print(f"  실행 오류: {record['run']['error']}"[:300])
        record["tools"] = tool_match(q, record["run"], registered)
        if record["run"]["turns"]:
            try:
                record["judge"] = await judge(judge_model, q, record["run"])
            except Exception as exc:  # noqa: BLE001
                record["judge"] = {"passed": False, "violations": 0, "error": f"{type(exc).__name__}: {exc}"[:300]}
        else:
            record["judge"] = {"passed": False, "violations": 0, "error": "실행 실패"}
        results.append(record)
    return results


def main(argv: list[str] | None = None) -> int:
    """문항을 실행·채점하고 RAGAS 지표를 붙여 결과 파일을 저장한다.

    `ragas`는 불러올 때 이벤트 루프를 패치(nest_asyncio)하므로, 문항 실행 루프가 끝난 뒤 따로 계산한다.
    """
    parser = argparse.ArgumentParser(description="냉장고 Agent 15문항 평가")
    parser.add_argument("--only", help="실행할 문항 id 목록(쉼표 구분)")
    parser.add_argument("--compare", type=Path, help="도구 일치 결과를 비교할 이전 결과 JSON")
    args = parser.parse_args(argv)

    queries = load_queries()
    if args.only:
        wanted = {x.strip() for x in args.only.split(",")}
        queries = [q for q in queries if q["id"] in wanted]
    registered = registered_tool_names()
    for q in queries:
        if unknown_tools(q, registered):
            print(f"[도구] {q['id']}: 등록되지 않은 기대 도구 {unknown_tools(q, registered)} → 도구 일치 실패로 처리")

    results = asyncio.run(run_all(queries, registered))
    mode = compute_ragas(results)
    summary = summarize(results)
    print_report(results, summary, mode)

    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    payload = {
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "fridge_today": EVAL_TODAY,
        "agent_model": os.environ.get("BEDROCK_MODEL_ID"),
        "eval_model": os.environ.get("EVAL_MODEL_ID"),
        "ragas_mode": mode,
        "summary": summary,
        "results": results,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\n결과 저장: {out.relative_to(ROOT)}")
    if args.compare:
        compare_tools(results, args.compare)
    return 0


if __name__ == "__main__":
    sys.exit(main())
