"""
Furi OS — browser PAGE-LOAD speed harness (A/B against an un-intercepted control).

WHY THIS EXISTS. Three separate rounds have tuned this stack for speed by moving
constants — the per-request tax (2026-07-19), the event-driven settle
(2026-07-23), the action/deadline budgets (2026-07-26) — and not one of them left
behind a way to re-measure page load. So "the browser feels slow" has never been
a number, and the one cost nobody looked at was the cost Playwright imposes on
Chromium UNDERNEATH us: registering any route makes the driver send
`Network.setCacheDisabled: true` for the whole session and pause every request.

This measures exactly that, by loading the same URLs two ways in one process:

  * ARM "jarvis"  — a real BrowserSession, so the real interceptor, the real
                    allowlist and the real launch flags are all in play.
  * ARM "control" — the same Chromium on the same profile with NO route
                    registered. This is the "normal browser" the complaint
                    compares against, so the gap is measured, not imagined.

Each URL is loaded TWICE per arm. The second load is the headline: a browser
with a working cache serves most of a repeat page from memory/disk, and one with
`setCacheDisabled` re-downloads all of it. If load-2 is not much faster than
load-1, the cache is off.

Request counts come from `page.on("request")` on BOTH arms — a Network-domain
event Playwright already receives, which does NOT enable Fetch interception, so
counting costs the control nothing and the two arms stay comparable.

Run from backend/:

    venv\\Scripts\\python -u scripts\\browse_speed.py
    venv\\Scripts\\python -u scripts\\browse_speed.py --keep-cache
    venv\\Scripts\\python -u scripts\\browse_speed.py https://example.com

NEVER collected by pytest (lives outside tests/, drives a real browser over the
real network). Results are written to scripts/bench-results/ so a change is a
DELTA, exactly like browse_bench.py.
"""
from __future__ import annotations

import asyncio
import functools
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Progress must be visible WHILE a load runs — stdout is fully buffered when
# piped, and a run takes minutes (the browse_bench.py lesson).
print = functools.partial(print, flush=True)  # noqa: A001

RESULTS_DIR = Path(__file__).resolve().parent / "bench-results"

# Real sites, deliberately spanning the weight range: a trivial static page (the
# floor — any gap here is pure overhead, not the site), two static content sites,
# and heavy commercial pages with many subresources, which is where a disabled
# cache costs the most. These are the same properties browse_tasks.json uses, so
# a speed result and a correctness result are talking about the same web.
URLS = [
    "https://example.com",
    "https://books.toscrape.com",
    "https://quotes.toscrape.com",
    "https://en.wikipedia.org/wiki/Chromium_(web_browser)",
    "https://www.daraz.pk",
    "https://www.ebay.com",
]

NAV_TIMEOUT_MS = 30_000
COMPLETE_CAP_SECONDS = 25.0
COMPLETE_POLL_SECONDS = 0.1
# Chromium's own HTTP cache lives in these profile subdirectories. Cleared
# between arms so both start cold and load-1 means the same thing on each side.
# Cookies, logins and history live in OTHER files and are never touched — the
# "log in once, the profile keeps you signed in" property is preserved.
_CACHE_DIRS = ("Cache", "Code Cache", "GPUCache", "Service Worker")


def _host(url: str) -> str:
    from urllib.parse import urlparse

    return (urlparse(url).hostname or "").lower()


def _clear_profile_cache() -> None:
    """Drop the profile's HTTP cache so an arm starts cold. Best-effort and
    NEVER raises (a measurement must not be able to break the profile); a
    directory still locked by a live Chromium is simply left alone."""
    from app.browser.session import BROWSER_PROFILE_DIR

    default = BROWSER_PROFILE_DIR / "Default"
    removed = []
    for name in _CACHE_DIRS:
        target = default / name
        try:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
                removed.append(name)
        except Exception as exc:  # noqa: BLE001
            print(f"    (cache clear {name}: {type(exc).__name__}: {exc})")
    if removed:
        print(f"    (cleared profile cache: {', '.join(removed)})")


