"""
_FORM_CONTRACT_JS_BODY's `axes` against a REAL browser. Opt-in.

    JARVIS_REAL_BROWSER=1 venv/Scripts/python -m pytest tests/test_browser_axes_js.py -v

WHY THIS FILE EXISTS
--------------------
The hermetic tests in test_browse_variant_axis.py drive the DECISION with a
fixture shaped like this program's output. They cannot tell you whether the
program produces that shape, because ScriptedPage.evaluate returns a canned dict
and never runs a line of the JS — and this codebase has shipped a feature built
against an imagined DOM five times.

The markup below is not invented. It is the shape measured on
junaidjamshed.com/products/grey-formal-kurta-jjka50589 (2026-08-08,
scripts/_measure_variant_form.py), and it carries the two things that make a
naive reader wrong:

  1. THE RADIOS ARE FORM-ASSOCIATED, NOT FORM-DESCENDANTS. The theme puts them
     outside the <form> and links them with `form="..."`, so
     form.querySelectorAll returns 0 for a radio form.elements has just yielded.
  2. AVAILABILITY IS MARKED TWO DIFFERENT WAYS ON ONE PAGE. The main product
     leaves the input enabled and marks only the LABEL (`is-disabled`); the
     related-product cards mark the INPUT (`disabled`).

The suite is skipped, not deleted, when Playwright or its Chromium is missing.
"""
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("JARVIS_REAL_BROWSER"),
    reason="real-browser test: set JARVIS_REAL_BROWSER=1 to run",
)


# The main product form, measured. Note `form="pf"` on the radios and the stock
# signal living ONLY on the label's class.
MAIN_FORM = """
<!doctype html><html><body>
  <form id="pf" action="/cart/add" method="post" data-jarvis-commit="1">
    <input type="hidden" name="id" value="">
    <input type="hidden" name="form_type" value="product">
    <input type="text" name="properties[_Charge Code]" value="MENS">
    <button type="submit" id="atc">Add to Cart</button>
  </form>
  <div class="swatches">
    <input form="pf" type="radio" id="s-xs" name="Size" value="XS">
    <label class="hdt-product-form_value is-type-block is-disabled" for="s-xs"></label>
    <input form="pf" type="radio" id="s-s" name="Size" value="S">
    <label class="hdt-product-form_value is-type-block is-disabled" for="s-s"></label>
    <input form="pf" type="radio" id="s-m" name="Size" value="M">
    <label class="hdt-product-form_value is-type-block is-disabled" for="s-m"></label>
    <input form="pf" type="radio" id="s-l" name="Size" value="L">
    <label class="hdt-product-form_value is-type-block " for="s-l"></label>
    <input form="pf" type="radio" id="s-xl" name="Size" value="XL">
    <label class="hdt-product-form_value is-type-block is-disabled" for="s-xl"></label>
    <input form="pf" type="radio" id="s-xxl" name="Size" value="XXL">
    <label class="hdt-product-form_value is-type-block is-disabled" for="s-xxl"></label>
    <input form="pf" type="radio" id="c-grey" name="Color" value="Grey">
    <label class="hdt-product-form_value is-type-color " for="c-grey">Grey</label>
  </div>
  <select name="review_sort"><option>Most recent</option><option>Highest rating</option></select>
</body></html>
"""

# A related-product card: opaque axis name, and the INPUT carries `disabled`.
CARD_FORM = """
<!doctype html><html><body>
  <form id="cf" action="/cart/add" method="post" data-jarvis-commit="1">
    <input type="hidden" name="id" value="">
    <input type="radio" name="option-15623440335008-1" value="XS" disabled>
    <input type="radio" name="option-15623440335008-1" value="S">
    <input type="radio" name="option-15623440335008-1" value="M">
    <input type="text" name="quantity" value="1">
    <button type="submit">Add</button>
  </form>
</body></html>
"""


@pytest.fixture
async def page():
    """A real headless Chromium page. Deliberately NOT a BrowserSession — this
    tests the contract-reading program, not the safety interceptor, and it must
    not touch the shared ~/.jarvis/browser profile."""
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"playwright not installed: {exc}")

    pw = await async_playwright().start()
    browser = None
    errors = []
    for channel in ("chrome", None, "msedge"):
        try:
            browser = await pw.chromium.launch(
                headless=True, **({"channel": channel} if channel else {})
            )
            break
        except Exception as exc:  # pragma: no cover
            errors.append(f"{channel or 'bundled'}: {exc}")
    if browser is None:  # pragma: no cover
        await pw.stop()
        pytest.skip("no Chromium available: " + " | ".join(errors))
    pg = await browser.new_page()
    try:
        yield pg
    finally:
        await browser.close()
        await pw.stop()


