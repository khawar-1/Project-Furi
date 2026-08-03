"""
"Which one did you mean?" for the things ON a page (2026-08-02).

THE INCIDENT. Asked to "add janan perfume to cart" on a storefront selling Janan
Sports, Janan Oud and Janan Leather — each in several sizes — the browse loop
picked one and carried on. Not by policy: `Handoff` had no member for item
ambiguity and the decision prompt has no "ask" verb, so neither code nor the
model could raise the question. The one downstream checkpoint named the chosen
variant by its barcode (`properties[_Barcode]: PM135415-100-999-M`).

THE PROPERTY THAT MATTERS MOST IS THE NEGATIVE ONE: a user who WAS specific must
never be interrupted. Every "no ambiguity" test here is that property, and there
are deliberately more of them than there are pause tests.

Four layers, because a pausing feature needs all four: the tie test itself, the
loop's gate, the planner's pause/answer, and the held window.
"""
import pytest

import app.tools  # noqa: F401 — registers the real tools
from app.agents import browser_commit, browser_loop, planner as planner_mod
from app.agents.planner import AgentPlanner
from app.agents.schemas import PlanStatus, StepStatus
from app.browser import choice
from app.browser.extract import find_price
from app.browser.state import Handoff, HandoffPayload, handoff_from_outcome
from app.core import browser_runtime, browser_session, dom_observe
from app.core.base_tool import PermissionLevel, ToolResult

from tests.test_agent_planner import FakeProvider, plan_json, step
from tests.test_browser_loop import FakeProvider as LoopProvider, ScriptedPage, _el, _page


# --------------------------------------------------------------- the real page
# The three products the incident's storefront actually returned, with the real
# shape a grid card carries: title, then price, in ONE element's text.
JANAN = (
    "JANAN SPORT - 100ml Rs. 4,500",
    "JANAN OUD - 100ml Rs. 5,200",
    "JANAN LEATHER - 50ml Rs. 3,900",
)
_SITE = "https://www.junaidjamshed.com/search?q=janan"


class _El:
    """An observation Element, as browser.choice reads one."""

    def __init__(self, index, name, role="link", href=""):
        self.index = index
        self.role = role
        self.name = name
        self.name_full = name
        self.href = href or f"/products/{index}"


def _els(*titles):
    return [_El(i + 1, t) for i, t in enumerate(titles)]


def _ask(goal, titles=JANAN, url=_SITE, extra=()):
    """The verdict for one goal against one page: the option strings, or []."""
    target = choice.target_tokens(goal, url, extra=extra)
    return [c.option() for c in choice.tied_candidates(target, _els(*titles), find_price=find_price)]


# ============================================================ 1. the tie test
def test_the_incident_asks_which_janan():
    """THE DEFECT, frozen. Three products match 'janan perfume' equally well, so
    the user's words cannot say which — and each option carries the page's own
    title and price, which is what tells them apart."""
    options = _ask("add janan perfume to cart on junaidjamshed.com")
    assert len(options) == 3
    assert "JANAN SPORT - 100ml" in options[0]
    assert "JANAN OUD - 100ml" in options[1]
    assert "JANAN LEATHER - 50ml" in options[2]
    assert "Rs. 5,200" in options[1]


@pytest.mark.parametrize(
    "goal",
    [
        "add janan sports 100ml to cart",
        "add janan sport 100 ml to cart",       # the size written with a space
        "add the janan oud to my cart",
        "add janan leather 50ml to cart",
        "buy janan sports perfume 100ml",
    ],
)
def test_a_specific_request_is_never_interrupted(goal):
    """THE PROPERTY THE WHOLE DESIGN IS FOR. A user whose words single one out
    gets no question at all — the tie test is a comparison, not a threshold, so
    naming the variant simply makes one candidate score highest."""
    assert _ask(goal) == []


@pytest.mark.parametrize(
    "goal,titles",
    [
        # Clothes: the axis is waist, and nothing here knows what a waist is.
        ("add black chinos to my cart", ("Black Chinos 30W", "Black Chinos 32W", "Black Chinos 34W")),
        # Laptops: the axis is storage.
        ("buy a macbook air", ("MacBook Air 13 256GB", "MacBook Air 13 512GB", "MacBook Air 13 1TB")),
        # Shoes: the axis is colour.
        ("order the running shoes", ("Running Shoes Black", "Running Shoes White")),
    ],
)
def test_it_generalises_with_no_shopping_vocabulary(goal, titles):
    """The same comparison ties on any axis. If this ever needed a per-domain
    word list it would be the falsified `_WEB_QUESTION_MARKER_RE` shape."""
    assert len(_ask(goal, titles, url="https://shop.test/")) == len(titles)


@pytest.mark.parametrize(
    "goal,titles",
    [
        ("add black chinos 32w to my cart", ("Black Chinos 30W", "Black Chinos 32W", "Black Chinos 34W")),
        ("buy a macbook air 512gb", ("MacBook Air 13 256GB", "MacBook Air 13 512GB", "MacBook Air 13 1TB")),
        ("order the white running shoes", ("Running Shoes Black", "Running Shoes White")),
    ],
)
def test_naming_the_axis_settles_it_on_every_domain(goal, titles):
    assert _ask(goal, titles, url="https://shop.test/") == []


