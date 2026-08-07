"""
Jarvis OS — Structured series/season lookup (2026-08-07)

"Which season of X is current, and what episode is it on?" answered from a
CATALOG API instead of read out of search prose. Zero LLM calls, zero
fabrication surface: every value this module can return is a field of a JSON
response from a database whose entire job is to know this.

WHY THIS REPLACED READING SNIPPETS AS THE PRIMARY SOURCE
--------------------------------------------------------
`season.py` shipped on 2026-08-07 asking a temp-0 model to read six search
results. It was the right instrument for a prose question and it is KEPT as the
fallback — but the live run that followed measured three separate ways for it to
not answer at all, none of them a bug in its own logic:

    16:59:47  the episode-number search settled first and the loop jumped
              to /watch/bleach-yaa9n/ep-304 before any season was known
    16:59:49  the season search only STARTED here — two seconds too late
    16:59:55  "deepseek could not be reached ... (ReadError)"
              "Cannot send a request, as the client has been closed."

The read needs a search (~2s) plus an 8192-token reasoning call (MEASURED at
30-60s), and it needs the browse-local provider to still be alive when it lands.
A catalog API needs one HTTP round trip and no provider at all, which removes
the race, the lifecycle coupling and the token-budget fragility together.

It is also simply more accurate. MEASURED against the live services on
2026-08-07 for the incident's own title:

    web snippets, max("episode N")   -> 304 / 343 / 380   (watch-order listicles)
    LLM read of snippets             -> name 5/5 stable, episode None/1/1/1/1
    AniList                          -> "BLEACH: Thousand-Year Blood War -
                                         The Calamity", episode 2
    the true answer                  -> The Calamity, episode 2

WHAT EACH SOURCE IS FOR
-----------------------
AniList (anime) needs NO API KEY and is the reason this ships working out of the
box. Anime is also where the problem is hardest: a catalog lists each cour as a
SEPARATE entry with its own numbering restarting at 1, which is exactly the shape
that made the tightest-slug rule pick the 2004 original.

TMDb (live-action TV) nests seasons under one show and needs a free key. It is
key-gated and returns None when unset, so a default install is unaffected.

⚠️ TMDb IS NOT LIVE-VERIFIED. No key was available on this machine (an
unauthenticated call returns a clean 401, which is all that could be confirmed),
so its response handling is written to the documented shape and covered
hermetically only. AniList is verified against the real service.

THE EPISODE NUMBER IS A CROSS-CHECK, NOT A TARGET
-------------------------------------------------
Per-cour vs absolute numbering is a SITE convention no catalog reports, and a
site may simply not have uploaded the newest episode yet. So the page still
decides which episode to open; this number only bounds it and flags a landing on
the wrong entry. That division — the world names the SEASON, the site counts the
EPISODES — is the whole design, and it is why a name that the site spells
differently is survivable while a wrong number is not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from loguru import logger

# One round trip each, on a path that runs concurrently with the browser opening
# a page. Short on purpose: a catalog that is slow is a catalog we do without.
REQUEST_TIMEOUT = 8.0

ANILIST_URL = "https://graphql.anilist.co"
TMDB_BASE = "https://api.themoviedb.org/3"

# How many catalog rows to consider. AniList's SEARCH_MATCH puts the franchise in
# the first few; 20 is enough to include every cour of a long-running series
# (Bleach has 12 entries) without paging.
SEARCH_ROWS = 20

# ⚠️ MEASURED 2026-08-07, and the gap is not close. This guard answers ONE
# question: did the catalog return the show we actually asked about, or did its
# fuzzy search hand back something unrelated? AniList indexes anime only, so a
# live-action query is the case that matters:
#
#     "breaking bad"     -> NO ROWS AT ALL
#     "stranger things"  -> NO ROWS AT ALL
#     "the office"       -> "OL Kaizou Kouza"            ratio  16.0   <- garbage
#     "bleach"           -> "Bleach"                     ratio 100.0
#     "one piece"        -> "ONE PIECE"                  ratio 100.0
#     "attack on titan"  -> "Attack on Titan"            ratio 100.0
#     "black clover"     -> "Black Clover"               ratio 100.0
#
# 60 sits between 16 and 100 with enormous margin both ways, and it is a FLOOR
# rather than a ranking — an abbreviation the catalog cannot match ("tybw")
# scores low, returns None, and the caller keeps the behaviour it had before this
# module existed. Failing closed is the safe direction here: no season scoping is
# today's behaviour, a WRONG season is a wrong navigation.
MATCH_FLOOR = 60.0

# A query shorter than this cannot be matched responsibly — "the" would score
# well against half a catalog.
MIN_QUERY_CHARS = 3

# Only these AniList formats are a "season". MOVIE/SPECIAL/OVA/MUSIC are
# companion releases, not the next season, and letting them win is how a
# "latest season" goal lands on a 2010 film. MEASURED: without this,
# "black clover" resolves to the 2019 ONA short "Squishy! Black Clover"
# instead of the 170-episode series.
SEASON_FORMATS = frozenset({"TV", "TV_SHORT"})

# ⚠️ A SEASON THAT HAS NOT AIRED IS NOT THE LATEST SEASON. MEASURED 2026-08-07,
# and this was a real trap in the first draft of the fallback:
#
#     "black clover"            newest by date -> "Black Clover Season 2"
#                                                 NOT_YET_RELEASED (Oct 2026)
#     "the dangers in my heart" newest by date -> "... 3rd Season"
#                                                 NOT_YET_RELEASED (2027)
#
# Navigating to a season with zero episodes in existence is a guaranteed dead
# run. Only these two statuses describe something a person can actually watch.
WATCHABLE = frozenset({"RELEASING", "FINISHED"})

_TOKEN_RE = re.compile(r"[^a-z0-9]+")

# Injectable transport seams (the GMAIL_SERVICE_FACTORY / HTTP_FETCH_FACTORY
# pattern): tests swap these and the suite never reaches the network. Each is an
# async callable; None means "build the real one".
ANILIST_FACTORY: Optional[Callable[..., Any]] = None
TMDB_FACTORY: Optional[Callable[..., Any]] = None


@dataclass(frozen=True)
class SeasonFacts:
    """One catalog's answer about what is current.

    `season_name` is the load-bearing field — it is what picks the entry on the
    streaming site. `episode` is a cross-check (see the module docstring).
    `series_title` is the catalog's canonical name for the FRANCHISE, which is
    what the caller excludes when scoring entries, since every entry carries it.
    """

    season_name: str
    episode: Optional[int]
    series_title: str
    source: str  # "anilist" | "tmdb"
    status: str  # "RELEASING" | "FINISHED"


def _tokens(text: str) -> set[str]:
    """Lowercased alphanumeric tokens, keeping bare digits (a season number is
    often the only discriminator — "Season 2" vs "Season 3")."""
    return {
        t for t in _TOKEN_RE.split((text or "").lower()) if len(t) > 1 or t.isdigit()
    }


def _match_score(query: str, *titles: Optional[str]) -> float:
    """How well the catalog's title(s) match what was asked for, 0-100.

    Plain ratio against each available title, best wins. Deliberately NOT
    partial_ratio: a short query partial-matches a long unrelated title far too
    easily, and this guard exists precisely to catch "the office" answered with
    "OL Kaizou Kouza"."""
    try:
        from rapidfuzz import fuzz
    except Exception:  # pragma: no cover - rapidfuzz is a hard dependency
        return 0.0
    q = (query or "").strip().lower()
    if not q:
        return 0.0
    best = 0.0
    for title in titles:
        if title:
            best = max(best, float(fuzz.ratio(q, title.strip().lower())))
    return best


def _same_franchise(series_title: str, season_title: str) -> bool:
    """Is this season entry part of the series we matched?

    Every token of the canonical series title must appear in the season entry's
    title. "Bleach" ⊆ "BLEACH: Thousand-Year Blood War - The Calamity" holds; an
    unrelated show that happened to be RELEASING in the same result page does
    not. Cheap, and it is the guard that lets the RELEASING filter be trusted."""
    base = _tokens(series_title)
    if not base:
        return False
    return base <= _tokens(season_title)


# ------------------------------------------------------------------- AniList

_ANILIST_QUERY = """
query ($search: String, $perPage: Int) {
  Page(page: 1, perPage: $perPage) {
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
      title { romaji english }
      format
      status
      episodes
      startDate { year month day }
      nextAiringEpisode { episode }
    }
  }
}
"""


async def _anilist_fetch(search: str) -> list[dict]:
    """The raw media rows for `search`, or []. Never raises."""
    if ANILIST_FACTORY is not None:
        return list(await ANILIST_FACTORY(search) or [])
    import httpx

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.post(
            ANILIST_URL,
            json={
                "query": _ANILIST_QUERY,
                "variables": {"search": search, "perPage": SEARCH_ROWS},
            },
        )
        response.raise_for_status()
        payload = response.json()
    rows = (((payload or {}).get("data") or {}).get("Page") or {}).get("media")
    return list(rows or [])


def _row_title(row: dict) -> str:
    """An entry's display title — English when the catalog has one, else romaji.

    English first because that is what an English-language streaming catalog
    slugs its entries with, and the slug is what this name has to match."""
    title = (row or {}).get("title") or {}
    return str(title.get("english") or title.get("romaji") or "").strip()


def _start_key(row: dict) -> tuple[int, int, int]:
    date = (row or {}).get("startDate") or {}
    return (
        int(date.get("year") or 0),
        int(date.get("month") or 0),
        int(date.get("day") or 0),
    )


def _anilist_episode(row: dict) -> Optional[int]:
    """The latest AIRED episode of this entry, or None.

    While an entry is RELEASING the catalog reports the NEXT episode to air, so
    the newest one a person can watch is one before it — MEASURED: Bleach's
    Calamity cour reports next=3, and the answer is 2. A FINISHED entry has all
    of its episodes, so the count IS the latest.

    ⚠️ `episodes` IS THE PLANNED TOTAL, NOT THE AIRED COUNT, so it is only an
    answer once nothing is still to air. Bleach's Calamity cour reports
    episodes=10 while exactly 2 have aired; a version of this that fell through to
    `episodes` whenever the next-episode arithmetic came up empty would report 10
    for a season that had just premiered — a number the site cannot have, aimed at
    by a leg whose whole job is to reach the newest one. Caught by
    test_a_first_episode_still_airing_reports_no_episode."""
    airing = (row or {}).get("nextAiringEpisode") or {}
    nxt = airing.get("episode")
    if isinstance(nxt, int) and not isinstance(nxt, bool):
        # Something is still to air: everything before it has, and nothing else
        # in this row can improve on that.
        return nxt - 1 if nxt > 1 else None
    total = (row or {}).get("episodes")
    if isinstance(total, int) and not isinstance(total, bool) and total > 0:
        return total
    return None


def select_anilist_season(rows: list[dict], query: str) -> Optional[SeasonFacts]:
    """Pick the current season out of AniList rows. Pure, deterministic, no I/O.

    Order of decisions, each one measured:

    1. The catalog must have answered about the right show at all (MATCH_FLOOR
       against the best-matching row). A live-action query returns either no
       rows or an unrelated one; both end here.
    2. Only watchable TV entries of the SAME franchise are candidates —
       NOT_YET_RELEASED seasons and companion movies are excluded by
       construction, not by ranking.
    3. A RELEASING entry IS the current season, and there is normally exactly
       one. This is the case the incident needed and the one that measured
       unambiguously right.
    4. Otherwise the most recently STARTED entry is the latest season.
    """
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    if not rows or len((query or "").strip()) < MIN_QUERY_CHARS:
        return None

    # 1. Did we get the right show? Score every row and keep the best — the
    #    franchise's canonical entry is not always first.
    best_row: Optional[dict] = None
    best_score = 0.0
    for row in rows:
        title = (row.get("title") or {})
        score = _match_score(query, title.get("english"), title.get("romaji"))
        if score > best_score:
            best_score, best_row = score, row
    if best_row is None or best_score < MATCH_FLOOR:
        return None
    series_title = _row_title(best_row)
    if not series_title:
        return None

    # 2. Watchable seasons of this franchise only.
    candidates = [
        row
        for row in rows
        if row.get("format") in SEASON_FORMATS
        and row.get("status") in WATCHABLE
        and _same_franchise(series_title, _row_title(row))
        and _row_title(row)
    ]
    if not candidates:
        return None

    # 3. Airing now wins outright; 4. else the most recent start.
    releasing = [row for row in candidates if row.get("status") == "RELEASING"]
    pool = releasing or candidates
    chosen = max(pool, key=_start_key)

    return SeasonFacts(
        season_name=_row_title(chosen),
        episode=_anilist_episode(chosen),
        series_title=series_title,
        source="anilist",
        status=str(chosen.get("status") or ""),
    )


async def resolve_anilist(title: str) -> Optional[SeasonFacts]:
    """AniList's answer for `title`, or None. Never raises."""
    try:
        rows = await _anilist_fetch(title)
    except Exception as e:
        logger.info(f"browse: AniList lookup failed ({e}) — falling back")
        return None
    return select_anilist_season(rows, title)


