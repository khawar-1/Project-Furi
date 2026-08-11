"""
Jarvis OS — what does a variant AXIS look like in the DOM? (2026-08-08)

The 2026-08-08 item-choice round measured a FRAGRANCE page and found no
multi-valued axis at all, so the variant gate was deliberately not built. The
Shopify JSON for a KURTA says otherwise:

    Size: ['XS','S','M','L','XL','XXL']   6 values, 1 AVAILABLE
    Color: ['Grey']                      1 value
    Style: ['JJK-A-.../S26/...']         1 value (a SKU code)

So this reads the `/cart/add` form the way `_FORM_CONTRACT_JS_BODY` does and adds
the two things that body does not capture, which are exactly the two the gate
needs:

    * the ALTERNATIVES each control offers, and
    * whether each alternative can actually be BOUGHT (disabled / aria-disabled /
      a sold-out class or label), because offering a size that cannot be added to
      the cart is offering a dead end.

    venv\\Scripts\\python -u scripts\\_measure_variant_form.py [url]

NEVER collected by pytest (real browser, real network).
"""
from __future__ import annotations

import asyncio
import functools
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

print = functools.partial(print, flush=True)  # noqa: A001
# A legacy cp1252 console cannot encode a label out of a real product page, and a
# probe that dies mid-report is a probe that reports nothing (the recorded rule).
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# A REAL garment: 6 sizes, exactly ONE of them in stock.
DEFAULT_URL = "https://www.junaidjamshed.com/products/grey-formal-kurta-jjka50589"

PROBE_JS = r"""
() => {
  const seen = (el) => !!(el.offsetParent || el.getClientRects().length);
  const dead = (el) => {
    // Every signal a storefront uses to say "you cannot buy this one".
    const cls = String(el.className || '');
    const lab = (el.labels && el.labels[0]) ? el.labels[0] : null;
    const labCls = lab ? String(lab.className || '') : '';
    const labTxt = lab ? String(lab.innerText || '') : '';
    return {
      disabled: !!el.disabled,
      aria: el.getAttribute('aria-disabled'),
      cls: cls,
      labCls: labCls,
      soldText: /sold\s*out|unavailable|out of stock/i.test(labTxt),
      labTxt: labTxt.trim().slice(0, 60),
    };
  };
  const out = [];
  for (const form of document.querySelectorAll('form')) {
    const action = form.action || '';
    if (!/cart\/add/i.test(action)) continue;
    const byName = {};
    for (const c of Array.from(form.elements || [])) {
      const type = (c.type || '').toLowerCase();
      if (!c.name) continue;
      if (['submit', 'button', 'reset', 'image'].includes(type)) continue;
      const key = c.name;
      if (byName[key]) { byName[key].members += 1; continue; }
      let options = [];
      if (c.tagName === 'SELECT') {
        options = Array.from(c.options).slice(0, 16).map(o => ({
          value: o.value, label: (o.textContent || '').trim(),
          selected: o.selected, disabled: !!o.disabled,
          cls: String(o.className || ''),
        }));
      } else if (type === 'radio' || type === 'checkbox') {
        // ⚠️ THE WHOLE DOCUMENT filtered by FORM ASSOCIATION — not
        // form.querySelectorAll. This theme puts the variant radios OUTSIDE the
        // <form> and links them with `form=`, so they are form-ASSOCIATED but
        // not form-DESCENDANTS (measured 2026-08-08).
        options = Array.from(document.querySelectorAll(
          `input[name="${CSS.escape(c.name)}"]`)).filter(r => r.form === form)
          .slice(0, 16).map(r => Object.assign({
            value: r.value, checked: r.checked, visible: seen(r),
          }, dead(r)));
      }
      byName[key] = {
        name: c.name, type: type, tag: c.tagName, members: 1,
        value: String(c.value || '').slice(0, 60),
        visible: seen(c), options: options,
      };
    }
    out.push({
      action: action, method: form.method,
      visible: seen(form),
      fields: Object.values(byName),
    });
  }
  return out;
}
"""

# What a size axis looks like when it is NOT a form control at all — some themes
# render it as links to ?variant=<id>, which no form read can ever see.
LINKS_JS = r"""
() => {
  const out = [];
  for (const a of document.querySelectorAll('a[href*="variant="]')) {
    out.push({text: (a.innerText || '').trim().slice(0, 40), href: a.getAttribute('href')});
    if (out.length >= 12) break;
  }
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
            forms = await session.page.evaluate(PROBE_JS)
            links = await session.page.evaluate(LINKS_JS)

            print(f"\n{url}")
            print(f"{len(forms)} /cart/add form(s)\n")
            for i, f in enumerate(forms[:6]):
                print(f"  form {i}  visible={int(f['visible'])}  {f['method']}")
                for fl in f["fields"]:
                    n = len(fl["options"])
                    buyable = [
                        o for o in fl["options"]
                        if not o.get("disabled") and o.get("aria") != "true"
                        and not o.get("soldText")
                    ]
                    print(
                        f"    {fl['name']:<26} {fl['tag']:<7} type={fl['type']:<8} "
                        f"vis={int(fl['visible'])} members={fl['members']} "
                        f"opts={n} buyable={len(buyable)} value={fl['value']!r}"
                    )
                    for o in fl["options"][:8]:
                        print(f"        {o}")
                print()

            print(f"?variant= links on the page: {len(links)}")
            for lk in links[:8]:
                print(f"    {lk}")

            out = Path(__file__).resolve().parent / "bench-results" / "variant-form.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                json.dumps({"url": url, "forms": forms, "variant_links": links}, indent=2),
                encoding="utf-8",
            )
            print(f"\nwrote {out}")
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
