"""
Jarvis OS — HOW does this product page carry its size? (2026-08-10)

`_measure_cart_controls.py` found the buy-box form and `choice.axes_of` reported
**AXES = 0** on it: the form's own `form.elements` are

    form_type, utf8, id, properties[...], quantity, add, product-id, section-id

with no `Size` anywhere, while the raw DOM plainly carries a `Size` radio group
of XS/S/M/L/XL/XXL. So the size gate cannot see this page's size, and a submit
would carry whatever the hidden `id` happens to be.

The 2026-08-08 round measured a DIFFERENT product where the radios WERE
form-associated (`.form === form`). This asks the question directly on the
incident's own product, because the two imply different fixes:

  * `.form` points at the buy-box form  -> our axis scan has a bug
  * `.form` is null / another form      -> the binding is JS-only, and the fix
                                           is to bind by PRODUCT, not by form

It also watches what CLICKING a size does to the hidden `id`, which is the only
thing that decides whether a code-settled size actually reaches the cart.

    venv\\Scripts\\python -u scripts\\_measure_variant_binding.py [url]

NEVER collected by pytest. Reads and clicks a size swatch; NOTHING is submitted.
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

BINDING_JS = """
() => {
  const out = {forms: [], size: [], buybox: null};
  // Every /cart/add form, with the fields it would send.
  const forms = Array.from(document.querySelectorAll('form'));
  forms.forEach((f, i) => {
    const names = [];
    for (const c of f.elements) if (c.name) names.push(c.name);
    const btn = f.querySelector('button[type=submit],input[type=submit],button[name=add]');
    out.forms.push({
      i,
      action: f.getAttribute('action') || '',
      names: Array.from(new Set(names)).slice(0, 14),
      id_value: (f.querySelector('[name=id]') || {}).value || '',
      btn: btn ? (btn.textContent || btn.value || '').trim().slice(0, 24) : '',
      visible: !!(f.offsetParent || f.getClientRects().length),
    });
  });
  // Where does each Size radio think it belongs?
  const radios = Array.from(document.querySelectorAll('input[name=Size]'));
  radios.forEach((r) => {
    const f = r.form;
    out.size.push({
      value: r.value,
      checked: r.checked,
      disabled: r.disabled,
      has_form_attr: r.hasAttribute('form'),
      form_attr: r.getAttribute('form') || '',
      form_index: f ? forms.indexOf(f) : -1,
      label_cls: (r.labels && r.labels[0] ? r.labels[0].className : '').slice(0, 60),
      parent_cls: (r.parentElement ? r.parentElement.className : '').slice(0, 60),
    });
  });
  // The buy box: the visible form whose submit button says add-to-bag.
  const bb = forms.find((f) => {
    const b = f.querySelector('button[name=add],button[type=submit]');
    return b && /add to (bag|cart)/i.test(b.textContent || '');
  });
  if (bb) out.buybox = {index: forms.indexOf(bb),
                       id_value: (bb.querySelector('[name=id]') || {}).value || ''};
  return out;
}
"""

CLICK_AND_WATCH_JS = """
(label) => {
  const out = {before: '', after: '', clicked: '', err: ''};
  const idOf = () => {
    const f = Array.from(document.querySelectorAll('form')).find((f) => {
      const b = f.querySelector('button[name=add],button[type=submit]');
      return b && /add to (bag|cart)/i.test(b.textContent || '');
    });
    return f ? ((f.querySelector('[name=id]') || {}).value || '') : '(no buybox)';
  };
  out.before = idOf();
  try {
    const r = Array.from(document.querySelectorAll('input[name=Size]'))
      .find((x) => String(x.value).toLowerCase() === String(label).toLowerCase());
    if (!r) { out.err = 'no such size'; return out; }
    const lab = (r.labels && r.labels[0]) ? r.labels[0] : r;
    lab.click();
    out.clicked = r.value;
  } catch (e) { out.err = String(e); }
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
            data = await session.page.evaluate(BINDING_JS)

            print("=== /cart/add FORMS (visible ones first) ===")
            rows = [f for f in data["forms"] if f["action"].endswith("/cart/add")]
            print(f"  {len(rows)} of {len(data['forms'])} forms post to /cart/add")
            for f in rows:
                if f["visible"] or f["btn"]:
                    print(f"  [{f['i']:>2}] vis={int(f['visible'])} btn={f['btn']!r} "
                          f"id={f['id_value']!r}")
                    print(f"        names={f['names']}")

            print(f"\n=== buy box === {data['buybox']}")

            print(f"\n=== Size radios ({len(data['size'])}) ===")
            for s in data["size"]:
                print(f"  {s['value']:<4} checked={int(s['checked'])} "
                      f"disabled={int(s['disabled'])} form_attr={s['form_attr']!r} "
                      f"form_index={s['form_index']} label_cls={s['label_cls']!r}")

            print("\n=== does clicking a size change the buy box's hidden id? ===")
            res = await session.page.evaluate(CLICK_AND_WATCH_JS, "L")
            await session.settle()
            after = await session.page.evaluate(
                "() => { const f = Array.from(document.querySelectorAll('form'))"
                ".find(f => { const b = f.querySelector('button[name=add],button[type=submit]');"
                " return b && /add to (bag|cart)/i.test(b.textContent || ''); });"
                " return f ? ((f.querySelector('[name=id]')||{}).value || '') : '(none)'; }"
            )
            print(f"  clicked   : {res.get('clicked')!r}  err={res.get('err')!r}")
            print(f"  id before : {res.get('before')!r}")
            print(f"  id after  : {after!r}")
            print(f"  CHANGED   : {res.get('before') != after}")
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
