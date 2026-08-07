"""Structured season lookup (app/browser/series_api.py).

⚠️ EVERY AniList FIXTURE IN THIS FILE IS REAL. The rows below were fetched from
the live service on 2026-08-07 and pasted verbatim, because a fixture nobody
checked against the real shape is a claim, not a test — the lesson this codebase
has now recorded four times (2026-07-17 fan-out tests that bypassed the planner,
07-30 bulk tests that drove the singular tool, 08-01 a page fake returning one
object twice, 08-02 grid fakes with no quick-add). Both measured traps are
present in the real data and neither would appear in a fixture written from
imagination:

    black clover   -> the NEWEST entry by date is NOT_YET_RELEASED (Oct 2026)
                      and the second newest is a MOVIE (2023)
    the office     -> AniList answers a live-action query with unrelated anime
"""

from __future__ import annotations

import pytest

from app.browser import season as browse_season
from app.browser import series_api


def _row(english, fmt, status, episodes, start, next_ep=None, romaji=None):
    year, month, day = start
    return {
        "title": {"english": english, "romaji": romaji or english},
        "format": fmt,
        "status": status,
        "episodes": episodes,
        "startDate": {"year": year, "month": month, "day": day},
        "nextAiringEpisode": {"episode": next_ep} if next_ep else None,
    }


# --- REAL, fetched 2026-08-07 -------------------------------------------------
BLEACH_ROWS = [
    _row("Bleach", "TV", "FINISHED", 366, (2004, 10, 5)),
    _row("BLEACH: Thousand-Year Blood War", "TV", "FINISHED", 13, (2022, 10, 11)),
    _row("BLEACH: Thousand-Year Blood War - The Conflict", "TV", "FINISHED", 14, (2024, 10, 5)),
    _row("BLEACH: Thousand-Year Blood War - The Separation", "TV", "FINISHED", 13, (2023, 7, 8)),
    _row("BLEACH: Thousand-Year Blood War - The Calamity", "TV", "RELEASING", 10, (2026, 7, 25), 3),
    _row("BLEACH 20th Anime Anniversary Official Trailer", "SPECIAL", "FINISHED", 1, (2024, 10, 5)),
    _row("Bleach the Movie: Hell Verse", "MOVIE", "FINISHED", 1, (2010, 12, 4)),
    _row("Bleach the Movie: Fade to Black", "MOVIE", "FINISHED", 1, (2008, 12, 13)),
]

BLACK_CLOVER_ROWS = [
    _row("Black Clover", "TV", "FINISHED", 170, (2017, 10, 3)),
    _row("Petit Clover Advance", "SPECIAL", "FINISHED", 15, (2018, 2, 23)),
    _row("Black Clover: Sword of the Wizard King", "MOVIE", "FINISHED", 1, (2023, 6, 16)),
    _row("Squishy! Black Clover", "ONA", "FINISHED", 8, (2019, 7, 1)),
    _row("Black Clover Season 2", "TV", "NOT_YET_RELEASED", None, (2026, 10, None)),
]

THE_OFFICE_ROWS = [
    _row("OL Kaizou Kouza", "OVA", "FINISHED", 1, (1999, 1, 1)),
    _row("Kenji no Trunk", "MOVIE", "FINISHED", 1, (2009, 1, 1)),
    _row("Office Work", "ONA", "FINISHED", 1, (2016, 1, 1)),
]

# The real anikoto listing for "bleach", fetched the same day.
ANIKOTO_SLUGS = [
    "bleach-thousand-year-blood-war-the-calamity-752db",
    "bleach-yaa9n",
    "bleach-the-movie-the-diamonddust-rebellion-hhatz",
    "bleach-the-sealed-sword-frenzy-jp2bb",
    "bleach-the-movie-fade-to-black-hudbv",
    "bleach-the-movie-memories-of-nobody-jp3eo",
    "bleach-thousand-year-blood-war-the-separation-zvwg0",
    "bleach-thousand-year-blood-war-arc-2izxu",
    "bleach-the-movie-hell-verse-vi7ia",
    "bleach-thousand-year-blood-war-the-conflict-sqamb",
    "bleach-memories-in-the-rain-yqrxb",
]