def test_nothing_matching_is_not_an_ambiguity():
    """Zero candidates is not a tie. Asking "which toaster?" about a page with no
    toasters would be worse than the silence it replaced."""
    assert _ask("add a toaster to cart") == []


def test_one_candidate_is_not_an_ambiguity():
    assert _ask("add janan oud to cart", ("JANAN OUD - 100ml Rs. 5,200",)) == []


def test_a_price_can_never_manufacture_a_match():
    """"100" in a PRICE must not score as the "100ml" the user asked for. The
    title is split off the card's text before scoring for exactly this."""
    assert _ask("add janan 100ml", ("JANAN OUD Rs. 100", "JANAN SPORT 100ml Rs. 4,500")) == []


def test_every_offered_option_is_the_page_s_own_text():
    """NO FABRICATION — extract.py's property, applied to choices. A question can
    never offer a product the site does not sell."""
    source = " ".join(JANAN)
    for option in _ask("add janan perfume to cart"):
        for part in option.split(" — "):
            assert part.strip() in source


def test_the_site_s_own_name_cannot_discriminate():
    """Every candidate on junaidjamshed.com mentions Junaid Jamshed, so those
    words are dropped — otherwise they would score equally everywhere and only
    add noise."""
    assert "junaidjamshed" not in choice.target_tokens("add janan perfume", _SITE)


# ---- the token rule. Numbers measured 2026-08-02; see choice.py's docstring.
@pytest.mark.parametrize(
    "a,b",
    [("sport", "sports"), ("chino", "chinos"), ("watch", "watches"),
     ("perfume", "perfumes"), ("leather", "leathers"), ("hoodie", "hoodies")],
)
def test_an_inflection_is_the_same_word(a, b):
    assert choice.tokens_match(a, b) and choice.tokens_match(b, a)


@pytest.mark.parametrize(
    "a,b",
    [
        # ⚠️ EACH OF THESE DEFEATS A FUZZY RATIO FLOOR. Measured rapidfuzz.ratio:
        ("pants", "paints"),   # 90.9 — HIGHER than watch/watches at 83.3
        ("short", "shirt"),    # 80.0
        ("small", "stall"),    # 80.0
        ("large", "lager"),    # 80.0
        ("olive", "alive"),    # 80.0
        # ⚠️ AND EACH OF THESE IS WHY SHORT TOKENS GET NO ALLOWANCE AT ALL:
        ("oud", "loud"),       # 85.7, and a prefix relation would admit tan/tank
        ("tan", "tank"),       # 85.7
        ("cap", "cape"),       # 85.7
        ("100ml", "50ml"),
        ("black", "blue"),
        ("16gb", "8gb"),
    ],
)
def test_a_near_miss_is_a_different_word(a, b):
    assert not choice.tokens_match(a, b)
    assert not choice.tokens_match(b, a)


# ---- picking the answer back up
def test_the_reply_is_matched_deterministically():
    options = _ask("add janan perfume to cart")
    assert "OUD" in choice.pick_by_answer("the oud one", options)
    assert "LEATHER" in choice.pick_by_answer("leather", options)
    assert choice.pick_by_answer(options[0], options) == options[0]  # a clicked button


@pytest.mark.parametrize(
    "reply,options,want",
    [
        ("add janan sport 100 ml", ["100ml", "50ml"], "100ml"),   # spaced -> joined
        ("add janan sport 100ml", ["100 ML", "50 ML"], "100 ML"),  # joined -> spaced
    ],
)
def test_a_size_matches_however_either_side_spells_it(reply, options, want):
    """"100 ml" as a person types it and "100ml" as the page writes it are the
    same size. Neither "100" nor "ml" clears the length gate alone, so the joined
    form is the only thing that can carry the match — and it has to be built on
    BOTH sides, which the first cut only did for the goal."""
    assert choice.pick_by_answer(reply, options) == want


def test_a_reply_that_still_cannot_choose_picks_nothing():
    """FAIL-CLOSED, the _match_site_choice rule. "janan" is in all three, so it
    answers nothing — and code must never break that tie itself."""
    assert choice.pick_by_answer("janan", _ask("add janan perfume to cart")) == ""
    assert choice.pick_by_answer("", _ask("add janan perfume to cart")) == ""


def test_the_chosen_item_is_found_again_on_the_page():
    """locate() is what makes the answer ENFORCEABLE: the picked option becomes
    an element index, in code, with no model involved."""
    picked = choice.locate("JANAN OUD - 100ml — Rs. 5,200", _els(*JANAN), find_price=find_price)
    assert picked is not None and picked.index == 2
    assert choice.locate("a product this page never had", _els(*JANAN)) is None


def test_the_answer_breaks_the_tie_on_the_next_pass():
    """TERMINATION. The answer joins the user's own words, so the same page is no
    longer ambiguous and the question cannot repeat."""
    assert _ask("add janan perfume to cart", extra=["JANAN OUD - 100ml"]) == []


