"""
_EXTRACT_JS against a REAL browser. Opt-in; never runs in the normal suite.

    JARVIS_REAL_BROWSER=1 venv/Scripts/python -m pytest tests/test_browser_extract_js.py -v

WHY THIS FILE EXISTS
--------------------
_EXTRACT_JS is a ~250-line program that runs inside hostile documents, and until
2026-07-26 it had ZERO execution coverage. It could not have any: the hermetic
suite's ScriptedPage.evaluate returns a canned dict and never runs a line of the
JS, and tests/conftest.py's autouse _hermetic_browser_session refuses real
launches suite-wide — deliberately, because the loop can splice navigations on
its own and a test that reaches the network is not a test.

The existing coverage in test_dom_observe.py is string-matching over the JS
SOURCE. That is worth having (it pins the challenge-zone gate structurally) but
it cannot tell you whether the program works. So this file exists, opt-in, and
the four fixtures below are the four things the 2026-07-26 rewrite claimed:

  1. an ordinary rich page produces the SAME output as before the rewrite —
     the wide tier must not engage where the strict selector already works;
  2. a <div>-card results grid with no semantic controls is now addressable
     (the daraz.pk case: a fully-rendered page observed as ZERO elements);
  3. open shadow roots are traversed (querySelectorAll never pierced them, so a
     web-component page reported nothing however well it had rendered);
  4. an element under a cookie banner is NOT listed (it was, and the click
     landed on the banner).

The suite is skipped, not deleted, when Playwright or its Chromium is missing.
"""
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("JARVIS_REAL_BROWSER"),
    reason="real-browser test: set JARVIS_REAL_BROWSER=1 to run",
)


@pytest.fixture
async def page():
    """A real headless Chromium page. Deliberately NOT a BrowserSession — this
    tests the observation program, not the safety interceptor, and it must not
    touch the shared ~/.jarvis/browser profile."""
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"playwright not installed: {exc}")

    pw = await async_playwright().start()
    # Channel order mirrors browser_session._CHANNELS: real Chrome first, because
    # that is what production actually drives, then the bundled Chromium (often
    # not downloaded on a machine that only ever uses Chrome), then Edge.
    browser = None
    errors = []
    for channel in ("chrome", None, "msedge"):
        try:
            browser = await pw.chromium.launch(
                headless=True, **({"channel": channel} if channel else {})
            )
            break
        except Exception as exc:  # pragma: no cover
            errors.append(f"{channel or 'bundled'}: {str(exc)[:80]}")
    if browser is None:  # pragma: no cover
        await pw.stop()
        pytest.skip(f"no browser available — {'; '.join(errors)}")
    ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
    p = await ctx.new_page()
    try:
        yield p
    finally:
        await ctx.close()
        await browser.close()
        await pw.stop()


async def _observe(page, html: str):
    from app.browser import observe as dom_observe

    await page.set_content(html, wait_until="load")
    return await dom_observe.observe(page)


# --------------------------------------------------- 1. no regression at all
_RICH = """
<html><body>
  <nav><a href="/home">Home</a><a href="/about">About</a></nav>
  <form role="search"><input type="search" name="q" placeholder="Search"><button type="submit">Go</button></form>
  <main>
    <a href="/p/1">Product One</a><a href="/p/2">Product Two</a>
    <a href="/p/3">Product Three</a><a href="/p/4">Product Four</a>
    <a href="/p/5">Product Five</a><a href="/p/6">Product Six</a>
    <button id="more">Load more</button>
    <p>Some real prose so the page has text content worth reading.</p>
  </main>
</body></html>
"""


async def test_a_rich_page_is_unchanged_by_the_wide_tier(page):
    """THE REGRESSION INVARIANT. With 11 strict hits (>= WIDE_THRESHOLD) the wide
    tier must not engage at all, so every listed element is a real control and
    nothing reads as 'item'."""
    obs = await _observe(page, _RICH)

    assert obs.element_total >= 10
    roles = {e.role for e in obs.elements}
    assert "item" not in roles, f"the wide tier engaged on a rich page: {roles}"
    names = {e.name for e in obs.elements}
    assert "Product One" in names
    assert "Load more" in names


