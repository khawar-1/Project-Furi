"""
Jarvis OS — what does a PRODUCT page really offer? (2026-08-08)

The listing measurement returned a product page with 38 elements and NO
add-to-cart control, no size selector and no quantity input. That is either
(a) the page genuinely hydrates its buy-box after our readiness exit, or
(b) our observation is missing it — and the two imply completely different work.

So this observes the same page THREE times with a growing wait, and prints every
form control it can see plus what the raw DOM says, so the variant question is
designed against the DOM the site really serves.

    venv\\Scripts\\python -u scripts\\_measure_product_page.py [url]

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

DEFAULT_URL = "https://www.junaidjamshed.com/products/janan-gold-100ml"

# What a buy-box is made of, asked of the raw DOM rather than of our element
# list, so "we cannot see it" and "it is not there" are told apart.
RAW_JS = """
() => {
  const out = {sel: [], radio: [], number: [], submit: [], forms: [], swatch: []};
  for (const s of document.querySelectorAll('select')) {
    out.sel.push({
      name: s.name || s.id || '',
      options: Array.from(s.options).slice(0, 12).map(o => (o.textContent || '').trim()),
      visible: !!(s.offsetParent || s.getClientRects().length)
    });
  }
  for (const r of document.querySelectorAll('input[type=radio]')) {
    out.radio.push({name: r.name || '', value: r.value || '',
                    label: (r.labels && r.labels[0] ? r.labels[0].textContent : '').trim().slice(0, 40)});
  }
  for (const n of document.querySelectorAll('input[type=number]')) {
    out.number.push({name: n.name || n.id || '', value: n.value || '',
                     min: n.min, max: n.max,
                     visible: !!(n.offsetParent || n.getClientRects().length)});
  }
  for (const b of document.querySelectorAll('button, input[type=submit]')) {
    const t = ((b.textContent || b.value || '') + '').trim().slice(0, 40);
    if (/cart|bag|buy|add/i.test(t)) out.submit.push({
      text: t, type: b.type || '', name: b.name || '',
      visible: !!(b.offsetParent || b.getClientRects().length),
      inForm: !!b.closest('form')
    });
  }
  for (const f of document.querySelectorAll('form')) {
    out.forms.push({action: f.getAttribute('action') || '', method: f.method || '',
                    fields: f.elements.length});
  }
  // Variant pickers that are neither <select> nor radio — the shape a swatch
  // takes on a modern storefront.
  for (const g of document.querySelectorAll('[class*=variant],[class*=swatch],[class*=option],fieldset')) {
    const label = (g.querySelector('legend,label')||{}).textContent || '';
    const kids = g.querySelectorAll('input,button,label,li');
    if (kids.length >= 2 && kids.length <= 30) {
      out.swatch.push({
        cls: (g.className || '').slice(0, 60),
        label: label.trim().slice(0, 40),
        kids: Array.from(kids).slice(0, 10).map(k => (k.textContent || k.value || '').trim().slice(0, 24)).filter(Boolean)
      });
    }
  }
  return out;
}
"""


async def _main_async(url: str) -> int:
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
            if "/search" in url:
                # Follow the first real product link, so a listing URL can be
                # handed in and the probe still lands on a detail page.
                await session.settle()
                first = await session.page.evaluate(
                    "() => { const a = document.querySelector('a[href*=\"/products/\"]');"
                    " return a ? a.href : ''; }"
                )
                print(f"following first product: {first}")
                if first:
                    await session.goto(first)
            for wait in (0.0, 3.0, 6.0):
                if wait:
                    await asyncio.sleep(wait)
                await session.settle()
                obs = await dom_observe.observe(session.page)
                print(f"\n--- after +{wait:.0f}s: {obs.element_total} elements, "
                      f"{len(obs.page_text or '')} chars of text")
                for e in obs.elements:
                    role = str(e.role or "")
                    name = (getattr(e, "name_full", "") or e.name or "")[:60]
                    if role != "link" or "cart" in name.lower() or "bag" in name.lower():
                        print(f"    [{e.index:>3}] {role:<10} form={int(bool(getattr(e, 'form', False)))} {name!r}")
            raw = await session.page.evaluate(RAW_JS)
            print("\n=== RAW DOM ===")
            for key, rows in raw.items():
                print(f"  {key}: {len(rows)}")
                for r in rows[:8]:
                    print(f"      {r}")
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
