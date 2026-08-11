"""
Jarvis OS — does the WHOLE chain work once aimed at the right form? (2026-08-10)

The design is now measured rather than assumed:

  * the page's own buy box is the only /cart/add form NOT inside another
    product's card (rule R1, `_measure_buybox_rules.py`: uniquely form 0 on the
    incident's page, and only the page's own twin forms on a second product)
  * the buy button's LABEL is useless as a finder — measured, it reads
    "Select Size" on the incident's product, "Out of stock" on another, while
    the RAIL products are the ones that say "Add to bag"/"ADD"/"Quick add"
  * the size radios ARE form-associated with that form, so `choice.axes_of`
    finds Size(6) once it is pointed there

This checks the last link: that the buy box's submit control is actually LISTED
in our observation (the leg needs an element index), that `read_commit_target`
on it yields the axes, and that `choice.unresolved_axis` then does the right
thing for (a) no answer and (b) the user's own word "large".

    venv\\Scripts\\python -u scripts\\_measure_buybox_chain.py [url]

NEVER collected by pytest. Reads only; NOTHING is ever submitted (arm_commit is
never called in this file).
"""
from __future__ import annotations

import asyncio
import functools
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

print = functools.partial(print, flush=True)  # noqa: A001

DEFAULT_URL = (
    "https://www.junaidjamshed.com/products/"
    "black-blended-kameez-shalwar-jjksa30729r52ap"
)

# R1, plus: mark the buy box's submit control so we can find it in the element
# list by the same stamp the observation carries.
FIND_BUYBOX_JS = """
() => {
  const path = location.pathname.toLowerCase();
  const CARD_TEXT_MAX = 600;
  const WALK_MAX = 8;
  const otherProductNear = (form) => {
    let node = form;
    for (let i = 0; i < WALK_MAX && node && node !== document.body; i++) {
      const links = node.querySelectorAll ? node.querySelectorAll('a[href*="/products/"]') : [];
      for (const a of links) {
        let p = '';
        try { p = new URL(a.getAttribute('href'), location.href).pathname.toLowerCase(); }
        catch (e) { continue; }
        if (p.indexOf('/products/') !== 0) continue;
        if (p !== path) return p;
      }
      if (String(node.innerText || '').length > CARD_TEXT_MAX) break;
      node = node.parentElement;
    }
    return '';
  };
  const out = {own: [], marked: []};
  Array.from(document.querySelectorAll('form')).forEach((f, i) => {
    const act = f.getAttribute('action') || '';
    if (!/cart\\/add/.test(act)) return;
    if (otherProductNear(f)) return;
    const btn = f.querySelector('button[name=add],button[type=submit],input[type=submit]');
    out.own.push({
      i,
      btn_text: btn ? (btn.textContent || btn.value || '').trim().slice(0, 30) : '(none)',
      btn_disabled: btn ? !!btn.disabled : null,
      btn_visible: btn ? !!(btn.offsetParent || btn.getClientRects().length) : false,
      // The stamp our observation puts on listed elements, if this control is
      // listed at all. That is the whole question for the leg.
      obs_idx: btn ? (btn.getAttribute('data-jarvis-idx') || '') : '',
      obs_id: btn ? (btn.getAttribute('data-jarvis-obs') || '') : '',
    });
  });
  return out;
}
"""


async def _main_async(url: str) -> int:
    from app.browser import choice
    from app.browser import observe as dom_observe
    from app.browser import session as browser_session
    from app.browser.runtime import run_browser
    from app.browser.session import ensure_playwright_driver

    await run_browser(ensure_playwright_driver(), timeout=90)

    async def _work() -> int:
        host = (urlparse(url).hostname or "").lower()
        session = await browser_session.BrowserSession.open({host})
        try:
            await session.goto(url)
            await session.settle()
            obs = await dom_observe.observe(session.page)
            print(f"observation: {obs.element_total} elements, title={obs.title[:50]!r}")

            found = await session.page.evaluate(FIND_BUYBOX_JS)
            print(f"\n=== R1: {len(found['own'])} form(s) are the page's own ===")
            for f in found["own"]:
                print(f"  form[{f['i']}] btn={f['btn_text']!r} disabled={f['btn_disabled']} "
                      f"visible={f['btn_visible']} OBS_IDX={f['obs_idx']!r}")

            listed = [f for f in found["own"] if str(f["obs_idx"]).strip()]
            print(f"\n  of those, LISTED in our observation: "
                  f"{[f['obs_idx'] for f in listed]}")
            if not listed:
                print("  !! the buy box's submit control is NOT in the element list —")
                print("     the leg would need the control to be listed, so this is")
                print("     the thing to fix first.")
                # Show what IS listed near the buy box, to see why.
                for e in obs.elements[:40]:
                    nm = (getattr(e, "name_full", "") or e.name or "")[:44]
                    print(f"     [{e.index:>3}] {str(e.role):<9} {nm!r}")
                return 0

            idx = int(listed[0]["obs_idx"])
            print(f"\n=== read_commit_target on element {idx} ===")
            target = await session.read_commit_target(obs, idx)
            if not isinstance(target, dict) or not target.get("action"):
                print("  NOT A FORM — chain broken here")
                return 0
            print(f"  action  : {target.get('action')}")
            print(f"  method  : {target.get('method')}")
            print(f"  fields  : {len(target.get('fields') or [])}")
            for f in (target.get("fields") or [])[:12]:
                print(f"      {f}")
            axes = choice.axes_of(target)
            print(f"  AXES    : {len(axes)}")
            for a in axes:
                print(f"     {a.name!r}: "
                      f"{[(o.label or o.value) + ('' if o.available else '(x)') for o in a.options]}")
                print(f"       buyable={[o.label or o.value for o in a.buyable]} "
                      f"chosen={a.chosen.value if a.chosen else None}")

            print("\n=== choice.unresolved_axis ===")
            words = choice.target_tokens(
                "go to junaidjamshed.com and add black kameez kurta in cart", url
            )
            d0 = choice.unresolved_axis(axes, words)
            print(f"  (a) no answer      -> {d0}")
            words_l = choice.target_tokens(
                "go to junaidjamshed.com and add black kameez kurta in cart", url,
                extra=["select size large and add to cart"],
            )
            d1 = choice.unresolved_axis(axes, words_l)
            print(f"  (b) said 'large'   -> {d1}")
            d2 = choice.unresolved_axis(axes, words, answer="L")
            print(f"  (c) answered 'L'   -> {d2}")
            d3 = choice.unresolved_axis(axes, words, answer="large")
            print(f"  (d) answered 'large' -> {d3}")
        finally:
            await session.close()
        return 0

    return await run_browser(_work(), timeout=300)


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    code = 1
    try:
        code = loop.run_until_complete(_main_async(url))
    finally:
        try:
            from app.browser.runtime import shutdown_browser_runtime

            shutdown_browser_runtime()
        except Exception:  # noqa: BLE001
            pass
    sys.stdout.flush()
    import os

    os._exit(code)


if __name__ == "__main__":
    raise SystemExit(main())
