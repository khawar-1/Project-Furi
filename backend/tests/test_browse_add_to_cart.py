"""
Adding to the cart — the 2026-08-10 incident.

THE INCIDENT. "go to junaidjamshed.com and add black kameez kurta in cart".
Furi searched, PICKED A PRODUCT BY ITSELF out of 1000 results, opened it,
never asked for a size, never added anything, stalled and asked what to do. The
user answered "select size large and add to cart" — and it RESTARTED the
journey, searched again, and opened a DIFFERENT product.

⚠️ THE ROOT CAUSE IS AN OBSERVATION GAP, NOT A DECISION GAP, and every part of
this file exists because the measurement said something a hand-written fixture
would not have (scripts/_measure_product_page.py, _measure_cart_controls.py,
_measure_variant_binding.py, _measure_buybox_rules.py, _measure_decision_budget.py,
all run against the live page on 2026-08-10):

  * the page carries FIFTEEN cart-labelled elements. Thirteen say "Quick add"
    and are not in a form at all; the two that say "Add to bag" belong to
    RELATED PRODUCTS. A leg keyed on the cart LABEL would have added somebody
    else's item to the cart.
  * the page's OWN buy button says "Select Size" (and on another product,
    "Out of stock"), so the label is useless as a finder.
  * that button is `disabled` until a size is chosen, and `observe.eligible()`
    drops disabled elements — so THE BUY BOX AND THE SIZE PICKER WERE NOT IN
    THE ELEMENT LIST AT ALL. The model was blind, not confused.
  * and it is not a token budget: the decision call returns EMPTY at 2048, 4096,
    8192 and 12288 on that page, while a near-identically sized prompt on the
    SEARCH page parses at every one of them.
"""
import pytest

from app.agents import browser_loop
from app.browser import choice
from app.browser import commit_flow as browser_commit
from app.browser import session as browser_session

from tests.test_browser_loop import FakeProvider as LoopProvider, ScriptedPage, _el, _page
from tests.test_browser_target_choice import _CommitSession


# ------------------------------------------------- the real page, verbatim
# What junaidjamshed.com actually serves on the incident's own product page.
# The buy box is NOT among these — that is the point.
PRODUCT_ELEMENTS = [
    _el(21, role="button", name="SIZE GUIDE"),
    _el(22, role="button", name="Add to Wishlist"),
    _el(24, role="button", name="PRODUCT DETAILS"),
    _el(32, role="button", name="Quick add"),
    _el(35, role="button", name="Quick add"),
    _el(62, role="button", name="Add to bag"),   # a RELATED product's button
]
PRODUCT_URL = (
    "https://www.junaidjamshed.com/products/"
    "black-blended-kameez-shalwar-jjksa30729r52ap"
)
PRODUCT_TITLE = "Black Kameez Shalwar – J."
USER_WORDS = "go to junaidjamshed.com and add black kameez kurta in cart"
PLANNER_GOAL = (
    "Go to junaidjamshed.com, search for a black kameez kurta, open it, and add "
    "it to the cart."
)

# The buy box exactly as `session.find_buy_box` reads it off that page: the form
# whose Product ID is the page's own SKU, its six-value Size axis with XS/XL/XXL
# out of stock, and a submit that is disabled until one is chosen.
def _buy_box(*, axes=True, disabled=True, label="Select Size"):
    contract = {
        "found": True,
        "action": "https://www.junaidjamshed.com/cart/add",
        "method": "POST",
        "fields": [
            {"name": "id", "value": ""},
            {"name": "properties[Product ID]", "value": "JJKSA30729R52AP"},
            {"name": "quantity", "value": "1"},
        ],
        "has_password": False,
        "axes": [],
        "submit_label": label,
        "submit_disabled": disabled,
        "submit_present": True,
    }
    if axes:
        contract["axes"] = [{
            "name": "Size",
            "options": [
                {"value": "XS", "label": "XS", "chosen": False, "available": False},
                {"value": "S", "label": "S", "chosen": False, "available": True},
                {"value": "M", "label": "M", "chosen": False, "available": True},
                {"value": "L", "label": "L", "chosen": False, "available": True},
                {"value": "XL", "label": "XL", "chosen": False, "available": False},
                {"value": "XXL", "label": "XXL", "chosen": False, "available": False},
            ],
        }]
    return contract


