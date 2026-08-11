"""
The 2026-08-09 e-commerce round: stock, the rail re-ask, and asking instead of
dying.

TWO LIVE RUNS ON junaidjamshed.com, four user-visible defects.

  A  "go to junaidjamshed.com and add janan in cart" asked which Janan and
     listed products that were SOLD OUT.
  B  "go to junaidjamshed.com in the men kameez shalwar section add black plain
     sharwar kameez to cart" opened the general Men page, asked which of two
     products, and then — on the product page the user had just chosen — ASKED
     AGAIN, about the "you may also like" rail, instead of asking for a size.
     It had not re-asked on `janan leather` minutes earlier.
  C  underneath both: a page the loop cannot act on kills the run and closes the
     window.

⚠️ EVERY FIXTURE HERE IS MEASURED, NOT IMAGINED
-----------------------------------------------
scripts/_measure_ecommerce_round.py, run against the live site 2026-08-09. That
matters more than usual, because planning this round off invented pages went
wrong TWICE — and both times the invented version said the code was already
correct:

  * scoring an imagined rail against the kameez PDP gave page 9 vs rails 4-7,
    i.e. suppression WOULD have fired. Measured, the rail holds two OTHER
    garments both named `BLACK COTTON CASUAL KAMEEZ SHALWAR`, so the page ties
    with itself at 9-9 and `page_is_the_target`'s strictly-greater test cannot
    fire — and the user was offered two byte-identical buttons.
  * scoring the string "Men Kameez Shalwar" on the men page gave 5, a unique
    leader nothing was clicking. Measured, the real label is `KAMEEZ SHALWAR`
    and it scores 3, LOSING to two products at 4 — because the user named a
    section AND an item and products carry more of those words. There was no
    leader to take.

The measured numbers are pinned below, so a scorer change that would have
re-opened either incident fails here rather than live.
"""
import pytest

from app.agents import browser_loop
from app.browser import choice
from app.browser.state import Handoff, HandoffPayload, handoff_from_outcome

from tests.test_browser_loop import FakeProvider as LoopProvider, ScriptedPage, _el, _page
from tests.test_browser_target_choice import _CommitSession


# ============================================================ measured fixtures
# /search?q=janan — the twenty tied products in the site's own DOM order, with
# the SIX the store lists as sold out marked. Measured: the signal is the card
# ancestor's class (`hdt-pr-sold_out`), never the title link the user is
# offered, and the card text reads "SOLD OUT … View product" where a buyable
# card reads "… Add to bag".
JANAN_SOLD_OUT = (
    "JANAN GOLD - 100ML",
    "JANAN SPORT - 100ML",
    "JANAN SPORT - 30ML",
    "JANAN PLATINUM - 100ML",
    "JANAN GOLD POUR HOMME PERFUME BODY SPRAY",
    "JANAN GOLD - 30ML",
)
JANAN_BUYABLE = (
    "JANAN GIFT SET (30ML)",
    "JANAN INTENSE",
    "JANAN OUD",
    "JANAN GOLD - GIFT SET",
    "JANAN POUR FEMME - 30ML",
    "JANAN PLATINIUM SHOWER GEL",
    "JANAN POUR FEMME",
    "JANAN MUSK - 30ML",
    "JANAN PLATINUM - 200ML",
    "JANAN LEATHER",
    "JANAN SPORT - GIFT SET",
    "JANAN VANILLA",
    "JANAN VANILLA - 30ML",
    "JANAN OUD - 30ML",
)
SEARCH_URL = "https://www.junaidjamshed.com/search?q=janan"
SEARCH_TITLE = 'Search: 1000 results found for "janan" – J.'

