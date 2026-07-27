"""
Jarvis OS — browse acceptance benchmark.

WHY THIS EXISTS, stated plainly: this browser stack has repeatedly been called
"live-verified" on the strength of one run that was later contradicted. The
project's own notes record two ways it happened — a check run on a Proactor event
loop that production never uses, and a check whose input matched a prompt example
so the model was recalling the prompt. Both read exactly like a pass.

So the gate is a NUMBER you can re-measure, not a run someone eyeballed:

  * Real `browse` tool, real Chromium, real providers, real websites.
  * Scored in CODE against GROUNDED evidence — a task passes when the records it
    gathered actually contain what was asked for, or the page it reached actually
    says the thing. A confident-sounding answer cannot pass, because no model is
    asked for an opinion anywhere in the scoring.
  * On the SELECTOR event loop, which is what `uvicorn --reload` gives production.
    A standalone asyncio.run() is Proactor on Windows and hides the one bug class
    (Playwright subprocess spawning) that has already shipped twice.
  * Every run written to scripts/bench-results/, so a change is a DELTA and a
    regression is visible instead of anecdotal.

Run from backend/:

    venv\\Scripts\\python scripts\\browse_bench.py                 # everything
    venv\\Scripts\\python scripts\\browse_bench.py daraz-compare   # one task
    venv\\Scripts\\python scripts\\browse_bench.py --list

NEVER collected by pytest (lives outside tests/, drives a real browser and spends
real provider credits). The hermetic suite proves the parts; this proves the whole.
"""
from __future__ import annotations

import asyncio
import functools
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Progress must be visible WHILE a task runs — a browse takes tens of seconds and
# stdout is fully buffered when piped, so the first version of this script looked
# like a hang and produced an empty file.
print = functools.partial(print, flush=True)  # noqa: A001

TASKS_FILE = Path(__file__).resolve().parent / "browse_tasks.json"
RESULTS_DIR = Path(__file__).resolve().parent / "bench-results"


# --------------------------------------------------------------------- scoring
def _records(output: dict) -> list[dict]:
    return [r for r in (output.get("extracted") or []) if isinstance(r, dict)]


def _all_text(output: dict) -> str:
    """Everything the run brought back, for a grounding check. Deliberately does
    NOT include the goal — the point is to test the OUTPUT against the PAGE."""
    parts = [
        str(output.get("page_excerpt") or ""),
        str(output.get("rendered") or ""),
        json.dumps(_records(output), ensure_ascii=False),
    ]
    return "\n".join(parts)


def _check(kind: str, spec: dict, output: dict) -> tuple[bool, str]:
    from app.browser import extract as browser_extract

    records = _records(output)

    if kind == "records_at_least":
        want = int(spec.get("n", 1))
        return len(records) >= want, f"{len(records)} records (needed {want})"

    if kind == "field_present":
        field = str(spec.get("field") or "")
        if not records:
            return False, "no records at all"
        missing = [r for r in records if not r.get(field)]
        return (
            not missing,
            f"{len(records) - len(missing)}/{len(records)} records carry '{field}'",
        )

    if kind == "price_grounded":
        # THE ANTI-FABRICATION CHECK, and the reason scoring is code-only. Every
        # price in the gathered records must be a real currency-shaped token, and
        # must appear verbatim in what the page gave us. A model that invents a
        # tidy price fails here; a model that copies one cannot.
        if not records:
            return False, "no records at all"
        haystack = _all_text(output)
        priced = [str(r.get("price") or "") for r in records if r.get("price")]
        if not priced:
            return False, "no record carried a price"
        bad = [
            p for p in priced
            if browser_extract.find_price(p) is None or p not in haystack
        ]
        return (
            not bad,
            f"{len(priced) - len(bad)}/{len(priced)} prices grounded"
            + (f"; ungrounded: {bad[:3]}" if bad else ""),
        )

    if kind == "url_contains":
        value = str(spec.get("value") or "").lower()
        url = str(output.get("url") or "").lower()
        return value in url, f"final url {url[:90]!r}"

    if kind == "text_contains_any":
        values = [str(v) for v in (spec.get("values") or [])]
        haystack = _all_text(output)
        hit = next((v for v in values if v in haystack), "")
        return bool(hit), (f"found {hit!r}" if hit else f"none of {values} on the page")

    return False, f"unknown check kind {kind!r}"


_HANDOFFS = (
    ("challenge_required", "a CAPTCHA / verification wall (never auto-solved)"),
    ("login_required", "a sign-in wall (Jarvis never enters credentials)"),
    ("action_approval_required", "waiting for the user to approve a world-acting gesture"),
    ("origin_approval_required", "waiting for the user to approve an off-site jump"),
    ("commit_required", "waiting for the user to approve a form submit"),
    ("site_unreachable", "the site was unreachable (DNS / certificate / refused)"),
)


