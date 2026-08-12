"""
Furi OS — the 2026-08-09 e-commerce round against the REAL site.

The hermetic suite drives fixtures shaped like the observation; the opt-in
test_browser_extract_js.py runs the JS over hand-written markup. Neither tells
you what happens on the live page, and this codebase has shipped a feature built
against an imagined DOM five times — twice during the PLANNING of this very
round.

So this runs the REAL chain — real Chromium, real BrowserSession, real
`observe`, real `choice` — over the two incident pages plus the deterministic
search path:

    /search?q=janan   are the sold-out products excluded from what we offer?
    the kameez PDP    does the belt suppress the related-items re-ask?
    the incident goal does it now yield a search term and reach real results?

NOTHING IS EVER SUBMITTED, and nothing is clicked except a search box. The
commit path is not touched at all.

    venv\\Scripts\\python -u scripts\\_verify_ecommerce_runtime.py

NEVER collected by pytest (real browser, real network).
"""
from __future__ import annotations

import asyncio
import functools
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

print = functools.partial(print, flush=True)  # noqa: A001
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SEARCH = "https://www.junaidjamshed.com/search?q=janan"
PDP = (
    "https://www.junaidjamshed.com/collections/mens-stitched/products/"
    "black-cotton-casual-kameez-shalwar-jjkss60094"
)
HOME = "https://www.junaidjamshed.com/"

JANAN_INTENT = "go to junaidjamshed.com and add janan in cart"
SECTION_INTENT = (
    "go to junaidjamshed.com in the men kameez shalwar section "
    "add black plain sharwar kameez to cart"
)
PDP_CHOSEN = "BLACK COTTON CASUAL KAMEEZ SHALWAR"

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


