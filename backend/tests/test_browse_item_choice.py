"""
Ask which one BEFORE asking the model to guess — the 2026-08-08 incident.

THE INCIDENT. "go to junaidjamshed.com and add janan to cart". The run reached
`/search?q=janan`, and then DIED: `_decide` returned an empty string twice and
the browse ended "couldn't work out a safe next action on this page", closing the
window. The user's expectation — ask which janan, and list them — was already
built (browser/choice.py, 2026-08-02) and could not fire, because THE TIE GATE
IS DOWNSTREAM OF A DECISION THAT THE TIE ITSELF PREVENTS: it reads
`action["action"] == "click"`, and there was no action to read.

⚠️ THE FIXTURE IS THE REAL LISTING, captured 2026-08-08 by
scripts/_measure_janan_choice.py against the live site. That matters more than
usual here, because the measurement contradicted two things a hand-written
fixture would have encoded:

    homepage           0 candidates carry "janan"   (so the bound is not needed
                                                     on this site — the model is
                                                     right to search first)
    /search?q=janan   20 tie at score 1             (not three, as the 2026-08-02
                                                     fixture assumed — and 20 is
                                                     why the shown cap must say
                                                     how many it is hiding)
    "janan sports"     4 tie at score 3             (there is no SPORT 100ML on
                                                     the page at all, so even a
                                                     specific user is asked —
                                                     correctly)
    cards carry NO price in their element text      (so "a real item has a price"
                                                     is not available as a
                                                     discriminator, which was the
                                                     first design and is measured
                                                     dead)
"""
import pytest

from app.agents import browser_loop
from app.browser import choice
from app.browser.state import Handoff, HandoffPayload, handoff_from_outcome

from tests.test_browser_loop import FakeProvider as LoopProvider, ScriptedPage, _el, _page
from tests.test_browser_target_choice import _CommitSession


# ------------------------------------------------- the real page, verbatim
# Every product junaidjamshed.com/search?q=janan returned whose name carries
# "janan", in the site's own DOM order. Twenty of them.
JANAN_20 = (
    "JANAN GOLD - 100ML",
    "JANAN SPORT - 30ML",
    "JANAN PLATINUM - 100ML",
    "JANAN GIFT SET (30ML)",
    "JANAN INTENSE",
    "JANAN OUD",
    "JANAN GOLD - GIFT SET",
    "JANAN POUR FEMME - 30ML",
    "JANAN POUR FEMME",
    "JANAN PLATINIUM SHOWER GEL",
    "JANAN SPORT POUR HOMME PERFUME BODY SPRAY",
    "JANAN PLATINUM - 200ML",
    "JANAN MUSK - 30ML",
    "JANAN GOLD - 30ML",
    "JANAN LEATHER",
    "JANAN SPORT - 200ML",
    "JANAN SPORT - GIFT SET",
    "JANAN VANILLA",
    "JANAN VANILLA - 30ML",
    "JANAN OUD - 30ML",
)

# The site's own homepage nav, measured: not one entry carries "janan".
HOMEPAGE = (
    "FRAGRANCES",
    "MEN",
    "WOMEN",
    "KIDS",
    "SALE",
    "Quick add",
)

SEARCH_URL = "https://www.junaidjamshed.com/search?q=janan"
HOME_URL = "https://www.junaidjamshed.com/"

# The planner's paraphrase from the incident's own trace, verbatim. Note it keeps
# the user's typo'd host — a token that matches nothing, which is exactly the
# "a stray word cannot break the tie" property choice.py documents.
GOAL = "Go to junadjamshed.com, find the 'janan' product, and add it to the cart."


def _listing(*titles, url=SEARCH_URL):
    """A results page: one title LINK per product, the shape `candidates_of`
    reads. (The grid's sibling quick-add button is exercised by the 2026-08-02
    suite; here the model never gets as far as clicking anything.)"""
    return _page(
        [_el(i + 1, name=t, href=f"/products/{i}") for i, t in enumerate(titles)],
        url=url,
        title="janan – J.",
    )


def _elements(*titles):
    """The payload above, THROUGH the real `_elements_of` — because `_el` builds
    a JS-record dict and `choice` reads attributes off an Element. Constructing
    Elements by hand here would skip the very conversion the loop relies on."""
    return browser_loop.dom_observe._elements_of(_listing(*titles))