def test_a_malformed_observation_is_never_an_ambiguity():
    """Best-effort in the only safe direction: junk yields "carry on as before"."""
    assert choice.tied_candidates(["janan"], [object(), None]) == []
    assert choice.tied_candidates([], _els(*JANAN)) == []


# ------------------------------------------- the page's own subject (2026-08-02b)
# The incident's real strings: the run reached the product the user had CHOSEN,
# and the rail beneath it tied 3-3-3.
_PDP_URL = "https://www.junaidjamshed.com/products/janan-sport-30ml?_pos=3&_ss=r"
_PDP_TITLE = "JANAN SPORT - 30ml – J."
_SEARCH_TITLE = 'Search: 1000 results found for "Janan" – J.'
_RAIL = ("JANAN SPORT - 200ml", "JANAN SPORT - GIFT SET", "JANAN SPORT - 100ml")
_STEP_GOAL = "Find the product named 'Janan' on junaidjamshed.com and add it to the cart"


def _subject_beats(target, title, url, titles):
    return choice.page_is_the_target(target, title, url, _cands(*titles))


def _cands(*titles):
    return [choice.Candidate(label=t, index=i + 1) for i, t in enumerate(titles)]


def test_the_second_ask_is_suppressed_on_the_chosen_products_own_page():
    """THE DEFECT, frozen with the incident's own numbers (traces 80da37b030ab →
    9ee36900e6e0). The answer IS in the corpus and cannot help: 'sport'/'30ml'
    tell the chosen item from its siblings, and the siblings are still equal to
    EACH OTHER. What resolves it is the page itself."""
    target = choice.target_tokens(_STEP_GOAL, _PDP_URL, extra=["JANAN SPORT - 30ML"])
    subject = choice.page_subject(_PDP_TITLE, _PDP_URL)

    assert [choice._score(target, t) for t in _RAIL] == [3, 3, 3]  # a real tie
    assert choice._score(target, subject) == 5  # and the page outranks it
    assert _subject_beats(target, _PDP_TITLE, _PDP_URL, _RAIL) is True


def test_a_results_page_never_claims_to_be_the_thing_it_lists():
    """⚠️ THE LOAD-BEARING HALF, and why the comparison is STRICT. A listing
    page's title echoes the query, so it ties with the products it lists: 1 vs 1.
    With `>=` this suppresses the one question that MUST be asked."""
    target = choice.target_tokens(_STEP_GOAL, _SITE)
    subject = choice.page_subject(_SEARCH_TITLE, _SITE)

    assert choice._score(target, subject) == 1
    assert choice._score(target, "JANAN SPORT - 200ml") == 1
    assert _subject_beats(target, _SEARCH_TITLE, _SITE, _RAIL) is False


def test_a_category_page_ties_with_its_own_products():
    """The general case of the above: a collection page named for the family
    scores exactly what its members score, so it never suppresses."""
    target = choice.target_tokens("add janan sport to cart", "https://x.test/")
    assert _subject_beats(
        target, "JANAN SPORT – J.", "https://x.test/collections/janan-sport", _RAIL
    ) is False


def test_the_subject_never_reads_the_query_string():
    """⚠️ The mirror of `target_tokens`' host-only rule. `?q=` is the user's own
    words; counting them would let every search page claim to BE what was
    searched for — the request scored against itself."""
    subject = choice.page_subject(
        "Search results", "https://x.test/search?q=janan+sport+30ml"
    )
    assert "janan" not in subject and "30ml" not in subject


def test_the_words_read_back_are_the_words_a_person_said():
    """Live, the question said it was matching 'find product named janan
    findproduct productnamed' — goal plumbing plus the internal joined pairs."""
    target = choice.target_tokens(_STEP_GOAL, _PDP_URL, extra=["JANAN SPORT - 30ML"])
    assert choice.plain_words(target) == ["janan", "sport", "30ml"]


def test_dropping_the_plumbing_never_changes_a_verdict():
    """The stopwords grew, so the verdict must be shown not to rest on them: a
    word that appears in no candidate scored equally everywhere anyway."""
    assert _ask("find the product named janan and add it to cart") == _ask(
        "add janan to cart"
    )


# ================================================================= 2. the loop
def _obs_payload(*titles, url=_SITE):
    return _page([_el(i + 1, name=t, href=f"/products/{i}") for i, t in enumerate(titles)], url=url)


def _grid_payload(*titles, url=_SITE):
    """A REAL listing grid: each card is a title LINK **plus a sibling quick-add
    BUTTON**, which is the shape junaidjamshed.com actually returns.

    ⚠️ THIS FAKE IS THE POINT. Every other page here makes the clicked element BE
    the titled candidate, and that assumption is what let the live run fail while
    64 tests stayed green: on a grid, the element that NAMES a product and the
    element that COMMITS to it are different elements, and `_LABEL_NOISE_RE`
    strips "add to cart" so the button can never be a candidate at all."""
    els = []
    for i, title in enumerate(titles):
        els.append(_el(2 * i + 1, name=title, href=f"/products/{i}"))
        els.append(_el(2 * i + 2, role="button", name="Add to Cart", href=""))
    return _page(els, url=url)


