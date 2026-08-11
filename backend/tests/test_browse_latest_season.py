"""
Latest SEASON + latest episode — the 2026-08-07 incident, frozen.

Live: "play latest episode of latest season of bleach on anikoto" opened Bleach
(2004), then the Thousand-Year Blood War arc-1 entry, played its ep-13, called it
done, took 391 seconds, and never handed the video to the user's normal browser.
Google answers "Season 4 / Part 4 'The Calamity', Episode 2".

FOUR independent defects, each with its own test below:

  1. the planner's paraphrase turned three deterministic paths off SILENTLY
  2. the title extractor left "latest season of" in the title
  3. the tightest-slug rule picks the OLDEST entry for a "latest season" goal
  4. the media hand-off read the paraphrase, so a play goal lost its hand-off

⚠️ THE FAKE PAGE IS THE REAL PAGE. REAL_SLUGS below was FETCHED from anikoto on
2026-08-07, not invented — and that mattered: the first draft made up a cour-4
slug, while the live site actually lists TWO entries for The Calamity. A fixture
with one entry per season would have hidden that entirely, and this codebase has
already shipped three test-shape blindnesses (2026-07-17 fan-out tests that
bypassed the planner, 07-30 bulk tests that drove the singular tool, 08-02 grid
fakes with no quick-add). A fake page is a claim about the live DOM; check it.
"""
import pytest

from app.agents import browser_loop
from app.agents.browser_loop import (
    _extract_search_term,
    _season_entry_action,
    _wants_latest_episode,
    goal_wants_playback,
    run_browse,
)
from app.browser import season as browse_season

from test_browser_loop import (  # the established fakes — one shape, one place
    FakeProvider,
    FakeSession,
    ScriptedPage,
    _el,
    _page,
)


# The user's own request, and what the planner actually drafted from it (both
# verbatim from backend.log:10601 / :10633 of the incident run).
USER_WORDS = "play latest episode of latest season of bleach on anikoto"
PLANNER_GOAL = (
    "Find Bleach on anikoto, go to its latest season, and start playing the "
    "newest episode"
)

# The season name the resolver returns for "bleach" — MEASURED 5/5 against the
# live provider on 2026-08-07.
SEASON = "The Calamity"

# ⚠️ THE REAL ANIKOTO SLUGS, fetched from the live site on 2026-08-07 — not
# invented. The first draft of this file made up a cour-4 slug, and the real
# search results turned out to carry TWO entries for The Calamity (a duplicate,
# almost certainly sub/dub), which the invented single-entry fixture could never
# have shown. A fake page is a claim about the live DOM; this one is checked.
SLUG_ORIGINAL = "bleach-yaa9n"
SLUG_COUR1 = "bleach-thousand-year-blood-war-arc-2izxu"
SLUG_COUR4 = "bleach-thousand-year-blood-war-the-calamity-752db"
SLUG_COUR4_DUP = "bleach-thousand-year-blood-war-the-calamity-xdf5"

# All twelve entries anikoto returns for "bleach".
REAL_SLUGS = [
    "bleach-memories-in-the-rain-yqrxb",
    "bleach-the-movie-fade-to-black-hudbv",
    "bleach-the-movie-hell-verse-vi7ia",
    "bleach-the-movie-memories-of-nobody-jp3eo",
    "bleach-the-movie-the-diamonddust-rebellion-hhatz",
    "bleach-the-sealed-sword-frenzy-jp2bb",
    SLUG_COUR1,
    SLUG_COUR4,
    SLUG_COUR4_DUP,
    "bleach-thousand-year-blood-war-the-conflict-sqamb",
    "bleach-thousand-year-blood-war-the-separation-zvwg0",
    SLUG_ORIGINAL,
]


def _results_page(slugs=None):
    """anikoto's real search results for "bleach" — twelve sibling entries."""
    slugs = REAL_SLUGS if slugs is None else slugs
    return _page(
        [
            _el(i, "link", s.replace("-", " ").title(), href=f"/watch/{s}")
            for i, s in enumerate(slugs, 1)
        ],
        url="https://anikoto.cz/search?keyword=bleach",
        title="Search results - Anikoto",
    )


