"""냉장고 관리 Agent 터미널 데모.

실행 중인 API 서버(`uvicorn src.api:app`)에 `POST /query`로 질문을 보낸다. 유효기간 경고는
화면 맨 위에, 승인 요청은 y/n 질문으로, 이번 주 폐기 회피량은 화면 맨 아래에 보여준다.

실행: `python -m src.cli --user dad`
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

from dotenv import load_dotenv

load_dotenv()

DEFAULT_URL = "http://127.0.0.1:8000"
LEVEL_LABEL = {"imminent": "임박", "expired": "만료"}
LINE = "─" * 60


def user_token(user_id: str | None) -> str | None:
    """`.env`의 `USER_TOKENS`에서 사용자의 토큰을 찾는다. 없으면 None."""
    for pair in os.environ.get("USER_TOKENS", "").split(","):
        user, sep, token = pair.strip().partition(":")
        if sep and user.strip() == user_id:
            return token.strip()
    return None


def post_query(url: str, body: dict, token: str | None, timeout: float = 120) -> dict:
    """`POST /query`를 보내고 응답 JSON을 돌려준다."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-User-Token"] = token
    request = urllib.request.Request(
        f"{url.rstrip('/')}/query",
        data=json.dumps({k: v for k, v in body.items() if v is not None}, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def format_alerts(alerts: list[dict]) -> list[str]:
    """유효기간 경고를 화면 맨 위 줄 목록으로 만든다."""
    lines = []
    for a in alerts:
        qty = f" {a['qty']:g}{a['unit']}" if a.get("qty") is not None else ""
        label = LEVEL_LABEL.get(a["level"], a["level"])
        lines.append(f"⚠ [{label}] {a['item']}{qty} (S/N {a['sn']}) D-day {a['d_day']}")
    return lines


def render(response: dict) -> str:
    """응답 하나를 화면 문자열로 만든다: 경고 → 답변 → 오류 → 폐기 회피량."""
    trace = response.get("trace", {})
    parts = [LINE]
    alerts = format_alerts(trace.get("alerts") or [])
    if alerts:
        parts += alerts + [LINE]
    parts.append(response.get("answer", ""))
    for err in trace.get("errors") or []:
        parts.append(f"  ({err['code']}: {err['message']})")
    tools = [s["tool"] for s in trace.get("steps") or [] if "tool" in s]
    if tools:
        parts.append(f"  도구: {' → '.join(tools)}")
    parts += [LINE, f"이번 주 폐기 회피 {trace.get('rescued_g_week', 0):g}g", LINE]
    return "\n".join(parts)


def ask_yes_no(prompt: str) -> bool:
    """y/n 답을 받을 때까지 묻는다."""
    while True:
        answer = input(prompt).strip().lower()
        if answer in ("y", "yes", "ㅇ", "네", "승인"):
            return True
        if answer in ("n", "no", "ㄴ", "아니요", "거절"):
            return False
        print("y 또는 n으로 답해 주세요.")


def main(argv: list[str] | None = None) -> int:
    """질문을 반복해서 받아 서버에 보내고, 승인 요청이 오면 y/n으로 승인·거절을 보낸다."""
    parser = argparse.ArgumentParser(description="냉장고 관리 Agent 터미널 데모")
    parser.add_argument("--url", default=os.environ.get("FRIDGE_API_URL", DEFAULT_URL), help="API 서버 주소")
    parser.add_argument("--user", default=None, help="요청자 user_id (없으면 서버의 DEFAULT_USER)")
    parser.add_argument("--thread", default=None, help="이어서 쓸 thread_id")
    args = parser.parse_args(argv)

    token = user_token(args.user)
    if args.user and not token:
        print(f"USER_TOKENS에 {args.user}의 토큰이 없어요.", file=sys.stderr)
        return 1
    thread_id = args.thread
    print("냉장고 관리 Agent 데모입니다. 빈 줄이나 exit를 입력하면 끝납니다.")

    while True:
        try:
            question = input("\n질문> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if question in ("", "exit", "quit", "종료"):
            return 0
        body = {"question": question, "user_id": args.user, "thread_id": thread_id}
        try:
            response = post_query(args.url, body, token)
            print(render(response))
            thread_id = response["trace"].get("thread_id") or thread_id
            while response["trace"].get("pending_interrupt"):
                approve = ask_yes_no("승인할까요? (y/n) ")
                body = {
                    "question": "승인" if approve else "거절",
                    "user_id": args.user,
                    "thread_id": thread_id,
                    "resume": "approve" if approve else "reject",
                }
                response = post_query(args.url, body, token)
                print(render(response))
        except (urllib.error.URLError, TimeoutError) as exc:
            print(f"서버에 연결하지 못했어요 ({args.url}): {exc}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
