"""
An explicitly NUMBERED season — "play ep 4 of season 4 of my hero academia".

Recorded in CLAUDE.md on 2026-08-07 as a measured, deliberately-unfixed gap:
`_wants_latest_season` only fires on the WORD latest/newest/current, so a goal
naming a number got no season handling at all and the model picked from the
listing unaided. The user hit it on 2026-08-08.

⚠️ THE FIXTURE IS THE REAL LISTING. These slugs are anikoto's own, and the point
is that ONE SITE NAMES ITS SEASONS FIVE WAYS in a single result set — a bare
number, the word, an ordinal, no number at all — beside movie decoys carrying
the very digits the goal asks for. MEASURED against it before the fix:

    goal 'Season 4'   1  my-hero-academia-4-mt2j9
                      1  my-hero-academia-the-movie-4-you-re-next   <- TIE
    goal 'Season 5'   0  my-hero-academia-5th-season                <- '5th' != '5'
    goal 'Season 6'   1  my-hero-academia-season-6                  -> by luck

1 of 3. An invented fixture with one tidy entry per season would have shown none
of that, and this codebase has already shipped four test-shape blindnesses.
"""
import pytest

from app.agents import browser_loop
from app.agents.browser_loop import _target_season, run_browse, season_label
from app.browser import season as browse_season

from test_browser_loop import (  # the established fakes — one shape, one place
    FakeProvider,
    FakeSession,
    ScriptedPage,
    _el,
    _page,
)

TITLE = "my hero academia"

# ⚠️ ANIKOTO'S REAL RESULTS FOR "my hero academia", FETCHED 2026-08-08 — all 40
# of them, and the first draft of this file was a tidy hand-picked ten. The
# runtime check is what caught that, and what it caught was a REAL DEFECT: the
# site's search is FUZZY, so the page carries a dozen other franchises, and
# "Season 4" matched `that-time-i-got-reincarnated-as-a-slime-season-4` exactly
# as well as the real entry. The question that produced was not merely wrong, it
# was nonsense. A tidy fixture could never have shown it.
SLUG_S2 = "my-hero-academia-2-l3eyd"
SLUG_S3 = "my-hero-academia-3-iojeg"
SLUG_S4 = "my-hero-academia-4-mt2j9"
SLUG_S5 = "my-hero-academia-5th-season-4lw3i"
SLUG_S6 = "my-hero-academia-season-6-thlwp"
SLUG_S7 = "my-hero-academia-season-7-wgpee"
SLUG_FINAL = "my-hero-academia-final-season-ro7lw"
MOVIE_4 = "my-hero-academia-the-movie-4-you-re-next-wp0tc"
# The spin-off is a genuine part of the franchise with its own season 2 — it
# competes honestly, and a tie ASKS.
SLUG_VIGILANTES_S2 = "my-hero-academia-vigilantes-season-2-5uow1"
# Other shows the fuzzy search returns. `slime-season-4` is the one that tied.
OTHER_SLIME_S4 = "that-time-i-got-reincarnated-as-a-slime-season-4-0u851"
OTHER_HUSBAND_S2 = "my-heroic-husband-2nd-season-zadsl"