# The same page with the duplicate removed — the shape a site without a sub/dub
# double listing serves, and where the deterministic leg can act alone.
def _results_page_no_duplicate():
    return _results_page([s for s in REAL_SLUGS if s != SLUG_COUR4_DUP])


# ---------------------------------------------------------------- defect 1
def test_the_planners_paraphrase_silently_disabled_three_paths():
    """THE INCIDENT, at its root. Every one of these is a deterministic path
    keyed on a string the PLANNER authors, and all three failed CLOSED at once —
    with no log line, because run_browse only starts the latest-episode task
    `if latest_title` and a None title says nothing."""
    # What the user said: every path armed.
    assert goal_wants_playback(USER_WORDS) is True
    assert _wants_latest_episode(USER_WORDS) is True
    assert _extract_search_term(USER_WORDS) == "bleach"

    # What the planner wrote: the hand-off and the whole web search switch off.
    assert goal_wants_playback(PLANNER_GOAL) is False
    assert _extract_search_term(PLANNER_GOAL) is None


# ---------------------------------------------------------------- defect 2
@pytest.mark.parametrize(
    "goal,expected",
    [
        # THE INCIDENT: a STACKED ordinal. One strip left "latest season of
        # bleach", which is not just an ugly query — it is the web-search string
        # AND the token set matched against a slug, and {latest, season, of,
        # bleach} can never be a subset of `bleach-…`.
        ("play latest episode of latest season of bleach on anikoto", "bleach"),
        ("play the last episode of the newest season of one piece", "one piece"),
        # Single ordinals keep working (regression).
        ("play the last episode of the dangers in my heart", "the dangers in my heart"),
        ("play latest released ep of black clover", "black clover"),
        ("play latest season of bleach on anikoto", "bleach"),
        # ⚠️ TITLES THAT BEGIN WITH AN ORDINAL WORD ARE NOT EATEN. The chain only
        # strips when a MEDIA word follows the ordinal, so these are untouched —
        # the property that makes repeating the strip safe.
        ("play The Last of Us", "The Last of Us"),
        ("watch The First Slam Dunk", "The First Slam Dunk"),
        ("play The Last Airbender", "The Last Airbender"),
    ],
)
def test_the_title_survives_a_stacked_ordinal(goal, expected):
    assert _extract_search_term(goal) == expected


# ---------------------------------------------------------------- defect 3
def test_tightest_slug_picks_the_OLDEST_entry_which_is_the_incident():
    """Why a season name was needed at all: the pre-existing rule prefers the
    entry with the FEWEST extra tokens, and for a multi-cour series that is by
    construction the original. This test does not assert a fix — it pins the
    reason one was necessary."""
    from app.agents.browser_loop import _title_tokens

    want = _title_tokens("bleach")
    extras = {
        s: len(_title_tokens(s.replace("-", " ")) - want)
        for s in (SLUG_ORIGINAL, SLUG_COUR1, SLUG_COUR4)
    }
    assert min(extras, key=extras.get) == SLUG_ORIGINAL


def test_the_season_name_picks_the_cour_entry_not_the_original():
    action = _season_entry_action(
        browser_loop.dom_observe.Observation(
            observation_id="o",
            url="https://anikoto.cz/search?q=bleach",
            title="Search results - Anikoto",
            element_total=11,
            elements=[
                browser_loop.dom_observe.Element(
                    index=e["index"], role=e["role"], name=e["name"],
                    value="", href=e["href"],
                )
                for e in _results_page_no_duplicate()["elements"]
            ],
            page_text="",
            text_truncated=False,
        ),
        "bleach",
        SEASON,
    )
    assert action is not None
    assert action["action"] == "navigate"
    assert SLUG_COUR4 in action["url"]
    assert SLUG_ORIGINAL not in action["url"]