async def _main_async() -> int:
    from app.browser import choice
    from app.browser import extract as browser_extract
    from app.browser import loop as browser_loop
    from app.browser import observe as dom_observe
    from app.browser import session as browser_session
    from app.browser.runtime import run_browser
    from app.browser.session import ensure_playwright_driver

    try:
        await run_browser(ensure_playwright_driver(), timeout=90)
    except Exception as exc:  # noqa: BLE001
        print(f"driver warm-up skipped: {type(exc).__name__}: {exc}")

    async def _work() -> int:
        session = await browser_session.BrowserSession.open({_host(HOME)})
        try:
            # ---------------------------------------------- 1. the search page
            print("\n--- /search?q=janan: sold-out products ---")
            await session.goto(SEARCH)
            await session.settle()
            obs = await dom_observe.observe(session.page)
            target = choice.target_tokens(JANAN_INTENT, obs.url)

            raw = choice.tied_matches(
                target, obs.elements, find_price=browser_extract.find_price
            )
            decision = choice.item_choice(
                target, obs.elements, find_price=browser_extract.find_price
            )
            check(
                "the live listing still ties many ways",
                len(raw) >= 10,
                f"{len(raw)} tied",
            )
            # ⚠️ COUNTED AGAINST THE PAGE'S OWN MARKUP, not just asserted to be
            # "more than zero". The site's stock changes between runs (the
            # 2026-08-09 measurement saw six sold out; an hour later it was one),
            # so a bare `> 0` would pass on a page where the walk found ONE of
            # six and silently missed five. This asks the page how many it says
            # are sold out and requires us to have found exactly that many.
            # ⚠️ THE FIRST product anchor in a card is the IMAGE wrapper and its
            # innerText is EMPTY — the first cut of this query took it, got ""
            # for every card, and reported "the page says 0 sold out" while the
            # code had correctly found two. A probe that asserts the wrong thing
            # manufactures defects; take the first anchor that HAS text.
            # ⚠️ TWO WRONG ANCHORS BEFORE THIS ONE, both of which made the probe
            # report a defect the code did not have:
            #   the card's FIRST product anchor is the IMAGE wrapper — innerText
            #     "" — so the query returned nothing and "the page says 0";
            #   the first anchor WITH text is the sold-out card's CTA, whose
            #     text is "View product" — so the names never matched.
            # The card's own title element is the one that names the product.
            truth = await session.page.evaluate(
                "() => Array.from(document.querySelectorAll('.hdt-card-product'))"
                ".filter(c => /sold[\\s_-]*out/i.test(String(c.className)))"
                ".map(c => { const t = c.querySelector('.hdt-card-product__title');"
                "            return t ? (t.innerText||'').trim() : ''; })"
                ".filter(Boolean)"
            )
            print(f"      page's own sold-out list: {truth}")
            found = [c.label for c in raw if not c.available]

            def _norm(s: str) -> str:
                # The page's raw innerText and a Candidate.label differ in
                # whitespace and in the chrome `_LABEL_NOISE_RE` strips, so
                # compare them the way this module compares everything else.
                return " ".join(choice._tokens(s))

            want = {_norm(t) for t in truth} & {_norm(c.label) for c in raw}
            check(
                "we find EVERY product the page marks sold out — no more, no less",
                {_norm(f) for f in found} == want,
                f"page says {len(truth)}, we found {len(found)}: {found}",
            )
            check(
                "…and the store really does mark some of them",
                bool(truth),
                f"{len(truth)} sold out on the page right now",
            )
            offered = [c.label for c in (decision.tied if decision else ())]
            unbuyable = [c.label for c in raw if not c.available]
            check(
                "NOT ONE sold-out product is offered",
                all(u not in offered for u in unbuyable),
                f"{len(unbuyable)} sold out, {len(offered)} offered",
            )
            check(
                "the buyable ones ARE offered",
                len(offered) == len(raw) - len(unbuyable),
                f"{len(offered)} == {len(raw)} - {len(unbuyable)}",
            )
            print(f"      sold out: {unbuyable[:6]}")
            print(f"      offered : {offered[:6]}")

            # The end of the chain — the strings that become buttons.
            if decision is not None:
                out = browser_loop._stamp_item_choice(
                    browser_loop.BrowseOutcome(success=False, actions_taken=0),
                    decision,
                    target,
                )
                shown = " | ".join(out.choice_options)
                check(
                    "no sold-out name reaches the question text",
                    all(u not in shown for u in unbuyable),
                )
                check(
                    "the question reports the whole tie, not just what it shows",
                    out.choice_total == len(decision.tied),
                    f"total={out.choice_total} shown={len(out.choice_options)}",
                )

            # A deliberately-picked sold-out item must STILL be locatable — the
            # answer-enforcement path must not be filtered.
            if unbuyable:
                found = choice.locate(
                    unbuyable[0], obs.elements, find_price=browser_extract.find_price
                )
                check(
                    "a deliberately-picked sold-out item is still honoured",
                    found is not None,
                    f"{unbuyable[0]!r}",
                )

            # ---------------------------------------------------- 2. the PDP
            print("\n--- the kameez product page: the rail re-ask ---")
            await session.goto(PDP)
            await session.settle()
            pdp = await dom_observe.observe(session.page)
            pdp_target = choice.target_tokens(
                SECTION_INTENT, pdp.url, extra=[PDP_CHOSEN]
            )
            pdp_tied = choice.tied_matches(
                pdp_target, pdp.elements, find_price=browser_extract.find_price
            )
            proxy = choice.page_is_the_target(
                pdp_target, pdp.title, pdp.url, pdp_tied
            )
            belt = choice.answered_here(PDP_CHOSEN, pdp.title, pdp.url)
            check(
                "this is the page the user picked",
                belt is True,
                f"title={pdp.title[:50]!r}",
            )
            check(
                "the old proxy alone would NOT have suppressed it",
                proxy is False or len(pdp_tied) < 2,
                f"page_is_the_target={proxy} tied={len(pdp_tied)}",
            )

            # ------------------------------------------ 3. the search term
            print("\n--- the incident goal: search is switched back on ---")
            term = browser_loop._extract_search_term(SECTION_INTENT)
            check(
                "the incident goal yields a search term at all",
                bool(term),
                f"term={term!r}",
            )
            check(
                "…and it is the ITEM, not the site or the section",
                term == "black plain sharwar kameez",
                f"term={term!r}",
            )
            check(
                "it is not read as a destination, so the search leg runs",
                browser_loop._names_only_the_destination(term or "", HOME) is False,
            )

            # And the whole leg, on the live homepage: the search UI is hidden
            # behind an icon here, so this exercises the toggle too.
            await session.goto(HOME)
            await session.settle()
            home = await dom_observe.observe(session.page)
            fast = browser_loop._fast_path_action(SECTION_INTENT, home)
            toggle = browser_loop._open_search_ui_action(home)
            check(
                "the homepage has no typeable box, so the toggle is the move",
                fast is None and toggle is not None,
                f"fast={fast} toggle={toggle}",
            )
            if toggle is not None:
                # The loop's own actuator, so this exercises the real path.
                # Clicking a search TOGGLE is reading — it reveals a field and
                # acts on nothing (`_is_action_gesture` returns False for it).
                await browser_loop._act(session, home, toggle)
                await session.settle()
                revealed = await dom_observe.observe(session.page)
                typed = browser_loop._fast_path_action(SECTION_INTENT, revealed)
                check(
                    "after opening it, the fast path types the ITEM",
                    typed is not None
                    and typed.get("text") == "black plain sharwar kameez",
                    f"{typed}",
                )
        finally:
            await session.close()
        return 0

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
        code = 1
    finally:
        try:
            from app.browser.runtime import shutdown_browser_runtime

            shutdown_browser_runtime()
        except Exception:  # noqa: BLE001
            pass
    passed = sum(1 for _, ok, _ in CHECKS if ok)
    print(f"\n{'=' * 60}\n{passed}/{len(CHECKS)} checks passed")
    for name, ok, detail in CHECKS:
        if not ok:
            print(f"  FAILED: {name}  {detail}")
    sys.stdout.flush()
    import os

    os._exit(0 if (passed == len(CHECKS) and CHECKS and code == 0) else 1)


if __name__ == "__main__":
    raise SystemExit(main())