# ============================================================ the incident

def test_the_incident_resolves_to_the_calamity():
    """THE INCIDENT, frozen. 'play latest ep of latest season of bleach' opened
    Bleach (2004) and played episode 304. Against the real catalog rows the
    answer is the cour that is actually airing, and the episode is 2 — which is
    what Google returned for the same question."""
    facts = series_api.select_anilist_season(BLEACH_ROWS, "bleach")

    assert facts is not None
    assert facts.season_name == "BLEACH: Thousand-Year Blood War - The Calamity"
    assert facts.episode == 2
    assert facts.status == "RELEASING"
    assert facts.series_title == "Bleach"
    assert facts.source == "anilist"


def test_the_wrong_entry_scores_zero_against_the_resolved_season():
    """The end-to-end property the fix rests on: the season name the catalog
    returns must PICK the right anikoto entry out of the real listing, and must
    give the incident's own wrong answer a score of zero.

    Both halves matter. A name that merely 'looks right' but scores every entry
    equally would leave the loop exactly where it was."""
    facts = series_api.select_anilist_season(BLEACH_ROWS, "bleach")
    assert facts is not None

    scores = {
        slug: browse_season.score_entry(
            slug.replace("-", " "), facts.season_name, facts.series_title
        )
        for slug in ANIKOTO_SLUGS
    }

    # The entry the incident opened is not merely outranked — it matches nothing.
    assert scores["bleach-yaa9n"] == 0
    # The right one wins outright over every other cour of the same franchise.
    target = scores["bleach-thousand-year-blood-war-the-calamity-752db"]
    assert target == 5
    assert all(v < target for k, v in scores.items() if "calamity" not in k)

    winner = browse_season.best_entry(
        ANIKOTO_SLUGS,
        facts.season_name,
        facts.series_title,
        key=lambda c: c.replace("-", " "),
    )
    assert winner == "bleach-thousand-year-blood-war-the-calamity-752db"


# ============================================================ the measured traps

def test_an_unaired_season_is_never_the_latest_season():
    """MEASURED trap. 'Black Clover Season 2' is the newest entry by start date
    in the REAL rows and has NOT_YET_RELEASED status — zero episodes exist. A
    naive newest-wins rule picks it and the browse then hunts a season the site
    cannot possibly carry."""
    facts = series_api.select_anilist_season(BLACK_CLOVER_ROWS, "black clover")

    assert facts is not None
    assert facts.season_name == "Black Clover"
    assert facts.episode == 170


def test_a_companion_release_is_never_a_season():
    """MEASURED trap, same real rows: a 2023 MOVIE and a 2019 ONA short are both
    newer than the 2017 series. Neither is 'the latest season'."""
    facts = series_api.select_anilist_season(BLACK_CLOVER_ROWS, "black clover")

    assert facts is not None
    assert "Squishy" not in facts.season_name
    assert "Sword of the Wizard King" not in facts.season_name


def test_a_live_action_query_is_refused():
    """AniList indexes anime only, and its fuzzy search answers ANYTHING. The
    real reply to 'the office' is an unrelated 1999 OVA. Returning it would scope
    a browse to a show the user never mentioned."""
    assert series_api.select_anilist_season(THE_OFFICE_ROWS, "the office") is None


