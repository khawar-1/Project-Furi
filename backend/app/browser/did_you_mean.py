"""
Furi OS — "did you mean…?" for a site that does not exist (2026-08-01)

THE INCIDENT
------------
Spoken: "Go to junaidjamshed.com and add Jhanan Sports 100 ML in cart."
Transcribed: "Go to **junitjamsheed.com** …". Chromium answered
ERR_NAME_NOT_RESOLVED, commit_flow turned it into a flat step failure, both
replans failed, and the task died. Google, given the identical misspelling,
puts the real site on the first screen. The user's question was exactly right:
why did Furi not ASK?

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

⚠️ THE FLOOR'S "MARGIN ON BOTH SIDES" WAS TRUE OF A CLEAN NAME ONLY (2026-08-02)
--------------------------------------------------------------------------------
Live: spoken "open junaidjamshed.com", transcribed **openjunetjamshed.com** —
"open" GLUED to the host, because speech has no spaces. MEASURED against the real
provider: the search still returns junaidjamshed.com (3 of 8 rows), and it scores

    openjunetjamshed vs junaidjamshed   69.0   <- FLOOR is 70.0. Missed by one.
         junetjamshed vs junaidjamshed   80.0   <- after stripping the glue

A verb welded to a proper noun is not an exotic input; it is what STT does every
time it does not know the noun, which is the exact case this module exists for.
So the typed name is compared in TWO forms. The stripped form is a strict
FALLBACK — consulted only when the raw name yields nothing — so the common path
is byte-identical and a strip can only ever turn "no suggestion" into "a
suggestion", never change one that already worked.

WHY THERE IS A SECOND TIER, AND WHY IT IS NOT FABRICATION
----------------------------------------------------------
The docstring above credits Google with putting the real site on the first
screen. It does that by SPELL-CORRECTING THE QUERY — "These are results for
junaid jamshed" — and Tavily, a retrieval API, does not. So when the official
domain is simply absent from the rows, tier 1 has nothing to rank. MEASURED:

    amazn      -> amazon.com absent entirely; best host is aboutamazon.com (62)
    opengithb  -> github.com present once, 66.7 — under the floor

Both queries return TITLES that say the corrected name over and over ("Amazon",
"GitHub", "Junaid Jamshed"). So tier 2 reads the name out of the titles, forms
`<name>.<suffix>`, and RESOLVES IT. That is Google's spell-correction rebuilt
deterministically, and it is bounded by four independent things: the name is a
token from a returned title (never invented — extract.py's property), it must
recur (BRAND_MIN_OCCURRENCES), it must clear the SAME similarity floor, and the
host it forms must answer DNS. It is consulted only when tier 1 is empty, and
what it produces is still only ever OFFERED — the user's reply is what grounds
the origin. We suggest; we never navigate.
"""
from __future__ import annotations

import asyncio
import re
from collections import Counter
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable, Optional, Sequence

from loguru import logger

from app.browser.publicsuffix import (
    has_known_tld,
    public_suffix,
    registrable,
    registrable_name,
)

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

# How many titles must agree on a name before tier 2 will build a host out of it.
# One mention is a passing reference; two is the page set being ABOUT that thing.
BRAND_MIN_OCCURRENCES = 2

# Hosts tier 2 may synthesise before it stops. It is a fallback, not a sweep.
BRAND_MAX_HOSTS = 4

# A leading navigation verb welded to the host by speech-to-text. Longest
# alternative first, so "openup" is not read as "open" + "up". Applied to the
# NAME LABEL only — never to the suffix, and never to the host the browse
# actually navigates to — and only as the fallback described in the module
# docstring.
#
# This is a literal list, and this codebase has measured literal lists at zero
# three times — so the difference matters: it does not CLASSIFY anything. It
# strips a known transcription artifact, its failure mode is "no suggestion"
# (today's behaviour), and it can widen nothing, because what it produces is
# offered to the user and never navigated to.
_LEAD_NOISE_RE = re.compile(
    r"^(?:takemeto|navigateto|browseto|headto|openup|bringup|pullup|goto|"
    r"navigate|browse|launch|visit|show|load|open|head|go)"
)