REAL_SLUGS = [
    "boku-no-hero-academia-i-am-a-hero-too-cc2a2",
    "boku-no-hero-academia-memories-jlmsg",
    "boku-no-hero-academia-sukue-kyuujo-kunren-fkqbm",
    "boku-no-hero-academia-training-of-the-dead-ashfj",
    "classroom-of-the-elite-iv-rzzt2",
    "daemons-of-the-shadow-realm-hxj32",
    "dorohedoro-season-2-bqfe6",
    "dr-stone-science-future-part-3-6d6cc",
    "gals-can-t-be-kind-to-otaku-whjvd",
    "ginga-eiyuu-densetsu-waga-yuku-wa-hoshi-no-taikai-ejrsv",
    "hitorijime-my-hero-bvh86",
    "i-want-to-end-this-love-game-vf4q5",
    "kill-blue-gcqj5",
    "kusunoki-s-garden-of-gods-po5hl",
    "maoyu-archenemy-hero-8jx86",
    SLUG_S2,
    SLUG_S3,
    SLUG_S4,
    SLUG_S5,
    SLUG_FINAL,
    "my-hero-academia-kuzfp",
    "my-hero-academia-make-it-do-or-die-survival-training-gyvog",
    "my-hero-academia-more-ftcg6",
    "my-hero-academia-movie-1-two-heroes-pvnus",
    "my-hero-academia-ona-jdr4f",
    "my-hero-academia-season-2-hero-notebook-oyepe",
    SLUG_S6,
    SLUG_S7,
    "my-hero-academia-the-movie-2-heroes-rising-wfipk",
    "my-hero-academia-the-movie-3-world-heroes-mission-tzhzq",
    MOVIE_4,
    "my-hero-academia-ua-heroes-battle-ow4vv",
    SLUG_VIGILANTES_S2,
    "my-hero-academia-vigilantes-zfl30",
    OTHER_HUSBAND_S2,
    "my-heroic-husband-in7oc",
    "my-home-hero-bvsgs",
    "my-status-as-an-assassin-obviously-exceeds-the-hero-s-pzrcq",
    OTHER_SLIME_S4,
    "the-ramparts-of-ice-dxyxt",
]


def _results_page(slugs=None, url="https://anikoto.cz/filter?keyword=my+hero+academia"):
    slugs = REAL_SLUGS if slugs is None else slugs
    return _page(
        [
            _el(i, "link", s.replace("-", " ").title(), href=f"/watch/{s}")
            for i, s in enumerate(slugs, 1)
        ],
        url=url,
        title="Browse & Filter - Anikoto",
    )


# --------------------------------------------------------- what the goal names
@pytest.mark.parametrize(
    "goal, expected",
    [
        ("play ep 4 of season 4 of my hero academia on anikoto", 4),
        ("play season 12 of a show", 12),
        ("watch s4e4 of my hero academia", 4),
        ("play Season 6 episode 1", 6),
        ("play season4 of x", 4),
        # No number, so this stays _wants_latest_season's job (it resolves a
        # season NAME from the web instead).
        ("play latest episode of latest season of bleach", None),
        ("play the newest season of bleach", None),
        # Not a season at all.
        ("play ep 4 of my hero academia", None),
        ("play blink 182", None),
        # `part` is deliberately not a season — see _GOAL_SEASON_RE.
        ("play part 4 of bleach", None),
    ],
)
def test_the_season_the_goal_names(goal, expected):
    assert _target_season(goal) == expected


# ------------------------------------------------------------- the measurement
def test_the_incident_season_4_no_longer_ties_with_a_movie():
    """THE DEFECT, with its own numbers. `my-hero-academia-the-movie-4` carries
    the digit 4 as surely as the season does, so scoring alone tied them and
    best_entry returned None — which is why the live run clicked blind."""
    assert browse_season.score_entry(MOVIE_4, "Season 4", TITLE) == 0, (
        "a film is still scoring as a season"
    )
    assert browse_season.best_entry(REAL_SLUGS, "Season 4", TITLE) == SLUG_S4