def _handoff_reason(output: dict) -> str:
    """A hand-off the loop raised on purpose, or "" for an ordinary outcome."""
    for flag, reason in _HANDOFFS:
        if output.get(flag):
            return reason
    return ""


# ------------------------------------------------------------------ one task
async def _run_task(task: dict) -> dict:
    from app.tools.browser_agent_tools import BrowseTool

    print(f"\n--- {task['id']} ---")
    print(f"    goal: {task['goal']}")
    if task.get("why"):
        print(f"    why:  {task['why']}")

    started = time.perf_counter()
    result = await BrowseTool().safe_execute(
        goal=task["goal"],
        start_url=task["start_url"],
        allowed_origins=list(task.get("allowed_origins") or []),
        keep_open=False,
    )
    elapsed = time.perf_counter() - started

    output = result.output if isinstance(result.output, dict) else {}
    records = _records(output)

    # BLOCKED is not FAILED, and conflating them makes the number untrustworthy.
    # A CAPTCHA wall, a login wall, or a pause for the user's approval is the loop
    # behaving CORRECTLY — those are hand-offs to a human, and an unattended
    # benchmark cannot answer them. Measured live: eBay served a CAPTCHA
    # interstitial, the loop refused to solve it (a hard rule) and opened a clean
    # window for the user, and the run scored FAIL — reading as a regression in
    # code that had done exactly the right thing. Report it as what it is.
    blocked_reason = _handoff_reason(output)
    if blocked_reason:
        print(f"    BLOCKED  {blocked_reason}")
        print(f"    => BLOCKED in {elapsed:.1f}s (a human hand-off, not a failure)")
        return {
            "id": task["id"],
            "passed": None,
            "blocked": blocked_reason,
            "seconds": round(elapsed, 1),
            "actions": output.get("actions_taken"),
            "records": len(records),
            "url": output.get("url"),
            "error": str(output.get("error") or "")[:300],
        }

    checks = []
    for spec in task.get("checks") or []:
        kind = str(spec.get("kind") or "")
        ok, detail = _check(kind, spec, output)
        checks.append({"kind": kind, "passed": ok, "detail": detail})
        print(f"    {'PASS' if ok else 'FAIL'}  {kind}: {detail}")

    passed = bool(checks) and all(c["passed"] for c in checks)
    row = {
        "id": task["id"],
        "passed": passed,
        "seconds": round(elapsed, 1),
        "actions": output.get("actions_taken"),
        "records": len(records),
        "url": output.get("url"),
        "tool_success": bool(result.success),
        "error": str(output.get("error") or result.error or "")[:300],
        "checks": checks,
        # The sample is kept so a "pass" can be inspected later. A number with no
        # evidence behind it is the thing this harness exists to replace.
        "sample": records[:3],
    }
    print(
        f"    => {'PASS' if passed else 'FAIL'} in {elapsed:.1f}s, "
        f"{output.get('actions_taken')} actions, {len(records)} records"
        + (f" — {row['error']}" if row["error"] else "")
    )
    return row


async def _main_async(only: list[str]) -> int:
    tasks = json.loads(TASKS_FILE.read_text("utf-8"))["tasks"]
    if only:
        tasks = [t for t in tasks if t["id"] in only]
        if not tasks:
            print(f"no task matched {only}")
            return 2

    # Warm the shared Playwright driver once, as main.py's lifespan does. Paying
    # a cold ~5s import inside the first task's budget is how a launch timeout
    # gets misread as a site problem (the 2026-07-21 incident).
    try:
        from app.core.browser_session import ensure_playwright_driver
        from app.core.browser_runtime import run_browser

        await run_browser(ensure_playwright_driver(), timeout=90)
        print("playwright driver warm")
    except Exception as exc:
        print(f"driver warm-up skipped: {type(exc).__name__}: {exc}")

    rows = []
    try:
        for task in tasks:
            try:
                rows.append(await _run_task(task))
            except Exception as exc:  # noqa: BLE001 — one bad task must not end the run
                print(f"    => ERROR {type(exc).__name__}: {exc}")
                rows.append(
                    {"id": task["id"], "passed": False, "error": f"{type(exc).__name__}: {exc}"}
                )
            # EACH TASK STARTS CLEAN. The browse tool deliberately HOLDS its window
            # so a follow-up turn continues the same session — right for a
            # conversation, wrong for a benchmark, where task N would inherit task
            # N-1's page and score something nobody asked for.
            await _close_windows()
    finally:
        # ALWAYS. A held window keeps ~/.jarvis/browser's single-instance lock, and
        # an orphan makes the NEXT run fail to launch — a self-inflicted red that
        # looks exactly like a site problem.
        await _close_windows()
        _reclaim()

    _report(rows)
    # A blocked task is not a failure — exit non-zero only when a SCORED task
    # failed, so this is usable as a gate without a CAPTCHA turning it red.
    return 0 if all(r.get("passed") or r.get("blocked") for r in rows) else 1


