"""Runtime verification for Desktop control (Feature 2).

A hermetic test cannot tell you the wiring BOOTS. This drives the REAL main.py
lifespan on an isolated port with a scratch DB, so the whole chain is exercised
end to end: migrations, router mount, config persistence, the tool registry, the
structural approval gate, and the real ctypes controller reading this machine.

⚠️ EVERYTHING RUNS IN ONE PROCESS/INVOCATION. The 2026-08-03 lesson: a probe
that stashed its token in a temp file between shell invocations sent every
request with an EMPTY token, got 401 on everything, and "all denied" read
exactly like success. A negative result is only evidence if the positive control
passes in the same breath.

⚠️ AND IT IS DELIBERATELY READ-MOSTLY ON THE REAL MACHINE. This probe runs on
the developer's own desktop, so it never closes a window, never launches an app
and never leaves the clipboard or the volume changed: the two state-changing
checks (volume, clipboard) SAVE the user's value first and assert it is restored
at the end. Everything destructive is proven through the gate — i.e. by showing
it does NOT happen without approval.

Run:  venv\\Scripts\\python scripts\\_verify_desktop_runtime.py
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
API_PORT = 18002

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    RESULTS.append((ok, label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  — {detail}" if detail else ""))


async def main() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="jarvis-desktop-verify-"))
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{scratch / 'scratch.db'}"
    os.environ["QDRANT_HOST"] = ""
    os.environ["BACKEND_PORT"] = str(API_PORT)
    os.environ["REMOTE_ENABLED"] = "false"
    os.environ["DEBUG"] = "false"  # SQL echo drowns the checks
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

    headers = {"X-Jarvis-Token": get_or_create_token()}
    base = f"http://127.0.0.1:{API_PORT}"

    # Save the user's real state up front — restored in the finally below.
    from app.core.desktop import get_controller

    controller = get_controller()
    saved_volume = controller.get_volume()
    saved_clipboard = controller.read_clipboard()

    try:
        async with httpx.AsyncClient(headers=headers, timeout=30) as http:
            # -- the router is mounted --------------------------------------
            r = await http.get(f"{base}/api/desktop/settings")
            check(r.status_code == 200, "GET /api/desktop/settings is mounted",
                  f"HTTP {r.status_code}")
            body = r.json()
            check(body.get("enabled") is False,
                  "desktop control defaults OFF (opt-in)", str(body.get("enabled")))
            check(
                body.get("allow_close") is False
                and body.get("allow_clipboard") is False
                and body.get("allow_screenshot") is False,
                "the three risky capabilities default OFF",
                f"close={body.get('allow_close')} clip={body.get('allow_clipboard')} "
                f"shot={body.get('allow_screenshot')}",
            )
            check(body.get("supported") is True,
                  "this machine reports desktop control as supported",
                  body.get("detail") or "")

            # -- validation is a 400, not a silent clamp --------------------
            r = await http.put(f"{base}/api/desktop/settings", json={
                "enabled": True, "screenshot_retention_days": 99999})
            check(r.status_code == 400, "an absurd retention is a 400", r.text[:70])

            # -- a real save ------------------------------------------------
            r = await http.put(f"{base}/api/desktop/settings", json={
                "enabled": True, "allow_launch": True, "allow_close": True,
                "allow_input": True, "allow_clipboard": True,
                "allow_screenshot": True, "screenshot_retention_days": 7,
            })
            check(r.status_code == 200 and r.json()["enabled"] is True,
                  "settings persist", r.text[:80])

            # -- the app registry is real -----------------------------------
            r = await http.get(f"{base}/api/desktop/apps")
            apps = r.json()
            check(apps.get("count", 0) > 0,
                  "the Start Menu registry is readable over HTTP",
                  f"{apps.get('count')} apps")

            # -- the tools, through the REAL registry and gate ---------------
            from app.db.database import AsyncSessionLocal
            from app.tools.registry import execute_tool

            async with AsyncSessionLocal() as db:
                res = await execute_tool("list_windows", {}, db)
                windows = (res.output or {}).get("windows") or []
                check(res.success and len(windows) > 0,
                      "list_windows reads this machine's real windows",
                      f"{len(windows)} open")
                if windows:
                    check(
                        all(w.get("handle") and w.get("title") for w in windows),
                        "every window row carries the handle the lock grounds on",
                        str(windows[0].get("title", ""))[:40],
                    )

                # ⚠️ THE CORE SAFETY CHECK. A real window handle, a real
                # machine — and no approval. Nothing may close.
                if windows:
                    target = windows[0]
                    res = await execute_tool(
                        "close_window",
                        {"handle": target["handle"], "title": target["title"]},
                        db, approved=False,
                    )
                    still_open = [
                        w for w in
                        ((await execute_tool("list_windows", {}, db)).output or {}).get("windows", [])
                        if w["handle"] == target["handle"]
                    ]
                    check(
                        (not res.success) and res.requires_approval and bool(still_open),
                        "AN UNAPPROVED CLOSE NEVER REACHES A REAL WINDOW",
                        f"'{target['title'][:34]}' still open",
                    )

                # A handle-reuse refusal, against a live window.
                if windows:
                    res = await execute_tool(
                        "close_window",
                        {"handle": windows[0]["handle"], "title": "Some Other Window"},
                        db, approved=True,
                    )
                    check(not res.success and "Refused" in (res.error or ""),
                          "a title mismatch refuses to close, even when approved",
                          (res.error or "")[:60])

                # launch_app cannot reach outside the registry.
                res = await execute_tool(
                    "launch_app", {"name": r"C:\Windows\System32\cmd.exe"},
                    db, approved=True,
                )
                check(not res.success and "No installed application" in (res.error or ""),
                      "launch_app cannot be handed a path",
                      (res.error or "")[:60])

                # Volume: change it, read it back, restore it.
                res = await execute_tool("set_volume", {"level": 42}, db, approved=True)
                check(res.success and res.output.get("level") == 42,
                      "set_volume really moves this machine's volume",
                      str(res.output))

                res = await execute_tool("set_volume", {"level": 500}, db, approved=True)
                check(not res.success, "an absurd volume fails rather than clamping",
                      (res.error or "")[:50])

                # Clipboard round trip.
                res = await execute_tool(
                    "write_clipboard", {"text": "jarvis-desktop-runtime-probe"},
                    db, approved=True,
                )
                res = await execute_tool("read_clipboard", {}, db)
                check(
                    res.success and res.output.get("text") == "jarvis-desktop-runtime-probe",
                    "the clipboard round-trips through the tools",
                    str(res.output.get("text"))[:40],
                )

                # Screenshot: a PATH, and never bytes.
                res = await execute_tool("take_screenshot", {}, db)
                path = (res.output or {}).get("path", "")
                check(res.success and path.endswith(".png") and Path(path).is_file(),
                      "take_screenshot writes a real file", path)
                check(
                    not any(isinstance(v, (bytes, bytearray)) for v in (res.output or {}).values()),
                    "the screenshot result carries NO image bytes",
                    "path + size only",
                )
                if path:
                    Path(path).unlink(missing_ok=True)

                # The sub-toggle, live.
                await http.put(f"{base}/api/desktop/settings", json={
                    "enabled": True, "allow_launch": True, "allow_close": True,
                    "allow_input": True, "allow_clipboard": False,
                    "allow_screenshot": True, "screenshot_retention_days": 7,
                })
                res = await execute_tool("read_clipboard", {}, db)
                check(not res.success and "switched off" in (res.error or ""),
                      "a sub-toggle switched off really blocks its tool",
                      (res.error or "")[:50])

                # The master switch, live.
                await http.put(f"{base}/api/desktop/settings", json={"enabled": False})
                res = await execute_tool("list_windows", {}, db)
                check(not res.success and "turned off" in (res.error or ""),
                      "the master switch really blocks every tool",
                      (res.error or "")[:50])
    finally:
        # ⚠️ Put the developer's machine back exactly as it was.
        try:
            controller.set_volume(saved_volume.level, saved_volume.muted)
            controller.write_clipboard(saved_clipboard)
        except Exception as e:  # noqa: BLE001
            print(f"  !! could not restore machine state: {e}")
        server.should_exit = True
        await task
        shutil.rmtree(scratch, ignore_errors=True)

    now = controller.get_volume()
    check(
        now.level == saved_volume.level and now.muted == saved_volume.muted,
        "the user's volume was restored",
        f"{now.level}/{now.muted} (was {saved_volume.level}/{saved_volume.muted})",
    )
    check(controller.read_clipboard() == saved_clipboard,
          "the user's clipboard was restored", f"{len(saved_clipboard)} chars")

    failed = [label for ok, label in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    for label in failed:
        print(f"  FAILED: {label}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
