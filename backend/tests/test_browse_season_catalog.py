"""The second latest-season incident (2026-08-07, round 2).

The round-1 fix shipped and the SAME prompt failed again, differently. The trace
(`~/.jarvis/logs/browse/2026-08-07_16-59-42_0cf8e91fd86b.jsonl`) and
`backend.log:17843-17855` name all three defects frozen here:

    16:59:47  web says the latest episode of 'bleach' is 304
    16:59:47  latest-episode series navigation -> /watch/bleach-yaa9n/ep-304
    16:59:49  ... the SEASON search only STARTS here, two seconds too late
    16:59:55  latest-season read failed (deepseek could not be reached (ReadError))
    16:59:55  latest-season read failed (Cannot send a request, as the client
              has been closed.)

So: an absolute episode number was fetched for a goal that asked for a SEASON and
used to dive into the 2004 series from the homepage; the season lookup could not
possibly have arrived in time; and when it did land, the provider it needed had
already been closed underneath it.
"""

from __future__ import annotations

from app.browser import loop as browser_loop
from app.browser import season as browse_season
from app.browser.loop import run_browse

from test_browse_latest_season import (  # the real, fetched fixtures
    PLANNER_GOAL,
    SEASON,
    SLUG_ORIGINAL,
    USER_WORDS,
    _results_page,
)
from test_browser_loop import FakeProvider, FakeSession, ScriptedPage, _el, _page

# Captured at import, BEFORE the autouse `_hermetic_season` fixture swaps it for
# a stub. Tests that need the genuine resolver (the catalog leg) use this.
_REAL_RESOLVE = browse_season.resolve_latest_season


def _homepage_with_one_bleach_link():
    """anikoto's homepage as the run actually observed it: a short page carrying
    exactly ONE Bleach link, the 2004 original. That uniqueness is what let
    `_latest_series_action` treat it as an unambiguous match and dive in."""
    return _page(
        [
            _el(1, "searchbox", "Search anime"),
            _el(2, "link", "Bleach", href=f"/watch/{SLUG_ORIGINAL}"),
            _el(3, "link", "Home", href="/"),
        ],
        url="https://anikoto.cz/",
        title="Anikoto - Stream Anime Online Free in HD",
    )


# ============================================================== the incident

async def test_the_second_incident_a_season_goal_never_jumps_to_the_original():
    """THE SECOND INCIDENT, frozen. With the web reporting episode 304 for
    'bleach', the loop jumped from the HOMEPAGE straight to
    /watch/bleach-yaa9n/ep-304 at step 0 — the 2004 series, before any season was
    known, and two seconds before the season search had even started.

    A "latest SEASON" goal asks for a cour whose episodes restart at 1, so an
    ABSOLUTE series number is meaningless to it by definition. Not fetching one
    denies `_latest_series_action` the input it needs to jump."""
    from app.tools import browser_tools

    real_search = browser_tools.SEARCH_PROVIDER_FACTORY
    searches: list[str] = []

    def _search(query, n):
        searches.append(query)
        return [
            {
                "title": "How to Watch Bleach in Order",
                "snippet": "Bleach Season 14 (Ep 300-316) ... Ep 343 ... episode 304",
                "content": "",
            }
        ]

    browser_tools.SEARCH_PROVIDER_FACTORY = _search
    page = ScriptedPage([_homepage_with_one_bleach_link(), _results_page()])
    try:
        await run_browse(
            FakeSession(page),
            PLANNER_GOAL,
            FakeProvider([]),
            intent_text="play latest ep of latest season of bleach on anikoto",
            max_actions=1,
        )
    finally:
        browser_tools.SEARCH_PROVIDER_FACTORY = real_search

    requested = [url for (_, _, kind, url) in page.acted if kind == "goto"]
    assert not any("ep-304" in (u or "") for u in requested), (
        f"jumped to the incident's own URL: {requested}"
    )
    assert not any(f"{SLUG_ORIGINAL}/ep-" in (u or "") for u in requested), (
        f"jumped into the 2004 series for a latest-SEASON goal: {requested}"
    )
    assert not any("latest episode number" in q for q in searches), (
        f"a season goal still paid for an absolute-episode search: {searches}"
    )