async def test_a_quick_add_on_a_listing_grid_still_asks():
    """THE LIVE MISS, frozen (2026-08-02, trace ab4aeb2673a7). "add janan to
    cart" reached /search?q=janan and the model clicked a card's Add-to-Cart
    button. The gate keyed on `picked_index in tied`, the button is not and can
    never be a candidate, so it passed silently — and the click then died on the
    submit-gesture backstop with "couldn't work out a safe next action".

    The commitment is the same whichever control the model reaches for."""
    page = ScriptedPage([_grid_payload(*JANAN), _grid_payload(*JANAN)])
    session = _CommitSession(page)
    provider = LoopProvider(['{"action":"click","index":2}'])  # card 1's quick-add

    outcome = await browser_loop.run_browse(
        session, "add janan to cart", provider, commit=True
    )

    assert outcome.target_choice_required is True
    assert outcome.choice_kind == "item"
    assert len(outcome.choice_options) == 3
    assert "JANAN SPORT - 100ml" in outcome.choice_options[0]
    # ⚠️ THE NEGATIVE HALF: nothing was added to any cart.
    assert page.acted == []


async def test_a_quick_add_with_one_match_is_never_interrupted():
    """The never-interrupt property, on the grid shape. One product matches, so
    there is no ambiguity to raise and the gesture goes on to the gate that owns
    it — this must NOT become "any add-to-cart click pauses"."""
    page = ScriptedPage([
        _grid_payload("JANAN SPORT - 100ml", "OMBRE OUD - 50ml"),
        _grid_payload("JANAN SPORT - 100ml", "OMBRE OUD - 50ml"),
    ])
    session = _CommitSession(page)
    provider = LoopProvider(['{"action":"click","index":2}', '{"action":"done","reason":"ok"}'])

    outcome = await browser_loop.run_browse(
        session, "add janan to cart", provider, commit=True
    )

    assert outcome.target_choice_required is False


def _pdp_payload(title=_PDP_TITLE, url=_PDP_URL, rail=_RAIL):
    """A product DETAIL page: the page's own Add-to-Cart, then a "you may also
    like" rail of siblings. The shape junaidjamshed.com serves, and the one the
    incident died on — the gesture belongs to the PAGE, not to the rail."""
    els = [_el(1, role="button", name="Add to Cart", href="")]
    els += [_el(i + 2, name=t, href=f"/products/rail-{i}") for i, t in enumerate(rail)]
    return _page(els, url=url, title=title)


async def test_the_answered_choice_is_not_re_asked_on_the_products_own_page():
    """THE INCIDENT, end to end (2026-08-02, traces 80da37b030ab →
    9ee36900e6e0). The user answered "JANAN SPORT - 30ML", the pick was enforced
    in code, the run landed on that product's page — and then the related-items
    rail tied 3-3-3 and it asked AGAIN, from three things they had not chosen.

    A rail is navigation, not a choice. The click here is refused by the
    submit-gesture backstop and fed back as history, which is how the model
    reaches "submit" and the commit approval card — the flow the user wanted."""
    page = ScriptedPage([_pdp_payload(), _pdp_payload()])
    session = _CommitSession(page)
    provider = LoopProvider([
        '{"action":"click","index":1}',            # the page's own Add to Cart
        '{"action":"done","reason":"handed off"}',
    ])

    outcome = await browser_loop.run_browse(
        session, _STEP_GOAL, provider, commit=True, chosen_target="JANAN SPORT - 30ML",
    )

    assert outcome.target_choice_required is False
    assert outcome.choice_options == []


async def test_a_rail_on_a_page_the_user_never_singled_out_still_asks():
    """The other direction, so the suppression cannot be read as "detail pages
    never ask". Same page, same rail — but nothing the user said points at THIS
    product rather than at the three beside it, so the question stands."""
    page = ScriptedPage([
        _pdp_payload(title="Fragrance – J."),
        _pdp_payload(title="Fragrance – J."),
    ])
    session = _CommitSession(page)
    provider = LoopProvider(['{"action":"click","index":1}'])

    outcome = await browser_loop.run_browse(
        session, "add janan to cart", provider, commit=True
    )

    assert outcome.target_choice_required is True
    assert len(outcome.choice_options) == 3
    assert page.acted == []


async def test_the_question_never_reads_back_the_goals_plumbing():
    """The rendered phrase is the user's words — not the LLM-authored goal's
    nouns, and not the internal joined pairs."""
    page = ScriptedPage([_grid_payload(*JANAN), _grid_payload(*JANAN)])
    session = _CommitSession(page)
    provider = LoopProvider(['{"action":"click","index":2}'])

    outcome = await browser_loop.run_browse(session, _STEP_GOAL, provider, commit=True)

    assert outcome.target_choice_required is True
    assert outcome.choice_target == "janan"


async def test_the_loop_pauses_instead_of_opening_one_of_them(monkeypatch):
    """The gate fires on the model's OWN choice: it is about to click one of
    several equally-matching items, on a task that ends in a submit."""
    page = ScriptedPage([_obs_payload(*JANAN), _obs_payload(*JANAN)])
    session = _CommitSession(page)
    provider = LoopProvider(['{"action":"click","index":1}'])

    outcome = await browser_loop.run_browse(
        session, "add janan perfume to cart", provider, commit=True
    )

    assert outcome.target_choice_required is True
    assert outcome.choice_kind == "item"
    assert len(outcome.choice_options) == 3
    # ⚠️ THE NEGATIVE HALF: nothing was opened, clicked or added.
    assert page.acted == []


