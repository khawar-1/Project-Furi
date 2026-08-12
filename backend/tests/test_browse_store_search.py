"""Search the store — the 2026-08-09 incident.

THE INCIDENT. "go to junaidjamshed.com and add janan perfume in cart". Furi
opened the homepage and clicked a NAV CATEGORY instead of searching for "janan",
so the tie question then offered six items off the Fragrances listing and the one
the user meant was not among them. Their report: "if it had searched janan then
all the perfumes containing janan would have listed and not the others… and the
one i meant would have been on the first page".

⚠️ FIVE INDEPENDENT SWITCHES WERE OFF, and fixing any ONE of them alone changes
nothing — that is why this file exists as a unit. Measured from the run's own
trace (2026-08-09_10-04-02_de4206b704de.jsonl), backend.log and the live page:

  S1  the deterministic search is gated `step == 0 and not commit`, and the run
      was mode=commit -> it could never have searched, whatever else was true
  S2  the live homepage has ZERO text inputs; search sits behind a LINK named
      'drawer-search' -> there is no box to type into until it is clicked
  S3  _fast_path_action's len==1 branch took candidates[0] UNCHECKED, so it
      would have typed the product name into that link
  S4  _inject_user_words was scoped to `browse`, never `browse_commit`, so
      intent fell back to the planner's paraphrase
  S5  no shopping verb was in the extraction chain, so even the user's own words
      yielded 'junaidjamshed.com and add janan perfume'

MEASURED, before the fix:

    _extract_search_term(user's words)   'junaidjamshed.com and add janan perfume'
    _extract_search_term(planner's goal)  None
    live homepage                         103 elements, 0 text inputs,
                                          [9] role=link name='drawer-search'
    the tie question read                 'janan perfume BY SUBMITTING form'

⚠️ THE FIXTURE IS THE MEASURED PAGE SHAPE, captured by
scripts/_measure_store_search.py against the live site. A fake page is a claim
about the live DOM, and this codebase has shipped a feature built against an
imagined one five times. The claim that matters here is the NEGATIVE one — that
the homepage has no typeable search box at all — because every switch above
follows from it.
"""
import pytest

from app.agents import browser_loop
from app.browser import choice
from app.browser.loop import (
    _extract_search_term,
    _fast_path_action,
    _open_search_ui_action,
)

from tests.test_browser_loop import FakeProvider as LoopProvider, ScriptedPage, _el, _page
from tests.test_browser_target_choice import _CommitSession


HOME_URL = "https://www.junaidjamshed.com/"
SEARCH_URL = "https://www.junaidjamshed.com/search?q=janan+perfume"

USER_WORDS = "go to junaidjamshed.com and add janan perfume in cart"
# What the planner actually authored on the incident, verbatim from the trace.
PLANNER_GOAL = (
    "Go to junaidjamshed.com, find the Janan perfume, and add it to the cart "
    "by submitting the add-to-cart form."
)

# The live homepage, measured: nav categories, a search TOGGLE, and no input.
HOME_ELEMENTS = [
    _el(9, role="link", name="drawer-search"),
    _el(27, role="link", name="FRAGRANCES", href="/collections/fragrances"),
    _el(28, role="link", name="MEN", href="/collections/men"),
    _el(29, role="link", name="WOMEN", href="/collections/women"),
]
# The same page once the drawer is open — now there IS a box.
HOME_WITH_DRAWER = [
    _el(9, role="link", name="drawer-search"),
    _el(10, role="searchbox", name="Search"),
    _el(27, role="link", name="FRAGRANCES", href="/collections/fragrances"),
]


def _home(elements=None, url=HOME_URL):
    return _page(
        list(elements if elements is not None else HOME_ELEMENTS),
        url=url,
        title="J. Junaid Jamshed Official Website",
    )


def _results(*names, url=SEARCH_URL):
    return _page(
        [_el(i + 1, role="link", name=n, href=f"/products/{i}") for i, n in enumerate(names)],
        url=url,
        title="Search results",
    )


