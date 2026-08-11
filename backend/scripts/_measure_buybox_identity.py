"""
Jarvis OS — WHICH /cart/add form is the page's OWN product? (2026-08-10)

⚠️ THIS QUESTION IS LOAD-BEARING FOR SAFETY, not for tidiness.
`_measure_variant_binding.py` measured the incident's own product page and found:

    form  0  btn 'Select Size'  id=''                names include Size, Style
    form 11  btn 'Add to bag'   id='58126569832608'  no Size/Style
    form 14  btn 'Add to bag'   id='56957187981472'  no Size/Style
    + 13 more '/cart/add' forms whose buttons say 'ADD' / 'Quick add'

The buttons that SAY "Add to bag" are RELATED PRODUCTS. The page's own buy box
says "Select Size" (because no size is chosen and its `id` is still empty). So a
deterministic leg that submitted "the add-to-cart control" would have added
SOMEBODY ELSE'S PRODUCT to the cart — which is worse than the bug it fixes.

This finds a discriminator that is a FACT about the page rather than a guess:
for every /cart/add form it prints the ancestry, the nearest product link, the
product id, and whether it sits inside the block that holds the page's <h1>.

    venv\\Scripts\\python -u scripts\\_measure_buybox_identity.py [url]

NEVER collected by pytest. Reads only; nothing is submitted.
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

IDENTITY_JS = """
() => {
  const out = {page: {}, forms: []};
  out.page.path = location.pathname;
  const h1 = document.querySelector('h1');
  out.page.h1 = h1 ? (h1.innerText || '').trim().slice(0, 60) : '';
  const forms = Array.from(document.querySelectorAll('form'));
  forms.forEach((f, i) => {
    const act = f.getAttribute('action') || '';
    if (!/cart\\/add/.test(act)) return;
    const names = [];
    for (const c of f.elements) if (c.name) names.push(c.name);
    const uniq = Array.from(new Set(names));
    const btn = f.querySelector('button[name=add],button[type=submit],input[type=submit]');
    // ancestry: class names from the form up to <body>
    const chain = [];
    let n = f;
    while (n && n !== document.body && chain.length < 12) {
      chain.push(((n.tagName || '') + '.' + (n.className || '')).slice(0, 46));
      n = n.parentElement;
    }
    // The nearest PRODUCT LINK above this form — a rail card links its own item.
    let card = f, href = '';
    for (let d = 0; d < 12 && card; d++) {
      const a = card.querySelector ? card.querySelector('a[href*="/products/"]') : null;
      if (a) { href = new URL(a.getAttribute('href'), location.href).pathname; break; }
      card = card.parentElement;
    }
    // Does this form share an ancestor with the page's own <h1>?
    let withH1 = false;
    if (h1) {
      let a = f.parentElement, hops = 0;
      while (a && hops < 6) { if (a.contains(h1)) { withH1 = true; break; } a = a.parentElement; hops++; }
    }
    // Multi-valued option groups this form offers (the AXIS test).
    const groups = {};
    for (const c of f.elements) {
      const t = (c.type || '').toLowerCase();
      if ((t === 'radio' || t === 'select-one') && c.name) {
        groups[c.name] = (groups[c.name] || 0) + (t === 'radio' ? 1 : (c.options || []).length);
      }
    }
    const axes = Object.entries(groups).filter(([, n]) => n > 1);
    out.forms.push({
      i,
      btn: btn ? (btn.textContent || btn.value || '').trim().slice(0, 22) : '',
      id_value: (f.querySelector('[name=id]') || {}).value || '',
      product_id: (f.querySelector('[name="properties[Product ID]"]') || {}).value || '',
      axes: axes.map(([k, n]) => k + '(' + n + ')'),
      near_product_href: href,
      shares_ancestor_with_h1: withH1,
      chain: chain.slice(0, 5),
      n_fields: uniq.length,
    });
  });
  return out;
}
"""


async def _main_async(url: str) -> int:
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
            data = await session.page.evaluate(IDENTITY_JS)
            print(f"=== PAGE === path={data['page']['path']!r}")
            print(f"            h1={data['page']['h1']!r}\n")
            for f in data["forms"]:
                mark = ""
                if f["shares_ancestor_with_h1"]:
                    mark = "   <<< shares an ancestor with the page's own <h1>"
                print(f"form[{f['i']:>2}] btn={f['btn']!r:<16} id={f['id_value']!r:<18} "
                      f"axes={f['axes']}{mark}")
                print(f"          near_product_href={f['near_product_href']!r}")
                print(f"          product_id={f['product_id']!r}  chain={f['chain'][:3]}")
            print("\n=== DISCRIMINATOR SUMMARY ===")
            own = [f for f in data["forms"]
                   if f["near_product_href"] in ("", data["page"]["path"])]
            print(f"  forms whose nearest product link is the page itself (or none): "
                  f"{[f['i'] for f in own]}")
            withax = [f for f in data["forms"] if f["axes"]]
            print(f"  forms carrying a multi-valued axis: {[f['i'] for f in withax]}")
            h1s = [f for f in data["forms"] if f["shares_ancestor_with_h1"]]
            print(f"  forms sharing an ancestor with <h1>: {[f['i'] for f in h1s]}")
            both = [f for f in data["forms"]
                    if f["axes"] and f["near_product_href"] in ("", data["page"]["path"])]
            print(f"  axes AND own-product link          : {[f['i'] for f in both]}")
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