# ------------------------------------------------------------------ one load
async def _load_once(page, url: str, counter: dict) -> dict:
    """Load one URL and time two DEFINITIVE, arm-independent milestones.

    `commit` — the response arrived and the document is being built. This is the
    same signal session.goto() uses for phase A, so it is the honest "did the
    network answer" number.
    `complete` — document.readyState === 'complete', i.e. the load event fired
    and every subresource finished. It is the one milestone the platform
    defines, which is why it is measured here instead of Furi's own readiness
    predicate: the control has no such predicate, and a metric only one arm can
    produce is not a comparison.
    """
    counter["n"] = 0
    started = time.perf_counter()
    commit_s = None
    error = ""
    try:
        await page.goto(url, wait_until="commit", timeout=NAV_TIMEOUT_MS)
        commit_s = time.perf_counter() - started
    except Exception as exc:  # noqa: BLE001 — a dead site must not end the run
        error = f"{type(exc).__name__}: {str(exc)[:120]}"

    complete = False
    if not error:
        deadline = started + COMPLETE_CAP_SECONDS
        while time.perf_counter() < deadline:
            try:
                if await page.evaluate("() => document.readyState") == "complete":
                    complete = True
                    break
            except Exception:  # noqa: BLE001 — a navigating page can refuse eval
                pass
            await asyncio.sleep(COMPLETE_POLL_SECONDS)

    return {
        "commit_s": round(commit_s, 2) if commit_s is not None else None,
        "complete_s": round(time.perf_counter() - started, 2),
        "completed": complete,
        "requests": counter["n"],
        "error": error,
    }


async def _measure_arm(arm: str, urls: list[str], keep_cache: bool) -> list[dict]:
    """One arm, one Chromium, every URL loaded twice.

    Runs ENTIRELY inside a single coroutine because Playwright objects are bound
    to the loop that created them, and this whole function is marshaled onto the
    dedicated browser loop by run_browser() (the browser_runtime contract).
    """
    from app.browser import session as browser_session

    if not keep_cache:
        _clear_profile_cache()

    counter = {"n": 0}
    rows: list[dict] = []
    session = None
    browser = None

    if arm == "jarvis":
        # The REAL thing: real interceptor, real allowlist, real launch flags.
        session = await browser_session.BrowserSession.open({_host(u) for u in urls})
        page = session.page
    else:
        # The control: the same Chromium, the same profile, the same flags — and
        # no route. Nothing else may differ, or the comparison measures the
        # difference in the harness instead of the difference under test.
        browser = await browser_session._launch()
        page = await browser.new_page()

    try:
        page.on("request", lambda _req: counter.__setitem__("n", counter["n"] + 1))
        for url in urls:
            print(f"  [{arm}] {url}")
            first = await _load_once(page, url, counter)
            # A blank page between loads so load 2 is a genuine re-navigation and
            # not a no-op — while leaving the cache exactly as load 1 left it.
            try:
                await page.goto("about:blank", wait_until="commit", timeout=10_000)
            except Exception:  # noqa: BLE001
                pass
            second = await _load_once(page, url, counter)
            rows.append({"url": url, "arm": arm, "first": first, "second": second})
            print(
                f"      1st {_fmt(first)}   2nd {_fmt(second)}"
                + (f"   !! {first['error'] or second['error']}"
                   if (first["error"] or second["error"]) else "")
            )
    finally:
        if session is not None:
            await session.close()
        elif browser is not None:
            await browser_session._maybe_await(browser.close())

    if session is not None:
        s = session.stats
        print(
            f"  [{arm}] interceptor saw {s.total_requests} requests, "
            f"{s.ssrf_checks} ssrf checks, {s.blocked_ads} ads blocked"
        )
    return rows


def _fmt(load: dict) -> str:
    if load["error"]:
        return "  ERROR"
    done = "" if load["completed"] else "~"
    return f"{done}{load['complete_s']:>5.2f}s / {load['requests']:>3} req"