# Words a title uses about ANY site, which therefore name no brand. Kept small:
# over-pruning costs a candidate, under-pruning costs nothing (a generic word
# will not clear the similarity floor against the typed name anyway).
_BRAND_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "your", "our", "www", "com", "net",
    "org", "official", "website", "web", "site", "sites", "page", "pages",
    "home", "homepage", "online", "shop", "shopping", "store", "buy", "sale",
    "new", "best", "top", "free", "login", "sign", "account", "wikipedia",
    "biography", "bio", "life", "story", "news", "video", "videos", "watch",
    "how", "what", "why", "who", "when", "where", "all", "more", "info",
    "profile", "photos", "review", "reviews", "install", "download", "guide",
})


def _strip_lead_noise(name: str) -> str:
    """`name` without a leading navigation verb, or "" when there is nothing to
    strip or too little left to be a name."""
    match = _LEAD_NOISE_RE.match(name or "")
    if not match:
        return ""
    rest = name[match.end():]
    return rest if len(rest) >= MIN_NAME_LENGTH else ""


def typed_variants(typed_host: str) -> list[str]:
    """The forms of the typed NAME worth comparing against: what they said, and
    — when a navigation verb looks welded to the front — what they meant."""
    name = registrable_name(typed_host)
    if len(name) < MIN_NAME_LENGTH:
        return []
    stripped = _strip_lead_noise(name)
    return [name, stripped] if stripped and stripped != name else [name]


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


# How many hosts one caller may ask us to verify. A clarifying question offers a
# handful of options; anything past that is not a question.
MAX_VERIFY_HOSTS = 6

# A string whose WHOLE content is an address: an optional scheme, a dotted host,
# an optional trailing slash. Deliberately anchored at both ends — an option that
# merely MENTIONS a domain inside a sentence is prose, and prose is none of our
# business. `has_known_tld` then separates `junetjamshed.com` from `report.txt`,
# which are the same shape (the 2026-08-01 lesson).
_ADDRESS_ONLY_RE = re.compile(
    r"^(?:https?://)?([a-z0-9](?:[a-z0-9.\-]*[a-z0-9])?\.[a-z]{2,24})/?$", re.IGNORECASE
)


def option_host(text: str) -> str:
    """The registrable host of a string written as an ADDRESS, or "". Used to
    tell a clarifying question's "https://junetjamshed.com" — a clickable fact
    that must be true — from "March 4", "C:\\Users\\me", or "No — none of these"."""
    raw = (text or "").strip().strip("'\"").rstrip(".,;:!?")
    match = _ADDRESS_ONLY_RE.match(raw)
    if not match:
        return ""
    host = match.group(1).lower().rstrip(".")
    if not has_known_tld(host):
        return ""
    return registrable(host) or host


async def verify_hosts(hosts: Iterable[str]) -> set[str]:
    """The subset of `hosts` that answers DNS and passes the SSRF guard. Bounded,
    concurrent, and best-effort in one direction only: a lookup that errors is
    NOT verified, so a failure can only ever drop an option, never add one."""
    unique = [h for h in dict.fromkeys(h for h in hosts if h)][:MAX_VERIFY_HOSTS]
    if not unique:
        return set()
    checks = await asyncio.gather(*(_resolves(h) for h in unique), return_exceptions=True)
    return {host for host, ok in zip(unique, checks) if ok is True}


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


def _best_similarity(names: Sequence[str], candidate_name: str) -> float:
    """The closest any form of the typed name comes to `candidate_name`."""
    return max((_similarity(n, candidate_name) for n in names), default=0.0)