async def _read(pg, html):
    from app.browser.session import _REREAD_COMMIT_FORM_JS

    await pg.set_content(html)
    return await pg.evaluate(_REREAD_COMMIT_FORM_JS)


async def test_the_main_products_size_axis_is_read_with_only_L_buyable(page):
    """THE INCIDENT'S OWN MARKUP. Six sizes reach the contract; exactly one is
    marked buyable, and the signal is on the LABEL — the input is enabled on all
    six, so a reader that trusted `input.disabled` would offer all six."""
    raw = await _read(page, MAIN_FORM)
    axes = {a["name"]: a for a in raw["axes"]}

    assert "Size" in axes, "the form-ASSOCIATED radios were not found"
    values = [o["value"] for o in axes["Size"]["options"]]
    assert values == ["XS", "S", "M", "L", "XL", "XXL"]
    buyable = [o["value"] for o in axes["Size"]["options"] if o["available"]]
    assert buyable == ["L"]
    assert all(o["chosen"] is False for o in axes["Size"]["options"])


async def test_a_single_valued_axis_never_becomes_an_axis(page):
    """Color has one value on every product measured, so it is not a choice and
    must not reach the gate."""
    raw = await _read(page, MAIN_FORM)
    assert "Color" not in {a["name"] for a in raw["axes"]}


async def test_the_review_sort_dropdown_is_not_offered_as_a_variant(page):
    """⚠️ WHY THE GATE IS ANCHORED TO THE FORM. The only visible <select> on a
    real product page is the REVIEW SORT dropdown; it is outside the cart form,
    so a page-anchored gate would have asked the user to choose "Most recent"."""
    raw = await _read(page, MAIN_FORM)
    assert "review_sort" not in {a["name"] for a in raw["axes"]}


async def test_the_card_forms_input_disabled_signal_is_read_too(page):
    """The other half of the measurement: on the related-product cards the INPUT
    carries `disabled` and there is no label at all."""
    raw = await _read(page, CARD_FORM)
    axes = {a["name"]: a for a in raw["axes"]}
    axis = axes["option-15623440335008-1"]
    assert [o["value"] for o in axis["options"] if o["available"]] == ["S", "M"]


async def test_the_contract_still_reports_what_a_submit_would_send(page):
    """`axes` is additive: `fields` is unchanged, and it is what the approval
    fingerprint binds to. The measured EMPTY `id` is exactly the defect — an
    add-to-cart carrying no variant."""
    raw = await _read(page, MAIN_FORM)
    fields = {f["name"]: f["value"] for f in raw["fields"]}
    assert fields["id"] == ""
    assert fields["properties[_Charge Code]"] == "MENS"
    # No radio is checked, so no Size field is sent at all.
    assert "Size" not in fields


async def test_choosing_an_option_sets_it_on_the_real_form(page):
    """choose_form_option clicks the LABEL (these swatches hide the input) and
    the radio ends up checked, so the re-read carries the variant."""
    from app.browser.session import _CHOOSE_FORM_OPTION_JS, _REREAD_COMMIT_FORM_JS

    await page.set_content(MAIN_FORM)
    status = await page.evaluate(_CHOOSE_FORM_OPTION_JS, {"name": "Size", "value": "L"})
    assert status == "ok"

    raw = await page.evaluate(_REREAD_COMMIT_FORM_JS)
    fields = {f["name"]: f["value"] for f in raw["fields"]}
    assert fields.get("Size") == "L"
    chosen = [o["value"] for a in raw["axes"] if a["name"] == "Size"
              for o in a["options"] if o["chosen"]]
    assert chosen == ["L"]


async def test_choosing_a_value_the_form_does_not_offer_is_refused(page):
    """Fail-closed: a value that is not on the page reports no-option rather than
    inventing a control or silently doing nothing."""
    from app.browser.session import _CHOOSE_FORM_OPTION_JS

    await page.set_content(MAIN_FORM)
    assert await page.evaluate(
        _CHOOSE_FORM_OPTION_JS, {"name": "Size", "value": "XXXL"}) == "no-option"
    assert await page.evaluate(
        _CHOOSE_FORM_OPTION_JS, {"name": "Nope", "value": "L"}) == "no-option"