# --------------------------------------------------------------------- report
def _report(rows: list[dict]) -> None:
    by_url: dict[str, dict[str, dict]] = {}
    for row in rows:
        by_url.setdefault(row["url"], {})[row["arm"]] = row

    print("\n" + "=" * 96)
    print(
        f"{'url':38}  {'control 1st':>11}  {'control 2nd':>11}  "
        f"{'jarvis 1st':>11}  {'jarvis 2nd':>11}  {'slower':>7}"
    )
    print("-" * 96)
    ratios = []
    for url, arms in by_url.items():
        ctl, jar = arms.get("control"), arms.get("jarvis")
        if not ctl or not jar:
            continue
        c1, c2 = ctl["first"]["complete_s"], ctl["second"]["complete_s"]
        j1, j2 = jar["first"]["complete_s"], jar["second"]["complete_s"]
        ratio = (j2 / c2) if c2 else 0.0
        if c2 and not (ctl["second"]["error"] or jar["second"]["error"]):
            ratios.append(ratio)
        short = url.replace("https://", "")[:38]
        print(
            f"{short:38}  {c1:>10.2f}s  {c2:>10.2f}s  "
            f"{j1:>10.2f}s  {j2:>10.2f}s  {ratio:>6.1f}x"
        )
    print("-" * 96)
    if ratios:
        median = sorted(ratios)[len(ratios) // 2]
        print(f"median repeat-load slowdown vs an un-intercepted browser: {median:.1f}x")
    # The cache tell, stated as its own line because it is the whole hypothesis:
    # a browser with a working cache gets FASTER on the second load.
    for arm in ("control", "jarvis"):
        speedups = [
            r["first"]["complete_s"] / r["second"]["complete_s"]
            for r in rows
            if r["arm"] == arm and r["second"]["complete_s"] and not r["second"]["error"]
        ]
        if speedups:
            print(
                f"{arm:>8}: repeat load is {sorted(speedups)[len(speedups) // 2]:.2f}x "
                "the speed of the first (>1 means the cache is working)"
            )

    try:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        path = RESULTS_DIR / f"speed-{stamp}.json"
        path.write_text(
            json.dumps({"when": stamp, "loads": rows}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"written: {path}")
    except Exception as exc:  # noqa: BLE001
        print(f"could not write results: {exc}")


# ----------------------------------------------------------------------- main
async def _main_async(urls: list[str], keep_cache: bool) -> int:
    from app.browser.runtime import run_browser
    from app.browser.session import ensure_playwright_driver

    try:
        await run_browser(ensure_playwright_driver(), timeout=90)
        print("playwright driver warm")
    except Exception as exc:  # noqa: BLE001
        print(f"driver warm-up skipped: {type(exc).__name__}: {exc}")

    rows: list[dict] = []
    # CONTROL FIRST, and the profile's HTTP cache is CLEARED at the top of each
    # arm — otherwise whichever arm ran second would inherit a disk cache the
    # first one warmed, and "load 1" would mean something different on each
    # side. The clear is what makes the two arms comparable; the ordering just
    # means the arm under test is never the one that got the clean run.
    for arm in ("control", "jarvis"):
        print(f"\n=== arm: {arm} ===")
        try:
            rows += await run_browser(_measure_arm(arm, urls, keep_cache), timeout=900)
        except Exception as exc:  # noqa: BLE001 — one bad arm still reports the other
            print(f"  arm {arm} failed: {type(exc).__name__}: {exc}")
        await _close_windows()
        _reclaim()

    _report(rows)
    return 0


async def _close_windows() -> None:
    try:
        from app.browser import runtime, session

        await runtime.run_browser(session.shutdown_browser_windows(), timeout=60)
    except Exception as exc:  # noqa: BLE001
        print(f"    (window cleanup: {type(exc).__name__}: {exc})")


def _reclaim() -> None:
    """Kill anything still holding OUR profile — the reaper matches only
    --user-data-dir=<the Furi profile>, never the user's own Chrome."""
    try:
        from app.browser.session import reclaim_orphaned_profile

        if reclaim_orphaned_profile():
            print("    (reclaimed an orphaned browser process)")
    except Exception as exc:  # noqa: BLE001
        print(f"    (reclaim skipped: {type(exc).__name__}: {exc})")


def main() -> int:
    args = sys.argv[1:]
    keep_cache = "--keep-cache" in args
    urls = [a for a in args if a.startswith("http")] or list(URLS)

    # THE SELECTOR LOOP — the same reason browse_bench.py forces it. uvicorn
    # --reload gives production WindowsSelectorEventLoopPolicy, which cannot
    # spawn the Playwright driver subprocess; asyncio.run() on Windows gives
    # Proactor, the one loop where that bug class is invisible.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    code = 1
    try:
        print(f"event loop: {type(loop).__name__}")
        code = loop.run_until_complete(_main_async(urls, keep_cache))
    finally:
        try:
            from app.browser.runtime import shutdown_browser_runtime

            shutdown_browser_runtime()
        except Exception:  # noqa: BLE001
            pass
        _reclaim()

    # HARD EXIT for the same reason browse_bench.py does it: the driver lives on
    # its own loop thread and tearing the interpreter down around it prints a
    # page of "Event loop is closed" noise AFTER the results.
    sys.stdout.flush()
    sys.stderr.flush()
    import os

    os._exit(code)


if __name__ == "__main__":
    raise SystemExit(main())
