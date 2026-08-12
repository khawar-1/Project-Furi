"""
Furi OS — the 2026-08-10 add-to-cart round, against the REAL site.

A hermetic test can only tell you the code agrees with a fixture. This drives the
REAL modules — real Chromium, the real `BrowserSession`, the real `observe`, the
real `find_buy_box`, the real `choice` — against junaidjamshed.com, because every
single design decision in this round was overturned at least once by measurement.

⚠️ NOTHING IS EVER SUBMITTED. `arm_commit` and `submit_commit` are not called
anywhere in this file, and the run asserts the session's own commit counter is
still zero at the end. The variant IS clicked — that is a page interaction, not a
purchase, and it is the only way to prove the size really reaches the form.

    venv\\Scripts\\python -u scripts\\_verify_ecommerce_flow_runtime.py

NEVER collected by pytest.
"""
from __future__ import annotations

import asyncio
import functools
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

print = functools.partial(print, flush=True)  # noqa: A001

HOST = "www.junaidjamshed.com"
SEARCH_URL = "https://www.junaidjamshed.com/search?q=black+kameez+kurta"
PRODUCT_URL = (
    "https://www.junaidjamshed.com/products/"
    "black-blended-kameez-shalwar-jjksa30729r52ap"
)
USER_WORDS = "go to junaidjamshed.com and add black kameez kurta in cart"
STEER = "select size large and add to cart"