# The kameez product page, measured. `page_subject` scores 9; BOTH tied rail
# items carry the page's own product name and also score 9.
PDP_URL = (
    "https://www.junaidjamshed.com/collections/mens-stitched/products/"
    "black-cotton-casual-kameez-shalwar-jjkss60094"
)
PDP_TITLE = "Black Cotton Casual Kameez Shalwar – J."
PDP_CHOSEN = "BLACK COTTON CASUAL KAMEEZ SHALWAR"
# The rail, verbatim — note the first two are DIFFERENT products (different
# hrefs) carrying the SAME name.
PDP_RAIL = (
    ("BLACK COTTON CASUAL KAMEEZ SHALWAR", "/products/black-cotton-casual-kameez-shalwar-jjksa60184"),
    ("BLACK COTTON CASUAL KAMEEZ SHALWAR", "/products/black-cotton-casual-kurta-trousers-jjksa47622r1"),
    ("BLACK COTTON PLAIN KAMEEZ SHALWAR", "/products/black-cotton-plain-kameez-shalwar-jjksa47673"),
    ("GREY COTTON CASUAL KAMEEZ SHALWAR", "/products/grey-cotton-casual-kameez-shalwar-jjksa60132"),
)
# ⚠️ THE SAME RAIL WITH DISTINCT NAMES, and it exists because the first version
# of the end-to-end test below PASSED WITH THE BELT REVERTED. On the measured
# rail the two top items are byte-identical, so the identical-option dedupe
# ALSO suppresses the question — two mechanisms, and the falsification could not
# tell which was load-bearing ("a falsification must remove the GUARANTEE, not
# one of several copies of it"). These are distinct labels that still tie with
# the page at 9, the shape the live listing shows elsewhere ("JANAN GOLD -
# 100ML" beside "JANAN GOLD - GIFT SET"), so the belt is the only thing left.
PDP_RAIL_DISTINCT = (
    ("BLACK COTTON CASUAL KAMEEZ SHALWAR - GIFT SET", "/products/bcc-gift-set"),
    ("BLACK COTTON CASUAL KAMEEZ SHALWAR SLIM FIT", "/products/bcc-slim"),
    ("BLACK COTTON PLAIN KAMEEZ SHALWAR", "/products/black-cotton-plain-kameez-shalwar-jjksa47673"),
)

# The men page, measured — the section link and the two products that beat it.
MEN_URL = "https://www.junaidjamshed.com/pages/men-collections"
MEN_RANKED = (
    ("DARK GREEN PLAIN KAMEEZ SHALWAR", "/collections/mens-stitched/products/dark-green-blended-plain-kameez-shalwar-jjksa47592"),
    ("BLACK COTTON CASUAL KAMEEZ SHALWAR", "/collections/mens-stitched/products/black-cotton-casual-kameez-shalwar-jjkss60094"),
    ("KAMEEZ SHALWAR", "/collections/mens-kameez-shalwar"),
    ("KURTA", "/collections/mens-kurta"),
    ("MEN", "/pages/men-collections"),
)

SECTION_INTENT = (
    "go to junaidjamshed.com in the men kameez shalwar section "
    "add black plain sharwar kameez to cart"
)
JANAN_INTENT = "go to junaidjamshed.com and add janan in cart"
# The planner's paraphrase, which is what `goal` carries.
JANAN_GOAL = "Find the 'janan' product on junaidjamshed.com and add it to the cart."


def _cards(titles_sold, titles_live, url=SEARCH_URL, title=SEARCH_TITLE):
    """A listing whose cards carry the measured stock signal."""
    els, i = [], 0
    for t in titles_sold:
        i += 1
        els.append(_el(i, name=t, href=f"/products/s{i}", sold_out=True))
    for t in titles_live:
        i += 1
        els.append(_el(i, name=t, href=f"/products/b{i}"))
    return _page(els, url=url, title=title)


def _pdp(url=PDP_URL, title=PDP_TITLE, rail=PDP_RAIL):
    return _page(
        [_el(i + 1, name=t, href=h) for i, (t, h) in enumerate(rail)],
        url=url,
        title=title,
    )


def _men_page():
    return _page(
        [_el(i + 1, name=t, href=h) for i, (t, h) in enumerate(MEN_RANKED)],
        url=MEN_URL,
        title="Men's Collection Online in Pakistan - JunaidJamshed – J.",
    )


def _elements(payload):
    """THROUGH the real `_elements_of` — `_el` builds a JS-record dict and
    `choice` reads attributes off an Element, so building Elements by hand would
    skip the very conversion the loop depends on (and the `sold_out` key with
    it)."""
    return browser_loop.dom_observe._elements_of(payload)


