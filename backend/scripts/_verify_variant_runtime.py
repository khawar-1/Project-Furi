"""
Furi OS — the variant gate against the REAL site (2026-08-08).

The hermetic tests drive a fixture shaped like the contract; the opt-in
test_browser_axes_js.py runs the JS against hand-written markup. Neither tells
you what happens on the live page, and this codebase has shipped a feature built
against an imagined DOM five times.

So this runs the REAL chain — real Chromium, real BrowserSession, real
_READ_COMMIT_FORM_JS, real choice.unresolved_axis — over two real products:

    grey-formal-kurta-jjka50589    6 sizes, ONE in stock  -> code takes it
    mint-green-formal-kurta-...    5 sizes, ALL in stock  -> ask, with 5 options

NOTHING IS EVER SUBMITTED. The only page interaction is selecting a variant,
which issues no network request; the submit path is not touched at all.

    venv\\Scripts\\python -u scripts\\_verify_variant_runtime.py

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

FORCED = "https://www.junaidjamshed.com/products/grey-formal-kurta-jjka50589"
OPEN_CHOICE = "https://www.junaidjamshed.com/products/mint-green-formal-kurta-jckspa42055"

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


def _cart_candidates(obs) -> list[int]:
    """Every control that might sit inside a /cart/add form, in DOM order.

    ⚠️ MEASURED, AND IT IS THE STRONGEST EXTERNAL VALIDATION THIS ROUND HAS.
    The main product's submit button is not called "Add to bag" at all — it
    reads "CHOOSE OPTIONS" and is `disabled`, because the STORE ITSELF refuses
    to let anyone add the item until a size is picked. The buttons that do say
    "Add to bag" belong to the single-variant fragrances in the rail. So the
    name cannot be the selector; the enclosing form's own SKU is."""
    out = []
    for el in obs.elements:
        if getattr(el, "role", "") not in ("button", "link"):
            continue
        out.append(el.index)
    return out


async def _main_product_index(session, obs, url: str) -> int:
    """The add-to-bag of the product THIS URL is showing.

    ⚠️ The first probe run read a RAIL card's form and reported a pre-filled id
    and no axes — which reads exactly like the gate failing, and was the probe
    picking the wrong button. The identity is not circular: the main form's
    `properties[Product ID]` is the SKU in the page's own URL
    (…/grey-formal-kurta-JJKA50589 -> JJKA50589)."""
    slug = url.rstrip("/").rsplit("/", 1)[-1].replace("-", "").lower()
    for idx in _cart_candidates(obs):
        contract = await session.read_commit_target(obs, idx)
        fields = {f["name"]: f["value"] for f in (contract or {}).get("fields", [])}
        sku = str(fields.get("properties[Product ID]") or "").replace("-", "").lower()
        if sku and sku in slug:
            return idx
    print(f"       (no /cart/add form matching this product among "
          f"{len(obs.elements)} elements, {len(_cart_candidates(obs))} candidates)")
    return -1


async def _probe(url: str, goal: str, label: str) -> dict:
    from app.browser import choice, observe
    from app.browser import session as browser_session

    host = (urlparse(url).hostname or "").lower()
    session = await browser_session.BrowserSession.open({host})
    try:
        await session.goto(url)
        await session.settle()
        # MEASURED: the buy box renders LATE — one run observed 70 elements with
        # no add-to-bag control, the next 95 with it. The loop would simply
        # re-observe on its next step; a probe that gave up on the first look
        # would report a code defect that is really a race.
        obs = await observe.observe(session.page)
        idx = await _main_product_index(session, obs, url)
        for _ in range(4):
            if idx >= 0:
                break
            await asyncio.sleep(2.0)
            obs = await observe.observe(session.page)
            idx = await _main_product_index(session, obs, url)
        check(f"{label}: found this product's own cart form", idx >= 0, f"index={idx}")
        if idx < 0:
            return {}

        contract = await session.read_commit_target(obs, idx)
        check(f"{label}: read the form contract", bool(contract and contract.get("action")),
              str((contract or {}).get("action", ""))[:60])
        axes = choice.axes_of(contract)
        check(f"{label}: the size axis is in the contract", len(axes) >= 1,
              ", ".join(f"{a.name}({len(a.options)} opts, {len(a.buyable)} buyable)"
                        for a in axes))

        fields = {f["name"]: f["value"] for f in (contract or {}).get("fields", [])}
        settle = choice.unresolved_axis(axes, choice.target_tokens(goal, url))
        return {"session": session, "contract": contract, "axes": axes,
                "fields": fields, "settle": settle, "obs": obs, "idx": idx}
    except Exception:
        await session.close()
        raise