def test_the_runtime_finding_a_numbered_season_must_be_the_right_show_first():
    """⚠️ FOUND BY THE RUNTIME CHECK, against the real page — not by any test.

    anikoto's search is FUZZY: "my hero academia" returns 40 rows spanning a
    dozen franchises. A numbered season has only its digit to match on, so
    without a series-membership test "Season 4" scored 1 for the real entry AND
    1 for an unrelated show — a tie, and a question asking the user to choose
    between My Hero Academia and That Time I Got Reincarnated as a Slime.

    A season NAME was never this exposed, which is why the 2026-08-07 round did
    not need the rule: "The Calamity" is distinctive where "4" is not."""
    assert browse_season.score_entry(OTHER_SLIME_S4, "Season 4", TITLE) == 0
    assert browse_season.score_entry(OTHER_HUSBAND_S2, "Season 2", TITLE) == 0
    assert browse_season.score_entry(SLUG_S4, "Season 4", TITLE) > 0
    assert browse_season.best_entry(REAL_SLUGS, "Season 4", TITLE) == SLUG_S4

    # No title means no constraint, so every caller that scores without one
    # keeps exactly the behaviour it had.
    assert browse_season.score_entry(OTHER_SLIME_S4, "Season 4", "") > 0


def test_every_way_this_one_site_spells_a_season_resolves():
    """FIVE SPELLINGS, ONE SITE, over the real 40-row listing. Before the fix
    this was 1 of 3: season 4 tied with a film, and the ordinal case scored zero
    against everything so no amount of tie-breaking could have reached it."""
    wanted = {
        3: SLUG_S3,
        4: SLUG_S4,   # bare number, against three films carrying a digit
        5: SLUG_S5,   # an ordinal:  my-hero-academia-5th-season
        6: SLUG_S6,   # the word:    my-hero-academia-season-6
        7: SLUG_S7,
    }
    for number, slug in wanted.items():
        assert browse_season.best_entry(
            REAL_SLUGS, season_label(number), TITLE
        ) == slug, f"season {number} did not resolve to {slug}"


def test_a_real_spin_off_competes_honestly_and_a_tie_asks():
    """Season 2 is genuinely ambiguous on this listing, and MEASURED rather than
    assumed — the first version of this test expected two entries and the real
    data returned three:

        my-hero-academia-2-l3eyd                    the season
        my-hero-academia-vigilantes-season-2-5uow1  a real spin-off's season 2
        my-hero-academia-season-2-hero-notebook-…   an extra tied to season 2

    All three belong to the franchise and all three carry a 2, so nothing here
    can separate them — and nothing should try. The "hero notebook" extra is
    deliberately NOT caught by the companion filter: it is named by its own
    title, not by a format word, and a per-name list is exactly what this
    codebase forbids. Code never picks between real equals; it asks."""
    tied = browse_season.top_entries(REAL_SLUGS, "Season 2", TITLE)
    assert set(tied) == {
        SLUG_S2,
        SLUG_VIGILANTES_S2,
        "my-hero-academia-season-2-hero-notebook-oyepe",
    }, f"tied = {tied}"
    assert browse_season.best_entry(REAL_SLUGS, "Season 2", TITLE) is None


def test_a_season_the_site_does_not_carry_resolves_to_nothing():
    """Rather than forcing the nearest entry: no match means the model decides,
    with the season still in its goal. Scoring something wrong would be worse."""
    assert browse_season.best_entry(REAL_SLUGS, season_label(11), TITLE) is None


def test_the_web_resolved_season_name_path_is_untouched():
    """The 2026-08-07 latest-season behaviour must not move. Bleach's real
    listing carries TWO entries for The Calamity (a sub/dub pair), so this still
    ties and still defers — code never picks between real equals."""
    bleach = [
        "bleach-yaa9n",
        "bleach-the-movie-hell-verse-vi7ia",
        "bleach-thousand-year-blood-war-arc-2izxu",
        "bleach-thousand-year-blood-war-the-calamity-752db",
        "bleach-thousand-year-blood-war-the-calamity-xdf5",
    ]
    assert browse_season.best_entry(bleach, "The Calamity", "bleach") is None
    assert browse_season.top_entries(bleach, "The Calamity", "bleach") == [
        "bleach-thousand-year-blood-war-the-calamity-752db",
        "bleach-thousand-year-blood-war-the-calamity-xdf5",
    ]