# ================================================== 1. sold out is not an option
def test_the_incident_sold_out_items_are_not_offered():
    """DEFECT A, frozen. Twenty products carry "janan"; six cannot be bought.
    Before this every one of the six could be offered, and picking one sent the
    user to a page that cannot add it."""
    target = choice.target_tokens(JANAN_INTENT, SEARCH_URL)
    els = _elements(_cards(JANAN_SOLD_OUT, JANAN_BUYABLE))

    raw = choice.tied_matches(target, els)
    assert len(raw) == 20, "the fixture is the measured twenty-way tie"

    decision = choice.item_choice(target, els)
    assert decision is not None
    offered = [c.label for c in decision.tied]
    assert decision.dropped == 6, "six were measured sold out on the live page"
    assert len(offered) == 14, "the fourteen the store can actually sell"
    for gone in JANAN_SOLD_OUT:
        assert gone not in offered, f"{gone} is sold out and must not be offered"
    for kept in JANAN_BUYABLE:
        assert kept in offered


def test_a_sold_out_item_never_reaches_the_question_text():
    """The end of the chain, not just the decision: the strings that become
    clickable options."""
    target = choice.target_tokens(JANAN_INTENT, SEARCH_URL)
    els = _elements(_cards(JANAN_SOLD_OUT, JANAN_BUYABLE))
    decision = choice.item_choice(target, els)
    out = browser_loop._stamp_item_choice(
        browser_loop.BrowseOutcome(success=False, actions_taken=0), decision, target
    )
    shown = " | ".join(out.choice_options)
    assert "JANAN GOLD - 100ML" not in shown
    assert out.choice_unbuyable is False
    assert choice.SOLD_OUT_MARK not in shown, (
        "the mark is only for the case where nothing can be bought"
    )


def test_one_item_left_in_stock_is_taken_not_asked_about():
    """`unresolved_axis` rule 3, one layer up: the user's words could not
    separate these, and the store did. Asking would offer a list of one."""
    target = choice.target_tokens("add janan sport to cart", SEARCH_URL)
    els = _elements(
        _cards(("JANAN SPORT - 30ML", "JANAN SPORT - 200ML"), ("JANAN SPORT - GIFT SET",))
    )
    decision = choice.item_choice(target, els)
    assert decision is not None
    assert decision.tied == (), "nothing to ask — one survivor"
    assert decision.settled is not None
    assert decision.settled.label == "JANAN SPORT - GIFT SET"
    assert decision.dropped == 2


def test_when_nothing_can_be_bought_it_still_asks_and_says_so():
    """Silently returning "no question" would hand the page to the model with
    every option dead; silently returning the buyable set would be a question
    with no answers."""
    target = choice.target_tokens(JANAN_INTENT, SEARCH_URL)
    els = _elements(_cards(JANAN_SOLD_OUT, ()))
    decision = choice.item_choice(target, els)
    assert decision is not None
    assert decision.none_buyable is True
    assert len(decision.tied) == 6
    assert all(choice.SOLD_OUT_MARK in c.option() for c in decision.tied)


def test_the_all_sold_out_question_says_none_can_be_added():
    from app.agents.planner import _target_choice_question

    payload = HandoffPayload(
        reason=Handoff.TARGET_CHOICE,
        choice_kind="item",
        choice_target="janan",
        choice_total=6,
        choice_unbuyable=True,
    )
    q = _target_choice_question(payload, ["A (sold out)", "B (sold out)"])
    assert "sold out" in q.text.lower()
    assert "can't add" in q.text.lower() or "cannot add" in q.text.lower()


def test_an_element_with_no_stock_signal_is_available():
    """FAIL-OPEN, and the direction is load-bearing: a theme we do not
    understand must keep offering everything, never hide everything."""
    els = _elements(_cards((), JANAN_BUYABLE))
    cands = choice.candidates_of(els)
    assert cands and all(c.available for c in cands)