async def _close_windows() -> None:
    try:
        from app.core import browser_runtime, browser_session

        await browser_runtime.run_browser(
            browser_session.shutdown_browser_windows(), timeout=60
        )
    except Exception as exc:  # noqa: BLE001
        print(f"    (window cleanup: {type(exc).__name__}: {exc})")


def _reclaim() -> None:
    """The belt under the braces: kill any browser still holding OUR profile. The
    reaper matches only --user-data-dir=<the Jarvis profile>, so a user's own
    Chrome is never touched."""
    try:
        from app.core.browser_session import reclaim_orphaned_profile

        killed = reclaim_orphaned_profile()
        if killed:
            print(f"    (reclaimed {killed} orphaned browser process(es))")
    except Exception as exc:  # noqa: BLE001
        print(f"    (reclaim skipped: {type(exc).__name__}: {exc})")


def _verdict(row: dict) -> str:
    if row.get("blocked"):
        return "BLOCK "
    return "PASS  " if row.get("passed") else "FAIL  "


def _report(rows: list[dict]) -> None:
    width = max([len(str(r["id"])) for r in rows] + [4])
    print("\n" + "=" * (width + 46))
    print(f"{'task'.ljust(width)}  result  {'secs':>6}  {'acts':>4}  {'recs':>4}")
    print("-" * (width + 46))
    for r in rows:
        print(
            f"{str(r['id']).ljust(width)}  "
            f"{_verdict(r)}  "
            f"{str(r.get('seconds', '-')):>6}  "
            f"{str(r.get('actions', '-')):>4}  "
            f"{str(r.get('records', '-')):>4}"
        )
    passed = sum(1 for r in rows if r.get("passed"))
    blocked = [r for r in rows if r.get("blocked")]
    scored = len(rows) - len(blocked)
    seconds = [r["seconds"] for r in rows if isinstance(r.get("seconds"), (int, float))]
    print("-" * (width + 46))
    line = f"{passed}/{scored} scored tasks passed"
    if blocked:
        line += f" ({len(blocked)} blocked by a human hand-off, not counted)"
    if seconds:
        line += f", median {sorted(seconds)[len(seconds) // 2]:.1f}s/task"
    print(line)
    for r in blocked:
        print(f"  blocked: {r['id']} — {r['blocked']}")

    try:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        path = RESULTS_DIR / f"bench-{stamp}.json"
        path.write_text(
            json.dumps(
                {
                    "when": stamp,
                    "passed": passed,
                    "scored": scored,
                    "blocked": len(blocked),
                    "total": len(rows),
                    "tasks": rows,
                },
                indent=2, ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(f"written: {path}")
    except Exception as exc:
        print(f"could not write results: {exc}")


def main() -> int:
    args = [a for a in sys.argv[1:]]
    if "--list" in args:
        for task in json.loads(TASKS_FILE.read_text("utf-8"))["tasks"]:
            print(f"{task['id']:26} {task['goal'][:70]}")
        return 0

    # THE SELECTOR LOOP, and this is the whole point of the file being a script.
    # uvicorn --reload forces WindowsSelectorEventLoopPolicy, which cannot spawn
    # the Playwright driver subprocess — the bug that shipped twice. asyncio.run()
    # on Windows gives Proactor, the one loop where that bug is invisible. So the
    # benchmark deliberately runs on the loop production actually has.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    code = 1
    try:
        print(f"event loop: {type(loop).__name__}")
        code = loop.run_until_complete(
            _main_async([a for a in args if not a.startswith("-")])
        )
    finally:
        try:
            from app.core.browser_runtime import shutdown_browser_runtime

            shutdown_browser_runtime()
        except Exception:
            pass
        _reclaim()

    # HARD EXIT, deliberately. The Playwright driver lives on its own event-loop
    # thread; tearing the interpreter down around it prints a page of
    # "Event loop is closed" / "Task was destroyed" noise AFTER the results — noise
    # that would hide a real error the next time something goes wrong. A benchmark
    # process has nothing left to persist (the results file is already written and
    # stdout is flushed), so leaving is the honest end.
    sys.stdout.flush()
    sys.stderr.flush()
    import os

    os._exit(code)


if __name__ == "__main__":
    raise SystemExit(main())
