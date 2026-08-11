"""
Jarvis OS — which RULE picks the page's own buy box? (2026-08-10)

`_measure_buybox_identity.py` established the ground truth on the incident's own
product page: of 16 '/cart/add' forms, **form 0** is the page's own buy box (its
`properties[Product ID]` is `JJKSA30729R52AP`, exactly the page slug's SKU, and
it carries the Size axis), while forms 1-15 belong to rail products — including
the only two whose buttons SAY "Add to bag".

This scores candidate rules against that ground truth, on several product pages,
so the leg is built on a rule that is MEASURED to select the right form rather
than one that sounds right:

  R1  the form is not inside another product's CARD
      (bounded ancestry walk, the soldOutOf boundary, looking for a link to a
      DIFFERENT /products/ path)
  R2  a field of this form identifies the product, and that identifier appears
      in the page's own URL path
  R3  the form is the one the page's largest visible variant picker is bound to
      (radios/selects whose `form` attribute or ancestry names it)

    venv\\Scripts\\python -u scripts\\_measure_buybox_rules.py [url ...]

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

DEFAULT_URLS = [
    # sizes XS-XXL, buy button reads "Select Size", 16 cart forms
    "https://www.junaidjamshed.com/products/"
    "black-blended-kameez-shalwar-jjksa30729r52ap",
    # a single-variant fragrance — the shape with no axis at all
    "https://www.junaidjamshed.com/products/janan-gold-100ml",
]

RULES_JS = """
() => {
  const path = location.pathname.toLowerCase();
  const CARD_TEXT_MAX = 600;      // the soldOutOf boundary, same reasoning
  const WALK_MAX = 8;
  const norm = (s) => String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, '');
  const pathKey = norm(path);

  const otherProductNear = (form) => {
    // Walk up, bounded by the card's own size, looking for a link to a
    // DIFFERENT product. A rail card links its own item; the main buy box
    // sits in no card at all.
    let node = form;
    for (let i = 0; i < WALK_MAX && node && node !== document.body; i++) {
      const links = node.querySelectorAll ? node.querySelectorAll('a[href*="/products/"]') : [];
      for (const a of links) {
        let p = '';
        try { p = new URL(a.getAttribute('href'), location.href).pathname.toLowerCase(); }
        catch (e) { continue; }
        if (p.indexOf('/products/') !== 0) continue;   // sharer.php &c
        if (p !== path) return p;
      }
      if (String(node.innerText || '').length > CARD_TEXT_MAX) break;
      node = node.parentElement;
    }
    return '';
  };

  const idInPath = (form) => {
    for (const c of form.elements) {
      const v = norm(c.value);
      if (v.length >= 5 && pathKey.indexOf(v) >= 0) return c.name + '=' + c.value;
    }
    return '';
  };

  const pickerBound = (form) => {
    // Visible multi-option groups whose controls resolve to THIS form.
    const groups = {};
    for (const el of document.querySelectorAll('input[type=radio],select')) {
      if (el.form !== form) continue;
      const vis = !!(el.offsetParent || el.getClientRects().length) ||
                  !!(el.labels && el.labels[0] &&
                     (el.labels[0].offsetParent || el.labels[0].getClientRects().length));
      if (!vis || !el.name) continue;
      groups[el.name] = (groups[el.name] || 0) + 1;
    }
    return Object.entries(groups).filter(([, n]) => n > 1).map(([k, n]) => k + '(' + n + ')');
  };

  const out = [];
  Array.from(document.querySelectorAll('form')).forEach((f, i) => {
    if (!/cart\\/add/.test(f.getAttribute('action') || '')) return;
    const btn = f.querySelector('button[name=add],button[type=submit],input[type=submit]');
    out.push({
      i,
      btn: btn ? (btn.textContent || btn.value || '').trim().slice(0, 22) : '',
      product_id: (f.querySelector('[name="properties[Product ID]"]') || {}).value || '',
      R1_other_product: otherProductNear(f),
      R2_id_in_path: idInPath(f),
      R3_picker: pickerBound(f),
      visible: !!(f.offsetParent || f.getClientRects().length),
    });
  });
  return {path, forms: out};
}
"""


async def _one(session, url: str) -> None:
    data = await session.page.evaluate(RULES_JS)
    forms = data["forms"]
    print(f"\n{'=' * 78}\n{url}\n  {len(forms)} cart forms")
    r1 = [f["i"] for f in forms if not f["R1_other_product"]]
    r2 = [f["i"] for f in forms if f["R2_id_in_path"]]
    r3 = [f["i"] for f in forms if f["R3_picker"]]
    for f in forms:
        flags = "".join([
            "1" if not f["R1_other_product"] else ".",
            "2" if f["R2_id_in_path"] else ".",
            "3" if f["R3_picker"] else ".",
        ])
        print(f"  form[{f['i']:>2}] {flags}  btn={f['btn']!r:<16} "
              f"pid={f['product_id']!r:<18} picker={f['R3_picker']}")
        if f["R1_other_product"]:
            print(f"            other product near: {f['R1_other_product']}")
        if f["R2_id_in_path"]:
            print(f"            id in path        : {f['R2_id_in_path']}")
    print(f"  R1 (no other product's card) -> {r1}")
    print(f"  R2 (a field id is in the URL)-> {r2}")
    print(f"  R3 (a visible picker binds)  -> {r3}")
    print(f"  R1&R2 -> {sorted(set(r1) & set(r2))}   "
          f"R1|R2 -> {sorted(set(r1) | set(r2))}")


async def _main_async(urls: list[str]) -> int:
    from app.browser import session as browser_session
    from app.browser.runtime import run_browser
    from app.browser.session import ensure_playwright_driver

    await run_browser(ensure_playwright_driver(), timeout=90)

    async def _work() -> int:
        hosts = {(urlparse(u).hostname or "").lower() for u in urls}
        session = await browser_session.BrowserSession.open(hosts)
        try:
            for url in urls:
                await session.goto(url)
                await session.settle()
                await _one(session, url)
        finally:
            await session.close()
        return 0

    return await run_browser(_work(), timeout=420)


def main() -> int:
    urls = sys.argv[1:] or DEFAULT_URLS
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    code = 1
    try:
        code = loop.run_until_complete(_main_async(urls))
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