def test_stock_never_filters_the_answered_pick():
    """`locate` is the ENFORCEMENT of what the user said. Someone who
    deliberately picks a sold-out product (to see it, to ask for a restock) must
    still have their answer honoured — dropping it in `candidates_of` would
    silently ignore them."""
    els = _elements(_cards(JANAN_SOLD_OUT, JANAN_BUYABLE))
    picked = choice.locate("JANAN GOLD - 100ML", els)
    assert picked is not None and picked.label == "JANAN GOLD - 100ML"


def test_the_sold_out_mark_round_trips_through_an_answer():
    """The option we offer is the string that comes back. It must still locate."""
    els = _elements(_cards(JANAN_SOLD_OUT, ()))
    decision = choice.item_choice(choice.target_tokens(JANAN_INTENT, SEARCH_URL), els)
    offered = decision.tied[0].option()
    assert choice.SOLD_OUT_MARK in offered
    assert choice.locate(offered, els) is not None


# ================================ 2. an answered choice is not asked again
def test_the_incident_a_product_page_does_not_re_ask_about_its_rail():
    """DEFECT B, frozen with the measured rail. `page_is_the_target` cannot
    suppress here — MEASURED 9 vs 9 — because two rail items carry the page's
    own product name."""
    target = choice.target_tokens(SECTION_INTENT, PDP_URL, extra=[PDP_CHOSEN])
    subject = choice.page_subject(PDP_TITLE, PDP_URL)
    # 9 and 9 when this was written; 5 and 5 since the 2026-08-10 coverage
    # scoring. What matters is that they are EQUAL — that is why
    # `page_is_the_target` (which needs STRICTLY greater) cannot suppress here
    # and belt 1 has to exist.
    assert choice._score(target, subject) == 5, "the measured page score"
    assert choice._score(target, PDP_RAIL[0][0]) == 5, "the measured rail score"
    assert choice._score(target, subject) == choice._score(target, PDP_RAIL[0][0])

    # The direct test the belt uses instead.
    assert choice.answered_here(PDP_CHOSEN, PDP_TITLE, PDP_URL) is True


def test_the_belt_does_not_fire_on_a_different_product():
    """It must suppress the page they PICKED, not any product page. Measured:
    False when the chosen item is not what this page is."""
    assert (
        choice.answered_here(
            "JANAN SPORT - 30ML",
            "Buy JANAN GOLD Perfume for Men online at J. Junaid Jamshed",
            "https://www.junaidjamshed.com/products/janan-gold-100ml",
        )
        is False
    )


def test_the_belt_is_inert_before_anything_was_chosen():
    """On the search page there is no answer yet, so the question must still be
    asked — the belt cannot silence the ask that matters."""
    assert choice.answered_here("", SEARCH_TITLE, SEARCH_URL) is False


def test_the_distinct_rail_really_does_tie_with_the_page():
    """The fixture below is only meaningful if it reproduces the condition:
    `page_is_the_target` must NOT suppress, or the test proves nothing."""
    target = choice.target_tokens(SECTION_INTENT, PDP_URL, extra=[PDP_CHOSEN])
    els = _elements(_pdp(rail=PDP_RAIL_DISTINCT))
    tied = choice.tied_matches(target, els)
    # Two when written; three since the 2026-08-10 coverage scoring stopped a
    # rail item winning an extra point for spelling two of the words next to
    # each other. The fixture's PURPOSE is unchanged: a real tie that
    # `page_is_the_target` does not suppress.
    assert len(tied) == 3, "the distinct rail items tie at the top"
    assert choice.page_is_the_target(target, PDP_TITLE, PDP_URL, tied) is False, (
        "9 vs 9 — the proxy cannot fire, which is the whole point"
    )


@pytest.mark.asyncio
async def test_the_rail_question_is_gone_end_to_end():
    """Through the real loop, with the user's answer in hand: no second question.

    Uses PDP_RAIL_DISTINCT so the ONLY thing that can suppress is the belt — on
    the measured rail the dedupe would also do it, and a test that two
    mechanisms both cover cannot tell you either one works."""
    payload = _pdp(rail=PDP_RAIL_DISTINCT)
    page = ScriptedPage([payload, payload])
    session = _CommitSession(page)
    provider = LoopProvider(["", ""])

    outcome = await browser_loop.run_browse(
        session,
        "Find the black plain shalwar kameez and add it to the cart.",
        provider,
        commit=True,
        intent_text=SECTION_INTENT,
        chosen_target=PDP_CHOSEN,
    )
    assert outcome.target_choice_required is False, (
        "the user already picked this very product — the rail is not a new question"
    )


