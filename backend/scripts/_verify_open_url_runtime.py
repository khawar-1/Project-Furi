"""The "open <site>" fix on the REAL backend lifespan (2026-08-11).

THE INCIDENT. `open junaidjamshed.com` was routed BROWSE — deterministically, in
code — then planned as a single `read_webpage`, so the user got the site's whole
homepage as a chat message and NO WINDOW EVER OPENED.

WHY THIS RUNS THE CHAT SURFACE AND NOT /api/agent/execute. That endpoint builds
`AgentPlanner(db, provider, ...)` with no agent, i.e. the GENERAL agent — and
this round's guard keys on the ROUTER's verdict carried as the browser agent's
key. Verifying through /execute would exercise a path where the guard correctly
never fires, and report a confident green about nothing. The real path is
chat -> task_router -> start_task(agent=browser), so that is what this drives.

⚠️ IT OPENS A REAL CHROME WINDOW on the real site and leaves it open — that is
the fix, and the last check closes it. Nothing is ever approved or submitted:
`browse` is READ-level, and the script asserts zero commits at the end.

    venv\\Scripts\\python -u scripts\\_verify_open_url_runtime.py

NEVER collected by pytest (real lifespan, real browser, real network).
"""
from __future__ import annotations

import asyncio
import functools
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

print = functools.partial(print, flush=True)  # noqa: A001
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

API_PORT = 18011
GOAL = "open junaidjamshed.com"
SESSION = "verify-open-url"

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    RESULTS.append((bool(ok), label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  — {detail}" if detail else ""))


async def main() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="jarvis-openurl-verify-"))
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{scratch / 'scratch.db'}"
    os.environ["QDRANT_HOST"] = ""
    os.environ["BACKEND_PORT"] = str(API_PORT)
    os.environ["REMOTE_ENABLED"] = "false"
    os.environ["DEBUG"] = "false"

    import uvicorn
    from main import app
    from app.core.auth import get_or_create_token

    config = uvicorn.Config(app, host="127.0.0.1", port=API_PORT, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(600):
        if server.started:
            break
        await asyncio.sleep(0.1)
    if not server.started:
        print("backend never started")
        return 1
    print(f"real backend lifespan booted on :{API_PORT}\n")

    import httpx
    from sqlalchemy import select
    from app.db.database import AsyncSessionLocal
    from app.db.models import Message, RoutingDecision, Task

    headers = {"X-Jarvis-Token": get_or_create_token()}
    base = f"http://127.0.0.1:{API_PORT}"
    ok_overall = 1

    try:
        async with httpx.AsyncClient(headers=headers, timeout=300) as http:
            body = {
                "messages": [{"role": "user", "content": GOAL}],
                "session_id": SESSION,
            }
            chunks: list[str] = []
            async with http.stream("POST", f"{base}/chat/stream", json=body) as r:
                check(r.status_code == 200, "chat surface answered",
                      f"HTTP {r.status_code}")
                async for line in r.aiter_lines():
                    chunks.append(line)
            reply = "\n".join(chunks)

            # -- 1. routing was the deterministic BROWSE shortcut --------------
            async with AsyncSessionLocal() as db:
                rows = (await db.execute(
                    select(RoutingDecision).order_by(RoutingDecision.id.desc())
                )).scalars().all()
            row = rows[0] if rows else None
            check(row is not None and row.label == "BROWSE",
                  "routed BROWSE", getattr(row, "label", "<no row>"))
            check(row is not None and (row.classifier_ms in (None, 0)),
                  "decided in code — no classifier call",
                  f"classifier_ms={getattr(row, 'classifier_ms', '?')}")

            # -- 2. it went to the BROWSER agent as a background task ----------
            print("\n  waiting for the background browse to settle…")
            settled = None
            for _ in range(240):
                async with AsyncSessionLocal() as db:
                    tasks = (await db.execute(
                        select(Task).order_by(Task.id.desc())
                    )).scalars().all()
                if tasks and tasks[0].status not in ("running", "pending"):
                    settled = tasks[0]
                    break
                await asyncio.sleep(2)

            check(settled is not None, "the background task settled",
                  getattr(settled, "status", "still running"))
            if settled is None:
                return 1
            check(settled.domain == "browser", "owned by the browser agent",
                  str(settled.domain))
            check(settled.status == "completed", "completed",
                  str(settled.status))

            # -- 3. THE FIX: the executed step is browse, never read_webpage ---
            payload = json.loads(settled.plan_payload or "{}")
            tools = [s.get("tool") for s in (payload.get("steps") or [])]
            check("browse" in tools, "the plan DROVE the browser", str(tools))
            check("read_webpage" not in tools,
                  "read_webpage never ran (the incident)", str(tools))

            step = next((s for s in (payload.get("steps") or [])
                         if s.get("tool") == "browse"), {})
            out = (step.get("result") or {}).get("output") or {}
            check(bool(out.get("window_open")),
                  "the window was left OPEN — what 'open' means",
                  f"window_open={out.get('window_open')}")
            check(bool(out.get("destination_only")),
                  "terminated on ARRIVAL (nothing searched, nothing played)",
                  f"destination_only={out.get('destination_only')}")
            blocked = out.get("blocked") or {}
            check(not blocked.get("commits"),
                  "nothing was ever submitted", str(blocked.get("commits")))

            # -- 4. what the user actually reads ------------------------------
            async with AsyncSessionLocal() as db:
                msgs = (await db.execute(
                    select(Message).where(Message.session_id == SESSION)
                )).scalars().all()
            final = [m.content for m in msgs
                     if m.role == "assistant" and "Finished" in (m.content or "")]
            text = final[-1] if final else ""
            check(bool(text), "a completion message was persisted",
                  f"{len(text)} chars")
            check(len(text) < 400,
                  "the completion is ONE LINE, not a page",
                  f"{len(text)} chars (the incident was ~1800+)")
            check("Select Your Country" not in text and "TRACKING INFO" not in text,
                  "the homepage dump is gone",
                  text[:90].replace("\n", " ⏎ "))
            print(f"\n  --- what the user sees ---\n  {text[:300]}\n")

    finally:
        # Close the real window we opened, and stop the instance (it holds the
        # embedded-Qdrant lock).
        try:
            from app.browser import window as browser_window
            from app.browser.runtime import run_browser
            await run_browser(browser_window.close_all())
        except Exception as exc:
            print(f"  (window cleanup: {exc})")
        server.should_exit = True
        await task

    print("\n=== RESULT ===")
    failed = [label for ok, label in RESULTS if not ok]
    for ok, label in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    print(f"\n  {len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
