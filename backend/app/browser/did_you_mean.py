"""
Jarvis OS — "did you mean…?" for a site that does not exist (2026-08-01)

THE INCIDENT
------------
Spoken: "Go to junaidjamshed.com and add Jhanan Sports 100 ML in cart."
Transcribed: "Go to **junitjamsheed.com** …". Chromium answered
ERR_NAME_NOT_RESOLVED, commit_flow turned it into a flat step failure, both
replans failed, and the task died. Google, given the identical misspelling,
puts the real site on the first screen. The user's question was exactly right:
why did Jarvis not ASK?

WHY THIS IS STRUCTURAL, NOT A NICETY
------------------------------------
Voice is a first-class input (Phase 7 STT, Phase 12 wake word / ambient). No
speech model can spell a proper noun it has never seen — "junaidjamshed" is a
Pakistani retail brand and is in no Whisper LM. So the moment voice became an
input path, "the domain in the goal is misspelled" stopped being user error and
became an EXPECTED failure mode of the browse stack. Its response was a dead
end with no recourse: the user had to notice the typo themselves and retype it.

WHY WE SUGGEST BUT NEVER NAVIGATE — the load-bearing rule
----------------------------------------------------------
browser/grounding.py holds the exfiltration bound: the sites a loop may visit
trace to the USER'S OWN WORDS and never to page or search content. A search
result IS search content. So auto-correcting — "close enough, go there" — would
route straight around that bound, and a poisoned or typosquatted result could
place a COMMIT flow on a site the user never named. BrowserUnreachable's own
docstring says it: a failure "is NOT an invitation to guess another address".

That rule bans GUESSING. It does not ban ASKING, and asking is what the whole
codebase already does with an ambiguity it cannot resolve in code:

    folder_resolver     two real folders named "downloads" -> ask, never pick
    _origin_approval    a page-derived origin              -> ask, fail-closed
    lookup_contact      an ambiguous name                  -> ask, never pick

The search finds the candidate; **the user grounds it**. Their answer lands in
plan.user_answers, which ground_origins already reads — so after a "yes" the
origin is grounded by the user's own words, exactly as if they had typed it.

WHY AN OFFERED OPTION IS VERIFIED, NOT MERELY PLAUSIBLE
-------------------------------------------------------
The 2026-07-10 lesson (planner._validated_question): a draft asked "what is the
full path of phase3test?" offering two INVENTED paths, the user clicked one, and
the plan died on it. An option written as a concrete thing must EXIST. The
analogue here is DNS: every candidate is resolved before it is offered, so we
can never answer "that address doesn't resolve" with a second address that also
does not resolve. Candidates are also run through the SSRF guard — a search
result pointing at a private address is never offered.

WHY THE SIMILARITY FLOOR IS THE FILTER, AND NOT "TAKE RESULT #1"
-----------------------------------------------------------------
MEASURED on the incident's own query against the real provider: searching
"junitjamsheed" returns, in order — en.wikipedia.org, instagram.com,
junaidjamshed.com, junit.org, parasoft.com, wearedevelopers.com. **The right
answer is third.** Taking the top hit would have offered Wikipedia.

What separates them is string distance to the typed name (rapidfuzz.ratio, the
same library and the same deterministic-fuzzy-matching shape as the memory
engine's MIN_SCORE = 81 identity resolution):

    junaidjamshed   84.6   <- the site the user meant
    junit           55.6   <- the worst near-miss (a prefix coincidence)
    instagram       36.4
    wikipedia       27.3
    parasoft        19.0

and for other real typo shapes: amazn->amazon 90.9, githb->github 90.9,
stackoverflw->stackoverflow 96.0. SIMILARITY_FLOOR = 70 sits between 55.6 and
84.6 with margin on both sides. Two INDEPENDENT signals must agree before
anything is offered: search returned it (it is a real, indexed site) AND it is
spelled nearly like what the user said (it is the same word). Neither alone.

NO LLM. Every value here is a slice of a search result or a number from a string
comparison, so this cannot fabricate a domain — the property extract.py holds
for records, applied to hostnames.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from loguru import logger

from app.browser.publicsuffix import registrable, registrable_name

# See the module docstring for the measurement behind this number. Raising it
# loses real corrections (the incident's own is 84.6); lowering it past ~56
# starts admitting prefix coincidences like junit.org.
SIMILARITY_FLOOR = 70.0

# How many candidates the user is ever shown. A "did you mean" with six options
# is not a suggestion, it is a search-results page — and the plan is paused
# while the user reads it. Three is the most that reads as a question.
MAX_SUGGESTIONS = 3

# Results asked of the provider. The right answer was #3 on the incident, so a
# window of 3 would have been luck; 8 leaves room without paying for a second
# page. One search per unresolved host, once.
SEARCH_RESULTS = 8

# A name too short to be misspelled meaningfully — "abc" is within edit distance
# of far too much. Below this we do not guess at all.
MIN_NAME_LENGTH = 4


@dataclass(frozen=True)
class SiteSuggestion:
    """One offered alternative. `host` is the registrable domain (never a
    subdomain or a deep link) — what the user is being asked to approve is a
    SITE, and that is also what gets grounded."""

    host: str
    title: str = ""
    score: float = 0.0

    @property
    def url(self) -> str:
        return f"https://{self.host}/"


# The search seam. Defaults to the same provider chain web_search uses, so this
# inherits Tavily/Google-CSE/DuckDuckGo and their fallbacks for free. Injectable
# for tests, which must never touch the network (conftest's _hermetic_browser
# already nulls browser_tools' factories; this indirection keeps the suite
# hermetic even when they are not).
SEARCH_FACTORY: Optional[Callable[[str, int], Awaitable[list[dict]]]] = None

# The DNS check seam, for the same reason — the suite must not resolve names.
RESOLVER_FACTORY: Optional[Callable[[str], Awaitable[bool]]] = None


async def _search(query: str, limit: int) -> list[dict]:
    if SEARCH_FACTORY is not None:
        return list(await SEARCH_FACTORY(query, limit) or [])
    from app.tools.browser_tools import _search as web_search

    return list(await web_search(query, limit) or [])


def _resolves_blocking(host: str) -> bool:
    """True when `host` resolves AND is not refused by the SSRF guard. Both
    checks in one off-loop call because both are a getaddrinfo."""
    import socket

    from app.tools.browser_tools import _host_is_blocked

    try:
        if _host_is_blocked(host):
            return False
        socket.getaddrinfo(host, None)
        return True
    except Exception:
        return False


async def _resolves(host: str) -> bool:
    if RESOLVER_FACTORY is not None:
        return bool(await RESOLVER_FACTORY(host))
    return await asyncio.to_thread(_resolves_blocking, host)


def _similarity(typed_name: str, candidate_name: str) -> float:
    """rapidfuzz.ratio, or 0.0 if rapidfuzz is somehow unavailable — a missing
    optional import must degrade to "suggest nothing", never to "suggest
    anything" (the floor is the only thing keeping noise out)."""
    try:
        from rapidfuzz import fuzz

        return float(fuzz.ratio(typed_name, candidate_name))
    except Exception:  # pragma: no cover — rapidfuzz is a hard dep in practice
        logger.warning("rapidfuzz unavailable — offering no site suggestions")
        return 0.0