def test_scoring_ranks_the_real_anikoto_entries():
    """MEASURED numbers, pinned: the original scores ZERO on the season name, so
    it can never win, and the cour that carries the name outranks its siblings."""
    score = lambda s: browse_season.score_entry(s, SEASON, "bleach")
    assert score(SLUG_ORIGINAL) == 0
    assert score(SLUG_COUR1) == 0
    assert score(SLUG_COUR4) > 0
    assert browse_season.best_entry(
        [SLUG_ORIGINAL, SLUG_COUR1, SLUG_COUR4], SEASON, "bleach"
    ) == SLUG_COUR4


def test_the_real_site_narrows_twelve_entries_to_the_two_right_ones():
    """⚠️ THE LIVE CHECK THAT CHANGED THE PICTURE. anikoto really returns TWELVE
    entries for "bleach" — six movies/specials, three earlier cours, the original,
    and TWO listings of The Calamity (sub/dub). Scoring against the season name
    isolates exactly the two correct ones and gives EVERY other entry zero —
    including `bleach-yaa9n`, which the tightest-slug rule picks and which is the
    incident."""
    scores = {s: browse_season.score_entry(s, SEASON, "bleach") for s in REAL_SLUGS}
    assert {s for s, n in scores.items() if n > 0} == {SLUG_COUR4, SLUG_COUR4_DUP}
    assert scores[SLUG_ORIGINAL] == 0
    assert scores[SLUG_COUR1] == 0
    # The other cours are named too, and must not be confused with this one.
    assert scores["bleach-thousand-year-blood-war-the-conflict-sqamb"] == 0


def test_the_real_duplicate_defers_to_the_model_WITH_the_season_named():
    """The two Calamity listings tie, so code declines to pick — the house rule.
    That is not a dead end: the run carries on and the model is TOLD the season, so
    it chooses between two entries that are both correct instead of guessing among
    twelve. The incident's failure was that it had no season information at all."""
    assert browse_season.best_entry(REAL_SLUGS, SEASON, "bleach") is None

    obs = browser_loop.dom_observe.Observation(
        observation_id="o", url="https://anikoto.cz/search?keyword=bleach",
        title="Search results - Anikoto", element_total=len(REAL_SLUGS),
        elements=[
            browser_loop.dom_observe.Element(
                index=e["index"], role=e["role"], name=e["name"],
                value="", href=e["href"],
            )
            for e in _results_page()["elements"]
        ],
        page_text="", text_truncated=False,
    )
    assert _season_entry_action(obs, "bleach", SEASON) is None


async def test_when_code_defers_the_model_is_told_which_season():
    """The deferral above is only safe because the season reaches the decision
    prompt. Driven through the real loop: the model's prompt must name it."""
    async def _season(title, provider, *, today="", rows=None):
        return browse_season.SeasonHint(name=SEASON, episode=2)

    browse_season.resolve_latest_season = _season
    provider = FakeProvider(['{"action":"done","reason":"ok"}'])
    await run_browse(
        FakeSession(ScriptedPage([_results_page()])),  # the DUPLICATE-bearing page
        PLANNER_GOAL, provider, intent_text=USER_WORDS, max_actions=1,
    )
    assert provider.prompts, "the model must have been consulted"
    assert SEASON in provider.prompts[0]
    assert "not the original series" in provider.prompts[0]


def test_a_tie_defers_rather_than_guessing():
    """Code never picks between real equals — the model then decides, with the
    season name in its goal (see run_browse's decide_goal)."""
    assert browse_season.best_entry(
        ["show-the-calamity-a", "show-the-calamity-b"], SEASON, "show"
    ) is None
    # Nothing matching at all also defers, rather than forcing a wrong entry.
    assert browse_season.best_entry(["naruto-x", "one-piece-y"], SEASON, "bleach") is None


# ---------------------------------------------------------------- defect 4
def test_the_media_handoff_reads_the_user_not_the_paraphrase():
    """The user's report: "it didn't even switch from jarvis's browser to my
    normal browser which it normally does". browser_agent_tools gates the clean-
    window hand-off on goal_wants_playback, which is anchored to the LEADING verb
    — and "Find Bleach…" does not lead with one."""
    assert goal_wants_playback(PLANNER_GOAL) is False
    assert goal_wants_playback(USER_WORDS) is True
    # The positive gate itself is KEPT and still refuses a non-playback goal even
    # in the user's own words — it is what stopped a storefront being handed over
    # with the interceptor lifted (2026-08-01).
    assert goal_wants_playback("open junaidjamshed.com") is False
    assert goal_wants_playback("add janan perfume to cart on junaidjamshed") is False