# ====================================================== 1. the incident
@pytest.mark.asyncio
async def test_the_incident_a_stalled_decision_asks_instead_of_dying():
    """THE DEFECT, frozen end to end. The model returns nothing — twice, exactly
    as the live provider did — on a page holding twenty equal matches. Before
    this the run ended "couldn't work out a safe next action on this page" and
    the window closed with nothing to show."""
    page = ScriptedPage([_listing(*JANAN_20), _listing(*JANAN_20)])
    session = _CommitSession(page)
    provider = LoopProvider(["", ""])  # the empty replies from the real trace

    outcome = await browser_loop.run_browse(session, GOAL, provider, commit=True)

    assert outcome.target_choice_required is True, (
        f"still a dead end: {outcome.error!r}"
    )
    assert "couldn't work out a safe next action" not in (outcome.error or "")
    assert outcome.choice_kind == "item"
    # ⚠️ THE NEGATIVE HALF: nothing was added to any cart.
    assert page.acted == []


@pytest.mark.asyncio
async def test_the_incident_reports_how_many_there_really_were():
    """Twenty match and a question cannot show twenty. It shows the cap and says
    the total, because a silently shortened list cannot be told from a complete
    one — the "record lied" failure this codebase keeps unpicking."""
    page = ScriptedPage([_listing(*JANAN_20), _listing(*JANAN_20)])
    provider = LoopProvider(["", ""])

    outcome = await browser_loop.run_browse(
        _CommitSession(page), GOAL, provider, commit=True
    )

    assert outcome.choice_total == 20
    assert len(outcome.choice_options) == choice.MAX_CHOICE_OPTIONS == 8
    # Verbatim page labels, in the page's own order — never composed here.
    assert outcome.choice_options[0] == "JANAN GOLD - 100ML"
    assert outcome.choice_options[1] == "JANAN SPORT - 30ML"


# =============================================== 2. asked BEFORE the model
@pytest.mark.asyncio
async def test_the_tie_is_raised_without_spending_a_decision_on_it():
    """THE DESIGN CLAIM, as a call-count assertion (the evidence_resolver thesis
    applied to choosing): step 0 navigates to the results page, and the tie there
    is raised with NO second provider call. A real tie means the user's words
    cannot separate these items, so whatever the model returned would have been a
    guess — and the old gate's whole job was to notice the guess and throw it
    away. Not asking for it is strictly better, and in the incident it cost two
    calls and ~34s to produce something that had to be discarded."""
    # ⚠️ THE HOME PAGE HERE CARRIES THE MEASURED NAV, NOT A "Search" CONTROL
    # (changed 2026-08-09). This fixture originally held one hand-invented
    # element named "Search"; from 2026-08-09 that is a search TOGGLE the loop
    # opens in CODE (_open_search_ui_action), so step 0 stopped costing a
    # provider call and this test's `== 1` measured the new leg instead of the
    # tie gate it was written for. The real homepage nav is HOMEPAGE above,
    # measured off the live site, and contains no such entry — so the model
    # navigates at step 0 exactly as intended and the assertion below once again
    # distinguishes "one call for step 0" from "two calls, one wasted on the
    # tie". The toggle leg has its own test in test_browse_store_search.py.
    page = ScriptedPage([
        _page([_el(1, name=HOMEPAGE[0]), _el(2, name=HOMEPAGE[1])], url=HOME_URL),
        _listing(*JANAN_20),
    ])
    session = _CommitSession(page)
    provider = LoopProvider([f'{{"action":"navigate","url":"{SEARCH_URL}"}}'])

    outcome = await browser_loop.run_browse(session, GOAL, provider, commit=True)

    assert outcome.target_choice_required is True
    assert provider.calls == 1, (
        f"spent {provider.calls} calls — the tie was raised after asking, not before"
    )
    assert outcome.choice_total == 20