def _candidate_host(row: dict) -> str:
    """The registrable domain of a search result's url, or "". Reduces
    'https://us.junaidjamshed.com/collections/x' and
    'https://www.junaidjamshed.com' to the same 'junaidjamshed.com', which is
    both the dedupe key and the thing offered."""
    from app.tools.browser_tools import normalize_url

    url = str(row.get("url") or "").strip()
    if not url:
        return ""
    try:
        from urllib.parse import urlparse

        host = (urlparse(normalize_url(url)).hostname or "").lower()
    except Exception:
        return ""
    return registrable(host)


def rank_candidates(typed_host: str, rows: list[dict]) -> list[SiteSuggestion]:
    """The PURE half: search rows -> ranked, deduped, floor-passing candidates.
    No I/O, so the scoring rule is testable without a network or a resolver.

    Order is the SEARCH's own, not the score's. Rank is the "which of these is
    the real site" signal and string distance is the "is this the same word"
    signal; using distance to re-order would promote a typosquat over the
    genuine site it imitates, since a squat is by construction spelled closer to
    the typo. Distance filters, rank orders.
    """
    typed_name = registrable_name(typed_host)
    typed_reg = registrable(typed_host)
    if len(typed_name) < MIN_NAME_LENGTH:
        return []

    out: list[SiteSuggestion] = []
    seen: set[str] = set()
    for row in rows or []:
        host = _candidate_host(row)
        if not host or host in seen:
            continue
        # The host the user typed is not a suggestion — it is the thing that
        # failed. (A search engine happily returns reviews and social pages
        # ABOUT a dead domain.)
        if host == typed_reg:
            continue
        score = _similarity(typed_name, registrable_name(host))
        if score < SIMILARITY_FLOOR:
            continue
        seen.add(host)
        out.append(
            SiteSuggestion(host=host, title=str(row.get("title") or "").strip(), score=score)
        )
        if len(out) >= MAX_SUGGESTIONS:
            break
    return out


async def suggest_sites(typed_host: str, *, limit: int = MAX_SUGGESTIONS) -> list[SiteSuggestion]:
    """Sites the user may have meant when `typed_host` did not resolve.

    Best-effort in every direction: a search failure, a resolver failure, or no
    plausible candidate all yield [] — and [] means the caller reports the
    honest "that address doesn't resolve" it would have reported anyway. This
    can only ever ADD a question; it can never turn a working path into a
    failing one.
    """
    host = (typed_host or "").strip().lower().rstrip(".")
    if not host:
        return []
    name = registrable_name(host)
    if len(name) < MIN_NAME_LENGTH:
        return []

    try:
        rows = await _search(name, SEARCH_RESULTS)
    except Exception as exc:
        logger.info(f"site suggestion search failed for '{host}': {type(exc).__name__}: {exc}")
        return []

    ranked = rank_candidates(host, rows)[: max(1, limit)]
    if not ranked:
        logger.info(f"no plausible alternative found for the unresolved host '{host}'")
        return []

    # VERIFY before offering (the _validated_question rule): an option that also
    # fails to resolve is a fabricated clickable fact. Checked concurrently —
    # at most MAX_SUGGESTIONS lookups.
    checks = await asyncio.gather(*(_resolves(s.host) for s in ranked), return_exceptions=True)
    verified = [s for s, ok in zip(ranked, checks) if ok is True]
    if not verified:
        logger.info(f"every alternative for '{host}' failed to resolve — offering none")
        return []
    logger.info(
        f"'{host}' does not resolve — offering "
        + ", ".join(f"{s.host} ({s.score:.0f})" for s in verified)
    )
    return verified