def test_a_franchise_actually_called_the_movie_is_not_self_excluded():
    """The companion filter discounts words from the SERIES TITLE. Otherwise a
    franchise named "… the Movie" would have every entry excluded, and scoring
    nothing is a worse answer than scoring everything."""
    assert browse_season.is_companion_release(
        "detective-conan-the-movie-25", "detective conan the movie"
    ) is False
    assert browse_season.is_companion_release(
        "detective-conan-the-movie-25", "detective conan"
    ) is True


# ------------------------------------------------------------ through the loop
GOAL = "Find My Hero Academia Season 4 Episode 4 on anikoto and play it"
USER_WORDS = "play ep 4 of season 4 of my hero academia on anikoto"


def _episode_page(slug, number, title_number=None):
    """A watch page, with the title↔URL agreement _current_episode requires as
    proof of which episode we are on. anikoto redirects a series entry straight
    to its first episode, which is what makes the whole chain deterministic."""
    shown = number if title_number is None else title_number
    return _page(
        [_el(1, "link", f"Episode {number}", href=f"/watch/{slug}/ep-{number}")],
        url=f"https://anikoto.cz/watch/{slug}/ep-{number}",
        title=f"Anikoto - My Hero Academia 4 Episode {shown} Watch Anime Online",
    )


@pytest.mark.asyncio
async def test_the_loop_reaches_season_4_episode_4_with_no_llm_call():
    """THE INCIDENT'S GOAL, end to end and entirely in code: pick the season
    entry out of ten (three of them films carrying the digit 4), follow the
    site's redirect to episode 1, swap to episode 4, done.

    Zero LLM calls is the claim. The season is named, the page carries exactly
    one entry for it, and the episode number is the user's own — there is
    nothing left to decide anywhere in the chain."""
    page = ScriptedPage(
        [
            _results_page(),
            _episode_page(SLUG_S4, 1),   # the site's redirect
            _episode_page(SLUG_S4, 4),   # where the goal was going
        ]
    )
    session = FakeSession(page)
    provider = FakeProvider([])

    out = await run_browse(session, GOAL, provider=provider, intent_text=USER_WORDS)

    gotos = [v or "" for _, _, k, v in page.acted if k == "goto"]
    assert any(SLUG_S4 in g for g in gotos), f"never opened season 4: {page.acted}"
    assert not any(MOVIE_4 in g for g in gotos), "opened the FILM"
    assert any(f"{SLUG_S4}/ep-4" in g for g in gotos), f"never reached ep 4: {gotos}"
    assert out.success is True
    assert provider.calls == 0, "the whole chain was supposed to be deterministic"


@pytest.mark.asyncio
async def test_the_episode_is_never_swapped_into_the_wrong_season():
    """⚠️ THE GUARD THAT MATTERS MORE THAN THE FEATURE. _episode_action swaps the
    target number into WHATEVER url it is on, so a "season 4 episode 4" goal
    sitting on SEASON 1 episode 1 would navigate to season 1 EPISODE 4 and report
    success — a different show, reported as done."""
    wrong_season = _page(
        [_el(1, "link", "Episode 1", href=f"/watch/{SLUG_S2}/ep-1")],
        url=f"https://anikoto.cz/watch/{SLUG_S2}/ep-1",
        title="Anikoto - My Hero Academia 2 Episode 1 Watch Anime Online",
    )
    page = ScriptedPage([wrong_season, wrong_season])
    session = FakeSession(page)
    provider = FakeProvider(['{"action":"done","reason":"x"}'] * 3)

    await run_browse(session, GOAL, provider=provider, intent_text=USER_WORDS)

    gotos = [v or "" for _, _, k, v in page.acted if k == "goto"]
    assert not any(f"{SLUG_S2}/ep-4" in g for g in gotos), (
        f"navigated to episode 4 of the WRONG season: {gotos}"
    )


