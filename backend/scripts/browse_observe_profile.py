"""
Jarvis OS — where does OBSERVE time actually go? (2026-08-02)

WHY THIS EXISTS. The junaidjamshed run recorded `observe_ms` of 28,993 then
64,157 on a single page, while browse_bench sites observe in 30–160ms — and
`goto` reported "never reached readiness in 15000ms" on the same loads. Three
rounds of this stack have been tuned by moving a constant; the rule since
2026-07-27 is that a timeout does not move without a measured cause, and
`trace.py` only reports observe as ONE number, which cannot say whose cost it is.

So this splits `observe()` into the parts that can be slow and runs them against
a control site, on the SAME machine in the SAME minutes (the 2026-08-01 lesson:
a number recorded on another day is not a control):

    goto        navigation to the load state the loop waits for
    settle      the DOM-quiet detector
    top_eval    ONE page.evaluate(_EXTRACT_JS) over the top document
    frames      _worthwhile_frames + one evaluate per qualifying frame
    render      Observation.render() — pure Python over what was captured
    raw_eval    the SAME evaluate on an UN-INTERCEPTED control page

`raw_eval` is the load-bearing comparison. If our evaluate and the control's
cost the same, the time is the page's weight and nothing in our code is to
blame; if ours is materially worse, the interception path is, and that is a bug
we own.

Run from backend/:

    venv\\Scripts\\python -u scripts\\browse_observe_profile.py
    venv\\Scripts\\python -u scripts\\browse_observe_profile.py https://example.com

NEVER collected by pytest (lives outside tests/, drives a real browser over the
real network). Results are written to scripts/bench-results/ so a change is a
DELTA, like browse_bench.py and browse_speed.py.
"""
from __future__ import annotations

import asyncio
import functools
import json
import statistics
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

print = functools.partial(print, flush=True)  # noqa: A001

RESULTS_DIR = Path(__file__).resolve().parent / "bench-results"

# The page the incident was on, plus controls spanning the weight range: a
# trivial static page (the floor), a content site, and a second heavy commercial
# page so "heavy storefront" is represented by more than one sample.
DEFAULT_URLS = [
    "https://www.junaidjamshed.com/",
    "https://books.toscrape.com/",
    "https://en.wikipedia.org/wiki/Pakistan",
    "https://www.daraz.pk/",
]

REPEATS = 2


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


async def _timed(coro):
    start = time.perf_counter()
    try:
        value = await coro
        return value, (time.perf_counter() - start) * 1000.0, ""
    except Exception as exc:  # noqa: BLE001
        return None, (time.perf_counter() - start) * 1000.0, f"{type(exc).__name__}: {exc}"


async def _profile_one(session, page, url: str) -> dict:
    from app.browser import observe as dom_observe

    row: dict = {"url": url}

    _, row["goto_ms"], row["goto_error"] = await _timed(session.goto(url))
    _, row["settle_ms"], _ = await _timed(session.settle())

    # The top document — ONE evaluate, the same call observe() makes first.
    raw, row["top_eval_ms"], row["top_error"] = await _timed(
        page.evaluate(dom_observe._EXTRACT_JS, {"obsId": "profile00000", "base": 0})
    )
    row["elements_top"] = int((raw or {}).get("total") or 0)
    row["text_len"] = len(str((raw or {}).get("text") or ""))

    # The frame half: discovery plus one evaluate per qualifying frame.
    frames, row["frame_scan_ms"], _ = await _timed(dom_observe._worthwhile_frames(page))
    frames = frames or []
    row["frames"] = len(frames)
    frame_start = time.perf_counter()
    for _fid, frame, _box in frames:
        try:
            await frame.evaluate(dom_observe._EXTRACT_JS, {"obsId": "profile00000", "base": 0})
        except Exception:  # noqa: BLE001 — a refusing frame is normal
            pass
    row["frame_eval_ms"] = (time.perf_counter() - frame_start) * 1000.0

    # The whole thing, as the loop calls it, plus the pure-Python render.
    obs, row["observe_ms"], row["observe_error"] = await _timed(dom_observe.observe(page))
    if obs is not None:
        start = time.perf_counter()
        rendered = dom_observe.render(obs)
        row["render_ms"] = (time.perf_counter() - start) * 1000.0
        row["elements_total"] = obs.element_total
        row["rendered_chars"] = len(rendered)

    return row


async def _measure_jarvis(urls: list[str]) -> list[dict]:
    """The real arm. ONE coroutine, marshaled whole onto the browser loop —
    Playwright objects are bound to the loop that made them (the browser_runtime
    contract)."""
    from app.browser import session as browser_session

    session = await browser_session.BrowserSession.open({_host(u) for u in urls})
    rows: list[dict] = []
    try:
        for url in urls:
            for run in range(REPEATS):
                print(f"  [jarvis] {url}  (run {run + 1}/{REPEATS})")
                row = await _profile_one(session, session.page, url)
                row["run"] = run + 1
                rows.append(row)
                print(
                    "      goto {goto_ms:7.0f}  settle {settle_ms:6.0f}  "
                    "top_eval {top_eval_ms:7.0f}  frames {frames}/{frame_eval_ms:.0f}  "
                    "observe {observe_ms:7.0f}  els {els}".format(
                        els=row.get("elements_total", 0),
                        **{k: row.get(k, 0.0) for k in
                           ("goto_ms", "settle_ms", "top_eval_ms", "frames",
                            "frame_eval_ms", "observe_ms")},
                    )
                )
    finally:
        await session.close()
    return rows