# ============================================================ 1. the incident
@pytest.mark.asyncio
async def test_the_incident_a_commit_journey_searches_instead_of_browsing_a_category():
    """END TO END, on the measured page shape: the run opens the hidden search
    box and types the product name — it does not click a nav category.

    Both moves are code-authored, so the model is never asked (`provider.calls
    == 0`). That is the claim: on a storefront homepage the first move needs no
    thinking, and leaving it to the model is what produced the incident."""
    page = ScriptedPage([_home(), _home(HOME_WITH_DRAWER), _results("JANAN GOLD - 100ML")])
    # ⚠️ THE MODEL IS SCRIPTED TO REPEAT THE INCIDENT — click FRAGRANCES. So if
    # either deterministic leg fails to fire, the wrong move is right there
    # waiting to be taken, and the negative assertion below catches it. A
    # provider that could only say "done" would let a broken leg pass quietly.
    wrong = '{"action":"click","index":27}'
    provider = LoopProvider([wrong, wrong, '{"action":"done","reason":"end"}'])

    await browser_loop.run_browse(
        _CommitSession(page), PLANNER_GOAL, provider,
        commit=True, intent_text=USER_WORDS,
    )

    kinds = [(idx, kind, value) for _, idx, kind, value in page.acted]
    assert kinds[0] == (9, "click", None), f"first move was {kinds[0]!r}"
    assert (10, "fill", "janan perfume") in kinds, f"never typed the term: {kinds!r}"
    # ⚠️ THE NEGATIVE HALF: the nav category the incident clicked is untouched,
    # even though the model asked for it.
    assert not any(idx == 27 for idx, _, _ in kinds), "clicked FRAGRANCES again"
    # Both moves were code-authored: the model is consulted only AFTER the
    # search, on the results page it produced.
    assert provider.calls == 1, (
        f"spent {provider.calls} calls — the search itself cost a decision"
    )


@pytest.mark.asyncio
async def test_without_the_users_words_there_is_no_term_and_no_search():
    """S4 frozen: driven with the PLANNER's paraphrase alone — exactly what the
    commit path received before this round — no term can be extracted, so the
    journey falls back to the model. This is the state the incident ran in."""
    assert _extract_search_term(PLANNER_GOAL) is None

    page = ScriptedPage([_home(), _home(HOME_WITH_DRAWER)])
    provider = LoopProvider(['{"action":"done","reason":"stop"}'])

    await browser_loop.run_browse(
        _CommitSession(page), PLANNER_GOAL, provider, commit=True,
    )

    assert not any(kind == "fill" for _, _, kind, _ in page.acted)
    assert provider.calls == 1, "the model should have had to decide"


# ================================================== 2. S1 — the commit gate
@pytest.mark.asyncio
async def test_a_commit_journey_may_search_at_all():
    """S1, the biggest switch. The deterministic search was gated `not commit`,
    so a goal that ENDS in a submit could never search on the way there — even
    though the journey is pure READ navigation and `_is_action_gesture` has
    always classified a genuine search submit as reading."""
    page = ScriptedPage([_home(HOME_WITH_DRAWER), _results("JANAN GOLD - 100ML")])
    # Again the model is offered the incident's wrong move first.
    provider = LoopProvider(
        ['{"action":"click","index":27}', '{"action":"done","reason":"end"}']
    )

    await browser_loop.run_browse(
        _CommitSession(page), PLANNER_GOAL, provider,
        commit=True, intent_text=USER_WORDS,
    )

    kinds = [(idx, kind, value) for _, idx, kind, value in page.acted]
    assert kinds[0] == (10, "fill", "janan perfume"), f"first move was {kinds[0]!r}"
    assert not any(idx == 27 for idx, _, _ in kinds)
    assert provider.calls == 1, "the search itself cost a decision"


@pytest.mark.asyncio
async def test_a_read_only_browse_is_unchanged():
    """The regression twin: read mode searched before this round and still does,
    by the same code path."""
    page = ScriptedPage([_home(HOME_WITH_DRAWER), _results("JANAN GOLD - 100ML")])
    provider = LoopProvider(['{"action":"done","reason":"found"}'])

    await browser_loop.run_browse(
        _CommitSession(page), PLANNER_GOAL, provider,
        commit=False, intent_text=USER_WORDS,
    )

    assert (10, "fill", "janan perfume") in [
        (idx, kind, value) for _, idx, kind, value in page.acted
    ]


def test_the_page_that_already_is_the_target_is_not_searched_away_from():
    """THE GUARD ON S1, and the one real regression risk of enabling it: a start
    URL that is already the product page must not be abandoned for a search.

    Uses the SAME scorer as the tie gate, so the two cannot disagree. MEASURED
    live: the homepage subject scores 0 against {janan, perfume}; the product
    page covers its own name."""
    assert choice.page_covers_target(
        ["janan", "sport", "30ml"], "JANAN SPORT - 30ml", "https://x/products/janan-sport-30ml"
    ) is True
    assert choice.page_covers_target(
        ["janan", "perfume"], "J. Junaid Jamshed Official Website", HOME_URL
    ) is False
    # A PARTIAL match still searches: "janan perfume" on the Janan Sport page is
    # exactly the ambiguous case where searching is the right move.
    assert choice.page_covers_target(
        ["janan", "perfume"], "JANAN SPORT - 30ml", "https://x/products/janan-sport-30ml"
    ) is False