@pytest.mark.asyncio
async def test_the_first_page_is_still_the_models_to_narrow():
    """THE BOUND. The run's FIRST move is its chance to narrow the page itself.
    Asking before it has had that chance could offer two nav links while twenty
    real products sat one search away — an incomplete list the user cannot tell
    is incomplete, which is worse than the behaviour being replaced.

    So a tie on step 0 does not pre-empt the model; here it navigates, exactly as
    the live run did. (Nothing is lost: had it stalled, the fallback would ask —
    that is the test above this one.)

    The page it starts on carries the tie and a DIFFERENT url, so the navigation
    is real work rather than a no-op the repeat guard would refuse."""
    page = ScriptedPage(
        [_listing(*JANAN_20, url=HOME_URL), _listing("JANAN OUD")]
    )
    session = _CommitSession(page)
    provider = LoopProvider([f'{{"action":"navigate","url":"{SEARCH_URL}"}}'])

    await browser_loop.run_browse(session, GOAL, provider, commit=True)

    # `_act` opens a url by NAVIGATING, which the page fake records as 'goto'.
    assert ("goto", SEARCH_URL) in [(k, v) for _, _, k, v in page.acted], (
        f"step 0 was pre-empted instead of being left to the model: {page.acted}"
    )


# ================================================= 3. never interrupt a
#                                                     user who was specific
@pytest.mark.asyncio
async def test_a_specific_request_is_never_interrupted():
    """THE PROPERTY THAT MATTERS MOST. 'janan gold 100ml' names exactly one of
    the twenty — measured: it alone carries janan+gold+100ml and both joined
    pairs — so there is nothing to ask and the run proceeds untouched."""
    page = ScriptedPage([_page([_el(1, name="Search")], url=HOME_URL), _listing(*JANAN_20)])
    session = _CommitSession(page)
    provider = LoopProvider(
        [f'{{"action":"navigate","url":"{SEARCH_URL}"}}', '{"action":"click","index":1}']
    )

    outcome = await browser_loop.run_browse(
        session, "add janan gold 100ml to cart", provider, commit=True
    )

    assert outcome.target_choice_required is False, (
        f"interrupted a specific user with {outcome.choice_options}"
    )


@pytest.mark.asyncio
async def test_the_homepage_raises_nothing():
    """MEASURED on the real site: not one homepage entry carries "janan", so the
    tie test is itself the bound there and the run searches, as it should."""
    page = ScriptedPage([_page(
        [_el(i + 1, name=t, href=f"/c/{i}") for i, t in enumerate(HOMEPAGE)],
        url=HOME_URL,
    )] * 2)
    session = _CommitSession(page)
    provider = LoopProvider([f'{{"action":"navigate","url":"{SEARCH_URL}"}}'])

    outcome = await browser_loop.run_browse(session, GOAL, provider, commit=True)

    assert outcome.target_choice_required is False


@pytest.mark.asyncio
async def test_a_read_only_browse_never_asks():
    """Read browses act on nothing; their world-acting gestures already stop at
    the action-approval gate, and pausing a search or a media run would interrupt
    the paths that work. Unchanged by this round."""
    page = ScriptedPage([_page([_el(1, name="Search")], url=HOME_URL), _listing(*JANAN_20)])
    provider = LoopProvider(
        [f'{{"action":"navigate","url":"{SEARCH_URL}"}}', '{"action":"done","reason":"read"}']
    )

    outcome = await browser_loop.run_browse(
        _CommitSession(page), "find janan on junaidjamshed", provider, commit=False
    )

    assert outcome.target_choice_required is False


@pytest.mark.asyncio
async def test_a_stall_on_a_page_with_no_tie_still_fails_honestly():
    """The fallback is a recall net for AMBIGUITY, not a way to turn every dead
    end into a question. A page the model simply could not read still stops, and
    says so."""
    page = ScriptedPage([_page([_el(1, name="Something")], url=HOME_URL)] * 2)
    provider = LoopProvider(["", ""])

    outcome = await browser_loop.run_browse(
        _CommitSession(page), GOAL, provider, commit=True
    )

    assert outcome.target_choice_required is False
    assert "couldn't work out a safe next action" in outcome.error