async def _measure_control(urls: list[str]) -> list[dict]:
    """THE CONTROL: the same JS on a Chromium with no route registered and no CDP
    Fetch enabled.

    ⚠️ A SEPARATE PASS, never alongside the arm above. `~/.jarvis/browser` is one
    persistent profile and at most one Chromium may hold it — running both at
    once does not measure a control, it fails to launch one (which is exactly
    what the first cut of this script did). browse_speed.py runs its arms
    sequentially for the same reason.
    """
    from app.browser import observe as dom_observe
    from app.browser import session as browser_session

    browser = await browser_session._launch()
    page = await browser.new_page()
    rows: list[dict] = []
    try:
        for url in urls:
            for run in range(REPEATS):
                row: dict = {"url": url, "run": run + 1}
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                except Exception as exc:  # noqa: BLE001
                    row["nav_error"] = f"{type(exc).__name__}: {exc}"
                _, row["raw_eval_ms"], row["raw_error"] = await _timed(
                    page.evaluate(dom_observe._EXTRACT_JS, {"obsId": "profile00000", "base": 0})
                )
                rows.append(row)
                print(f"  [control] {url}  raw_eval {row['raw_eval_ms']:.0f}ms")
    finally:
        try:
            await page.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await browser.close()
        except Exception:  # noqa: BLE001
            pass
    return rows


def _report(rows: list[dict]) -> None:
    print("\n=== observe profile ===")
    by_url: dict[str, list[dict]] = {}
    for row in rows:
        by_url.setdefault(row["url"], []).append(row)

    print(
        f"{'url':<44}{'goto':>8}{'settle':>8}{'top':>8}{'frames':>8}"
        f"{'observe':>9}{'raw':>8}{'ours/raw':>10}{'els':>7}"
    )
    for url, group in by_url.items():
        def med(key):
            vals = [r[key] for r in group if isinstance(r.get(key), (int, float))]
            return statistics.median(vals) if vals else float("nan")

        top, raw = med("top_eval_ms"), med("raw_eval_ms")
        ratio = (top / raw) if raw and raw == raw and raw > 0 else float("nan")
        print(
            f"{url[:43]:<44}{med('goto_ms'):>8.0f}{med('settle_ms'):>8.0f}"
            f"{top:>8.0f}{med('frame_eval_ms'):>8.0f}{med('observe_ms'):>9.0f}"
            f"{raw:>8.0f}{ratio:>10.2f}{med('elements_total'):>7.0f}"
        )
    print(
        "\nours/raw is the verdict: ~1.0 means the time is the PAGE's weight and "
        "none of it is ours;\nmaterially above 1.0 means the interception path is "
        "paying for it and that is our bug."
    )

    try:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        path = RESULTS_DIR / f"observe-{stamp}.json"
        path.write_text(json.dumps(rows, indent=1), encoding="utf-8")
        print(f"written: {path}")
    except Exception as exc:  # noqa: BLE001
        print(f"could not write results: {exc}")


async def _main_async(urls: list[str]) -> int:
    from app.browser.runtime import run_browser
    from app.browser.session import ensure_playwright_driver

    try:
        await run_browser(ensure_playwright_driver(), timeout=90)
        print("playwright driver warm")
    except Exception as exc:  # noqa: BLE001
        print(f"driver warm-up skipped: {type(exc).__name__}: {exc}")

    rows: list[dict] = []
    # ONE ARM AT A TIME — the single persistent profile allows exactly one live
    # Chromium, and the windows are closed + the profile reclaimed between them.
    for label, arm in (("jarvis", _measure_jarvis), ("control", _measure_control)):
        print(f"\n=== arm: {label} ===")
        try:
            rows += await run_browser(arm(urls), timeout=1800)
        except Exception as exc:  # noqa: BLE001 — one bad arm still reports the other
            print(f"  arm {label} failed: {type(exc).__name__}: {exc}")
        try:
            from app.browser import runtime, session

            await runtime.run_browser(session.shutdown_browser_windows(), timeout=60)
        except Exception:  # noqa: BLE001
            pass
        _reclaim()

    if rows:
        _report(rows)
    return 0 if rows else 1


def _reclaim() -> None:
    try:
        from app.browser.session import reclaim_orphaned_profile

        if reclaim_orphaned_profile():
            print("    (reclaimed an orphaned browser process)")
    except Exception as exc:  # noqa: BLE001
        print(f"    (reclaim skipped: {type(exc).__name__}: {exc})")


def main() -> int:
    urls = [a for a in sys.argv[1:] if not a.startswith("-")] or DEFAULT_URLS

    # THE SELECTOR LOOP production actually has (uvicorn --reload forces it). A
    # standalone asyncio.run() is Proactor on Windows, which hides the whole
    # Playwright-subprocess bug class — it shipped twice that way.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    code = 1
    try:
        print(f"event loop: {type(loop).__name__}")
        code = loop.run_until_complete(_main_async(urls))
    finally:
        try:
            from app.browser.runtime import shutdown_browser_runtime

            shutdown_browser_runtime()
        except Exception:  # noqa: BLE001
            pass
        _reclaim()

    sys.stdout.flush()
    sys.stderr.flush()
    import os

    os._exit(code)


if __name__ == "__main__":
    raise SystemExit(main())
