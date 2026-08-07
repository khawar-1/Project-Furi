"""Live runtime verification for the 2026-08-07 round-2 season-catalog work.

The hermetic suite proves the parts. This proves the WHOLE, against the real
services, in production's shape — which is the only thing that can show the
wiring boots. Two lessons from this project's own notes are built in:

  * the SELECTOR event loop, because a standalone asyncio.run() is Proactor on
    Windows and hides the Playwright-subprocess bug class that shipped twice
  * the real `browse` TOOL, not `run_browse` directly, so the `user_words` ->
    `intent_text` wiring is exercised rather than assumed

⚠️ THIS OPENS A REAL HEADED CHROME WINDOW and spends real provider credits. It
plays nothing (keep_open is False) and submits nothing (browse is READ-level).

Run from backend/:  venv\\Scripts\\python scripts\\_verify_season_runtime.py
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
# What the planner actually drafted on the live run (backend.log:17855).
PLANNER_GOAL = (
    "Find Bleach on anikoto, open the latest season, select and play the most "
    "recent episode."
)

# The incident's own wrong answers, from the trace.
WRONG_SLUG = "bleach-yaa9n"
WRONG_URL = "https://anikoto.cz/watch/bleach-yaa9n/ep-304"

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


async def part1_catalog() -> str:
    """The catalog leg, against the live AniList service."""
    print("\n== 1. the catalog answers, with no LLM and no search ==")
    from app.browser import series_api

    facts = await series_api.resolve_season("bleach")
    check(facts is not None, "AniList answered for 'bleach'")
    if facts is None:
        return ""

    check(
        "Calamity" in facts.season_name,
        "the season is the airing cour, not the 2004 series",
        f"season_name = {facts.season_name!r}",
    )
    check(
        facts.episode == 2,
        "the episode matches what Google reports (2)",
        f"episode = {facts.episode}",
    )
    check(facts.status == "RELEASING", "the chosen entry is the one airing now")
    check(facts.source == "anilist", "answered by the keyless catalog")

    # The instruments the incident used, for contrast.
    from app.browser import loop as browser_loop

    web_number = await browser_loop._resolve_latest_episode("bleach")
    check(
        web_number != facts.episode,
        "the OLD prose instrument still disagrees (this is why it was demoted)",
        f"prose max = {web_number!r} vs catalog = {facts.episode}",
    )
    return facts.season_name


async def part2_matching(season_name: str) -> None:
    """The season name against the REAL anikoto listing."""
    print("\n== 2. the name picks the right entry out of the real listing ==")
    import re

    import httpx

    from app.browser import season as browse_season

    try:
        page = httpx.get(
            "https://anikoto.cz/search?keyword=bleach",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=30,
            follow_redirects=True,
        )
        slugs: list[str] = []
        for slug in re.findall(r"/watch/([a-z0-9\-]+)", page.text):
            if slug not in slugs:
                slugs.append(slug)
    except Exception as e:
        check(False, "fetched the real anikoto listing", f"{type(e).__name__}: {e}")
        return

    check(len(slugs) > 5, f"the real listing returned {len(slugs)} entries")

    scored = {
        s: browse_season.score_entry(s.replace("-", " "), season_name, "Bleach")
        for s in slugs
    }
    check(
        scored.get(WRONG_SLUG) == 0,
        "the entry the incident opened scores ZERO",
        f"{WRONG_SLUG} -> {scored.get(WRONG_SLUG)}",
    )
    best = max(scored.values())
    winners = [s for s, v in scored.items() if v == best]
    check(
        all("calamity" in s for s in winners),
        "every top-scoring entry IS the airing cour",
        f"top ({best}): {winners}",
    )


async def part3_live_browse() -> None:
    """The real tool, real browser, real site."""
    print("\n== 3. the real browse tool against the real site ==")
    print("     (a Chrome window will open; nothing is played or submitted)")
    # The TOOL, not run_browse — `user_words` -> `intent_text` is wired inside
    # BrowseTool.execute, and that wiring is part of what is being verified.
    # (execute_tool() would additionally need a DB session for the audit row,
    # which adds nothing here; browse is READ-level so no approval is involved.)
    import app.tools  # noqa: F401  - registers every tool
    from app.tools.registry import registry as tool_registry

    tool = tool_registry.get("browse")
    check(tool is not None, "the browse tool is registered")
    if tool is None:
        return

    result = await tool.safe_execute(
        goal=PLANNER_GOAL,
        start_url="https://anikoto.cz",
        allowed_origins=["anikoto.cz"],
        keep_open=False,
        user_words=USER_WORDS,
    )

    output = result.output if isinstance(result.output, dict) else {}
    final_url = str(output.get("url") or "")
    print(f"     final url : {final_url}")
    print(f"     success   : {result.success}")

    check(
        final_url != WRONG_URL,
        "did not land on the incident's exact URL",
        f"incident was {WRONG_URL}",
    )
    check(
        f"{WRONG_SLUG}/ep-" not in final_url,
        "did not dive into the 2004 series",
        f"final = {final_url or '(none)'}",
    )


def main() -> int:
    # Production's loop: uvicorn --reload forces the Selector policy on Windows.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    async def _run():
        season = await part1_catalog()
        if season:
            await part2_matching(season)
        if "--no-browser" not in sys.argv:
            try:
                await part3_live_browse()
            except Exception as e:
                check(False, "the live browse ran", f"{type(e).__name__}: {e}")
        else:
            print("\n== 3. skipped (--no-browser) ==")

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
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