@pytest.mark.asyncio
async def test_stock_narrowing_a_tie_does_not_switch_off_the_page_belt():
    """FOUND IN SELF-REVIEW, and it would have been silent.

    `page_is_the_target` needs two candidates to compare against and answers
    False for any shorter list. So when stock narrows a rail tie to ONE
    survivor, a caller passing the post-stock set gets False unconditionally,
    the belt stops suppressing, and the run CLICKS a related product on the very
    page the user asked for.

    MEASURED shape: the page scores 5, the rail 3-3-3 — the full tie suppresses
    correctly — and two of the three are sold out."""
    rail = _page(
        [
            _el(1, name="JANAN SPORT - 200ML", href="/products/a", sold_out=True),
            _el(2, name="JANAN SPORT - GIFT SET", href="/products/b", sold_out=True),
            _el(3, name="JANAN SPORT - 50ML", href="/products/c"),
        ],
        url="https://www.junaidjamshed.com/products/janan-sport-30ml",
        title="JANAN SPORT - 30ml",
    )
    els = _elements(rail)
    target = choice.target_tokens(
        "add janan sport 30ml to cart",
        "https://www.junaidjamshed.com/products/janan-sport-30ml",
    )
    decision = choice.item_choice(target, els)
    assert decision is not None and decision.settled is not None, "stock leaves one"
    assert len(decision.all_tied) == 3, "the tie BEFORE stock is what the belt reads"
    assert choice.page_is_the_target(
        target, "JANAN SPORT - 30ml",
        "https://www.junaidjamshed.com/products/janan-sport-30ml",
        decision.all_tied,
    ) is True

    # END TO END, and it has to reach STEP 1: the settled-click leg is gated
    # `commit and step > 0`, so a run that stops on step 0 never touches it and
    # the test would pass in both worlds (it did, on the first cut — the
    # falsification caught it).
    landing = _page(
        [_el(1, name="Shop the range", href="/collections/all")],
        url="https://www.junaidjamshed.com/",
        title="J.",
    )
    page = ScriptedPage([landing, rail, rail])
    session = _CommitSession(page)
    provider = LoopProvider(['{"action": "click", "index": 1}', "", ""])
    outcome = await browser_loop.run_browse(
        session, "add janan sport 30ml to cart", provider, commit=True
    )
    assert page.acted, "the run must actually reach step 1 or this proves nothing"
    assert len(page.acted) == 1, (
        "only the step-0 click should have happened — this page IS the product "
        f"asked for, so its rail is not something to click: {page.acted}"
    )
    assert outcome.target_choice_required is False


def test_two_identical_options_are_never_both_offered():
    """MEASURED: the rail carries two DIFFERENT products with the SAME name, at
    different hrefs, so the old (label, href) key kept both — a question with two
    byte-identical answers, which `pick_by_answer` then settles by taking the
    first, i.e. code silently picking between real equals."""
    els = _elements(_pdp())
    cands = choice.candidates_of(els)
    labels = [c.label for c in cands]
    assert labels.count(PDP_CHOSEN) == 1, "an option nobody can tell apart is not an option"


def test_two_same_named_items_at_different_prices_stay_distinct():
    """The dedupe keys on what the user SEES, so a real difference survives."""
    a = choice.Candidate(label="BLACK KURTA", index=1, href="/a", price="PKR.8,490")
    b = choice.Candidate(label="BLACK KURTA", index=2, href="/b", price="PKR.6,743")
    assert choice._dedupe_key(a) != choice._dedupe_key(b)


