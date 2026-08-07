"""Live runtime verification for the 2026-08-07 round-3 pop-under work.

The hermetic suite proves the rules against fakes. Only the real site proves the
fix, because the thing being fixed is a real ad network's pop-under: anikoto's
episode-range selector opens one, and no fake can tell you whether the real one
still costs us the page.

Two lessons from this project's own notes are built in:
  * the SELECTOR event loop, because a standalone asyncio.run() is Proactor on
    Windows and hides the Playwright-subprocess bug class that shipped twice
  * the real `browse` TOOL, not run_browse, so the whole wiring is exercised

⚠️ THIS OPENS A REAL HEADED CHROME WINDOW and spends real provider credits. It
plays nothing (keep_open is False) and submits nothing (browse is READ-level).

Run from backend/:  venv\\Scripts\\python scripts\\_verify_popunder_runtime.py
"""

from __future__ import annotations

import asyncio
import functools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
print = functools.partial(print, flush=True)  # noqa: A001

USER_WORDS = "play latest ep of latest season of bleach on anikoto"
# What the planner actually drafted on the live run (backend.log:18:56).
PLANNER_GOAL = (
    "Find Bleach on anikoto.cz, go to its latest season, and play the latest "
    "episode, keeping the video playing."
)
# The incident's own failure, verbatim.
INCIDENT_ERROR = "Execution context was destroyed"
WRONG_SLUG = "bleach-yaa9n"

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


async def run_once(n: int) -> None:
    print(f"\n== run {n} — the incident goal against the real site ==")
    import app.tools  # noqa: F401  - registers every tool
    from app.tools.registry import registry as tool_registry

    tool = tool_registry.get("browse")
    result = await tool.safe_execute(
        goal=PLANNER_GOAL,
        start_url="https://anikoto.cz",
        allowed_origins=["anikoto.cz"],
        keep_open=False,
        user_words=USER_WORDS,
    )

    output = result.output if isinstance(result.output, dict) else {}
    final_url = str(output.get("url") or "")
    err = str(result.error or "")
    print(f"     success : {result.success}")
    print(f"     url     : {final_url}")
    if err:
        print(f"     error   : {err[:200]}")

    check(
        INCIDENT_ERROR not in err,
        f"run {n}: did not die on the incident's own error",
        f"error was: {err[:160] or '(none)'}",
    )
    check(
        f"{WRONG_SLUG}/ep-" not in final_url,
        f"run {n}: did not fall into the 2004 series",
        f"final = {final_url or '(none)'}",
    )
    check(
        result.success,
        f"run {n}: the browse completed",
        f"error was: {err[:160] or '(none)'}",
    )
    check(
        "the-calamity" in final_url,
        f"run {n}: landed on the airing cour",
        f"final = {final_url or '(none)'}",
    )


def main() -> int:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    runs = 2
    for arg in sys.argv[1:]:
        if arg.startswith("--runs="):
            runs = int(arg.split("=", 1)[1])

    async def _run():
        for n in range(1, runs + 1):
            try:
                await run_once(n)
            except Exception as e:
                check(False, f"run {n}: completed without raising", f"{type(e).__name__}: {e}")

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_run())
    finally:
        try:
            from app.browser import runtime as browser_runtime

            loop.run_until_complete(browser_runtime.shutdown_browser_runtime())
        except Exception:
            pass
        loop.close()

    passed = sum(1 for ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} checks passed")
    for ok, label in results:
        if not ok:
            print(f"   FAILED: {label}")
    print(
        "\nNOTE: a pop-under is the SITE's behaviour and cannot be forced to occur.\n"
        "Grep the run's log for 'never said where it was going' / 'not allowlisted,\n"
        "closing it' to see the new handling fire when one does."
    )
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