@pytest.mark.asyncio
async def test_a_start_url_that_is_the_product_is_left_alone():
    """The guard, driven through the loop rather than asserted on the helper."""
    product = _page(
        [_el(1, role="searchbox", name="Search"), _el(2, role="button", name="Add to bag")],
        url="https://www.junaidjamshed.com/products/janan-perfume",
        title="JANAN PERFUME",
    )
    page = ScriptedPage([product, product])
    provider = LoopProvider(['{"action":"done","reason":"already here"}'])

    await browser_loop.run_browse(
        _CommitSession(page), PLANNER_GOAL, provider,
        commit=True, intent_text=USER_WORDS,
    )

    assert not any(kind == "fill" for _, _, kind, _ in page.acted), "searched away"
    assert provider.calls == 1


# ============================================ 3. S2 — the hidden search box
def test_the_search_toggle_is_clicked_when_there_is_no_box():
    obs = browser_loop.dom_observe.Observation(
        observation_id="o", url=HOME_URL, title="", element_total=2,
        elements=[
            browser_loop.dom_observe.Element(index=9, role="link", name="drawer-search"),
            browser_loop.dom_observe.Element(index=27, role="link", name="FRAGRANCES"),
        ],
        page_text="", text_truncated=False,
    )
    assert _open_search_ui_action(obs) == {"action": "click", "index": 9}


def test_a_real_search_box_is_the_fast_paths_job_not_the_toggles():
    """When a genuine box is present the toggle leg stands down — otherwise a
    page with BOTH would click the icon and close the box it already had."""
    obs = browser_loop.dom_observe.Observation(
        observation_id="o", url=HOME_URL, title="", element_total=2,
        elements=[
            browser_loop.dom_observe.Element(index=9, role="link", name="drawer-search"),
            browser_loop.dom_observe.Element(index=10, role="searchbox", name="Search"),
        ],
        page_text="", text_truncated=False,
    )
    assert _open_search_ui_action(obs) is None


def test_several_toggles_defer_to_the_model():
    obs = browser_loop.dom_observe.Observation(
        observation_id="o", url=HOME_URL, title="", element_total=2,
        elements=[
            browser_loop.dom_observe.Element(index=1, role="link", name="Search"),
            browser_loop.dom_observe.Element(index=2, role="button", name="Search products"),
        ],
        page_text="", text_truncated=False,
    )
    assert _open_search_ui_action(obs) is None


def test_an_unrelated_control_is_never_mistaken_for_a_search_toggle():
    """Word-boundary: 'research' and 'searchable' are not search controls, and a
    plain nav link is not one either."""
    for name in ("Research", "Searchable filters", "FRAGRANCES", "MEN"):
        obs = browser_loop.dom_observe.Observation(
            observation_id="o", url=HOME_URL, title="", element_total=1,
            elements=[browser_loop.dom_observe.Element(index=1, role="link", name=name)],
            page_text="", text_truncated=False,
        )
        assert _open_search_ui_action(obs) is None, name


@pytest.mark.asyncio
async def test_a_toggle_that_reveals_nothing_is_not_clicked_twice():
    """THE BOUND. A control whose name says search but which reveals no box must
    not become a loop — once per page fingerprint, so the second look hands the
    page to the model instead."""
    page = ScriptedPage([_home(), _home(), _home()])
    provider = LoopProvider(['{"action":"done","reason":"nothing here"}'])

    await browser_loop.run_browse(
        _CommitSession(page), PLANNER_GOAL, provider,
        commit=True, intent_text=USER_WORDS,
    )

    clicks = [idx for _, idx, kind, _ in page.acted if kind == "click" and idx == 9]
    assert len(clicks) <= 1, f"clicked the toggle {len(clicks)} times"


# ================================== 4. S3 — the fast path must not type into a link
def test_the_fast_path_never_types_into_a_link():
    """S3 frozen. The lone 'search' element on the live homepage is a LINK, and
    the len==1 branch took it unchecked — the genuineness rule applied only when
    there were SEVERAL candidates, which is exactly backwards."""
    obs = browser_loop.dom_observe.Observation(
        observation_id="o", url=HOME_URL, title="", element_total=1,
        elements=[browser_loop.dom_observe.Element(index=9, role="link", name="drawer-search")],
        page_text="", text_truncated=False,
    )
    assert _fast_path_action(USER_WORDS, obs) is None