# ------------------------------------------------- the planner-side injector
def test_user_words_reach_a_commit_step_that_is_still_in_discovery():
    """⚠️ THIS TEST PINNED THE DEFECT UNTIL 2026-08-09, and its own docstring was
    the argument against it. It read "SCOPING IS A SAFETY PROPERTY… browse_commit
    is DESTRUCTIVE and its signature() is built from parameters, so stamping one
    there would invalidate an approval the user had already granted" — while
    constructing a commit step with NO contract, i.e. one where nothing has been
    discovered, shown or approved. There was no approval to invalidate.

    The real predicate is the one _inject_user_words' three siblings already
    used: is this step APPROVAL-BOUND (does it carry a contract)? — never "is
    this tool destructive?". Getting it wrong cost the whole feature: the two
    2026-08-08 consumers of these words are commit-mode ONLY, so they scored the
    planner's paraphrase and the live question read "janan perfume BY SUBMITTING
    form"."""
    from app.agents.planner import _inject_user_words
    from app.agents.schemas import AgentPlan, PlanStep
    from app.core.base_tool import PermissionLevel

    plan = AgentPlan(
        goal=USER_WORDS,
        steps=[
            PlanStep(
                description="browse", tool="browse",
                parameters={"goal": PLANNER_GOAL, "start_url": "https://anikoto.cz"},
                permission_level=PermissionLevel.READ, requires_approval=False,
            ),
            PlanStep(
                description="commit", tool="browse_commit",
                parameters={"goal": "submit it", "start_url": "https://x.test"},
                permission_level=PermissionLevel.DESTRUCTIVE, requires_approval=True,
            ),
        ],
    )
    _inject_user_words(plan)

    assert plan.steps[0].parameters["user_words"] == USER_WORDS
    assert plan.steps[1].parameters["user_words"] == USER_WORDS


def test_user_words_are_never_stamped_on_an_approval_bound_step():
    """THE SAFETY PROPERTY, pinned where it actually lives. Once a commit step
    carries a discovered contract it is what the user said yes to, so its
    signature must not move — the _inject_target_choices rule, verbatim. This is
    also what makes the change safe for a plan parked BEFORE it shipped: that
    plan's commit step already has its contract, so it is left alone and its
    granted approval still binds."""
    from app.agents.planner import _inject_user_words
    from app.agents.schemas import AgentPlan, PlanStep
    from app.browser.state import COMMIT_PARAM
    from app.core.base_tool import PermissionLevel

    plan = AgentPlan(
        goal=USER_WORDS,
        steps=[
            PlanStep(
                description="commit", tool="browse_commit",
                parameters={
                    "goal": "submit it",
                    "start_url": "https://x.test",
                    COMMIT_PARAM: {"url": "https://x.test/cart/add", "method": "POST"},
                },
                permission_level=PermissionLevel.DESTRUCTIVE, requires_approval=True,
            ),
        ],
    )
    before = plan.steps[0].signature()
    _inject_user_words(plan)

    assert "user_words" not in plan.steps[0].parameters
    assert plan.steps[0].signature() == before  # the approval still binds


def test_injection_is_a_no_op_without_a_goal():
    from app.agents.planner import _inject_user_words
    from app.agents.schemas import AgentPlan, PlanStep
    from app.core.base_tool import PermissionLevel

    plan = AgentPlan(
        goal="",
        steps=[PlanStep(description="b", tool="browse", parameters={"goal": "g"},
                        permission_level=PermissionLevel.READ, requires_approval=False)],
    )
    _inject_user_words(plan)
    assert "user_words" not in plan.steps[0].parameters