# ------------------------------------- 2. the daraz.pk case: div-card results
_DIV_CARDS = """
<html><body>
  <div class="results">
    <div class="product-card" style="width:300px;height:120px">Yonex Astrox 88D — Rs 8,499</div>
    <div class="product-card" style="width:300px;height:120px">Yonex Nanoflare 001 — Rs 3,200</div>
    <div class="product-card" style="width:300px;height:120px">Yonex Astrox Lite — Rs 1,950</div>
  </div>
</body></html>
"""


async def test_a_div_card_grid_is_addressable(page):
    """THE daraz.pk STEP. A results grid of <div> cards wired to a JS router:
    zero semantic controls, so the strict selector saw NOTHING and the model was
    asked what to click on an empty list."""
    obs = await _observe(page, _DIV_CARDS)

    assert obs.element_total >= 3, "the cards are still invisible"
    names = " ".join(e.name for e in obs.elements)
    assert "Astrox 88D" in names
    assert "Nanoflare" in names
    # And they read as cards, not as raw tag names.
    assert any(e.role == "item" for e in obs.elements)


async def test_an_opaque_spa_grid_is_found_via_the_pointer_cursor(page):
    """The general case, and the one that matters most: obfuscated class names,
    no href, click handled by a JS router. The only signal a HUMAN has is the
    pointer cursor, so it is the only signal available to us either."""
    obs = await _observe(page, """
      <html><head><style>
        .xY7 { cursor: pointer; width: 280px; height: 110px; }
      </style></head><body><div class="aB2">
        <div class="xY7"><div>Yonex Astrox 88D</div><div>Rs 8,499</div></div>
        <div class="xY7"><div>Yonex Nanoflare 001</div><div>Rs 3,200</div></div>
      </div></body></html>
    """)

    assert obs.element_total == 2, (
        "cursor:pointer INHERITS, so each card's inner divs carry the same signal "
        f"— nesting dedup must collapse them. Got {obs.element_total}: "
        f"{[e.name for e in obs.elements]}"
    )
    # The card's name carries the whole card, which is what makes it comparable.
    assert "Astrox 88D" in obs.elements[0].name
    assert "8,499" in obs.elements[0].name


async def test_a_card_wrapping_a_link_yields_the_link(page):
    """The no-containers rule: prefer the inner <a>, which carries an href and
    therefore routes to the loop's GET fast path instead of a synthetic click."""
    obs = await _observe(page, """
      <html><body><div class="results">
        <div class="product-card" style="width:300px;height:120px">
          <a href="/p/astrox">Yonex Astrox 88D</a>
        </div>
      </div></body></html>
    """)

    hrefs = [e.href for e in obs.elements if e.href]
    assert any("/p/astrox" in h for h in hrefs), "the inner link was shadowed by its card"


# ------------------------------------------------------ 3. open shadow roots
_SHADOW = """
<html><body>
  <div id="host"></div>
  <script>
    const root = document.getElementById('host').attachShadow({mode: 'open'});
    root.innerHTML = `
      <a href="/shadow-a">Inside Shadow A</a>
      <button>Shadow Button</button>
      <input type="search" placeholder="Shadow search">
    `;
  </script>
</body></html>
"""


async def test_open_shadow_roots_are_traversed(page):
    """querySelectorAll does not pierce shadow roots, so a page built from web
    components reported ZERO elements no matter how well it rendered."""
    obs = await _observe(page, _SHADOW)

    names = " ".join(e.name for e in obs.elements)
    assert "Inside Shadow A" in names, "shadow DOM is still invisible"
    assert "Shadow Button" in names


async def test_a_closed_shadow_root_is_honestly_unreachable(page):
    """The documented limit, pinned so it is never mistaken for a bug: a closed
    root returns null to every script, ours and Playwright's alike."""
    obs = await _observe(page, """
      <html><body>
        <div id="host"></div>
        <a href="/visible">Visible Link</a>
        <script>
          document.getElementById('host')
            .attachShadow({mode: 'closed'})
            .innerHTML = '<a href="/hidden">Hidden Link</a>';
        </script>
      </body></html>
    """)

    names = " ".join(e.name for e in obs.elements)
    assert "Visible Link" in names
    assert "Hidden Link" not in names


# ---------------------------------------------------------- 4. occlusion
_BANNER = """
<html><body style="margin:0">
  <a href="/under" style="position:absolute;top:40px;left:40px;width:200px;height:40px">Buried Link</a>
  <a href="/clear" style="position:absolute;top:400px;left:40px;width:200px;height:40px">Clear Link</a>
  <div id="banner" style="position:fixed;top:0;left:0;width:100%;height:200px;
       background:#fff;z-index:9999">Cookie banner</div>
</body></html>
"""