def test_the_match_floor_alone_refuses_an_unrelated_show():
    """⚠️ THE ISOLATED case for MATCH_FLOOR, and the reason it exists: the REAL
    'the office' rows are refused THREE times over — the floor, the format filter
    (OVA/MOVIE/ONA) and the franchise guard all reject them independently — so
    reverting the floor changed nothing and the falsification came back green.
    A falsification must remove the GUARANTEE, not one of several copies of it.

    Here the only defence is the floor: a single TV entry, airing now, whose own
    title is what the franchise guard would be checked against."""
    rows = [_row("Completely Unrelated Anime", "TV", "RELEASING", 12, (2026, 1, 1), 5)]

    assert series_api.select_anilist_season(rows, "the office") is None
    # ...and the identical rows ARE accepted for a query that matches them, so
    # the refusal above is the floor and not some other filter.
    assert series_api.select_anilist_season(rows, "completely unrelated anime") is not None


def test_the_measured_match_scores_are_pinned():
    """The numbers MATCH_FLOOR sits between, pinned so a floor change that would
    start admitting garbage fails loudly instead of quietly.

    Measured 2026-08-07 against the live service."""
    garbage = series_api._match_score("the office", "OL Kaizou Kouza")
    real = series_api._match_score("bleach", "Bleach")

    assert garbage == pytest.approx(16.0, abs=1.0)
    assert real == pytest.approx(100.0, abs=0.1)
    assert garbage < series_api.MATCH_FLOOR < real


# ============================================================ selection rules

def test_releasing_wins_over_a_newer_start_date():
    """An airing season IS the current season even when a later-STARTED entry
    exists — which is what makes RELEASING a filter and not a tiebreak.

    ⚠️ THE DATES MATTER, and the first version of this test got them wrong: its
    airing entry was ALSO the newest, so 'prefer releasing' and 'prefer newest'
    gave the same answer and the falsification came back green. The shape here is
    the real one — a long-running series that began years ago and is STILL airing
    (One Piece started in 1999), alongside a spin-off season that started later
    and has already finished. By date the spin-off wins; the right answer is the
    one still going out."""
    rows = [
        _row("Show", "TV", "RELEASING", None, (1999, 10, 20), 1173),
        _row("Show Side Stories", "TV", "FINISHED", 12, (2024, 1, 1)),
        _row("Show Season 3", "TV", "NOT_YET_RELEASED", None, (2030, 1, 1)),
    ]
    facts = series_api.select_anilist_season(rows, "show")

    assert facts is not None
    assert facts.season_name == "Show"
    assert facts.episode == 1172


def test_an_unrelated_releasing_row_cannot_win():
    """The franchise guard. A different show that happens to be airing and to
    appear in the same fuzzy result page must not be mistaken for this show's
    latest season."""
    rows = [
        _row("Show", "TV", "FINISHED", 12, (2020, 1, 1)),
        _row("Something Else Entirely", "TV", "RELEASING", 12, (2024, 1, 1), 5),
    ]
    facts = series_api.select_anilist_season(rows, "show")

    assert facts is not None
    assert facts.season_name == "Show"


def test_episode_is_one_before_the_next_airing():
    """While a cour airs, the catalog reports the NEXT episode. The newest one a
    person can watch is the one before it — MEASURED: Bleach reports next=3 and
    the answer is 2."""
    rows = [_row("Show", "TV", "RELEASING", 12, (2024, 1, 1), 7)]
    facts = series_api.select_anilist_season(rows, "show")

    assert facts is not None and facts.episode == 6


def test_a_finished_seasons_episode_is_its_full_count():
    rows = [_row("Show", "TV", "FINISHED", 13, (2024, 1, 1))]
    facts = series_api.select_anilist_season(rows, "show")

    assert facts is not None and facts.episode == 13


def test_a_first_episode_still_airing_reports_no_episode():
    """next=1 means nothing has aired yet. Reporting 0 would be a number the site
    can never match; None lets the page decide."""
    rows = [_row("Show", "TV", "RELEASING", 12, (2024, 1, 1), 1)]
    facts = series_api.select_anilist_season(rows, "show")

    assert facts is not None and facts.episode is None


@pytest.mark.parametrize("query", ["", "  ", "ab"])
def test_a_query_too_short_to_match_is_refused(query):
    assert series_api.select_anilist_season(BLEACH_ROWS, query) is None