@pytest.mark.asyncio
async def test_the_page_that_IS_the_product_still_acts_rather_than_re_asking():
    """The 2026-08-02b rule, which the new leg must not undo: on a product page
    the tie is a "you may also like" rail, and asking about it re-opens a
    question the user already answered. Same predicate as the post-decision gate,
    so the two cannot disagree.

    ⚠️ THE FIXTURE CARRIES NO SELF-LINK, and that is what makes this test able to
    fail. With the page's own title among the candidates it scores 5 against the
    rail's 3, so there is a unique leader and NO TIE AT ALL — the suppression is
    never consulted, and the falsification came back green proving nothing (the
    recorded rule: when a falsification passes, suspect the test's reach, then
    the fixture). The rail alone ties 3-3, and only `page_is_the_target` — the
    page scoring 5 — stands between that tie and a second question."""
    detail = _page(
        [
            _el(1, name="JANAN SPORT - 200ML", href="/products/a"),
            _el(2, name="JANAN SPORT - GIFT SET", href="/products/b"),
            _el(3, role="button", name="ADD TO BAG"),
        ],
        url="https://www.junaidjamshed.com/products/janan-sport-30ml",
        title="JANAN SPORT - 30ML – J.",
    )
    page = ScriptedPage([_page([_el(1, name="Search")], url=HOME_URL), detail, detail])
    provider = LoopProvider(
        [f'{{"action":"navigate","url":"https://www.junaidjamshed.com/products/janan-sport-30ml"}}',
         '{"action":"done","reason":"on it"}']
    )

    outcome = await browser_loop.run_browse(
        _CommitSession(page), "add janan sport 30ml to cart", provider, commit=True
    )

    assert outcome.target_choice_required is False, (
        "re-asked from the related-items rail on the product's own page"
    )


# ================================================ 4. the tie test itself
def test_tied_matches_returns_the_whole_tie_and_tied_candidates_caps_it():
    """One scoring pass, two shapes. The caller that must report a total needs
    the whole tie; the caller that renders a question needs the cap."""
    els = _elements(*JANAN_20)
    target = choice.target_tokens("add janan to cart", SEARCH_URL)

    assert len(choice.tied_matches(target, els)) == 20
    assert len(choice.tied_candidates(target, els)) == choice.MAX_CHOICE_OPTIONS


def test_the_measured_phrasings_land_where_the_live_site_put_them():
    """The three phrasings the user described, against the real listing, with the
    numbers scripts/_measure_janan_choice.py recorded. Pinned so a change to the
    token rule that would silently re-answer them fails loudly."""
    els = _elements(*JANAN_20)

    def tie(phrase):
        return len(choice.tied_matches(choice.target_tokens(phrase, SEARCH_URL), els))

    assert tie("add janan to cart") == 20
    # "janan sports" narrows to the SPORT family — and still asks, because the
    # page has no SPORT 100ML at all, so even a size does not single one out.
    assert tie("add janan sports to cart") == 4
    # 4 when written, 6 since the 2026-08-10 coverage scoring. The old 4 came
    # from an ADJACENCY BONUS: the joined 'janansports' matched 'JANAN SPORT…'
    # and lifted those four above the 100ML ones. With each idea counted once, a
    # SPORT-30ML (janan + sports) and an OUD-100ML (janan + 100ml) each carry two
    # of the three things the user said and are genuinely equal — the honest
    # answer to a request for something this page does not stock. A page that
    # DOES carry JANAN SPORT 100ML scores it 3, so it leads and nobody is asked.
    assert tie("add janan sports 100ml to cart") == 6
    # A name that IS on the page singles it out and asks nothing.
    assert tie("add janan gold 100ml to cart") == 0


# ============================================== 5. the total survives the wire
def test_the_total_rides_the_handoff_payload():
    """A pause parks the plan, so the count has to survive serialization or the
    resumed question quietly loses its honesty."""
    class _Out:
        target_choice_required = True
        choice_kind = "item"
        choice_target = "janan"
        choice_field = ""
        choice_options = ["JANAN GOLD - 100ML", "JANAN OUD"]
        choice_total = 20
        url = SEARCH_URL

    payload = handoff_from_outcome(_Out())
    assert payload is not None and payload.reason is Handoff.TARGET_CHOICE
    assert payload.choice_total == 20
    assert HandoffPayload.from_dict(payload.to_dict()).choice_total == 20


def test_the_question_says_how_many_it_is_not_showing():
    """THE USER-VISIBLE HALF. Eight buttons under "8 things match" reads as the
    complete answer when twenty matched. It has to say twenty, and say what to do
    instead of scrolling a list — naming the one you want is faster anyway."""
    from app.agents.planner import _target_choice_question

    payload = HandoffPayload(
        reason=Handoff.TARGET_CHOICE,
        choice_kind="item",
        choice_target="janan",
        choice_total=20,
    )
    text = _target_choice_question(payload, list(JANAN_20[:8])).text

    assert "20 things" in text
    assert "first 8" in text
    assert "tell me the name" in text


