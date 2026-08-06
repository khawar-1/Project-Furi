"""Runtime verification for open_folder (2026-08-06).

A hermetic test cannot tell you the wiring BOOTS. This drives the REAL main.py
lifespan on an isolated port with a scratch DB, so the chain is exercised end
to end: migrations, the tool registry, the structural approval gate, the real
path guards against this machine's real protected directories, and — once, at
the end — the real launcher.

⚠️ EVERYTHING RUNS IN ONE PROCESS/INVOCATION. The 2026-08-03 lesson: a probe
that stashed its token between shell invocations sent every request with an
EMPTY token, got 401 on everything, and "all denied" read exactly like success.
A negative result is only evidence if the positive control passes in the same
breath — which is why the real open at the end is not optional.

⚠️ IT OPENS EXACTLY ONE REAL WINDOW, on a scratch temp folder, announced. Every
other check runs with the launcher stubbed. A verification of "can Jarvis open
a folder" that never opens a folder is not a verification.

Run:  venv\\Scripts\\python scripts\\_verify_open_folder_runtime.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
API_PORT = 18003

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    RESULTS.append((ok, label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  — {detail}" if detail else ""))


async def main() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="jarvis-openfolder-verify-"))
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{scratch / 'scratch.db'}"
    os.environ["QDRANT_HOST"] = ""
    os.environ["BACKEND_PORT"] = str(API_PORT)
    os.environ["REMOTE_ENABLED"] = "false"
    os.environ["DEBUG"] = "false"
    sys.path.insert(0, str(ROOT))

    import uvicorn
    from main import app
    from app.core.auth import get_or_create_token

    config = uvicorn.Config(app, host="127.0.0.1", port=API_PORT, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(300):
        if server.started:
            break
        await asyncio.sleep(0.1)
    if not server.started:
        print("backend never started")
        return 1
    print(f"real backend lifespan booted on :{API_PORT}\n")

    import httpx

    from app.db.database import AsyncSessionLocal
    from app.tools import file_tools
    from app.tools.registry import execute_tool
    from sqlalchemy import select
    from app.db.models import ActivityLog

    headers = {"X-Jarvis-Token": get_or_create_token()}
    base = f"http://127.0.0.1:{API_PORT}"

    # Everything below runs against a stub until the very last check.
    launched: list[Path] = []
    real_launcher = file_tools.OPEN_LAUNCHER
    file_tools.OPEN_LAUNCHER = lambda folder: launched.append(folder)

    demo = scratch / "jarvis-open-folder-demo"
    demo.mkdir()
    (demo / "hello.txt").write_text("opened by Jarvis", encoding="utf-8")

    try:
        async with httpx.AsyncClient(headers=headers, timeout=30) as http:
            # -- the tool is in the REAL catalog the planner is shown ---------
            r = await http.get(f"{base}/api/agent/tools")
            tools = r.json()  # a bare list, not {"tools": [...]}
            names = {t["name"] for t in tools}
            check("open_folder" in names,
                  "open_folder is in the live tool catalog", f"{len(names)} tools")
            spec = next((t for t in tools if t["name"] == "open_folder"), {})
            check(spec.get("permission_level") == "write",
                  "it is WRITE, so the approval gate applies",
                  str(spec.get("permission_level")))
            check(set((spec.get("parameters") or {}).get("properties", {})) == {"path"},
                  "it exposes only 'path' — no command, no arguments")

        async with AsyncSessionLocal() as db:
            # -- ⚠️ THE CORE SAFETY CHECK: no approval, no window -------------
            launched.clear()
            res = await execute_tool("open_folder", {"path": str(demo)}, db)
            check(
                (not res.success) and res.requires_approval and launched == [],
                "AN UNAPPROVED OPEN NEVER REACHES THE OS",
                f"launched={len(launched)}",
            )

            # -- the real protected roots on THIS machine ---------------------
            launched.clear()
            protected = file_tools._PROTECTED
            res = await execute_tool(
                "open_folder", {"path": str(protected[0])}, db, approved=True,
            )
            check(
                (not res.success) and launched == [],
                "a real protected system directory is refused even when approved",
                str(protected[0]),
            )

            # -- a guessed folder fails with usable guidance ------------------
            launched.clear()
            res = await execute_tool(
                "open_folder", {"path": str(scratch / "ghost")}, db, approved=True,
            )
            check(
                (not res.success) and "search_files" in (res.error or "")
                and launched == [],
                "a path that does not exist is refused and names the way out",
            )

            # -- ⚠️ THE INVARIANT, on a real file -----------------------------
            launched.clear()
            res = await execute_tool(
                "open_folder", {"path": str(demo / "hello.txt")}, db, approved=True,
            )
            check(
                res.success and launched == [demo]
                and res.output.get("showed_containing_folder") is True,
                "A FILE OPENS ITS FOLDER — the file itself never reaches the OS",
                f"opened {launched[0].name if launched else '(nothing)'}",
            )

            # -- the audit trail recorded it ----------------------------------
            rows = (await db.execute(
                select(ActivityLog).where(ActivityLog.tool_name == "open_folder")
            )).scalars().all()
            check(len(rows) >= 4,
                  "every attempt is audited, blocked ones included",
                  f"{len(rows)} rows")

        # -- ONE real window, with the real launcher ---------------------------
        print("\n  … opening ONE real Explorer window on a temp folder now.\n")
        file_tools.OPEN_LAUNCHER = real_launcher
        async with AsyncSessionLocal() as db:
            res = await execute_tool(
                "open_folder", {"path": str(demo)}, db, approved=True,
            )
        check(res.success and res.output.get("opened") == str(demo),
              "THE REAL LAUNCHER OPENS THE FOLDER (close the window when done)",
              res.error or str(demo))

    finally:
        file_tools.OPEN_LAUNCHER = real_launcher
        server.should_exit = True
        await task
        # The demo folder is left on disk for a moment: deleting it out from
        # under the window that was just opened would make the proof vanish.
        print(f"\n  (scratch left at {scratch} — safe to delete)")

    passed = sum(1 for ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    for ok, label in RESULTS:
        if not ok:
            print(f"  FAILED: {label}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