class _BuySession(_CommitSession):
    """`_CommitSession` plus the one capability this round adds. The buy box is
    returned by the SESSION, not found in the element list, because on the real
    page it is not in the element list — see the module docstring."""

    def __init__(self, page, buy_box=None, allowlist=None):
        super().__init__(page, allowlist=allowlist)
        self._buy_box = buy_box
        self.buy_box_calls = 0
        self.chosen = []

    async def find_buy_box(self):
        self.buy_box_calls += 1
        return self._buy_box

    async def read_commit_target(self, observation, index):
        return None      # nothing in the element list is a form — measured

    async def choose_form_option(self, name, value):
        self.chosen.append((name, value))
        return "ok"

    async def reread_commit_form(self):
        return self._buy_box


def _product_page(url=PRODUCT_URL, title=PRODUCT_TITLE):
    return _page(list(PRODUCT_ELEMENTS), url=url, title=title)


# ============================================ 1. the real session can do this
def test_the_real_session_can_find_a_buy_box():
    """⚠️ THE INVARIANT UNDER THE TOLERANT READ. The loop asks for
    `find_buy_box` with getattr, so every hand-wired fake in the suite keeps its
    old behaviour — and that tolerance would silently switch the whole feature
    off if the real class ever lost the method. This is what stops that becoming
    a no-op that reports success."""
    assert callable(getattr(browser_session.BrowserSession, "find_buy_box", None))


@pytest.mark.asyncio
async def test_the_buy_box_finder_refuses_a_page_it_cannot_read():
    """A JS failure is a normal "no buy box here", never an exception into the
    run — a page torn down mid-read is exactly the case worth surviving."""
    class _Broken:
        async def evaluate(self, *a, **k):
            raise RuntimeError("execution context destroyed")

    session = browser_session.BrowserSession.__new__(browser_session.BrowserSession)
    session.page = _Broken()
    assert await session.find_buy_box() is None


# ================================================== 2. the goal shape it serves
@pytest.mark.parametrize("goal,want", [
    ("go to junaidjamshed.com and add black kameez kurta in cart", True),
    ("add janan sports 100ml to my cart", True),
    ("put the black kurta in the basket", True),
    ("buy the blue shirt and add it to the bag", True),
    # NOT a cart goal — the leg walks a form to an approval card, and it should
    # only do that when the user plainly asked for one.
    ("find me a black kameez kurta on junaidjamshed.com", False),
    ("what does a black kurta cost on junaidjamshed.com", False),
    ("play the latest episode of bleach", False),
    ("apply to the three most recent python jobs", False),
])
def test_wants_cart(goal, want):
    assert browser_loop.wants_cart(goal) is want


# ============================================== 3. the incident, end to end
@pytest.mark.asyncio
async def test_the_incident_the_buy_box_is_reached_and_the_size_is_asked():
    """THE DEFECT, frozen. On the real product page the model produced "more",
    then "scroll up", then nothing — because nothing it could see could add
    anything. Now the form is found in the PAGE and the run stops on the one
    question that was always missing: which size."""
    page = ScriptedPage([_product_page(), _product_page()])
    session = _BuySession(page, buy_box=_buy_box())
    # ⚠️ NO SCRIPTED RESPONSES AT ALL: if this leg ever needs the model, the
    # provider raises and the test fails rather than quietly passing.
    provider = LoopProvider([])

    outcome = await browser_loop.run_browse(
        session, PLANNER_GOAL, provider, commit=True, intent_text=USER_WORDS,
    )

    assert outcome.target_choice_required is True, f"still stuck: {outcome.error!r}"
    assert outcome.choice_kind == "option"
    assert outcome.choice_field == "Size"
    # Only what can actually be bought — XS/XL/XXL are out of stock.
    assert list(outcome.choice_options) == ["S", "M", "L"]
    assert provider.calls == 0, "the buy box costs no LLM call"
    assert page.acted == [], "and nothing was clicked or submitted"


