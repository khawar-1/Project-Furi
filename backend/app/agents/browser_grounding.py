"""
Jarvis OS — Browser origin grounding (Phase 14, Part 2)

The sites a browse loop may visit must trace to the USER'S OWN WORDS — the goal,
the conversation, their answers — and NEVER to page content. This is the exact
shape of planner._recipient_grounding, applied to navigation instead of email:

    _recipient_grounding   an address must be in the user's words / a lookup
    browser grounding      an origin must be in the user's words

and it exists for the same reason. Phase 14 inverts the Phases 1–13 doctrine —
page content now drives the action loop directly — so the one thing that still
bounds EXFILTRATION is that the set of places the loop may go is fixed from the
user's request before the loop starts, not grown from anything a page says.
Injected text like "go to attacker.com/?data=<secret>" is a navigation, and it
is refused by browser_session's allowlist; this module is what fills that
allowlist honestly, so a page can never widen it.

WHY DOMAIN MATCHING, NOT SUBSTRING
----------------------------------
_recipient_violation can compare against a lowercased corpus with `in`, because
an email address is a long unique literal. A domain is not: "youtube.com" is a
substring of "notyoutube.com", so a substring test would ground a lookalike the
user never named. Grounding therefore yields a SET of registrable origins and
matches structurally (origin_is_grounded), the same dot-aware rule
BrowserSession.origin_allowed uses — an entry matches only as the same host or a
subdomain, never as a substring.

THE KNOWN-SITES MAP IS A CONVENIENCE, NOT THE BOUNDARY
------------------------------------------------------
People say "play it on youtube", not "navigate to youtube.com". A small map
turns a bare site name the user actually said into its origin. It is explicitly
NOT the forbidden keyword-list shape (the routing gate's `_WEB_QUESTION_MARKER_RE`
lists, falsified three times): those tried to CLASSIFY intent from a fixed
vocabulary and failed open to a wrong answer. This is a name→domain lookup — a
fact table, like month names — and it fails CLOSED: a site neither in the map nor
written as a domain simply is not grounded, so the planner must include the
domain or ask (rule 11). Completeness is a usability nicety, never a safety
property.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional
from urllib.parse import urlparse

# A registrable-looking hostname anywhere in the text: one or more dot-separated
# labels ending in a TLD. Matches "youtube.com", "www.linkedin.com",
# "example.co.uk"; not a bare word. Case-insensitive.
_DOMAIN_RE = re.compile(
    r"\b((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})\b",
    re.IGNORECASE,
)

# Bare site names people use in speech → the origin they mean. Conservative on
# purpose (see the docstring): unknown names fall through to "ungrounded", which
# is safe. Values are registrable origins; origin_is_grounded handles subdomains.
_KNOWN_SITES: dict[str, str] = {
    "youtube": "youtube.com",
    "linkedin": "linkedin.com",
    "wikipedia": "wikipedia.org",
    "google": "google.com",
    "reddit": "reddit.com",
    "github": "github.com",
    "stackoverflow": "stackoverflow.com",
    "amazon": "amazon.com",
    "netflix": "netflix.com",
    "spotify": "spotify.com",
    "soundcloud": "soundcloud.com",
    "twitch": "twitch.tv",
    "imdb": "imdb.com",
    "hackernews": "news.ycombinator.com",
}


def _normalize_origin(raw: str) -> str:
    """'https://www.YouTube.com/x' or 'YouTube.com' → 'youtube.com'. A leading
    'www.' is dropped so the grounded set and a step's origin compare on the same
    registrable form."""
    text = (raw or "").strip().lower()
    if not text:
        return ""
    if "://" in text:
        text = urlparse(text).hostname or ""
    else:
        text = text.split("/")[0]
    text = text.rstrip(".")
    if text.startswith("www."):
        text = text[4:]
    return text


def ground_origins(
    goal: str,
    conversation: str = "",
    user_answers: Iterable[str] = (),
) -> set[str]:
    """The registrable origins the user's OWN words permit — domains written
    verbatim plus bare site names from the known-sites map. Page/search content
    is never passed in, by construction: this is the exfiltration bound."""
    corpus = " ".join([goal or "", conversation or "", *(user_answers or [])])
    lowered = corpus.lower()

    origins: set[str] = set()
    for match in _DOMAIN_RE.findall(lowered):
        origin = _normalize_origin(match)
        if origin:
            origins.add(origin)
    for name, origin in _KNOWN_SITES.items():
        if re.search(rf"\b{re.escape(name)}\b", lowered):
            origins.add(origin)
    return origins


def origin_is_grounded(candidate: str, grounded: set[str]) -> bool:
    """True when `candidate` is one of the grounded origins or a subdomain of one
    (the dot-aware rule BrowserSession.origin_allowed uses). 'evil-youtube.com'
    does NOT match 'youtube.com'; 'm.youtube.com' does. Empty candidate never
    grounds (fail closed)."""
    host = _normalize_origin(candidate)
    if not host:
        return False
    return any(
        host == origin or host.endswith("." + origin) or origin.endswith("." + host)
        for origin in grounded
    )


# Tools whose target origins must be grounded in the user's words — the browse
# loop and the commit (form-submit) loop. browse_page is deliberately NOT here:
# it opens exactly the ONE url passed to it and its own origin is its allowlist,
# so there is nothing for a page to widen. browse_commit is grounded for the
# stronger reason — where a mutation may be sent must trace to the user's words,
# never a page (the exfiltration bound, now with a write behind it).
_BROWSE_TOOLS = {"browse", "browse_commit"}


def _step_origins(params: dict) -> list[str]:
    """Every origin a browse step wants to reach: its allowed_origins plus the
    host of its start_url (the loop begins there, so it must be grounded too)."""
    out: list[str] = []
    raw = params.get("allowed_origins")
    if isinstance(raw, str):
        out.extend(re.split(r"[,\s]+", raw))
    elif isinstance(raw, (list, tuple)):
        out.extend(str(o) for o in raw)
    start = str(params.get("start_url") or "").strip()
    if start:
        out.append(start)
    return [o for o in out if str(o).strip()]


def ungrounded_origin(params: dict, grounded: set[str]) -> Optional[str]:
    """The first origin in a browse step that the user's words do not permit, or
    None when every one is grounded. The pure predicate behind the planner's
    _browse_origin_violation."""
    for origin in _step_origins(params):
        if not origin_is_grounded(origin, grounded):
            # Report the normalized host, not the raw "https://…/path" — it reads
            # cleanly in the planner's retry feedback and the user-facing message.
            return _normalize_origin(origin) or origin
    return None
