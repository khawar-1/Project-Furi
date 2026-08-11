"""
Jarvis OS — WHICH add-to-cart control belongs to the page's own product? (2026-08-10)

`_measure_product_page.py` refuted the obvious design. On the incident's own
product page there is not ONE add-to-cart control — there are FOURTEEN:

    [62] 'Add to bag'   [71] 'Add to bag'   + twelve 'Quick add' (the rails)
    forms: 26, every one of them action='/cart/add'

so a deterministic leg gated on "exactly one cart control" could never fire.
The question this answers is the one that actually decides the design: given a
cart-labelled element, what does `read_commit_target` + `choice.axes_of` say
about the form behind it — and is there a STRUCTURAL discriminator between the
page's own buy-box and a related-product card's quick-add?

    venv\\Scripts\\python -u scripts\\_measure_cart_controls.py [url]

NEVER collected by pytest (real browser, real network). Reads only; nothing is
ever submitted — `arm_commit` is not called anywhere in this file.
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

# The page's own product, as the page itself identifies it. Shopify renders the
# main product's handle into a few well-known places; we read them all and print
# them, rather than picking one and hoping.
IDENTITY_JS = """
() => {
  const out = {};
  out.path = location.pathname;
  const canon = document.querySelector('link[rel=canonical]');
  out.canonical = canon ? canon.href : '';
  const og = document.querySelector('meta[property="og:title"]');
  out.og_title = og ? og.content : '';
  out.title = document.title;
  return out;
}
"""

# For the element under the mouse: walk to its form and describe it the way a
# person would need to tell "the buy box" from "a card in the rail".
FORM_SHAPE_JS = """
(el) => {
  const f = el.closest('form');
  if (!f) return {form: false};
  const names = new Set();
  for (const c of f.elements) if (c.name) names.add(c.name);
  // How far up the tree is the form, and what does the nearest heading say?
  let node = el, depth = 0;
  while (node && node !== f && depth < 20) { node = node.parentElement; depth++; }
  let h = f.closest('[class*=product]');
  return {
    form: true,
    action: f.getAttribute('action') || '',
    method: (f.method || '').toLowerCase(),
    fields: f.elements.length,
    names: Array.from(names).slice(0, 20),
    depth_from_form: depth,
    // Does this form sit inside something the page marks as a RECOMMENDATION /
    // related block? That is the honest structural question.
    in_recommendations: !!el.closest(
      '[class*=recommend],[class*=related],[class*=upsell],[class*=you-may],' +
      '[id*=recommend],[id*=related],section[class*=complementary]'
    ),
    outer_cls: (h ? h.className : '').slice(0, 80),
    // The product link nearest this control — a rail card links its OWN product.
    near_href: (() => {
      const card = el.closest('[class*=card],li,article') || f;
      const a = card.querySelector('a[href*="/products/"]');
      return a ? new URL(a.getAttribute('href'), location.href).pathname : '';
    })(),
  };
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
            ident = await session.page.evaluate(IDENTITY_JS)
            print("=== PAGE IDENTITY ===")
            for k, v in ident.items():
                print(f"  {k:<10} {str(v)[:100]!r}")
            print(f"\n=== {obs.element_total} elements; cart-labelled ones ===")

            cart_words = ("add to bag", "add to cart", "quick add", "add to basket",
                          "buy", "choose options", "select options")
            hits = []
            for e in obs.elements:
                name = ((getattr(e, "name_full", "") or e.name or "")).strip()
                if any(w in name.lower() for w in cart_words):
                    hits.append((e, name))
            print(f"  {len(hits)} cart-labelled element(s)\n")

            for e, name in hits:
                print(f"--- [{e.index}] {str(e.role)!r} {name!r}")
                try:
                    handle = await dom_observe.resolve(session.page, obs, e.index)
                    shape = await handle.evaluate(FORM_SHAPE_JS)
                except Exception as exc:  # noqa: BLE001
                    print(f"      shape read failed: {type(exc).__name__}: {exc}")
                    shape = {}
                for k in ("form", "action", "method", "fields", "in_recommendations",
                          "near_href", "outer_cls"):
                    if k in shape:
                        print(f"      {k:<18} {str(shape[k])[:90]!r}")
                if shape.get("names"):
                    print(f"      {'names':<18} {shape['names']}")
                target = await session.read_commit_target(obs, e.index)
                if not isinstance(target, dict) or not target.get("action"):
                    print("      read_commit_target -> NOT A FORM")
                    continue
                axes = choice.axes_of(target)
                print(f"      contract action    {str(target.get('action'))[:80]!r}")
                print(f"      contract fields    {len(target.get('fields') or [])}")
                print(f"      AXES               {len(axes)}")
                for a in axes:
                    vals = [
                        f"{o.label or o.value}{'*' if o.chosen else ''}"
                        f"{'' if o.available else '(x)'}"
                        for o in a.options
                    ]
                    print(f"        - {a.name!r}: {vals}")
                    print(f"          buyable={[o.label or o.value for o in a.buyable]}")
            print("\n=== raw contract of the FIRST axis-bearing control ===")
            for e, _ in hits:
                target = await session.read_commit_target(obs, e.index)
                if isinstance(target, dict) and choice.axes_of(target):
                    for k, v in target.items():
                        if k == "fields":
                            print(f"  fields:")
                            for fld in (v or [])[:24]:
                                print(f"      {fld}")
                        elif k == "axes":
                            continue
                        else:
                            print(f"  {k}: {str(v)[:120]!r}")
                    break
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