# ============================== 3. the men page: why there was no leader to take
def test_the_section_link_loses_to_products_on_the_real_men_page():
    """WHY PHASE 5 WAS NOT SHIPPED, pinned so the reasoning is checkable.

    The user named a SECTION and an ITEM. Products carry more of those words
    than the section link does, so the "clear leader" a navigation actuator
    would need never existed — and taking the top scorer would have clicked a
    product, not the section the user asked for."""
    target = choice.target_tokens(SECTION_INTENT, MEN_URL)
    els = _elements(_men_page())
    scored = {c.label: choice._score(target, c.label) for c in choice.candidates_of(els)}
    # The magnitudes dropped by one on 2026-08-10, when `_score` stopped counting
    # joined-pair spellings as extra ideas (they used to add an ADJACENCY BONUS).
    # The RANKING — which is what this test is about — is unchanged.
    assert scored["KAMEEZ SHALWAR"] == 2, "the section link, measured"
    assert scored["DARK GREEN PLAIN KAMEEZ SHALWAR"] == 3
    assert scored["BLACK COTTON CASUAL KAMEEZ SHALWAR"] == 3
    assert scored["KURTA"] == 0
    tied = choice.tied_matches(target, els)
    assert len(tied) == 2 and all("KAMEEZ SHALWAR" in c.label for c in tied), (
        "two PRODUCTS tie at the top — there is no unique leader to navigate to"
    )


# ================================= 4. the extraction that puts search back on
@pytest.mark.parametrize(
    "goal,want",
    [
        # THE INCIDENT: None meant both deterministic search legs were
        # unreachable, so a storefront homepage went to the model to guess on.
        (SECTION_INTENT, "black plain sharwar kameez"),
        (
            "add black plain shalwar kameez to cart on junaidjamshed.com",
            "black plain shalwar kameez",
        ),
        (
            "go to junaidjamshed.com in the men section add a black kurta to cart",
            "a black kurta",
        ),
    ],
)
def test_a_section_clause_no_longer_swallows_the_search_term(goal, want):
    assert browser_loop._extract_search_term(goal) == want


@pytest.mark.parametrize(
    "goal,want",
    [
        ("go to junaidjamshed.com and add janan leather to cart", "janan leather"),
        ("go to junaidjamshed.com and add janan in cart", "janan"),
        ("add janan sports 100ml to my cart", "janan sports 100ml"),
        ("go to amazon and add airpods to cart", "airpods"),
        ("play jane by the long faces on youtube", "jane by the long faces"),
        ("play latest episode of latest season of bleach on anikoto", "bleach"),
        ("play the last of us", "the last of us"),
        ("search for the dangers in my heart", "the dangers in my heart"),
        ("find the black kurta", "the black kurta"),
        # A pure navigation goal still yields the destination, which
        # `_names_only_the_destination` then refuses — the 2026-08-01 contract
        # `_destination_reached` depends on.
        ("open youtube", "youtube"),
        ("go to youtube.com", "youtube.com"),
    ],
)
def test_the_extraction_controls_did_not_move(goal, want):
    assert browser_loop._extract_search_term(goal) == want


@pytest.mark.parametrize(
    "goal,url",
    [
        ("open youtube", "https://www.youtube.com/"),
        ("go to youtube.com", "https://www.youtube.com/"),
        ("open junaidjamshed.com", "https://www.junaidjamshed.com/"),
    ],
)
def test_a_navigation_goal_still_refuses_to_search(goal, url):
    """The other half of the contract: the term IS extracted, and the
    destination gate is what stops it being typed into a search box."""
    term = browser_loop._extract_search_term(goal)
    assert browser_loop._names_only_the_destination(term or "", url) is True


def test_a_shopping_goal_is_not_a_destination():
    term = browser_loop._extract_search_term(SECTION_INTENT)
    assert (
        browser_loop._names_only_the_destination(
            term or "", "https://www.junaidjamshed.com/"
        )
        is False
    )