@pytest.mark.asyncio
async def test_the_users_own_size_is_taken_without_asking():
    """"select size large and add to cart" — their answer, applied in code. The
    axis offers L; the token scorer cannot see a one-letter option and
    `_compact` needs an exact string, so before this their answer was ignored
    and they were asked again."""
    page = ScriptedPage([_product_page(), _product_page()])
    session = _BuySession(page, buy_box=_buy_box())
    provider = LoopProvider([])

    outcome = await browser_loop.run_browse(
        session, PLANNER_GOAL, provider, commit=True, intent_text=USER_WORDS,
        stuck_advice="select size large and add to cart",
    )

    assert session.chosen == [("Size", "L")], "the size was set in code"
    assert outcome.commit_required is True, f"got {outcome.error!r}"
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_a_page_with_no_axis_goes_straight_to_the_approval_card():
    """A single-variant product: nothing to choose, so the contract is handed
    to the user immediately."""
    page = ScriptedPage([_product_page(), _product_page()])
    session = _BuySession(page, buy_box=_buy_box(axes=False, disabled=False,
                                                 label="Add to bag"))
    outcome = await browser_loop.run_browse(
        session, PLANNER_GOAL, LoopProvider([]), commit=True,
        intent_text=USER_WORDS,
    )
    assert outcome.commit_required is True
    assert outcome.commit_state["url"].endswith("/cart/add")


@pytest.mark.asyncio
async def test_an_out_of_stock_product_is_reported_not_submitted():
    """MEASURED on a real product: the buy button reads "Out of stock" and is
    disabled, with no axis to change that. Submitting a form the site has
    switched off would be a claim we cannot support; its own words are the
    honest answer."""
    page = ScriptedPage([_product_page(), _product_page()])
    session = _BuySession(page, buy_box=_buy_box(axes=False, disabled=True,
                                                 label="Out of stock"))

    outcome = await browser_loop.run_browse(
        session, PLANNER_GOAL, LoopProvider([]), commit=True, intent_text=USER_WORDS,
    )

    assert outcome.success is False
    assert outcome.commit_required is False
    assert "will not let this be added" in (outcome.error or "")
    assert "Out of stock" in (outcome.error or "")
    assert page.acted == []


# ================================================ 4. where it must NOT fire
@pytest.mark.asyncio
async def test_a_read_only_browse_never_looks_for_a_buy_box():
    """`browse` is READ. The leg is commit-only, so a read run cannot walk a
    form towards an approval card."""
    page = ScriptedPage([_product_page(), _product_page()])
    session = _BuySession(page, buy_box=_buy_box())
    await browser_loop.run_browse(
        session, PLANNER_GOAL, LoopProvider(['{"action":"done","reason":"read"}']),
        commit=False, intent_text=USER_WORDS,
    )
    assert session.buy_box_calls == 0


@pytest.mark.asyncio
async def test_a_goal_that_never_mentioned_a_cart_never_looks():
    page = ScriptedPage([_product_page(), _product_page()])
    session = _BuySession(page, buy_box=_buy_box())
    await browser_loop.run_browse(
        session, PLANNER_GOAL, LoopProvider(['{"action":"done","reason":"looked"}']),
        commit=True, intent_text="what does the black kameez kurta cost",
    )
    assert session.buy_box_calls == 0


@pytest.mark.asyncio
async def test_a_page_with_no_buy_box_leaves_the_run_exactly_as_it_was():
    """A listing page: MEASURED, `find_buy_box` refuses it (21 candidates and no
    way to tell them apart). The model decides, as it always did."""
    page = ScriptedPage([_product_page(), _product_page()])
    session = _BuySession(
        page, buy_box={"found": False, "reason": "several buy forms", "candidates": 21}
    )
    outcome = await browser_loop.run_browse(
        session, PLANNER_GOAL, LoopProvider(['{"action":"done","reason":"ok"}']),
        commit=True, intent_text=USER_WORDS,
    )
    assert session.buy_box_calls == 1
    assert outcome.success is True, "the model still decided"


@pytest.mark.asyncio
async def test_a_landing_page_that_shares_none_of_the_users_words_is_left_alone():
    """The leg only acts on a page whose own subject carries at least one of the
    things the user said, so a stray landing page is never added to the cart."""
    page = ScriptedPage([
        _page(list(PRODUCT_ELEMENTS), url="https://www.junaidjamshed.com/products/gift-voucher",
              title="Gift Voucher – J."),
        _page(list(PRODUCT_ELEMENTS), url="https://www.junaidjamshed.com/products/gift-voucher",
              title="Gift Voucher – J."),
    ])
    session = _BuySession(page, buy_box=_buy_box())
    await browser_loop.run_browse(
        session, PLANNER_GOAL, LoopProvider(['{"action":"done","reason":"ok"}']),
        commit=True, intent_text=USER_WORDS,
    )
    assert session.buy_box_calls == 0