# ----------------------------------------------------- the loop reads intent
async def test_intent_text_revives_the_paths_the_paraphrase_killed():
    """END TO END through the real loop: given the planner's paraphrase as `goal`
    AND the user's words as `intent_text`, the loop reaches the COUR-4 entry.
    Without intent_text the season path cannot even start, because latest_title is
    None — which is the incident."""
    seen = {}

    async def _season(title, provider, *, today="", rows=None):
        seen["title"] = title
        return browse_season.SeasonHint(name=SEASON, episode=2)

    browse_season.resolve_latest_season = _season
    try:
        page = ScriptedPage([
            _results_page_no_duplicate(),
            _page([], url=f"https://anikoto.cz/watch/{SLUG_COUR4}",
                  title="Bleach: TYBW - The Calamity"),
        ])
        session = FakeSession(page)
        provider = FakeProvider([])
        await run_browse(
            session, PLANNER_GOAL, provider,
            intent_text=USER_WORDS, max_actions=2,
        )
    finally:
        pass  # the autouse _hermetic_season fixture restores the real function

    # The title handed to the resolver came from the USER's words, not the
    # paraphrase (which reduces to None).
    assert seen["title"] == "bleach"
    assert SLUG_COUR4 in page.url
    assert SLUG_ORIGINAL not in page.url


async def test_without_intent_text_the_goal_is_still_used():
    """Back-compat: a direct API call or a plan parked before this change passes
    no intent_text, and must behave exactly as it did — reading `goal`."""
    seen = {}

    async def _season(title, provider, *, today="", rows=None):
        seen["title"] = title
        return None

    browse_season.resolve_latest_season = _season
    page = ScriptedPage([_results_page()])
    await run_browse(
        FakeSession(page), "play the latest episode of bleach", FakeProvider([]),
        max_actions=1,
    )
    assert seen["title"] == "bleach"


# ------------------------------------------------------ the grounding guard
def test_a_season_the_search_never_mentioned_is_refused():
    """extract.py's no-fabrication property, applied to a season: every value the
    resolver can return is a slice of something a result actually said."""
    corpus = "Bleach: Thousand-Year Blood War - The Calamity. Episode 2 aired."
    assert browse_season._grounded("The Calamity", 2, corpus) == ("The Calamity", 2)
    # Invented name -> dropped. Invented number -> dropped. Independently.
    assert browse_season._grounded("The Hollow Wars", 2, corpus)[0] is None
    assert browse_season._grounded("The Calamity", 99, corpus)[1] is None
    # Punctuation style must not refuse a name that IS there.
    assert browse_season._grounded("Thousand Year Blood War: The Calamity", None, corpus)[0]
    # A one-word "season" is a fragment and is trivially present — refused, or
    # grounding could never filter it.
    assert browse_season._grounded("Season", None, corpus)[0] is None


def test_the_resolver_never_raises_and_needs_a_provider():
    import asyncio

    async def _go():
        assert await browse_season.resolve_latest_season("bleach", None) is None
        assert await browse_season.resolve_latest_season("", object()) is None

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(_go())


def test_the_injector_is_actually_wired_into_the_execute_path():
    """⚠️ THE TESTS ABOVE CALL _inject_user_words DIRECTLY, so every one of them
    stays green if the CALL SITE is deleted — the exact reach gap that cost this
    codebase four rounds (2026-07-17 fan-out, 07-30 bulk, 08-03 sweep wiring,
    08-04 home guard). This pins that the planner actually calls it, and calls it
    beside its three siblings rather than somewhere unreachable.

    HONEST LIMIT, stated rather than implied: this is a SOURCE check, the
    `test_every_failed_site_in_the_planner_stamps_a_fail_class` pattern. It
    proves the call exists in _execute_node; it does not prove it runs before
    every path that needs it."""
    import inspect

    from app.agents import planner

    src = inspect.getsource(planner.AgentPlanner._execute_node)
    assert "_inject_user_words(plan)" in src
    # Beside the three injectors that solve the same class of problem, and before
    # the loop that consumes the steps.
    assert src.index("_inject_target_choices(plan)") < src.index("_inject_user_words(plan)")
    assert src.index("_inject_user_words(plan)") < src.index("next_pending_index()")