async def test_an_unscoped_latest_episode_goal_still_uses_the_web_number():
    """REGRESSION — the narrow gate must stay narrow. "the latest episode of X"
    names no season, so an absolute number is exactly right for it: that is the
    hidden 101-170 range case it was written for (live 2026-07-25)."""
    from app.tools import browser_tools

    real_search = browser_tools.SEARCH_PROVIDER_FACTORY
    searches: list[str] = []

    def _search(query, n):
        searches.append(query)
        return [{"title": "Black Clover", "snippet": "episode 170", "content": ""}]

    browser_tools.SEARCH_PROVIDER_FACTORY = _search
    page = ScriptedPage(
        [
            _page(
                [_el(1, "link", "Black Clover", href="/watch/black-clover-g7tjy")],
                url="https://site.test/",
                title="Home",
            ),
            _page(
                [],
                url="https://site.test/watch/black-clover-g7tjy/ep-170",
                title="Black Clover Ep 170",
            ),
        ]
    )
    try:
        await run_browse(
            FakeSession(page),
            "play the latest episode of black clover",
            FakeProvider([]),
            max_actions=1,
        )
    finally:
        browser_tools.SEARCH_PROVIDER_FACTORY = real_search

    assert any("latest episode number" in q for q in searches), (
        f"the unscoped path lost its web search: {searches}"
    )
    requested = [url for (_, _, kind, url) in page.acted if kind == "goto"]
    assert any("ep-170" in (u or "") for u in requested), requested


def _series_page_with_range(slug, title, episodes=366):
    """A series landing page carrying an episode-RANGE selector.

    ⚠️ THIS SHAPE IS LOAD-BEARING, and the first version of these two tests got it
    wrong. With the prose search denied, the only remaining source of a number on
    a page that is not itself an episode page is `_range_latest` — because
    `_href_latest_episode` requires the CURRENT url to be a /ep-N page, and on one
    of those `_current_episode` is set and this leg is skipped entirely. Built
    from sibling /ep-N links instead, both tests passed with the belt REMOVED:
    no number could ever exist, so nothing was being measured."""
    return _page(
        [
            _el(1, "button", f"001-{episodes}"),
            _el(2, "link", title, href=f"/watch/{slug}"),
        ],
        url=f"https://anikoto.cz/watch/{slug}",
        title=title,
    )


async def test_the_belt_blocks_a_jump_when_the_page_supplies_the_number():
    """The BELT. Denying the prose search removes the usual source of a number,
    but a page's own range selector supplies one, so the rule is stated here
    rather than left to follow from a missing input.

    We are on the 2004 series' page — 366 episodes, no link to the airing cour —
    with the season known. Without the belt, `_latest_series_action` reads 366 off
    the range control and navigates deeper into precisely the wrong entry. Step 0
    opens the range; step 1 is where the number exists and the belt must hold."""

    async def _known(title, provider, *, today="", rows=None):
        return browse_season.SeasonHint(name=SEASON, episode=2, source="anilist")

    browse_season.resolve_latest_season = _known
    page = ScriptedPage(
        [
            _series_page_with_range(SLUG_ORIGINAL, "Bleach"),
            _series_page_with_range(SLUG_ORIGINAL, "Bleach"),
        ]
    )
    await run_browse(
        FakeSession(page),
        PLANNER_GOAL,
        FakeProvider([]),
        intent_text=USER_WORDS,
        max_actions=2,
    )

    requested = [url for (_, _, kind, url) in page.acted if kind == "goto"]
    assert not any("ep-366" in (u or "") for u in requested), (
        f"the belt let a season goal dive into the wrong entry: {requested}"
    )


async def test_the_hold_releases_when_no_catalog_knows_the_series():
    """The hold must never become a hang. A title neither catalog carries settles
    to "unknown", and the leg then behaves exactly as it did before any of this
    existed — otherwise the fix would break every series the APIs do not list.

    Same two-step shape as the belt test above, and the same page: the ONLY
    difference is that no season is known, and that difference alone must let the
    navigation through."""
    from app.tools import browser_tools

    real_search = browser_tools.SEARCH_PROVIDER_FACTORY
    browser_tools.SEARCH_PROVIDER_FACTORY = lambda q, n: []
    page = ScriptedPage(
        [
            _series_page_with_range("obscure-show-x1", "Obscure Show", episodes=9),
            _series_page_with_range("obscure-show-x1", "Obscure Show", episodes=9),
        ]
    )
    try:
        await run_browse(
            FakeSession(page),
            "play the latest episode of the latest season of obscure show",
            FakeProvider([]),
            max_actions=2,
        )
    finally:
        browser_tools.SEARCH_PROVIDER_FACTORY = real_search

    requested = [url for (_, _, kind, url) in page.acted if kind == "goto"]
    assert any("ep-9" in (u or "") for u in requested), (
        f"an unknown series was held instead of falling through: {requested}"
    )


# ================================================== the catalog is asked first