# ---------------------------------------------------------------------- TMDb

async def _tmdb_get(path: str, params: dict) -> dict:
    """One TMDb GET, or {}. Never raises."""
    if TMDB_FACTORY is not None:
        return dict(await TMDB_FACTORY(path, params) or {})
    from app.core.config import settings

    key = (getattr(settings, "TMDB_API_KEY", "") or "").strip()
    if not key:
        return {}
    import httpx

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.get(
            f"{TMDB_BASE}{path}", params={**params, "api_key": key}
        )
        response.raise_for_status()
        return dict(response.json() or {})


def select_tmdb_season(detail: dict, query: str) -> Optional[SeasonFacts]:
    """Pick the current season out of a TMDb `/tv/{id}` payload. Pure.

    TMDb nests every season under one show and reports `last_episode_to_air`
    directly, so there is no cour-selection problem to solve — only a name to
    look up. A TMDb season name is often the bare "Season 4", which is a thin
    discriminator for matching a streaming slug; the season NUMBER survives
    tokenisation and is what actually does the matching."""
    detail = detail if isinstance(detail, dict) else {}
    series_title = str(detail.get("name") or detail.get("original_name") or "").strip()
    if not series_title:
        return None
    if _match_score(query, series_title, detail.get("original_name")) < MATCH_FLOOR:
        return None

    last = detail.get("last_episode_to_air") or {}
    season_number = last.get("season_number")
    episode_number = last.get("episode_number")
    if not isinstance(season_number, int) or isinstance(season_number, bool):
        return None

    season_name = ""
    for season in detail.get("seasons") or []:
        if isinstance(season, dict) and season.get("season_number") == season_number:
            season_name = str(season.get("name") or "").strip()
            break
    if not season_name:
        season_name = f"Season {season_number}"

    episode: Optional[int] = None
    if isinstance(episode_number, int) and not isinstance(episode_number, bool):
        if episode_number > 0:
            episode = episode_number

    in_production = bool(detail.get("in_production"))
    return SeasonFacts(
        season_name=season_name,
        episode=episode,
        series_title=series_title,
        source="tmdb",
        status="RELEASING" if in_production else "FINISHED",
    )