# ============================== 5. a steered resume does not restart the journey
@pytest.mark.asyncio
async def test_a_steered_resume_does_not_search_away_from_the_page_it_asked_about():
    """⚠️ THE SECOND HALF OF THE INCIDENT. The run stopped on a product page and
    asked; the user answered; the resume began at step 0 on THAT page, fired the
    deterministic search leg, clicked the search toggle and opened a DIFFERENT
    product. `page_covers_target` cannot help — the page legitimately does not
    carry every word of the request, which is WHY they were asked."""
    searchable = _page(
        list(PRODUCT_ELEMENTS) + [_el(9, role="link", name="drawer-search")],
        url=PRODUCT_URL, title=PRODUCT_TITLE,
    )
    page = ScriptedPage([searchable, searchable])
    session = _BuySession(page, buy_box=_buy_box())

    await browser_loop.run_browse(
        session, PLANNER_GOAL, LoopProvider([]), commit=True, intent_text=USER_WORDS,
        stuck_advice="select size large and add to cart",
    )

    assert page.acted == [], "the search toggle was not clicked"
    assert session.chosen == [("Size", "L")], "it acted on the page it was asked about"


@pytest.mark.asyncio
async def test_a_fresh_run_still_searches_from_a_page_that_is_not_the_item():
    """The REGRESSION half: without an answer in hand, step 0 still searches —
    that leg is what put search back on the storefront (2026-08-09)."""
    home = _page(
        [_el(9, role="link", name="drawer-search")],
        url="https://www.junaidjamshed.com/", title="J. Junaid Jamshed",
    )
    page = ScriptedPage([home, home])
    session = _BuySession(page, buy_box=None)

    await browser_loop.run_browse(
        session, PLANNER_GOAL, LoopProvider([]), commit=True, intent_text=USER_WORDS,
    )

    assert page.acted, "a fresh run still opens the search UI in code"


# ==================================================== 6. did the cart change?
def test_cart_verification_reports_the_sites_own_numbers():
    from app.agents import rendering

    text = rendering._one_commit_block({
        "url": "https://www.junaidjamshed.com/cart/add",
        "title": "Black Kameez Shalwar",
        "page_changed": False,
        "cart_verified": True, "cart_before": 1, "cart_after": 2,
    })
    assert "went from 1 to 2 item(s)" in text


def test_cart_verification_says_so_when_nothing_changed():
    """⚠️ THE FAILURE THAT IS INVISIBLE TODAY. "Submitted" is a fact about the
    REQUEST — the interceptor watched it leave — and a storefront can accept an
    add and drop it. On an AJAX add the page never moves, so the response diff
    cannot tell either."""
    from app.agents import rendering

    text = rendering._one_commit_block({
        "url": "https://www.junaidjamshed.com/cart/add",
        "title": "Black Kameez Shalwar",
        "page_changed": False,
        "cart_verified": False, "cart_before": 1, "cart_after": 1,
    })
    assert "unchanged" in text and "does not look like it was added" in text


def test_no_cart_evidence_claims_nothing_either_way():
    from app.agents import rendering

    text = rendering._one_commit_block({
        "url": "https://x.test/cart/add", "title": "Thing", "page_changed": True,
        "cart_verified": None,
    })
    # The url itself says "cart", so the claim is about the VERDICT, not the word.
    assert "The cart went" not in text
    assert "unchanged" not in text
    assert "does not look like it was added" not in text


@pytest.mark.asyncio
async def test_the_cart_page_is_read_only_when_the_count_cannot_answer():
    """The cheap comparator first: a site that shows a count pays nothing for
    the second tier. Only when there is no number to compare does the page's own
    cart LINK get followed — and we come back to where we were."""
    calls = []

    class _S:
        class page:
            @staticmethod
            async def evaluate(js):
                return {"count": None, "href": "https://x.test/cart", "label": "Bag"}

        async def goto(self, url):
            calls.append(url)

        async def settle(self):
            pass

    facts = await browser_commit._verify_cart(_S(), {"count": None}, "https://x.test/p/1")
    assert facts["cart_verified"] is None
    assert calls[0] == "https://x.test/cart", "followed the page's OWN cart link"
    assert calls[-1] == "https://x.test/p/1", "and put the window back"