_ANILIST_TWO_SEASONS = [
    {
        "title": {"english": "Show Season 3", "romaji": "Show Season 3"},
        "format": "TV",
        "status": "RELEASING",
        "episodes": 12,
        "startDate": {"year": 2026, "month": 7, "day": 1},
        "nextAiringEpisode": {"episode": 4},
    },
    {
        "title": {"english": "Show", "romaji": "Show"},
        "format": "TV",
        "status": "FINISHED",
        "episodes": 12,
        "startDate": {"year": 2020, "month": 1, "day": 1},
        "nextAiringEpisode": None,
    },
]


async def test_the_catalog_answers_with_no_provider_call(monkeypatch):
    """The whole point of the API leg: one HTTP round trip, no provider, no
    search, no 8192-token read — so it cannot lose the race it used to lose by
    30-60 seconds, and cannot be cut off when the provider closes."""
    from app.browser import series_api

    async def _anilist(search):
        return _ANILIST_TWO_SEASONS

    monkeypatch.setattr(series_api, "ANILIST_FACTORY", _anilist)
    provider = FakeProvider([])

    hint = await _REAL_RESOLVE("show", provider)

    assert hint is not None
    assert hint.name == "Show Season 3"
    assert hint.episode == 3
    assert hint.source == "anilist"
    assert provider.calls == 0, "the catalog leg spent a provider call"


async def test_the_prose_read_is_still_the_fallback(monkeypatch):
    """`series_api` covers anime and live-action TV. Anything neither catalogs —
    a web series, a film, a title their search cannot match — is exactly where
    reading prose remains the only instrument, so it must still run."""
    from app.browser import series_api

    async def _nothing(search):
        return []

    monkeypatch.setattr(series_api, "ANILIST_FACTORY", _nothing)
    provider = FakeProvider(['{"season": "The Calamity", "episode": 2}'])

    hint = await _REAL_RESOLVE(
        "bleach",
        provider,
        rows=[{"title": "Bleach news", "snippet": "The Calamity, episode 2 airs"}],
    )

    assert hint is not None
    assert hint.name == "The Calamity"
    assert hint.source == "web"
    assert provider.calls == 1


# =============================================================== the lifecycle

def test_background_lookups_are_cancelled_before_the_provider_closes():
    """The lifecycle fix. Both lookups are parked on the session so they are not
    garbage-collected mid-flight, and nothing used to end them — so a browse that
    stopped early (the live run hit a login wall eight seconds in) returned while
    a lookup was still using the httpx client the caller closes on the very next
    line."""
    import asyncio

    async def _run():
        async def _forever():
            await asyncio.sleep(3600)

        class _Session:
            pass

        session = _Session()
        session._season_task = asyncio.ensure_future(_forever())
        session._latest_ep_task = asyncio.ensure_future(_forever())
        tasks = [session._season_task, session._latest_ep_task]

        browser_loop.cancel_background_lookups(session)
        await asyncio.sleep(0)

        assert all(t.cancelled() or t.done() for t in tasks), (
            "a lookup outlived the browse and will be cut off mid-request "
            "when the provider closes"
        )
        assert session._season_task is None
        assert session._latest_ep_task is None

    asyncio.run(_run())


def test_cancelling_lookups_never_raises():
    """It runs in a `finally` on the teardown path: a cleanup that can throw
    there would mask the real outcome of the browse."""

    class _Odd:
        @property
        def _season_task(self):
            raise RuntimeError("nope")

    browser_loop.cancel_background_lookups(_Odd())
    browser_loop.cancel_background_lookups(object())


def test_the_cancel_is_wired_into_the_teardown_path():
    """⚠️ A WIRING GUARD. The two tests above prove the function works and say
    NOTHING about whether anything calls it — the shape that let a whole feature
    ship unfired under 1,578 green tests (2026-07-17) and that a green
    falsification caught again in the 2026-08-03 housekeeping round. It must be
    called BEFORE the provider is closed, or it fixes nothing.

    ⚠️ AND IT MUST LOOK FOR THE CALL, NOT THE NAME. The first version searched the
    module source for "cancel_background_lookups" and PASSED against a tree where
    the call had been replaced by `pass` — because the comment above it still
    mentioned the function. A wiring guard satisfied by a comment guards nothing;
    the falsification harness is what caught it."""
    import inspect

    from app.tools import browser_agent_tools

    source = inspect.getsource(browser_agent_tools)
    call = "browser_loop.cancel_background_lookups(session)"
    assert call in source, (
        "nothing calls the cleanup — the lookups can still outlive the provider"
    )
    assert source.index(call) < source.index("await provider.__aexit__"), (
        "the cleanup runs AFTER the provider closes, which is the bug it fixes"
    )