async def test_an_element_under_a_cookie_banner_is_not_listed(page):
    """It used to be listed, the click landed on the banner, and the step was
    spent for nothing. The wide tier would have multiplied these."""
    obs = await _observe(page, _BANNER)

    names = " ".join(e.name for e in obs.elements)
    assert "Clear Link" in names, "an unobstructed element must survive"
    assert "Buried Link" not in names, "an occluded element is not actionable"


# ------------------------------------------------------- cross-frame (Phase 6)
async def test_a_content_frame_is_observed_and_its_rects_translated(page):
    """Frames are where checkout forms, booking widgets and player controls live.
    Before this they were invisible, so a page whose whole purpose sat inside one
    read as empty.

    The rect translation is the part that fails SILENTLY if missed: a frame
    element's box is relative to its own viewport, while overlay_marks draws
    badges in top-page coordinates."""
    from app.browser import observe as dom_observe

    inner = (
        "data:text/html,"
        "<body style='margin:0'>"
        "<button style='width:200px;height:40px'>Pay now</button>"
        "<input name='card' placeholder='Card number' style='width:200px;height:30px'>"
        "</body>"
    )
    # A same-origin http frame — the filter requires http(s), not data:, so serve
    # the inner document through a route.
    await page.route("**/checkout-frame", lambda r: r.fulfill(
        status=200, content_type="text/html",
        body="<body style='margin:0'><button style='width:200px;height:40px'>Pay now</button>"
             "<input name='card' placeholder='Card number' style='width:200px;height:30px'></body>",
    ))
    await page.route("**/host", lambda r: r.fulfill(
        status=200, content_type="text/html",
        body="<body style='margin:0'><h1>Checkout</h1>"
             "<div style='height:150px'>spacer</div>"
             "<iframe src='/checkout-frame' style='width:600px;height:400px;border:0'></iframe>"
             "</body>",
    ))
    await page.goto("http://localhost/host", wait_until="load")
    obs = await dom_observe.observe(page)

    names = " ".join(e.name for e in obs.elements)
    assert "Pay now" in names, "the frame's contents are still invisible"

    frame_els = [e for e in obs.elements if e.frame_id]
    assert frame_els, "frame elements must be tagged with their document"
    # The iframe sits ~150px down the host page, so a translated rect must be
    # BELOW that — an untranslated one would report y near 0.
    pay = next(e for e in frame_els if "Pay now" in e.name)
    assert pay.rect[1] > 100, f"rect not translated into top-page space: {pay.rect}"
    assert pay.frame_url.endswith("/checkout-frame")

    # Indices are globally unique and continue the top document's numbering.
    indices = [e.index for e in obs.elements]
    assert len(indices) == len(set(indices)), f"duplicate indices: {indices}"


async def test_a_frame_element_resolves_through_the_index_contract(page):
    """resolve() tries the top document first, then the stamped frame — and a
    fresh observation still invalidates the old one."""
    from app.browser import observe as dom_observe

    await page.route("**/f", lambda r: r.fulfill(
        status=200, content_type="text/html",
        body="<body><button style='width:200px;height:40px'>Inside Frame</button></body>",
    ))
    await page.route("**/h", lambda r: r.fulfill(
        status=200, content_type="text/html",
        body="<body><iframe src='/f' style='width:600px;height:400px'></iframe></body>",
    ))
    await page.goto("http://localhost/h", wait_until="load")
    obs = await dom_observe.observe(page)

    target = next(e for e in obs.elements if "Inside Frame" in e.name)
    assert target.frame_id, "the element must know which document it came from"
    handle = await dom_observe.resolve(page, obs, target.index)
    assert handle is not None

    await dom_observe.observe(page)
    with pytest.raises(dom_observe.StaleObservation):
        await dom_observe.resolve(page, obs, target.index)


async def test_a_tiny_frame_is_not_observed(page):
    """Tracking beacons and 1x1 ad slots must not cost a CDP round-trip each."""
    from app.browser import observe as dom_observe

    await page.route("**/beacon", lambda r: r.fulfill(
        status=200, content_type="text/html", body="<body><a href='/x'>Tracker</a></body>",
    ))
    await page.route("**/page", lambda r: r.fulfill(
        status=200, content_type="text/html",
        body="<body><a href='/real'>Real Link</a>"
             "<iframe src='/beacon' style='width:1px;height:1px'></iframe></body>",
    ))
    await page.goto("http://localhost/page", wait_until="load")
    obs = await dom_observe.observe(page)

    names = " ".join(e.name for e in obs.elements)
    assert "Real Link" in names
    assert "Tracker" not in names, "a 1x1 frame was walked"