async def test_a_slow_season_read_is_picked_up_on_a_later_step():
    """⚠️ A TIMEOUT IS NOT AN ANSWER. The season read is a search PLUS an
    8192-token model call — MEASURED at 30-60s — while the first step can arrive
    within ~20s of launch. One-shot caching (what the episode-number search does)
    would time out on the COMMON case and silently fall back to the old
    behaviour, i.e. the exact failure this module exists to end. So a not-yet-
    ready read is re-checked on the next step."""
    import asyncio

    # ⚠️ THE PER-STEP SLICE MUST BE SHORTER THAN THE READ, or step 0's await just
    # succeeds and the retry path is never exercised. The first version of this
    # test slept 0.25s against the real 4s slice and passed against the reverted
    # one-shot code — it was measuring nothing.
    real_wait = browser_loop._SEASON_STEP_WAIT
    browser_loop._SEASON_STEP_WAIT = 0.01

    # ⚠️ AND IT IS GATED, NOT TIMED. A sleep raced against three 0.01s slices is
    # a flaky test with an opinion: the second version failed on GOOD code because
    # 3 x 0.01s never reaches a 0.25s sleep. The read is released by the SECOND
    # observation instead, so "not ready at step 0, ready at step 1" is a fact
    # about the run rather than about the clock.
    released = asyncio.Event()

    class _GatedPage(ScriptedPage):
        async def evaluate(self, js, arg=None):
            if self.i >= 1:
                released.set()
            return await super().evaluate(js, arg)

    async def _slow(title, provider, *, today="", rows=None):
        await released.wait()
        return browse_season.SeasonHint(name=SEASON, episode=2)

    browse_season.resolve_latest_season = _slow
    page = _GatedPage([
        # Step 0: the homepage — no series links, so the season leg could not act
        # even if it knew, and the fast path searches instead.
        _page([_el(1, "searchbox", "Search")], url="https://anikoto.cz/",
              title="Anikoto"),
        _results_page_no_duplicate(),
        _page([], url=f"https://anikoto.cz/watch/{SLUG_COUR4}",
              title="Bleach: TYBW - The Calamity"),
    ])
    try:
        await run_browse(
            FakeSession(page), PLANNER_GOAL, FakeProvider([]),
            intent_text=USER_WORDS, max_actions=3,
        )
    finally:
        browser_loop._SEASON_STEP_WAIT = real_wait

    requested = [url for (_, _, kind, url) in page.acted if kind == "goto"]
    assert any(SLUG_COUR4 in (u or "") for u in requested), (
        f"the slow season read was dropped instead of retried: {requested}"
    )


# ----------------------------------------------- the web number is demoted
async def test_the_absolute_web_number_is_ignored_once_a_season_is_chosen():
    """⚠️ THE FIX THAT MAKES THE REST SAFE. `_resolve_latest_episode` reads an
    ABSOLUTE number out of search prose, and MEASURED it returns 343 for Bleach
    (from a watch-order listicle: "Ep 300-316", "Ep 343"). On a cour page whose
    own episodes run 1..13 that is not merely the wrong target — verify-before-
    done then rejects every legitimate `done` until the run hard-fails at three
    strikes, i.e. restoring the plumbing WITHOUT this would have traded a wrong
    answer for a dead run."""
    from app.tools import browser_tools

    real_search = browser_tools.SEARCH_PROVIDER_FACTORY
    browser_tools.SEARCH_PROVIDER_FACTORY = lambda q, n: [
        {"title": "How to Watch Bleach in Order",
         "snippet": "Bleach Season 14 (Ep 300-316) ... Ep 343", "content": ""}
    ]

    async def _season(title, provider, *, today="", rows=None):
        return browse_season.SeasonHint(name=SEASON, episode=2)

    browse_season.resolve_latest_season = _season
    try:
        page = ScriptedPage([
            _results_page_no_duplicate(),
            # The cour entry: its own episodes are 1 and 2, nothing near 343.
            _page(
                [
                    _el(1, "link", "Ep 1", href=f"/watch/{SLUG_COUR4}/ep-1"),
                    _el(2, "link", "Ep 2", href=f"/watch/{SLUG_COUR4}/ep-2"),
                ],
                url=f"https://anikoto.cz/watch/{SLUG_COUR4}/ep-1",
                title="Watch Bleach: TYBW - The Calamity Episode 1",
            ),
            _page([], url=f"https://anikoto.cz/watch/{SLUG_COUR4}/ep-2",
                  title="Watch Bleach: TYBW - The Calamity Episode 2"),
        ])
        session = FakeSession(page)
        await run_browse(
            session, PLANNER_GOAL, FakeProvider([]),
            intent_text=USER_WORDS, max_actions=4,
        )
    finally:
        browser_tools.SEARCH_PROVIDER_FACTORY = real_search

    # ⚠️ ASSERT ON WHAT THE CODE REQUESTED, not on where the fixture ended up.
    # ScriptedPage.navigate() sets page.url from the NEXT SCRIPTED PAYLOAD, so
    # `page.url` is the fixture's opinion and is identical whether or not 343 was
    # ever aimed at — the first version of this test asserted exactly that and
    # passed against the reverted code. `acted` records the real request.
    requested = [url for (_, _, kind, url) in page.acted if kind == "goto"]
    assert requested, "the loop must have navigated somewhere"
    assert not any("343" in (u or "") for u in requested), (
        f"the absolute web number leaked into a season-scoped URL: {requested}"
    )
    assert any(f"{SLUG_COUR4}/ep-2" in (u or "") for u in requested)