def test_a_tie_that_fits_reads_exactly_as_it_did():
    """The common case is unchanged — no count clause where there is nothing
    hidden, so this round adds words only where they carry information."""
    from app.agents.planner import _target_choice_question

    payload = HandoffPayload(
        reason=Handoff.TARGET_CHOICE,
        choice_kind="item",
        choice_target="janan sports",
        choice_total=3,
    )
    text = _target_choice_question(payload, ["A", "B", "C"]).text

    assert text.startswith("3 things on this page match 'janan sports'")
    assert "first" not in text


def test_a_payload_parked_before_this_field_still_deserializes():
    """A plan parked before 2026-08-08 has no `choice_total`; it must read as
    "the same as what is shown", never as a crash."""
    data = HandoffPayload(
        reason=Handoff.TARGET_CHOICE, choice_options=["A", "B"]
    ).to_dict()
    data.pop("choice_total")
    assert HandoffPayload.from_dict(data).choice_total == 0


# ========================================= 6. the 2026-08-10 multi-word incident
#
# "go to junaidjamshed.com and add black kameez kurta in cart". Furi picked a
# product by itself from 1000 results and never asked. TWO INDEPENDENT
# SUPPRESSORS, each of which alone is enough to silence the question — so a fix
# for either one on its own would have changed nothing.

# The real page: the incident's own products (traces a4a729b8ff31 / 482c12f736e3
# opened the first and third of these) plus the shapes around them.
BLACK_LISTING = (
    "Black Lawn Embroidered Kurta",
    "Black Kameez Shalwar",
    "Black Cotton Casual Kameez Shalwar",
    "Black Blended Kameez Shalwar",
    "White Cotton Kurta",
)
BLACK_INTENT = "go to junaidjamshed.com and add black kameez kurta in cart"
BLACK_URL = "https://www.junaidjamshed.com/search?q=black+kameez+kurta"
# Shopify prints the query INTO the title. This is the string from the live run.
BLACK_TITLE = 'Search: 1000 results found for "black kameez kurta" – J.'


def test_suppressor_one_a_joined_pair_no_longer_manufactures_a_leader():
    """⚠️ THE ADJACENCY BONUS, frozen with its measured numbers.

    `target_tokens` appends adjacent-joined pairs so "100 ml" can match "100ml".
    Counting them as extra IDEAS meant a label spelling two of the user's words
    NEXT TO EACH OTHER out-scored one that carried the same two words apart:

        Black Kameez Shalwar                 -> 3   ('blackkameez' is adjacent)
        Black Cotton Casual Kameez Shalwar   -> 2

    so a unique leader existed where the user saw equals, and `tied_matches`
    returned [] — no question, and the model picked freely."""
    target = choice.target_tokens(BLACK_INTENT, BLACK_URL)
    assert "blackkameez" in target, "the pair is still produced — this is not its removal"
    assert choice.plain_words(target) == ["black", "kameez", "kurta"]

    scored = {t: choice._score(target, t) for t in BLACK_LISTING}
    assert scored["Black Kameez Shalwar"] == 2
    assert scored["Black Cotton Casual Kameez Shalwar"] == 2
    assert scored["Black Blended Kameez Shalwar"] == 2
    assert scored["Black Lawn Embroidered Kurta"] == 2
    assert scored["White Cotton Kurta"] == 1, "a genuinely worse match still loses"

    tied = choice.tied_matches(target, _elements(*BLACK_LISTING))
    assert [c.label for c in tied] == [
        "Black Lawn Embroidered Kurta",
        "Black Kameez Shalwar",
        "Black Cotton Casual Kameez Shalwar",
        "Black Blended Kameez Shalwar",
    ]


def test_the_joined_pair_still_does_the_job_it_exists_for():
    """The REGRESSION half: "100 ml" as a person types it must still match
    "100ML" as the page writes it — neither half clears the length gate alone,
    so the joined spelling is the only thing that can carry that match."""
    target = choice.target_tokens("add janan sports 100 ml to cart", "https://x.test/")
    assert choice._score(target, "JANAN SPORT - 100ML") == 4
    assert choice._score(target, "JANAN SPORT - 50ML") == 2
    assert choice._score(target, "JANAN OUD - 100ML") == 3