@pytest.mark.asyncio
async def test_the_episode_swap_still_works_once_the_season_is_right():
    """The regression: on the season the goal named, the episode leg is free to
    do its job. Holding it everywhere would break the feature it guards."""
    on_target = _page(
        [_el(1, "link", "Episode 1", href=f"/watch/{SLUG_S4}/ep-1")],
        url=f"https://anikoto.cz/watch/{SLUG_S4}/ep-1",
        title="Anikoto - My Hero Academia 4 Episode 1 Watch Anime Online",
    )
    landed = _page(
        [_el(1, "link", "Episode 4", href=f"/watch/{SLUG_S4}/ep-4")],
        url=f"https://anikoto.cz/watch/{SLUG_S4}/ep-4",
        title="Anikoto - My Hero Academia 4 Episode 4 Watch Anime Online",
    )
    page = ScriptedPage([on_target, landed])
    session = FakeSession(page)
    provider = FakeProvider([])

    out = await run_browse(session, GOAL, provider=provider, intent_text=USER_WORDS)

    gotos = [v or "" for _, _, k, v in page.acted if k == "goto"]
    assert any(f"{SLUG_S4}/ep-4" in g for g in gotos), f"never reached ep 4: {gotos}"
    assert out.success is True
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_a_tie_asks_instead_of_guessing():
    """A site that lists one season twice (anikoto carries a sub and a dub copy
    of the same cour). Code never picks between real equals — it stops and asks,
    with the page's own labels as the options."""
    duplicated = [SLUG_S4, "my-hero-academia-4-dub-x9a1", MOVIE_4]
    page = ScriptedPage([_results_page(duplicated)])
    session = FakeSession(page)
    provider = FakeProvider(['{"action":"done","reason":"x"}'] * 3)

    out = await run_browse(session, GOAL, provider=provider, intent_text=USER_WORDS)

    assert out.target_choice_required is True
    assert out.choice_kind == "season"
    assert out.choice_target == "Season 4"
    assert len(out.choice_options) == 2, f"offered {out.choice_options}"
    # The options are the page's OWN labels, and the film is not among them.
    assert not any("Movie" in o for o in out.choice_options)


@pytest.mark.asyncio
async def test_an_answered_choice_is_clicked_not_re_asked():
    """⚠️ THE RESUME. The entries are still tied — that is WHY they were asked
    about — so a season leg that ran ahead of the answer would ask the same
    question again until the budget gave up. The user's own words outrank every
    deterministic guess, so the pick is clicked in code."""
    duplicated = [SLUG_S4, "my-hero-academia-4-dub-x9a1", MOVIE_4]
    page = ScriptedPage([_results_page(duplicated), _page([], url=f"https://anikoto.cz/watch/{SLUG_S4}")])
    session = FakeSession(page)
    provider = FakeProvider(['{"action":"done","reason":"x"}'] * 3)

    out = await run_browse(
        session,
        GOAL,
        provider=provider,
        intent_text=USER_WORDS,
        chosen_target="My Hero Academia 4 Mt2J9",
    )

    assert out.target_choice_required is False, "it re-asked a question just answered"
    # `_act` opens a LINK by NAVIGATING to its href — a GET — rather than by a JS
    # click, which its own docstring states and which is load-bearing on SPA
    # sites. So the evidence the pick was honoured is the navigation.
    gotos = [v or "" for _, _, k, v in page.acted if k == "goto"]
    assert any(SLUG_S4 in g for g in gotos), (
        f"the chosen entry was never opened: {page.acted}"
    )


@pytest.mark.asyncio
async def test_a_goal_with_no_season_is_untouched():
    """The whole feature costs nothing when the goal names no season — every
    pre-existing path behaves exactly as before."""
    page = ScriptedPage([_results_page()])
    session = FakeSession(page)
    provider = FakeProvider(['{"action":"done","reason":"x"}'])

    out = await run_browse(
        session,
        "Find My Hero Academia on anikoto",
        provider=provider,
        intent_text="play my hero academia on anikoto",
    )

    assert out.target_choice_required is False