# ----------------------------------------------- the adopted-tab allowlist
def _adopt_session(allowlist):
    from app.core.browser_session import BrowserSession

    s = BrowserSession.__new__(BrowserSession)
    s.allowlist = set(allowlist)
    s.stats = type("S", (), {"blocked_ads": 0})()
    return s


@pytest.mark.parametrize(
    "url,adopt,why",
    [
        # THE INCIDENT: a pop-under from the episode-range click. Following it
        # closed the anikoto page and cost ~180s of a 391s run.
        ("https://www.getsmartyapp.com/landers/lander39.php?sid=1", False,
         "an ad host nobody could have listed in advance"),
        ("https://anikoto.cz/watch/x", True, "the site we are browsing"),
        ("https://cdn.anikoto.cz/player", True, "a subdomain of it"),
        # Not yet knowable — a target=_blank tab opens blank and navigates a
        # moment later. Rule 3 judges the navigation itself.
        ("about:blank", True, "blank, so the destination is not knowable yet"),
        ("", True, "no url at all — fall back to today's behaviour"),
    ],
)
def test_a_new_tab_is_adopted_only_where_the_loop_may_go(url, adopt, why):
    session = _adopt_session({"anikoto.cz"})
    page = type("P", (), {"url": url})()
    assert session._may_adopt(page) is adopt, why
    # A refusal is counted, so a run's ad pressure is visible in the summary.
    assert session.stats.blocked_ads == (0 if adopt else 1)


def test_the_adopt_check_never_raises_on_an_odd_page():
    """A popup must never break a running browse: on any doubt it falls back to
    today's behaviour."""
    class Exploding:
        @property
        def url(self):
            raise RuntimeError("boom")

    assert _adopt_session({"anikoto.cz"})._may_adopt(Exploding()) is True


def test_an_id_suffix_can_never_be_read_as_a_season_number():
    """anikoto's `…-arc-2izxu` must not contribute a bare "2" to the match — the
    reason _tokens splits on non-alphanumerics rather than stripping them."""
    assert "2" not in browse_season._tokens(SLUG_COUR1)
    assert "2izxu" in browse_season._tokens(SLUG_COUR1)
    # But a genuinely standalone number IS kept — it is the whole discriminator
    # between "Season 2" and "Season 3" (MEASURED: the resolver returns exactly
    # "Season 2" for a series with no distinctive season name).
    assert "2" in browse_season._tokens("dangers-in-my-heart-season-2")
    assert browse_season.best_entry(
        ["dangers-x", "dangers-season-2-y", "dangers-season-3-z"],
        "Season 2", "dangers",
    ) == "dangers-season-2-y"
