"""What the loop actually SEES on the junaidjamshed.com homepage (2026-08-09).

The incident: "go to junaidjamshed.com and add janan perfume in cart" clicked the
"Fragrances" nav item instead of typing "janan" into the site's search box, and
the fragrances listing then tied 6 ways.

Two candidate causes, and they need OPPOSITE fixes, so guessing is not an option:

  (a) the search box IS in the observation and the fast path declined
      (a term-extraction problem), or
  (b) the search box is NOT observable at all — hidden behind an icon until
      clicked — in which case neither the fast path nor the model could ever
      have used it, and the term extraction is irrelevant.

So this reports, against the REAL page: every search-ish element the loop can
see, what element 27 (the one it clicked) is, and what _fast_path_action would
do given the planner's paraphrase vs the user's own words.

NOTHING IS CLICKED, TYPED OR SUBMITTED. It observes and reports.

    venv\\Scripts\\python -u scripts\\_measure_store_search.py

NEVER collected by pytest (real browser, real network).
"""
from __future__ import annotations

import asyncio
import functools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

print = functools.partial(print, flush=True)  # noqa: A001
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

URL = "https://www.junaidjamshed.com/"
USER = "go to junaidjamshed.com and add janan perfume in cart"
PLANNER = (
    "Go to junaidjamshed.com, find the Janan perfume, and add it to the cart "
    "by submitting the add-to-cart form."
)


async def _main_async() -> int:
    from app.browser import loop as browser_loop
    from app.browser import observe
    from app.browser import session as browser_session
    from app.browser.runtime import run_browser
    from app.browser.session import ensure_playwright_driver

    await run_browser(ensure_playwright_driver(), timeout=90)

    async def _work() -> int:
        session = await browser_session.BrowserSession.open({"junaidjamshed.com"})
        try:
            await session.goto(URL)
            await session.settle()
            obs = await observe.observe(session.page)
            print(f"\nurl={obs.url}\ntitle={obs.title!r}\nelements={len(obs.elements)}")

            # ---- 1. every search-ish element the fast path would consider
            cands = [
                e for e in obs.elements
                if e.role in browser_loop._SEARCH_ROLES
                or "search" in (e.name or "").lower()
            ]
            print(f"\n--- search candidates: {len(cands)}")
            for e in cands:
                real = browser_loop._is_search_target(e)
                print(f"  [{e.index:3}] role={e.role:12} genuine={real!s:5} "
                      f"name={(e.name or '')[:60]!r}")

            # ---- 2. what did it actually click?
            print("\n--- what the model clicked")
            for e in obs.elements:
                if e.index == 27:
                    print(f"  [27] role={e.role} name={(e.name or '')[:80]!r} "
                          f"href={(getattr(e, 'href', '') or '')[:80]!r}")

            # ---- 3. the fast path, both wordings
            print("\n--- _fast_path_action")
            for label, g in (("planner", PLANNER), ("user", USER)):
                term = browser_loop._extract_search_term(g)
                act = browser_loop._fast_path_action(g, obs)
                print(f"  {label:8} term={term!r}")
                print(f"  {'':8} action={act!r}")

            # ---- 4. any text input at all, for context
            inputs = [e for e in obs.elements if e.role in ("textbox", "searchbox", "combobox")]
            print(f"\n--- all text-ish inputs: {len(inputs)}")
            for e in inputs[:12]:
                print(f"  [{e.index:3}] role={e.role:12} name={(e.name or '')[:60]!r}")
            return 0
        finally:
            await session.close()

    return await run_browser(_work(), timeout=300)


def main() -> int:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    code = 1
    try:
        code = loop.run_until_complete(_main_async())
    except Exception as exc:  # noqa: BLE001
        print(f"\nPROBE ERROR: {type(exc).__name__}: {exc}")
        import traceback

        traceback.print_exc()
    finally:
        try:
            from app.browser.runtime import shutdown_browser_runtime

            shutdown_browser_runtime()
        except Exception:  # noqa: BLE001
            pass
    sys.stdout.flush()
    import os

    os._exit(code or 0)


if __name__ == "__main__":
    raise SystemExit(main())