def test_no_rows_is_none():
    assert series_api.select_anilist_season([], "bleach") is None


# ============================================================ TMDb (hermetic)

TMDB_DETAIL = {
    "name": "Stranger Things",
    "in_production": True,
    "last_episode_to_air": {"season_number": 4, "episode_number": 8},
    "seasons": [
        {"season_number": 0, "name": "Specials"},
        {"season_number": 3, "name": "Season 3"},
        {"season_number": 4, "name": "Season 4"},
    ],
}


def test_tmdb_reads_the_last_episode_to_air():
    """⚠️ HERMETIC ONLY — no TMDb key was available when this shipped, so this
    pins the DOCUMENTED response shape, not a verified live one."""
    facts = series_api.select_tmdb_season(TMDB_DETAIL, "stranger things")

    assert facts is not None
    assert facts.season_name == "Season 4"
    assert facts.episode == 8
    assert facts.source == "tmdb"
    assert facts.status == "RELEASING"


def test_tmdb_falls_back_to_a_synthesised_season_name():
    detail = dict(TMDB_DETAIL, seasons=[])
    facts = series_api.select_tmdb_season(detail, "stranger things")

    assert facts is not None and facts.season_name == "Season 4"


def test_tmdb_refuses_a_show_it_did_not_match():
    assert series_api.select_tmdb_season(TMDB_DETAIL, "breaking bad") is None


def test_tmdb_with_no_key_makes_no_request(monkeypatch):
    """Unset key = the leg is simply absent, with no network call attempted."""
    monkeypatch.setattr(series_api, "TMDB_FACTORY", None)
    from app.core.config import settings

    monkeypatch.setattr(settings, "TMDB_API_KEY", "", raising=False)

    import asyncio

    assert asyncio.get_event_loop_policy() is not None
    assert asyncio.run(series_api._tmdb_get("/search/tv", {"query": "x"})) == {}


# ============================================================ dispatch + safety

async def test_anilist_answers_and_tmdb_is_never_consulted(monkeypatch):
    """Whichever catalog answers first wins; there is no adjudication."""
    calls = {"tmdb": 0}

    async def _anilist(search):
        return BLEACH_ROWS

    async def _tmdb(path, params):
        calls["tmdb"] += 1
        return {}

    monkeypatch.setattr(series_api, "ANILIST_FACTORY", _anilist)
    monkeypatch.setattr(series_api, "TMDB_FACTORY", _tmdb)

    facts = await series_api.resolve_season("bleach")

    assert facts is not None and facts.source == "anilist"
    assert calls["tmdb"] == 0


async def test_tmdb_is_consulted_when_anilist_has_nothing(monkeypatch):
    async def _anilist(search):
        return []

    async def _tmdb(path, params):
        return {"results": [{"id": 66732, "name": "Stranger Things"}]} if "search" in path else TMDB_DETAIL

    monkeypatch.setattr(series_api, "ANILIST_FACTORY", _anilist)
    monkeypatch.setattr(series_api, "TMDB_FACTORY", _tmdb)

    facts = await series_api.resolve_season("stranger things")

    assert facts is not None and facts.source == "tmdb"
    assert facts.season_name == "Season 4"


async def test_a_catalog_that_blows_up_never_breaks_a_browse(monkeypatch):
    """Best-effort in every direction: the browse must keep the behaviour it had
    before this module existed, never inherit its failure."""

    async def _boom(*a, **k):
        raise RuntimeError("catalog on fire")

    monkeypatch.setattr(series_api, "ANILIST_FACTORY", _boom)
    monkeypatch.setattr(series_api, "TMDB_FACTORY", _boom)

    assert await series_api.resolve_season("bleach") is None


async def test_a_malformed_payload_is_not_an_answer(monkeypatch):
    async def _junk(search):
        return [{"title": None}, "not a dict", {}]

    monkeypatch.setattr(series_api, "ANILIST_FACTORY", _junk)

    assert await series_api.resolve_season("bleach") is None
