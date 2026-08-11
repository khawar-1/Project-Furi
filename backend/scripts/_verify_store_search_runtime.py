"""The store-search fix against the REAL site (2026-08-09).

The hermetic tests drive a fixture shaped like the measured page. This runs the
REAL chain — real Chromium, real BrowserSession, real observe, real
_fast_path_action / _open_search_ui_action / _act — over the actual
junaidjamshed.com homepage that produced the incident, and proves each of the
five switches at the surface that matters.

⚠️ NOTHING IS EVER SUBMITTED. The only page interactions are opening the search
drawer and typing into a SEARCH box, both of which are reading. The commit path
is not touched: this never calls arm_commit, never fires a form, and asserts at
the end that the session observed zero commits.

    venv\\Scripts\\python -u scripts\\_verify_store_search_runtime.py

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

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


async def _main_async() -> int:
    from app.browser import choice
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
            print(f"\n=== {obs.url}  ({len(obs.elements)} elements)")

            # ---------------- S5: the user's words yield a product name
            term_user = browser_loop._extract_search_term(USER)
            term_plan = browser_loop._extract_search_term(PLANNER)
            check("S5 the user's words yield the product name",
                  term_user == "janan perfume", repr(term_user))
            check("S4 the planner's paraphrase still yields nothing",
                  term_plan is None, repr(term_plan))

            # ---------------- S2: the page really has no typeable search box
            boxes = [e for e in obs.elements if browser_loop._is_search_target(e)]
            check("S2 the live homepage has NO typeable search box",
                  len(boxes) == 0, f"{len(boxes)} found")

            # ---------------- S3: the fast path declines rather than typing into a link
            fast = browser_loop._fast_path_action(USER, obs)
            check("S3 the fast path does not type into the 'search' link",
                  fast is None, repr(fast))

            # ---------------- S1 guard: the homepage is not the target
            covers = choice.page_covers_target(
                choice.target_tokens(USER, obs.url), obs.title, obs.url
            )
            check("S1 the homepage is not mistaken for the product page",
                  covers is False, f"covers={covers}")

            # ---------------- S2: the toggle is found and clicked
            open_act = browser_loop._open_search_ui_action(obs)
            check("S2 the hidden search box is found behind a control",
                  isinstance(open_act, dict) and open_act.get("action") == "click",
                  repr(open_act))
            if not open_act:
                return 1
            named = next(
                (e.name for e in obs.elements if e.index == open_act["index"]), ""
            )
            check("S2 and it is the site's own search control", "search" in named.lower(),
                  repr(named))

            await browser_loop._act(session, obs, open_act)
            await session.settle()
            await asyncio.sleep(1.5)  # the drawer animates open
            obs2 = await observe.observe(session.page)

            boxes2 = [e for e in obs2.elements if browser_loop._is_search_target(e)]
            check("S2 clicking it REVEALS a real search box",
                  len(boxes2) >= 1,
                  ", ".join(f"[{e.index}] {e.role} {e.name!r}" for e in boxes2[:3]))

            # ---------------- the fast path now fires, with the right term
            fast2 = browser_loop._fast_path_action(USER, obs2)
            check("the fast path now types the product name",
                  isinstance(fast2, dict)
                  and fast2.get("action") == "type"
                  and fast2.get("text") == "janan perfume",
                  repr(fast2))
            if not fast2:
                return 1

            # ---------------- and the search really returns janan products
            await browser_loop._act(session, obs2, fast2)
            await session.settle()
            await asyncio.sleep(2.0)
            obs3 = await observe.observe(session.page)
            print(f"\n=== {obs3.url}  ({len(obs3.elements)} elements)")

            janan = [
                e for e in obs3.elements
                if "janan" in (e.name or "").lower()
            ]
            check("the results page is about janan", "janan" in obs3.url.lower(),
                  obs3.url)
            check("and it lists janan products",
                  len(janan) >= 3,
                  ", ".join((e.name or "")[:28] for e in janan[:5]))

            # ⚠️ THE COMPARISON THAT IS THE WHOLE POINT: on the FRAGRANCES page
            # the incident landed on, how many of the listed items carry "janan"?
            # ⚠️ THE USER'S ACTUAL COMPLAINT, checked directly: "the one i meant
            # wasnt in them". On the Fragrances page the incident landed on, the
            # six items put to them were whatever the category happened to list.
            # Here every item the user could be asked about must carry their own
            # word. (An empty tie is the BETTER outcome, not a failure — it means
            # one product leads outright and there is nothing to ask.)
            from app.browser import extract as browse_extract

            tokens = choice.target_tokens(USER, obs3.url)
            cands = choice.candidates_of(
                obs3.elements, find_price=browse_extract.find_price
            )
            scored = [(choice._score(tokens, c.label), c.label) for c in cands]
            best = max((s for s, _ in scored), default=0)
            top = [label for s, label in scored if s == best and s > 0]
            check("every item the user could be asked about carries their word",
                  bool(top) and all("janan" in (t or "").lower() for t in top),
                  f"top score {best}, {len(top)} item(s): "
                  + ", ".join(t[:26] for t in top[:4]))

            # ---------------- NOTHING WAS SUBMITTED
            stats = session.stats.as_dict()
            check("NOTHING WAS SUBMITTED", stats.get("commits", 0) == 0,
                  f"commits={stats.get('commits', 0)}")
            return 0
        finally:
            await session.close()

    return await run_browser(_work(), timeout=420)


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
        code = 1
    finally:
        try:
            from app.browser.runtime import shutdown_browser_runtime

            shutdown_browser_runtime()
        except Exception:  # noqa: BLE001
            pass
    passed = sum(1 for _, ok, _ in CHECKS if ok)
    print(f"\n{'=' * 62}\n{passed}/{len(CHECKS)} checks passed")
    for name, ok, detail in CHECKS:
        if not ok:
            print(f"  FAILED: {name}  {detail}")
    sys.stdout.flush()
    import os

    os._exit(0 if (passed == len(CHECKS) and CHECKS and code == 0) else 1)


if __name__ == "__main__":
    raise SystemExit(main())