# ------------------------------------------------------------- the contract
async def test_the_index_contract_holds_across_the_new_walk(page):
    """Stamping + resolution still work end to end, including for a wide-tier
    hit and a shadow-DOM hit — resolve() is unchanged because Playwright's CSS
    engine pierces open shadow roots for free."""
    from app.browser import observe as dom_observe

    await page.set_content(_SHADOW, wait_until="load")
    obs = await dom_observe.observe(page)
    target = next(e for e in obs.elements if "Shadow" in e.name)

    handle = await dom_observe.resolve(page, obs, target.index)
    assert handle is not None

    # A fresh observation invalidates the old one — the staleness half.
    await dom_observe.observe(page)
    with pytest.raises(dom_observe.StaleObservation):
        await dom_observe.resolve(page, obs, target.index)


# ------------------------------------------- the structural reader, on real DOM
# A hand-written element list proves the PARSER. It cannot prove that a real
# rendered grid produces element names the parser can read — and that gap is
# exactly where the 2026-07-26 defect lived: the fixtures were fine and the live
# page returned zero records. This is the only place that link can be tested.
_GRID = """
<html><body>
  <header><nav><a href="/">Home</a></nav>
    <p>Free delivery on orders over Rs. 2,000. Sale ends in 24 hours 3 days.</p>
  </header>
  <div class="results">
    <div class="product-card" data-sku="1" onclick="go(1)">
      <img src="/i/1.jpg" alt="">
      <div class="title">Yonex Astrox 99 Pro Badminton Racket</div>
      <div class="price">Rs. 24,999</div><div class="was">Rs. 29,499</div>
      <div class="rate">4.7 (128)</div>
    </div>
    <div class="product-card" data-sku="2" onclick="go(2)">
      <div class="title">Yonex Nanoflare 001 Feel Racket</div>
      <div class="price">Rs. 8,499</div><div class="rate">4.2 (31)</div>
    </div>
    <div class="product-card" data-sku="3" onclick="go(3)">
      <div class="title">Yonex Arcsaber 11 Pro Racket</div>
      <div class="price">Rs. 41,500</div><div class="rate">4.9 (12)</div>
    </div>
  </div>
</body></html>
"""


async def test_a_real_rendered_grid_is_read_structurally(page):
    """The end-to-end perception claim, on a real browser: <div> cards wired to a
    JS router (no <a>, no semantic control) become elements whose names carry the
    card's own text, and the structural reader turns them into rows with the real
    prices — no LLM anywhere in this path."""
    from app.browser import extract as browser_extract

    obs = await _observe(page, _GRID)
    result = browser_extract.structured_records(obs, ["name", "price"])

    assert result.covers_requested is True
    prices = [r["price"] for r in result.records]
    assert prices == ["Rs. 24,999", "Rs. 8,499", "Rs. 41,500"], (
        f"read {result.records!r} from {[e.name_full for e in obs.elements]!r}"
    )
    assert "Astrox 99 Pro" in result.records[0]["name"]


async def test_the_header_prose_does_not_become_a_product(page):
    """"Free delivery on orders over Rs. 2,000" is a priced line in the header.
    It is not an item, and the reader must not emit it as one."""
    from app.browser import extract as browser_extract

    obs = await _observe(page, _GRID)
    rows = browser_extract.structured_records(obs, []).records
    assert not any("Free delivery" in r.get("title", "") for r in rows)
    assert not any("24 hours" in r.get("price", "") for r in rows)


async def test_the_full_name_is_what_makes_the_grid_readable(page):
    """The card's price lives past _NAME_MAX on a long title. If `name_full` were
    not captured, this page would read as zero records — the live failure."""
    from app.browser import observe as dom_observe

    obs = await _observe(page, _GRID)
    cards = [e for e in obs.elements if "Astrox" in (e.name_full or e.name)]
    assert cards, "the grid produced no card element at all"
    assert all(len(c.name) <= dom_observe._NAME_MAX for c in cards)
    assert any("Rs. 24,999" in c.name_full for c in cards)