def test_an_image_search_button_does_not_block_the_real_box():
    """⚠️ FOUND BY THE LIVE RUN, not by any fixture — the hermetic tests all
    passed while this was broken.

    junaidjamshed.com's search page carries TWO genuine search targets: the text
    box and an "Upload an image for search" BUTTON. Both are form_search=True
    (they belong to the same search form), so the old rule saw two real targets,
    called it ambiguous and deferred — leaving the loop unable to use a search
    box that was right in front of it. MEASURED live:

        [20] role='input'  name='Search'                      form_search=True
        [22] role='button' name='Upload an image for search'  form_search=True

    You cannot type into a button, so there was never an ambiguity to resolve."""
    obs = browser_loop.dom_observe.Observation(
        observation_id="o", url=HOME_URL, title="", element_total=3,
        elements=[
            browser_loop.dom_observe.Element(
                index=20, role="input", name="Search", form_search=True
            ),
            browser_loop.dom_observe.Element(
                index=22, role="button", name="Upload an image for search",
                form_search=True,
            ),
            browser_loop.dom_observe.Element(index=9, role="link", name="drawer-search"),
        ],
        page_text="", text_truncated=False,
    )
    assert _fast_path_action(USER_WORDS, obs) == {
        "action": "type", "index": 20, "text": "janan perfume", "submit": True,
    }


def test_two_real_boxes_are_still_ambiguous():
    """The regression twin of the rule above: narrowing to TYPEABLE must not
    turn a genuine ambiguity into a guess."""
    obs = browser_loop.dom_observe.Observation(
        observation_id="o", url=HOME_URL, title="", element_total=2,
        elements=[
            browser_loop.dom_observe.Element(index=1, role="searchbox", name="Search"),
            browser_loop.dom_observe.Element(index=2, role="combobox", name="Search filter"),
        ],
        page_text="", text_truncated=False,
    )
    assert _fast_path_action(USER_WORDS, obs) is None


def test_a_search_box_whose_label_never_says_search_is_still_found():
    """`form_search` joins the candidate gather, so a box labelled "What are you
    looking for?" is findable — the name is a weak signal and plenty of themes
    do not use the word."""
    obs = browser_loop.dom_observe.Observation(
        observation_id="o", url=HOME_URL, title="", element_total=1,
        elements=[
            browser_loop.dom_observe.Element(
                index=4, role="input", name="What are you looking for?",
                form_search=True,
            )
        ],
        page_text="", text_truncated=False,
    )
    assert _fast_path_action(USER_WORDS, obs) == {
        "action": "type", "index": 4, "text": "janan perfume", "submit": True,
    }


def test_the_fast_path_still_fires_on_a_single_real_box():
    """The regression twin — the lone-candidate branch is narrowed, not removed."""
    obs = browser_loop.dom_observe.Observation(
        observation_id="o", url=HOME_URL, title="", element_total=1,
        elements=[browser_loop.dom_observe.Element(index=3, role="searchbox", name="Search")],
        page_text="", text_truncated=False,
    )
    assert _fast_path_action(USER_WORDS, obs) == {
        "action": "type", "index": 3, "text": "janan perfume", "submit": True,
    }


# ====================================== 5. S5 — shopping goals yield a product name
@pytest.mark.parametrize(
    "goal,want",
    [
        (USER_WORDS, "janan perfume"),
        ("add janan perfume to cart", "janan perfume"),
        ("add janan sports 100ml to my cart", "janan sports 100ml"),
        ("go to amazon and add airpods to cart", "airpods"),
        ("add the grey formal kurta to the basket", "the grey formal kurta"),
        ("buy a black kurta from junaidjamshed", "a black kurta"),
        ("purchase janan gold 100ml", "janan gold 100ml"),
    ],
)
def test_a_shopping_goal_yields_the_product_name(goal, want):
    assert _extract_search_term(goal) == want


@pytest.mark.parametrize(
    "goal,want",
    [
        # ⚠️ THE CONTROLS. Adding shopping verbs to the leading chain must not
        # eat a real title. "order" is deliberately NOT in the list for exactly
        # this reason — see _VERB_ALT.
        ("play the dangers in my heart on anikoto.cz", "the dangers in my heart"),
        ("find the order of the phoenix on goodreads", "the order of the phoenix"),
        ("search for lofi beats on youtube", "lofi beats"),
        ("open youtube", "youtube"),
        ("go to junaidjamshed.com", "junaidjamshed.com"),
        ("play latest episode of latest season of bleach on anikoto", "bleach"),
        ("play ep 4 of season 4 of my hero academia on anikoto", "my hero academia"),
    ],
)
def test_existing_extraction_is_unchanged(goal, want):
    assert _extract_search_term(goal) == want


# ============================ 6. S4 — the words the gates score are the USER's
def test_the_tie_question_reads_the_users_words_not_the_planners_plumbing():
    """The live question read "6 things match 'janan perfume BY SUBMITTING
    form'" — the planner's sentence, scored as if the user had said it. The
    words 'by', 'submitting' and 'form' are machine noise that no product can
    match, so they add nothing but nonsense to what the user is shown."""
    planner = choice.target_tokens(PLANNER_GOAL, HOME_URL)
    user = choice.target_tokens(USER_WORDS, HOME_URL)

    assert "submitting" in planner and "form" in planner
    assert "submitting" not in user and "form" not in user
    assert choice.plain_words(user) == ["janan", "perfume"]