def rank_candidates(
    typed_host: str, rows: list[dict], *, extra_names: Sequence[str] = ()
) -> list[SiteSuggestion]:
    """The PURE half: search rows -> ranked, deduped, floor-passing candidates.
    No I/O, so the scoring rule is testable without a network or a resolver.

    Order is the SEARCH's own, not the score's. Rank is the "which of these is
    the real site" signal and string distance is the "is this the same word"
    signal; using distance to re-order would promote a typosquat over the
    genuine site it imitates, since a squat is by construction spelled closer to
    the typo. Distance filters, rank orders.

    `extra_names` are additional forms of the typed name to score against — the
    verb-stripped fallback (see the module docstring). Empty by default, so the
    ordinary call is unchanged.
    """
    typed_name = registrable_name(typed_host)
    typed_reg = registrable(typed_host)
    if len(typed_name) < MIN_NAME_LENGTH:
        return []
    names = [typed_name, *(n for n in extra_names if n)]

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
        score = _best_similarity(names, registrable_name(host))
        if score < SIMILARITY_FLOOR:
            continue
        seen.add(host)
        out.append(
            SiteSuggestion(host=host, title=str(row.get("title") or "").strip(), score=score)
        )
        if len(out) >= MAX_SUGGESTIONS:
            break
    return out


def _title_words(title: str) -> list[str]:
    return [
        w
        for w in re.split(r"[^a-z0-9]+", (title or "").lower())
        if len(w) > 2 and w not in _BRAND_STOPWORDS
    ]


def brand_candidates(
    typed_host: str, rows: list[dict], *, exclude: Iterable[str] = ()
) -> list[SiteSuggestion]:
    """TIER 2, also PURE: the corrected name read out of the results' own TITLES,
    turned into hosts to resolve. See the module docstring for why this exists
    and why it is not fabrication.

    A candidate must be a word (or an adjacent word pair, concatenated — "junaid
    jamshed" is one domain label) that several titles agree on AND that clears
    the same similarity floor as tier 1. The suffix comes from the host the user
    typed, plus .com; nothing else is invented, and every host produced here is
    DNS-verified by the caller before it is offered.
    """
    names = typed_variants(typed_host)
    if not names:
        return []

    counts: Counter[str] = Counter()
    titles: dict[str, str] = {}
    for row in rows or []:
        title = str(row.get("title") or "").strip()
        words = _title_words(title)
        for i, word in enumerate(words):
            for token in (word, word + words[i + 1] if i + 1 < len(words) else ""):
                if not token:
                    continue
                counts[token] += 1
                titles.setdefault(token, title)

    scored: list[tuple[int, float, str]] = []
    for token, count in counts.items():
        if count < BRAND_MIN_OCCURRENCES:
            continue
        score = _best_similarity(names, token)
        if score < SIMILARITY_FLOOR:
            continue
        scored.append((count, score, token))
    # Consensus first, then closeness, then the token itself — fully
    # deterministic, so the same rows always produce the same offer.
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))

    suffixes: list[str] = []
    for suffix in (public_suffix(typed_host), "com"):
        if suffix and suffix not in suffixes:
            suffixes.append(suffix)

    seen = {h for h in exclude if h}
    seen.add(registrable(typed_host))
    out: list[SiteSuggestion] = []
    for count, score, token in scored:
        for suffix in suffixes:
            host = f"{token}.{suffix}"
            if host in seen:
                continue
            seen.add(host)
            out.append(SiteSuggestion(host=host, title=titles.get(token, ""), score=score))
            if len(out) >= BRAND_MAX_HOSTS:
                return out
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
    variants = typed_variants(host)
    if not variants:
        return []
    name = variants[0]

    try:
        rows = await _search(name, SEARCH_RESULTS)
    except Exception as exc:
        logger.info(f"site suggestion search failed for '{host}': {type(exc).__name__}: {exc}")
        return []

    # ONE search, three readings of it, each a strict fallback for the last: the
    # name as typed, the name with a glued-on navigation verb removed, then the
    # name the titles themselves keep repeating. Nothing here costs another query.
    ranked = rank_candidates(host, rows)
    if not ranked and len(variants) > 1:
        ranked = rank_candidates(host, rows, extra_names=variants[1:])
        if ranked:
            logger.info(
                f"'{host}' reads as a navigation verb glued to '{variants[1]}' — "
                "scoring against that too"
            )
    if not ranked:
        ranked = brand_candidates(host, rows)
        if ranked:
            logger.info(
                f"no indexed domain resembles '{host}' — trying the name its "
                "results keep repeating: " + ", ".join(s.host for s in ranked)
            )
    ranked = ranked[: max(1, limit)]
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
