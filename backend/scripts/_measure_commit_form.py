"""
Furi OS — what is actually INSIDE the add-to-cart form? (2026-08-08)

The variant question ("which size? how many?") can only be anchored to the form
Furi is about to submit. Anchoring it to the PAGE is measurably wrong: the
product page's only visible <select> is the REVIEW SORT dropdown, so a page-level
gate would ask the user to choose "Most recent / Highest rating".

So this reads each `/cart/add` form the way `_FORM_CONTRACT_JS_BODY` does, and
adds the one thing that body does not capture — the ALTERNATIVES each control
offers — to answer:

    * is the variant picker (the XS/S/M/L radio group) INSIDE the form, or a
      sibling that only writes a hidden `id`?
    * is there a quantity field, and is it visible?
    * how many controls would a naive "ask about every multi-valued control"
      gate interrupt the user about?

    venv\\Scripts\\python -u scripts\\_measure_commit_form.py [url]

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
# A legacy cp1252 console cannot encode a '✓' out of a real product label, and a
# probe that dies mid-report is a probe that reports nothing (the recorded rule).
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_URL = "https://www.junaidjamshed.com/collections/fragrances/products/purple-serenity"

FORMS_JS = """
() => {
  const out = [];
  for (const form of document.querySelectorAll('form')) {
    const action = form.action || '';
    if (!/cart\\/add/i.test(action)) continue;
    const fields = [];
    for (const c of Array.from(form.elements || [])) {
      const type = (c.type || '').toLowerCase();
      if (!c.name) continue;
      if (['submit', 'button', 'reset', 'image'].includes(type)) continue;
      let options = [];
      if (c.tagName === 'SELECT') {
        options = Array.from(c.options).slice(0, 12).map(
          o => ({value: o.value, label: (o.textContent || '').trim()}));
      } else if (type === 'radio' || type === 'checkbox') {
        // ⚠️ THE WHOLE DOCUMENT, filtered by FORM ASSOCIATION — not
        // form.querySelectorAll. The first cut used the latter and reported
        // options=0 for a radio that form.elements had just yielded: this theme
        // puts the variant radios OUTSIDE the <form> and links them with the
        // `form=` attribute, so they are form-ASSOCIATED but not form-DESCENDANTS.
        options = Array.from(document.querySelectorAll(
          `input[name="${CSS.escape(c.name)}"]`)).filter(
            r => r.form === form).slice(0, 12).map(r => ({
            value: r.value,
            label: ((r.labels && r.labels[0] ? r.labels[0].textContent : '') || '').trim(),
            checked: r.checked
          }));
      }
      fields.push({
        name: c.name, type: type, value: String(c.value || '').slice(0, 40),
        visible: !!(c.offsetParent || c.getClientRects().length),
        options: options
      });
    }
    out.push({action: action, method: form.method, fields: fields,
              visible: !!(form.offsetParent || form.getClientRects().length)});
  }
  return out;
}
"""

# The variant pickers as they exist on the PAGE, so "inside the form" can be
# told from "a sibling that writes a hidden id".
OUTSIDE_JS = """
() => {
  const groups = {};
  for (const r of document.querySelectorAll('input[type=radio]')) {
    const key = r.name || '(unnamed)';
    groups[key] = groups[key] || {count: 0, inCartForm: false, values: []};
    groups[key].count += 1;
    if (groups[key].values.length < 8) groups[key].values.push(r.value);
    const f = r.closest('form');
    if (f && /cart\\/add/i.test(f.action || '')) groups[key].inCartForm = true;
  }
  return groups;
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
            forms = await session.page.evaluate(FORMS_JS)
            groups = await session.page.evaluate(OUTSIDE_JS)

            print(f"\n{len(forms)} /cart/add form(s)")
            for i, f in enumerate(forms[:4]):
                print(f"\n  form {i} visible={f['visible']} method={f['method']}")
                for fl in f["fields"]:
                    n = len(fl["options"])
                    print(
                        f"    {fl['name']:<28} type={fl['type']:<8} "
                        f"visible={int(fl['visible'])} options={n:<3} "
                        f"value={fl['value']!r}"
                    )
                    if n > 1:
                        for o in fl["options"][:8]:
                            print(f"         - {o}")

            print("\nradio groups on the page:")
            for name, g in groups.items():
                print(f"  {name:<34} n={g['count']:<3} inCartForm={g['inCartForm']} {g['values'][:6]}")

            out = Path(__file__).resolve().parent / "bench-results" / "commit-form.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps({"forms": forms, "groups": groups}, indent=2),
                           encoding="utf-8")
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