async def resolve_tmdb(title: str) -> Optional[SeasonFacts]:
    """TMDb's answer for `title`, or None (including when no key is set)."""
    try:
        found = await _tmdb_get("/search/tv", {"query": title})
        results = [r for r in (found.get("results") or []) if isinstance(r, dict)]
        if not results:
            return None
        best = max(
            results,
            key=lambda r: _match_score(title, r.get("name"), r.get("original_name")),
        )
        series_id = best.get("id")
        if not isinstance(series_id, int) or isinstance(series_id, bool):
            return None
        detail = await _tmdb_get(f"/tv/{series_id}", {})
        if not detail:
            return None
    except Exception as e:
        logger.info(f"browse: TMDb lookup failed ({e}) — falling back")
        return None
    return select_tmdb_season(detail, title)


# ------------------------------------------------------------------ dispatch

async def resolve_season(title: str) -> Optional[SeasonFacts]:
    """The current season of `title` from a catalog API, or None.

    AniList first because it needs no key and covers the case this exists for;
    TMDb second and only when a key is configured. Whichever answers first with
    a season wins — there is no adjudication between them, because a title that
    both know is a title AniList already got right, and asking two catalogs to
    agree would only ever turn an answer into a None.

    Best-effort in every direction. NEVER raises."""
    title = (title or "").strip()
    if len(title) < MIN_QUERY_CHARS:
        return None

    for resolve in (resolve_anilist, resolve_tmdb):
        try:
            facts = await resolve(title)
        except Exception as e:  # a resolver is already guarded; this is the belt
            logger.info(f"browse: catalog lookup raised ({e}) — falling back")
            continue
        if facts is not None and facts.season_name:
            logger.info(
                f"browse: {facts.source} says the current season of {title!r} is "
                f"{facts.season_name!r}"
                + (
                    f", episode {facts.episode}"
                    if facts.episode is not None
                    else " (episode unknown)"
                )
            )
            return facts
    return None