def test_suppressor_two_a_search_title_no_longer_claims_to_be_the_thing():
    """⚠️ THE TITLE IS THE REQUEST ARRIVING BY ANOTHER CHANNEL.

    `page_subject` already refused the `?q=` query string for exactly this
    circularity — and Shopify prints the query into `<title>`, so the words came
    back in anyway. MEASURED on the incident: subject 5 against a best candidate
    of 2, so the belt meaning "this page IS the thing, do not ask about what it
    lists" fired on a SEARCH RESULTS PAGE.

    The guard's own recorded measurement (search page 1 vs candidates 1 -> asks)
    held only because that query was ONE WORD."""
    target = choice.target_tokens(BLACK_INTENT, BLACK_URL)
    subject = choice.page_subject(BLACK_TITLE, BLACK_URL)

    assert "black" not in subject and "kameez" not in subject and "kurta" not in subject
    assert choice._score(target, subject) == 0

    # Forced tie, so this is measured on the belt itself and not on suppressor 1.
    forced = ("Black Cotton Casual Kameez Shalwar", "Black Blended Kameez Shalwar",
              "Black Printed Kameez Shalwar")
    decision = choice.item_choice(target, _elements(*forced))
    assert decision is not None and len(decision.tied) == 3
    assert choice.page_is_the_target(
        target, BLACK_TITLE, BLACK_URL, decision.all_tied
    ) is False


def test_a_one_word_query_was_the_reason_the_belt_looked_correct():
    """The control that explains the miss: with a ONE-WORD query the title
    echoes one word and every product carries it too, so the tie held at 1 vs 1
    and the STRICT comparison saved it. Nothing about that generalises."""
    target = choice.target_tokens("add janan to cart", SEARCH_URL)
    rail = ("JANAN SPORT - 100ML", "JANAN OUD - 100ML", "JANAN LEATHER - 100ML")
    decision = choice.item_choice(target, _elements(*rail))
    assert decision is not None
    assert choice.page_is_the_target(
        target, 'Search: 20 results found for "janan" – J.', SEARCH_URL,
        decision.all_tied,
    ) is False


def test_the_incident_asks_end_to_end():
    """Both suppressors gone: the real listing, the real title, the real URL —
    the user is asked, and every option is one of their black garments."""
    target = choice.target_tokens(BLACK_INTENT, BLACK_URL)
    els = _elements(*BLACK_LISTING)
    decision = choice.item_choice(target, els)

    assert decision is not None and decision.settled is None
    assert len(decision.tied) == 4
    assert choice.page_is_the_target(
        target, BLACK_TITLE, BLACK_URL, decision.all_tied
    ) is False
    assert all("Black" in c.label for c in decision.tied)


def test_a_product_page_still_beats_its_own_rail():
    """The 2026-08-02 belt must survive the rescoring: on the page the user's
    words name, the related-products rail is not a fresh question."""
    target = choice.target_tokens(
        "add janan sport 30ml to cart",
        "https://www.junaidjamshed.com/products/janan-sport-30ml",
    )
    rail = ("JANAN SPORT - 200ML", "JANAN GIFT SET", "JANAN SPORT - 100ML")
    decision = choice.item_choice(target, _elements(*rail))
    assert decision is not None
    assert choice.page_is_the_target(
        target,
        "JANAN SPORT - 30ml – J.",
        "https://www.junaidjamshed.com/products/janan-sport-30ml",
        decision.all_tied,
    ) is True


def test_page_covers_target_is_measured_against_ideas_not_tokens():
    """⚠️ `_score`'s ceiling is the number of IDEAS, so a `== len(target)` test —
    which also counts the joined-pair spellings — would be unsatisfiable for any
    multi-word request and would switch this belt off silently."""
    url = "https://www.junaidjamshed.com/products/janan-sport-30ml"
    assert choice.page_covers_target(
        choice.target_tokens("add janan sport 30ml to cart", "https://x.test/"),
        "JANAN SPORT - 30ml – J.", url,
    ) is True
    assert choice.page_covers_target(
        choice.target_tokens(BLACK_INTENT, BLACK_URL), "JANAN SPORT - 30ml – J.", url
    ) is False


def test_a_results_page_can_never_cover_the_target_either():
    """The search leg's own belt reads the same subject, so the title echo would
    have told it "this page already IS what was asked for" and stopped the
    search that finds the product."""
    assert choice.page_covers_target(
        choice.target_tokens(BLACK_INTENT, BLACK_URL), BLACK_TITLE, BLACK_URL
    ) is False