async def test_a_specific_goal_reaches_the_page_without_a_pause(monkeypatch):
    """The never-interrupt property, through the real loop."""
    page = ScriptedPage([_obs_payload(*JANAN), _obs_payload("JANAN SPORT - 100ml")])
    session = _CommitSession(page)
    provider = LoopProvider(['{"action":"click","index":1}', '{"action":"done","reason":"there"}'])

    outcome = await browser_loop.run_browse(
        session, "add janan sports 100ml to cart", provider, commit=True
    )

    assert outcome.target_choice_required is False
    assert [a[2] for a in page.acted] == ["goto"]  # the link was opened


async def test_the_site_name_is_discounted_from_the_page_we_are_actually_on():
    """The brand IS the site here, so "janan" names the shop, not the product —
    and once it is discounted nothing is left to match, so there is no ambiguity
    to raise.

    This is the regression for a real defect: the gate first learned which site
    to discount from `session.start_url`, an attribute the real BrowserSession
    does not have, so the filtering never ran outside tests. Reading the page's
    own url is what makes it true in production."""
    page = ScriptedPage([
        _obs_payload("JANAN SPORT - 100ml", "JANAN OUD - 100ml", url="https://janan.com/shop"),
        _obs_payload("JANAN SPORT - 100ml", url="https://janan.com/shop"),
    ])
    session = _CommitSession(page)
    provider = LoopProvider(['{"action":"click","index":1}', '{"action":"done","reason":"ok"}'])

    outcome = await browser_loop.run_browse(
        session, "add janan to cart", provider, commit=True
    )

    assert outcome.target_choice_required is False


async def test_a_read_only_browse_never_raises_this_question():
    """SCOPE, deliberate: a read browse acts on nothing, so it is not interrupted.
    Its world-acting gestures still stop at the action-approval gate."""
    page = ScriptedPage([_obs_payload(*JANAN), _obs_payload(*JANAN)])
    session = _CommitSession(page)
    provider = LoopProvider(['{"action":"click","index":1}', '{"action":"done","reason":"read"}'])

    outcome = await browser_loop.run_browse(session, "find me the janan perfume", provider)

    assert outcome.target_choice_required is False


async def test_the_chosen_item_is_clicked_in_code_at_zero_llm_cost():
    """ENFORCE, NEVER TRUST. The resumed run does not re-ask the model which item
    to open — the goal is still the ambiguous sentence, and the 2026-07-12
    folder_resolver lesson is that the model then keeps its original pick.

    FakeProvider([]) with `calls == 0` is the assertion, and the run is bounded to
    ONE action so that the count covers the click and nothing else — an empty
    queue means any consultation would also have been the wrong answer."""
    page = ScriptedPage([_obs_payload(*JANAN), _obs_payload("JANAN OUD - 100ml")])
    session = _CommitSession(page)
    provider = LoopProvider([])

    await browser_loop.run_browse(
        session, "add janan perfume to cart", provider, commit=True,
        chosen_target="JANAN OUD - 100ml — Rs. 5,200",
        max_actions=1,
    )

    assert provider.calls == 0
    # The OUD row (the second card), not the first match the model would have hit.
    assert page.acted[0][2] == "goto"
    assert "/products/1" in page.acted[0][3]


# ---- the option (size / colour) half
_SIZES = ["100 ML", "50 ML", "20 ML"]


def _size_page(url="https://www.junaidjamshed.com/products/janan-sport"):
    return _page(
        [
            _el(1, role="combobox", name="Size", options=_SIZES,
                form={"method": "POST", "submit": False, "search": False}),
            _el(2, role="button", name="Add to Cart",
                form={"method": "POST", "submit": True, "search": False}),
        ],
        url=url,
    )


async def test_an_ungrounded_size_asks_with_the_page_s_real_options():
    """A commit-mode select_option was NEVER grounded before this: the loop could
    put a size into the form that traced to nothing the user said."""
    page = ScriptedPage([_size_page(), _size_page()])
    session = _CommitSession(page)
    provider = LoopProvider(['{"action":"select_option","index":1,"value":"50 ML"}'])

    outcome = await browser_loop.run_browse(
        session, "add janan sport to cart", provider, commit=True
    )

    assert outcome.target_choice_required is True
    assert outcome.choice_kind == "option"
    assert outcome.choice_field == "Size"
    assert outcome.choice_options == _SIZES
    assert page.acted == []  # no size was chosen for the user


async def test_the_size_the_user_named_is_taken_over_the_model_s():
    """ENFORCE the other direction. The user said 100ml; the model picked 50 ML;
    code substitutes the option their own words name — no question needed."""
    page = ScriptedPage([_size_page(), _size_page()])
    session = _CommitSession(page)
    provider = LoopProvider(['{"action":"select_option","index":1,"value":"50 ML"}',
                             '{"action":"done","reason":"chosen"}'])

    outcome = await browser_loop.run_browse(
        session, "add janan sport 100ml to cart", provider, commit=True
    )

    assert outcome.target_choice_required is False
    assert ("select", "100 ML") in [(a[2], a[3]) for a in page.acted]