# ================================== 5. a dead end becomes a question
@pytest.mark.asyncio
async def test_the_incident_a_dead_end_asks_instead_of_killing_the_run():
    """DEFECT C, frozen. A page with nothing to act on and no tie to fall back
    on used to end the run with the window closed."""
    blank = _page([_el(1, name="Home", href="/")], url="https://site.test/x", title="X")
    page = ScriptedPage([blank, blank])
    session = _CommitSession(page)
    provider = LoopProvider(["", ""])

    outcome = await browser_loop.run_browse(
        session, "buy a widget", provider, commit=True
    )
    assert outcome.stuck_required is True
    assert outcome.stuck_page == "X"
    assert outcome.error == "couldn't work out a safe next action on this page", (
        "the honest description is UNCHANGED — a spent budget still falls back to it"
    )
    payload = handoff_from_outcome(outcome)
    assert payload is not None and payload.reason is Handoff.STUCK


@pytest.mark.asyncio
async def test_a_second_stall_after_advice_does_not_ask_again():
    """Once the user has steered, another stall means the steer did not unblock
    it — asking again is chaining questions off a question."""
    blank = _page([_el(1, name="Home", href="/")], url="https://site.test/x", title="X")
    page = ScriptedPage([blank, blank])
    session = _CommitSession(page)
    provider = LoopProvider(["", ""])

    outcome = await browser_loop.run_browse(
        session, "buy a widget", provider, commit=True,
        stuck_advice="click the second link",
    )
    assert outcome.stuck_required is False
    assert outcome.error == "couldn't work out a safe next action on this page"


def test_stuck_is_the_last_reason_so_it_shadows_nothing():
    """STUCK means "nothing else fired". Every other flag must still win — most
    of all a discovered contract, which is a run that SUCCEEDED."""
    import types

    for flag, expected in [
        ("commit_required", Handoff.COMMIT),
        ("login_required", Handoff.LOGIN),
        ("challenge_required", Handoff.CHALLENGE),
        ("target_choice_required", Handoff.TARGET_CHOICE),
        ("fill_required", Handoff.FILL_FIELD),
        ("origin_approval_required", Handoff.ORIGIN_APPROVAL),
        ("action_approval_required", Handoff.ACTION_APPROVAL),
    ]:
        outcome = types.SimpleNamespace(
            **{flag: True, "stuck_required": True, "commit_state": {"url": "u"}}
        )
        payload = handoff_from_outcome(outcome)
        assert payload is not None and payload.reason is expected, (
            f"{flag} must beat STUCK"
        )


def test_the_stuck_question_is_free_text_and_names_the_page():
    from app.agents.planner import _stuck_question

    q = _stuck_question("Men's Collection", "couldn't work out a safe next action")
    assert q.options == [], "there is nothing to enumerate — that is the state"
    assert "Men's Collection" in q.text
    assert "still open" in q.text.lower()


def test_the_stuck_question_carries_the_layers_own_reason():
    """"Failure is self-diagnosing" (2026-07-26): a pause that cannot say what
    stopped it sends the reader to the wrong layer."""
    from app.agents.planner import _stuck_question

    q = _stuck_question("A page", "read this page and found no name, price in it")
    assert "found no name" in q.text


def test_stuck_holds_the_window_open():
    """The reported defect was the window closing. A reason absent from
    _DISCOVERY_HOLD_REASONS has its session closed in discover()'s finally."""
    from app.browser.commit_flow import _DISCOVERY_HOLD_REASONS

    assert _DISCOVERY_HOLD_REASONS.get(Handoff.STUCK) == "stuck"


@pytest.mark.asyncio
async def test_the_hold_is_actually_taken_not_just_declared(monkeypatch):
    """The dict above is a DECLARATION; this drives the function that reads it.

    `_hold_for_handoff` returning False is what lets discover()'s `finally`
    close the session — which is precisely the reported defect (backend.log
    14:24:11: stopping → session summary → "last tab closed"). Asserting the
    dict alone would pass even if the lookup were removed."""
    from app.browser import commit_flow

    held: dict = {}

    async def _fake_hold(session, meta=None):
        held["meta"] = dict(meta or {})

    monkeypatch.setattr(
        "app.core.browser_session.hold_discovery", _fake_hold, raising=False
    )
    payload = HandoffPayload(reason=Handoff.STUCK, site="Men's Collection")
    kept = await commit_flow._hold_for_handoff(object(), "some goal", payload)

    assert kept is True, "False here is what closes the window in discover()"
    assert held["meta"]["reason"] == "stuck"
    assert held["meta"]["goal"] == "some goal", (
        "the re-attach is keyed on the goal — a wrong key silently starts fresh"
    )