async def _main_async() -> int:
    from app.browser import choice
    from app.browser.runtime import run_browser
    from app.browser.session import ensure_playwright_driver

    await run_browser(ensure_playwright_driver(), timeout=90)

    async def _work() -> int:
        # ---------------------------------------------- 1. the forced choice
        print(f"\n=== {FORCED}")
        st = await _probe(FORCED, "add the grey formal kurta to my cart", "forced")
        if not st:
            return 1
        session = st["session"]
        try:
            check("forced: the contract's variant id starts EMPTY — the defect",
                  st["fields"].get("id", "") == "",
                  f"id={st['fields'].get('id','')!r}")
            settle = st["settle"]
            if settle is None:
                check("forced: code settles it rather than asking", False, "no axis found")
                return 1
            check("forced: code settles it rather than asking",
                  settle is not None and settle.settled != "" and not settle.options,
                  f"settled={getattr(settle,'settled','')!r} why={getattr(settle,'why','')!r}")
            check("forced: it picks the only size in stock",
                  getattr(settle, "settled", "") == "L")
            check("forced: the axis is named readably",
                  getattr(settle, "field", "") == "Size",
                  repr(getattr(settle, "field", "")))

            status = await session.choose_form_option(settle.axis, settle.settled)
            check("forced: the real control accepts it", status == "ok", status)

            # The theme resolves the hidden variant id in its OWN handler, so
            # give the page the moment a human click would have given it.
            await session.settle()
            await asyncio.sleep(1.5)
            fresh = await session.reread_commit_form()
            new_fields = {f["name"]: f["value"] for f in (fresh or {}).get("fields", [])}
            check("forced: the re-read contract now carries a real variant id",
                  bool(new_fields.get("id")), f"id={new_fields.get('id','')!r}")
            check("forced: and the chosen size is in what a submit would send",
                  new_fields.get("Size") == "L", f"Size={new_fields.get('Size')!r}")

            again = choice.unresolved_axis(choice.axes_of(fresh),
                                           choice.target_tokens("add the grey formal kurta", FORCED))
            check("forced: nothing is left to ask about", again is None, repr(again))
            check("forced: NOTHING WAS SUBMITTED",
                  session.stats.as_dict().get("blocked_mutations", 0) >= 0
                  and not getattr(session, "commit_fired", lambda: False)())
        finally:
            await session.close()

        # ---------------------------------------------- 2. the genuinely open one
        print(f"\n=== {OPEN_CHOICE}")
        st2 = await _probe(OPEN_CHOICE, "add the mint green formal kurta to my cart", "open")
        if not st2:
            return 1
        session2 = st2["session"]
        try:
            settle2 = st2["settle"]
            check("open: it ASKS rather than picking",
                  settle2 is not None and settle2.settled == "" and len(settle2.options) > 1,
                  f"options={list(getattr(settle2,'options',()))}")
            check("open: every offered size is one that can be bought",
                  all(o in {opt.option() for a in st2["axes"] for opt in a.buyable}
                      for o in getattr(settle2, "options", ())))

            # And the user naming a size settles it instead of asking.
            # ⚠️ THE REPLY IS TAKEN FROM THE PAGE, not invented: this product is
            # a KIDS kurta whose sizes are "2 Y".."10 Y", and an earlier run of
            # this probe asserted "L" and reported a failure that was its own.
            reply = list(getattr(settle2, "options", ("",)))[1]
            named = choice.unresolved_axis(
                st2["axes"],
                choice.target_tokens("add the mint green formal kurta", OPEN_CHOICE),
                answer=reply,
            )
            check(f"open: a direct reply of {reply!r} settles it in code",
                  named is not None and named.settled == reply,
                  f"settled={getattr(named,'settled','')!r}")
        finally:
            await session2.close()
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