async def test_a_grounded_size_costs_no_option_read():
    """The common path pays nothing: a value the user actually said passes
    grounding and the control's options are never read."""
    page = ScriptedPage([_size_page(), _size_page()])
    session = _CommitSession(page)
    provider = LoopProvider(['{"action":"select_option","index":1,"value":"100 ML"}',
                             '{"action":"done","reason":"chosen"}'])

    outcome = await browser_loop.run_browse(
        session, "add janan sport 100 ML to cart", provider, commit=True
    )

    assert outcome.target_choice_required is False
    assert ("select", "100 ML") in [(a[2], a[3]) for a in page.acted]


async def test_an_unreadable_control_falls_back_to_the_free_text_ask():
    """No options to offer → the pre-existing fill question, unchanged. A new
    pause must never REPLACE a working one when it has nothing better to say."""
    page = ScriptedPage([
        _page([_el(1, role="combobox", name="Size")], url="https://x.test/p"),
        _page([_el(1, role="combobox", name="Size")], url="https://x.test/p"),
    ])
    session = _CommitSession(page)
    provider = LoopProvider(['{"action":"select_option","index":1,"value":"50 ML"}'])

    outcome = await browser_loop.run_browse(session, "add it to cart", provider, commit=True)

    assert outcome.target_choice_required is False
    assert outcome.fill_required is True
    assert outcome.fill_field == "Size"


class _CommitSession:
    """A session as run_browse touches one, with an allowlist so the off-site
    check has an "off" to measure.

    ⚠️ IT DELIBERATELY HAS NO `start_url`, because the real BrowserSession has
    none. The first cut of the item gate read `session.start_url` to learn which
    site to discount, which meant that filtering silently never ran in
    production — and a fake carrying the field by hand would have hidden it."""

    def __init__(self, page, allowlist=None):
        self.page = page
        self.allowlist = allowlist or {"junaidjamshed.com", "shop.test", "x.test",
                                       "janan.com"}
        self.stats = _Stats()
        self.browse_history = []
        self.last_redirect_offsite = None

    async def settle(self):
        pass

    async def goto(self, url):
        self.page.navigate(url)


class _Stats:
    def as_dict(self):
        return {"blocked_mutations": 0, "blocked_navigations": 0,
                "blocked_hosts": 0, "mutation_urls": []}


# ============================================================== 3. the planner
_CHOICE_OPTIONS = [f"{t.split(' Rs.')[0]} — Rs. {t.split('Rs. ')[1]}" for t in JANAN]


def _choice_discovery():
    return browser_commit.CommitDiscovery(
        target_choice_required=True,
        choice_kind="item",
        choice_target="janan perfume",
        choice_options=list(_CHOICE_OPTIONS),
        error="several things on this page match — needs you to choose one",
    )


def _commit_step():
    return step(
        "Add the perfume to the cart",
        "browse_commit",
        goal="add janan perfume to cart",
        start_url="https://www.junaidjamshed.com/",
        allowed_origins=["junaidjamshed.com"],
    )


_GOAL = "go to junaidjamshed.com and add janan perfume to cart"


def _record_exec(calls):
    async def fake_exec(tool, params, db, session_id=None, approved=False):
        calls.append({"tool": tool, "approved": approved, "params": dict(params)})
        return ToolResult(success=True, output={"submitted": True},
                          permission_level=PermissionLevel.DESTRUCTIVE)

    return fake_exec


async def test_the_plan_pauses_and_offers_the_real_products(db_session, monkeypatch):
    """The tool's signal becomes an AWAITING_CHOICE question carrying the page's
    own labels — and NOTHING runs."""
    async def fake_discover(params, session_id=None, **kwargs):
        return _choice_discovery()

    calls = []
    monkeypatch.setattr(browser_commit, "discover", fake_discover)
    monkeypatch.setattr(planner_mod, "execute_tool", _record_exec(calls))

    plan = await AgentPlanner(
        db_session, FakeProvider([plan_json([_commit_step()])]), session_id="s-c1"
    ).start(_GOAL)

    assert plan.status == PlanStatus.AWAITING_CHOICE
    assert plan.question is not None
    assert plan.question.kind == "target_choice"
    assert "JANAN OUD - 100ml" in " ".join(plan.question.options)
    assert plan.question.options[-1] == planner_mod._DECLINE_CHOICE
    assert calls == []  # nothing was submitted
    # A structural hand-off, never an LLM clarification (the 2026-07-19 split).
    assert plan.browse_handoffs == 1
    assert plan.questions_asked == 0
    assert plan.target_choices == 1