def test_stuck_advice_is_never_stamped_on_an_approved_step():
    """`stuck_advice` lives in step parameters, so it MOVES step.signature().
    Stamping it onto a step whose contract the user already approved would
    invalidate that approval — the contract they said yes to would no longer be
    the one being presented."""
    from app.agents.planner import _inject_stuck_advice
    from app.agents.schemas import AgentPlan, PlanStep, PermissionLevel

    approved = PlanStep(
        id="1", description="submit", tool="browse_commit",
        parameters={"goal": "g", "_commit": {"url": "u", "fields": []}},
        permission_level=PermissionLevel.DESTRUCTIVE, requires_approval=True,
    )
    pending = PlanStep(
        id="2", description="browse", tool="browse",
        parameters={"goal": "g"}, permission_level=PermissionLevel.READ,
        requires_approval=False,
    )
    plan = AgentPlan(goal="g", steps=[approved, pending], stuck_advice="do X")
    before = approved.signature()

    assert _inject_stuck_advice(plan) is True
    assert "stuck_advice" not in approved.parameters
    assert approved.signature() == before
    assert pending.parameters["stuck_advice"] == "do X"


def test_no_advice_stamps_nothing():
    from app.agents.planner import _inject_stuck_advice
    from app.agents.schemas import AgentPlan, PlanStep, PermissionLevel

    step = PlanStep(
        id="1", description="browse", tool="browse",
        parameters={"goal": "g"}, permission_level=PermissionLevel.READ,
        requires_approval=False,
    )
    plan = AgentPlan(goal="g", steps=[step])
    assert _inject_stuck_advice(plan) is False
    assert "stuck_advice" not in step.parameters


def test_the_advice_reaches_both_the_prompt_and_the_scoring_corpus():
    """Termination: the next round genuinely has different input, which is the
    same argument TARGET_CHOICE rests on."""
    words = choice.target_tokens(
        "buy a widget", "https://site.test/", extra=["click the kameez shalwar section"]
    )
    assert "kameez" in words and "shalwar" in words


@pytest.mark.asyncio
async def test_the_steer_changes_what_the_model_is_asked():
    blank = _page([_el(1, name="Home", href="/")], url="https://site.test/x", title="X")
    page = ScriptedPage([blank, blank])
    session = _CommitSession(page)
    provider = LoopProvider(["", ""])

    await browser_loop.run_browse(
        session, "buy a widget", provider, commit=True,
        stuck_advice="open the accessories tab",
    )
    asked = " ".join(str(p) for p in provider.prompts)
    assert "open the accessories tab" in asked, (
        "the user's instruction must reach the decision prompt, or the resume "
        "walks into the same wall"
    )


def test_a_plan_parked_before_this_round_still_deserializes():
    """Every new field is defaulted, so an old parked payload restores cleanly."""
    from app.agents.schemas import AgentPlan

    plan = AgentPlan.model_validate({"goal": "g", "steps": []})
    assert plan.pending_stuck is None
    assert plan.stuck_advice == ""
    assert plan.browse_stucks == 0
    payload = HandoffPayload.from_dict({"reason": "target_choice"})
    assert payload is not None and payload.choice_unbuyable is False


# ============================================ 6. the flag is prompt-invariant
def test_the_stock_flag_never_reaches_the_prompt_or_the_fingerprint():
    """CODE data, like `in_dialog` and `rect`. If this ever renders it changes
    the element budget, the action signature AND the page fingerprint."""
    els = _elements(_cards(JANAN_SOLD_OUT[:1], JANAN_BUYABLE[:1]))
    sold, live = els[0], els[1]
    assert sold.sold_out is True and live.sold_out is False
    assert "sold" not in sold.render().lower()
    # …and its MIRROR, so the assertion above cannot pass vacuously: a NAME
    # change DOES move the rendered line.
    renamed = browser_loop.dom_observe._elements_of(
        _cards(("SOMETHING ELSE",), (), url=SEARCH_URL)
    )[0]
    assert renamed.render() != sold.render()