_checks: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    _checks.append((bool(ok), label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  — {detail}" if detail else ""))


async def _main_async() -> int:
    from app.browser import choice
    from app.browser import commit_flow as browser_commit
    from app.browser import loop as browser_loop
    from app.browser import observe as dom_observe
    from app.browser import session as browser_session
    from app.browser.runtime import run_browser
    from app.browser.session import ensure_playwright_driver

    await run_browser(ensure_playwright_driver(), timeout=90)

    async def _work() -> int:
        session = await browser_session.BrowserSession.open({HOST})
        try:
            # ---------------------------------------- 1. the listing: does it ASK?
            print("\n=== 1. the search results page — the question that was never asked")
            await session.goto(SEARCH_URL)
            await session.settle()
            obs = await dom_observe.observe(session.page)
            words = choice.target_tokens(USER_WORDS, obs.url)
            decision = choice.item_choice(
                words, obs.elements, find_price=__import__(
                    "app.browser.extract", fromlist=["find_price"]
                ).find_price
            )
            check(decision is not None and bool(decision.tied),
                  "the listing raises a choice",
                  f"{len(decision.tied) if decision else 0} tied of "
                  f"{len(decision.all_tied) if decision else 0}")
            if decision:
                for c in decision.tied[:6]:
                    print(f"          - {c.label}")
                check(
                    choice.page_is_the_target(
                        words, obs.title, obs.url, decision.all_tied) is False,
                    "the search TITLE no longer claims to be the thing it lists",
                    f"subject={choice.page_subject(obs.title, obs.url)[:40]!r}",
                )
                check(all(choice._score(words, c.label) ==
                          choice._score(words, decision.tied[0].label)
                          for c in decision.tied),
                      "every option really is an equal match (no adjacency bonus)")
                check(all(c.available for c in decision.tied),
                      "and none of the offered items is sold out")

            # ------------------------------------- 2. the product page's buy box
            print("\n=== 2. the product page — the buy box the model could not see")
            await session.goto(PRODUCT_URL)
            await session.settle()
            obs = await dom_observe.observe(session.page)

            listed = [
                e for e in obs.elements
                if "add to" in str(getattr(e, "name_full", "") or e.name or "").lower()
                or "select size" in str(getattr(e, "name_full", "") or e.name or "").lower()
            ]
            own_listed = [e for e in listed if "select size" in
                          str(getattr(e, "name_full", "") or e.name or "").lower()]
            check(not own_listed,
                  "the page's own buy button is STILL not in the element list",
                  "which is why a leg that needs an index could never work")

            contract = await session.find_buy_box()
            check(isinstance(contract, dict) and contract.get("found"),
                  "find_buy_box finds it anyway",
                  str(contract.get("reason") if isinstance(contract, dict) else contract))
            if not (isinstance(contract, dict) and contract.get("found")):
                return 1

            pid = next(
                (f.get("value") for f in (contract.get("fields") or [])
                 if "Product ID" in str(f.get("name"))), "",
            )
            check(str(pid).lower() in PRODUCT_URL.lower(),
                  "and it is THIS page's product, not a rail item",
                  f"Product ID={pid!r}")
            check(str(contract.get("action", "")).endswith("/cart/add"),
                  "posting to the cart", str(contract.get("action")))

            axes = choice.axes_of(contract)
            check(len(axes) == 1 and axes[0].name.lower() == "size",
                  "the size axis is readable",
                  ", ".join(f"{a.name}({len(a.options)})" for a in axes))
            if not axes:
                return 1
            buyable = [o.label or o.value for o in axes[0].buyable]
            check(len(buyable) < len(axes[0].options),
                  "and out-of-stock sizes are excluded",
                  f"offered {buyable} of "
                  f"{[o.label or o.value for o in axes[0].options]}")

            # ------------------------------------------ 3. the size question
            print("\n=== 3. the size — asked, and settled from the user's own words")
            ask = choice.unresolved_axis(axes, choice.target_tokens(USER_WORDS, obs.url))
            check(ask is not None and not ask.settled and list(ask.options) == buyable,
                  "with nothing said, it ASKS with the real in-stock sizes",
                  str(list(ask.options) if ask else None))

            steered = choice.unresolved_axis(
                axes,
                choice.target_tokens(USER_WORDS, obs.url, extra=[STEER]),
            )
            check(steered is not None and steered.settled == "L",
                  "and 'select size large' settles L without asking",
                  str(steered.settled if steered else None))

            # ------------------------- 4. the choice really reaches the real form
            print("\n=== 4. the chosen size reaches the real form")
            status = await session.choose_form_option("Size", "L")
            check(status == "ok", "the real control takes it", status)
            await session.settle()
            # ⚠️ THE SAME CALL THE LOOP MAKES. An earlier cut of this probe used
            # `reread_commit_form()` directly and reported a FAILURE that was its
            # own — it was exercising the path the round replaced. A probe that
            # drives a shape the product no longer uses measures nothing.
            fresh = await session.await_form_change(contract)
            variant = next(
                (f.get("value") for f in ((fresh or {}).get("fields") or [])
                 if f.get("name") == "id"), "",
            )
            check(bool(str(variant).strip()),
                  "and the form's hidden variant id is now filled in",
                  f"id={variant!r}")
            again = choice.unresolved_axis(
                choice.axes_of(fresh or {}),
                choice.target_tokens(USER_WORDS, obs.url, extra=[STEER]),
            )
            check(again is None or not again.options,
                  "with nothing left to ask about")

            # ------------------------------------------- 5. the cart comparator
            print("\n=== 5. the cart comparator")
            marker = await browser_commit._read_cart_marker(session)
            check(isinstance(marker, dict) and bool(marker.get("href")),
                  "the page publishes its own cart link",
                  str(marker.get("href"))[:60])
            check(isinstance(marker.get("count"), int),
                  "and a count we can compare before/after",
                  f"count={marker.get('count')}")

            # -------------------------------------------- 6. the safety property
            print("\n=== 6. nothing was submitted")
            check(int(getattr(session, "commits_done", 0) or 0) == 0,
                  "the session's commit counter is still zero")
            check(session.commit_fired() is False,
                  "and no approved request was ever observed leaving")
            check(browser_loop.wants_cart(USER_WORDS) is True,
                  "the goal really is a cart goal (so the leg was in scope)")
        finally:
            await session.close()
        return 0

    return await run_browser(_work(), timeout=600)


def main() -> int:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_main_async())
    except Exception as exc:  # noqa: BLE001
        print(f"\n!! probe crashed: {type(exc).__name__}: {exc}")
    finally:
        try:
            from app.browser.runtime import shutdown_browser_runtime

            shutdown_browser_runtime()
        except Exception:  # noqa: BLE001
            pass
    passed = sum(1 for ok, _ in _checks if ok)
    print(f"\n{'=' * 70}\n{passed}/{len(_checks)} checks passed")
    for ok, label in _checks:
        if not ok:
            print(f"   FAILED: {label}")
    sys.stdout.flush()
    import os

    os._exit(0 if passed == len(_checks) and _checks else 1)


if __name__ == "__main__":
    raise SystemExit(main())