async def test_the_answer_is_stamped_on_the_step_and_the_run_resumes(db_session, monkeypatch):
    """The pick is ENFORCED in code onto the pending browse step, and the plan
    re-enters EXECUTE rather than being re-planned from the ambiguous goal.

    ⚠️ THE COST ASSERTION IS THE LOAD-BEARING ONE, and it is why this test exists
    in this shape. Without the direct EXECUTE re-entry the answer still reaches
    the step — the revise round's own _inject_target_choices puts it there — so
    "the right item was used" passes either way and proves nothing. What the
    enforcement branch actually buys is that the resumed run spends NO further
    planning call on a goal already known to be ambiguous (the
    "resume costs zero extra LLM calls" pattern from the off-site round)."""
    seen = []

    async def fake_discover(params, session_id=None, **kwargs):
        seen.append(str(params.get("chosen_target") or ""))
        if len(seen) == 1:
            return _choice_discovery()
        return browser_commit.CommitDiscovery(
            state={"url": "https://www.junaidjamshed.com/cart/add", "method": "POST",
                   "fields": [{"name": "id", "value": "42", "label": "JANAN OUD - 100 ML"}]}
        )

    monkeypatch.setattr(browser_commit, "discover", fake_discover)
    monkeypatch.setattr(planner_mod, "execute_tool", _record_exec([]))

    provider = FakeProvider([plan_json([_commit_step()])])
    planner = AgentPlanner(db_session, provider, session_id="s-c2")
    plan = await planner.start(_GOAL)
    assert plan.status == PlanStatus.AWAITING_CHOICE
    llm_before = provider.calls

    resumed = await planner.answer(plan, "the oud one")

    assert resumed.status == PlanStatus.AWAITING_APPROVAL
    assert seen[0] == "" and "OUD" in seen[1]
    assert resumed.chosen_target and "OUD" in resumed.chosen_target
    # The resume re-enters EXECUTE directly: no planning call is spent
    # re-reading a goal that is still the ambiguous sentence.
    assert provider.calls == llm_before
    # Part 3: the card names the variant in words, not only by its id.
    assert "JANAN OUD - 100 ML" in (resumed.steps[0].action_detail or "")


async def test_declining_stops_honestly_without_adding_anything(db_session, monkeypatch):
    """FAIL-CLOSED. "None of these" cancels; it never falls back to a guess."""
    async def fake_discover(params, session_id=None, **kwargs):
        return _choice_discovery()

    calls = []
    monkeypatch.setattr(browser_commit, "discover", fake_discover)
    monkeypatch.setattr(planner_mod, "execute_tool", _record_exec(calls))

    planner = AgentPlanner(
        db_session, FakeProvider([plan_json([_commit_step()])]), session_id="s-c3"
    )
    plan = await planner.start(_GOAL)
    resumed = await planner.answer(plan, planner_mod._DECLINE_CHOICE)

    assert resumed.status == PlanStatus.CANCELLED
    assert "won't guess" in resumed.message
    assert calls == []


async def test_a_reply_that_answers_nothing_also_stops(db_session, monkeypatch):
    """"janan" is in all three, so it has not chosen. Code must not break that
    tie on the user's behalf."""
    async def fake_discover(params, session_id=None, **kwargs):
        return _choice_discovery()

    monkeypatch.setattr(browser_commit, "discover", fake_discover)
    monkeypatch.setattr(planner_mod, "execute_tool", _record_exec([]))

    planner = AgentPlanner(
        db_session, FakeProvider([plan_json([_commit_step()])]), session_id="s-c4"
    )
    plan = await planner.start(_GOAL)
    resumed = await planner.answer(plan, "janan")

    assert resumed.status == PlanStatus.CANCELLED


async def test_the_choice_survives_a_replan(db_session):
    """A revise round re-drafts pending steps FROM THE GOAL, which is still the
    ambiguous sentence — so the answer has to be replayed onto the new step or it
    is silently lost and the same question is asked again."""
    from app.agents.schemas import AgentPlan, PlanStep
    from app.core.base_tool import PermissionLevel as PL

    plan = AgentPlan(goal="add janan perfume to cart", chosen_target="JANAN OUD - 100ml")
    plan.steps = [PlanStep(description="browse", tool="browse_commit", parameters={},
                           permission_level=PL.DESTRUCTIVE, requires_approval=True)]

    assert planner_mod._inject_target_choices(plan) is True
    assert plan.steps[0].parameters["chosen_target"] == "JANAN OUD - 100ml"


def test_the_budget_is_serialized_so_it_survives_the_park():
    """The ask PARKS the plan; a counter that did not survive would restart at
    zero on every resume and never terminate."""
    from app.agents.schemas import AgentPlan

    plan = AgentPlan(goal="g", target_choices=2, chosen_target="x",
                     pending_target_options=["a", "b"])
    restored = AgentPlan(**plan.model_dump())
    assert restored.target_choices == 2
    assert restored.chosen_target == "x"
    assert restored.pending_target_options == ["a", "b"]


def test_a_plan_parked_before_this_feature_still_deserializes():
    from app.agents.schemas import AgentPlan

    plan = AgentPlan(**{"goal": "g", "steps": []})
    assert plan.target_choices == 0 and plan.pending_target_choice is None