@pytest.mark.asyncio
async def test_a_rising_count_needs_no_navigation_at_all():
    calls = []

    class _S:
        class page:
            @staticmethod
            async def evaluate(js):
                return {"count": 2, "href": "https://x.test/cart", "label": "Bag 2"}

        async def goto(self, url):
            calls.append(url)

        async def settle(self):
            pass

    facts = await browser_commit._verify_cart(_S(), {"count": 1}, "https://x.test/p/1")
    assert facts["cart_verified"] is True
    assert facts["cart_before"] == 1 and facts["cart_after"] == 2
    assert calls == [], "confirmed by the site's own number — nothing else read"


@pytest.mark.asyncio
async def test_verification_never_turns_a_real_submit_into_a_failure():
    """Wholly best-effort: a submission that fired is still a submission that
    fired."""
    class _S:
        class page:
            @staticmethod
            async def evaluate(js):
                raise RuntimeError("gone")

    facts = await browser_commit._verify_cart(_S(), {"count": 1}, "")
    assert facts["cart_verified"] is None


# ================== 7. the variant has to actually REACH the form
#
# ⚠️ THE DEFECT ONLY A LIVE RUN FINDS, and the two wrong answers before it.
# MEASURED on the incident's own product page: clicking the size swatch updates
# `Size` and the barcode SYNCHRONOUSLY, while the theme resolves the hidden
# variant `id` about a second later.
#
#     +0.0s   Size=L, barcode changed, id=''
#     +1.0s   id='58054819250336'
#
# So a `settle()` (a DOM-quiet detector, ~250ms on a painted page) returns too
# early, "wait for the fields to CHANGE" returns in 20ms, and "wait for them to
# be STABLE" returns in 200ms — all three with the id still empty. The approval
# card would then have named a size in words over a contract carrying NO variant,
# and the submit would have added an unspecified one.

class _FormClock:
    """A form whose blank field fills in only after N reads — the real page's
    asynchronous variant resolution, as a fake that can express it."""

    def __init__(self, fills_after=3):
        self.reads = 0
        self.fills_after = fills_after

    async def reread_commit_form(self):
        self.reads += 1
        # The synchronous half lands at once; the id lags.
        filled = self.reads >= self.fills_after
        return {
            "action": "https://x.test/cart/add",
            "method": "POST",
            "fields": [
                {"name": "Size", "value": "L"},
                {"name": "barcode", "value": "1002"},
                {"name": "id", "value": "58054819250336" if filled else ""},
            ],
        }


@pytest.mark.asyncio
async def test_the_wait_holds_out_for_the_blank_field_to_fill():
    """The whole point: it does not return while the form still cannot say what
    it would send."""
    clock = _FormClock(fills_after=4)
    before = {"fields": [
        {"name": "Size", "value": ""},
        {"name": "barcode", "value": "1001"},
        {"name": "id", "value": ""},
    ]}

    got = await browser_session.BrowserSession.await_form_change(
        clock, before, timeout=5.0
    )

    ids = {f["name"]: f["value"] for f in got["fields"]}
    assert ids["id"] == "58054819250336"
    assert clock.reads >= 4, "it kept reading until the variant resolved"


@pytest.mark.asyncio
async def test_a_form_that_never_fills_returns_at_the_deadline_not_never():
    """Best-effort: a page that never resolves gives back what it has, which is
    exactly the behaviour that predates this."""
    clock = _FormClock(fills_after=10_000)
    before = {"fields": [{"name": "id", "value": ""}]}

    got = await browser_session.BrowserSession.await_form_change(
        clock, before, timeout=0.5
    )

    assert isinstance(got, dict), "it returns the last read rather than hanging"


@pytest.mark.asyncio
async def test_a_form_with_nothing_blank_settles_on_stability():
    """Nothing was blank, so "it stopped moving" is all the evidence there is —
    and it must not wait out the whole budget for a blank that never existed."""
    class _Stable:
        def __init__(self):
            self.reads = 0

        async def reread_commit_form(self):
            self.reads += 1
            return {"fields": [{"name": "Size", "value": "L"}]}

    stable = _Stable()
    got = await browser_session.BrowserSession.await_form_change(
        stable, {"fields": [{"name": "Size", "value": "M"}]}, timeout=5.0
    )
    assert got["fields"][0]["value"] == "L"
    assert stable.reads <= 3, "it did not sit out the deadline"