async def test_a_spent_budget_falls_through_to_the_step_s_own_failure(db_session, monkeypatch):
    """Every exit is a fall-through: with no pause available the step keeps its
    own honest error and the plan behaves as it did before this existed."""
    async def fake_discover(params, session_id=None, **kwargs):
        return _choice_discovery()

    monkeypatch.setattr(browser_commit, "discover", fake_discover)
    monkeypatch.setattr(planner_mod, "execute_tool", _record_exec([]))
    monkeypatch.setattr(planner_mod, "_MAX_TARGET_CHOICES", 0)

    plan = await AgentPlanner(
        db_session, FakeProvider([plan_json([_commit_step()]), plan_json([]), plan_json([])]),
        session_id="s-c5",
    ).start(_GOAL)

    assert plan.status != PlanStatus.AWAITING_CHOICE


async def test_fewer_than_two_options_is_never_a_question(db_session):
    """A "choice" with one answer answers itself — the _validated_question rule.
    Driven through the REAL dispatcher: it must REFUSE to pause (return False,
    the caller's "no pause is possible"), leaving the step's own failure."""
    from app.agents.schemas import AgentPlan, PlanStep
    from app.core.base_tool import PermissionLevel as PL

    plan = AgentPlan(goal="add janan perfume to cart")
    st = PlanStep(description="b", tool="browse_commit", parameters={},
                  permission_level=PL.DESTRUCTIVE, requires_approval=True)
    plan.steps = [st]
    planner = AgentPlanner(db_session, FakeProvider([]), session_id="s-one")

    paused = await planner._handle_browse_handoff(
        plan, st,
        HandoffPayload(reason=Handoff.TARGET_CHOICE, choice_options=["only one"]),
    )

    assert paused is False
    assert plan.status != PlanStatus.AWAITING_CHOICE
    assert plan.target_choices == 0  # the budget is not spent on a non-question


# ========================================================== 4. the held window
async def test_the_window_stays_open_and_the_resume_does_not_navigate():
    """The user is being asked about the page in front of them — closing it would
    be the 2026-08-02 defect (a storefront vanishing one second before the card
    that asks about it). And the reason must NOT be "origin", which is the one
    value that makes the re-attach navigate away from that very page."""
    from app.browser import commit_flow

    reason = commit_flow._DISCOVERY_HOLD_REASONS[Handoff.TARGET_CHOICE]
    assert reason == "choice"
    assert reason != "origin"


async def test_a_choice_pause_at_the_budget_discards_the_held_window(db_session, monkeypatch):
    """THE LEAK TEST. When no pause is possible the held window must not be left
    open forever — the 2026-07-19 live-bug class."""
    held = _HeldSession()

    async def fake_discover(params, session_id=None, **kwargs):
        await browser_session.hold_discovery(
            held, meta={"reason": "choice", "goal": "add janan perfume to cart"}
        )
        return _choice_discovery()

    async def passthrough(coro, *, timeout=None):
        return await coro

    monkeypatch.setattr(browser_commit, "discover", fake_discover)
    monkeypatch.setattr(planner_mod, "execute_tool", _record_exec([]))
    monkeypatch.setattr(browser_runtime, "run_browser", passthrough)
    monkeypatch.setattr(planner_mod, "_MAX_BROWSE_HANDOFFS", 0)

    await AgentPlanner(
        db_session, FakeProvider([plan_json([_commit_step()]), plan_json([]), plan_json([])]),
        session_id="s-c6",
    ).start(_GOAL)

    assert held.closed is True
    assert browser_session.pending_discovery() is None


class _HeldSession:
    def __init__(self):
        self.closed = False
        self.goto_calls = []
        self.allowlist = set()

    async def close(self):
        self.closed = True

    async def goto(self, url):
        self.goto_calls.append(url)


# ================================================== 5. the approval card (P3)
def test_the_card_names_the_variant_in_words():
    """THE INCIDENT'S CARD. `properties[_Barcode]: PM135415-100-999-M` is a
    complete contract and an unreadable one."""
    detail = planner_mod._render_commit_detail({
        "method": "POST",
        "url": "https://www.junaidjamshed.com/cart/add",
        "fields": [{"name": "id", "value": "41234567890", "label": "JANAN SPORT - 100 ML"}],
    })
    assert "JANAN SPORT - 100 ML" in detail
    assert "41234567890" in detail  # the real value is KEPT beside it


def test_a_label_can_never_change_what_was_approved():
    """⚠️ The fingerprint is what verify_commit fails closed on. A cosmetic label
    that re-renders differently must never refuse an approved submit — and it
    cannot, because the fingerprint reads named keys only."""
    from app.core.browser_session import _commit_fingerprint

    plain = {"method": "POST", "url": "https://x.test/a",
             "fields": [{"name": "id", "value": "42"}]}
    labelled = {"method": "POST", "url": "https://x.test/a",
                "fields": [{"name": "id", "value": "42", "label": "100 ML"}]}
    assert _commit_fingerprint(plain) == _commit_fingerprint(labelled)


def test_a_field_with_no_label_renders_exactly_as_before():
    detail = planner_mod._render_commit_detail(
        {"method": "POST", "url": "https://x.test/a", "fields": [{"name": "q", "value": "v"}]}
    )
    assert "  q: v" in detail
