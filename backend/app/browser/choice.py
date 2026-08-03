"""
Jarvis OS — "which one did you mean?" for the things ON a page (2026-08-02)

THE GAP THIS FILLS
------------------
Asked to "add janan perfume to cart" on a storefront that sells Janan Sports,
Janan Oud and Janan Leather — each in 100ml / 50ml / 20ml — the browse loop
picked one and carried on. Not by policy: `browser/state.py`'s Handoff enum is
the COMPLETE pause taxonomy and had no member for item ambiguity, and the
decision prompt's action vocabulary has no "ask" verb, so neither code nor the
model could raise the question. The one downstream checkpoint — the commit
approval card — named the chosen variant by its barcode
(`properties[_Barcode]: PM135415-100-999-M`), which nobody can read.

Every other under-specified thing in this codebase asks:

    folder_resolver   two real folders named "downloads"  -> ask, never pick
    did_you_mean      a domain that does not resolve       -> ask, never guess
    lookup_contact    an ambiguous name                    -> ask, never pick
    _origin_approval  a page-derived origin                -> ask, fail-closed

This module is that rule applied to the items a page offers.

THE TEST: A TIE IN THE USER'S OWN WORDS
---------------------------------------
Score each candidate by how many of the USER'S significant tokens its label
carries. One clear leader means their words discriminate — proceed silently,
they were specific and must not be interrupted. Two or more tied leaders means
their words CANNOT tell those candidates apart, and the difference between the
tied labels is precisely what they did not say:

    "add janan perfume to cart"        {janan, perfume}
      JANAN SPORT - 100ml      1  |
      JANAN OUD - 100ml        1  |- 3-way tie -> ASK
      JANAN LEATHER - 50ml     1  |

    "add janan sports 100ml to cart"   {janan, sports, 100ml}
      JANAN SPORT - 100ml      3  <- unique leader -> PROCEED, no question
      JANAN OUD - 100ml        1
      JANAN LEATHER - 50ml     1

NOTHING HERE KNOWS WHAT A "SIZE" OR AN "ML" IS, and nothing may ever learn.
The same comparison ties `{black, chinos}` three ways across
`Black Chinos 30W / 32W / 34W` and `{macbook, air}` across three storage tiers.
A per-domain vocabulary would be the `_WEB_QUESTION_MARKER_RE` shape the routing
gate had falsified three times: those tried to CLASSIFY from a fixed word list.
This classifies nothing — it compares the user's words against the page's own.

WHY STRAY GOAL WORDS CANNOT BREAK IT
------------------------------------
A token that appears in NO candidate adds 0 to every score; one that appears in
ALL adds 1 to every score. Neither can create or destroy a tie — only a token
that DISCRIMINATES can move the verdict, which is exactly the token worth
weighing. A stray token matching SOME candidates produces a spurious unique
leader, i.e. it fails toward NOT asking, which is the behaviour that shipped
before this module existed. Every failure direction here is that one.

⚠️ A FUZZY RATIO FLOOR CANNOT DO THIS JOB — MEASURED, NOT REASONED
-------------------------------------------------------------------
The obvious way to let "sports" match "sport" is `rapidfuzz.ratio` over a floor,
the shape `did_you_mean.SIMILARITY_FLOOR` and the memory engine's `MIN_SCORE`
both use. It was tried first and the measurement killed it. Real pairs, scored:

    watch   / watches   83.3   <- MUST match (same word)
    short   / shirt     80.0   <- must NOT (different garments)
    small   / stall     80.0   <- must NOT
    large   / lager     80.0   <- must NOT
    olive   / alive     80.0   <- must NOT
    pants   / paints    90.9   <- must NOT, and it OUTSCORES watch/watches

**No floor separates the sets**, because edit distance measures HOW BIG the
difference is and what matters here is WHERE it falls. An inflection is a change
at the END of a word; a different word is a change INSIDE it. So the rule is
positional and exact, and needs no tunable number:

    two tokens match when they are EQUAL, or when the shorter is a PREFIX of
    the longer, is at least _MIN_STEM_CHARS long, and the longer adds at most
    _MAX_INFLECTION_CHARS.

Verified against 38 measured pairs: 12/13 true (`sport/sports`, `watch/watches`,
`chino/chinos`, `perfume/perfumes`, `leather/leathers`, `trouser/trousers`,
`hoodie/hoodies`, `sneaker/sneakers`, …) and **25/25 false**, including every
pair that defeated the ratio floor. The single miss is `kid`/`kids`, blocked by
the length gate — a false NEGATIVE, which can only cause an extra question.

The length gate is load-bearing and its number is measured too: EVERY dangerous
pair in the sample is three characters or shorter — `oud`/`loud`, `tan`/`tank`,
`cap`/`cape`, `bag`/`bags`, `pro`/`pros` (all 85.7 by ratio, all a prefix
relation). One character is a third of a three-letter word, so three-letter
tokens get no inflection allowance at all.

NO LLM, AND IT CANNOT FABRICATE
-------------------------------
Every string this module emits is a slice of the observation it was handed — the
property `extract.py` holds for records, applied to choices. A test asserts it.
So the question can never offer a product the page does not sell.

PURE: imports nothing from `app.*`, like `extract.py`, so it cannot be a cycle
and needs no fixture to test.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

# --------------------------------------------------------------- the token rule
# See the docstring: measured, not tuned. A token shorter than this gets no
# inflection allowance — every false pair in the sample was three chars or less.
_MIN_STEM_CHARS = 4
# "watch" -> "watches" is the longest real inflection in the sample (+2).
_MAX_INFLECTION_CHARS = 2

# Words that name no product: the goal's own verbs and plumbing. Deliberately
# SMALL — this is not a vocabulary the verdict rests on (a word missing from it
# adds a token that scores equally across candidates and so changes nothing),
# it only keeps the rendered question honest about what was being matched.
_GOAL_STOPWORDS = frozenset(
    {
        "add", "put", "place", "buy", "order", "purchase", "get", "take",
        "cart", "bag", "basket", "checkout", "trolley",
        "the", "a", "an", "of", "and", "to", "for", "on", "in", "with",
        "my", "me", "i", "into", "from", "at", "it", "its", "this", "that",
        "please", "go", "open", "visit", "want", "need", "some", "one",
        "website", "site", "com", "www", "page", "shop", "store", "online",
        # The goal is LLM-authored prose ("Find the product named 'Janan' …"),
        # so its plumbing nouns leak in beside the user's own words: live
        # 2026-08-02 the question read "3 things match 'find product named janan
        # findproduct productnamed'". These name no product, and they double as
        # the URL-path plumbing `page_subject` walks past.
        "find", "named", "name", "product", "products", "item", "items",
        "search", "results", "collections", "pages", "dp",
    }
)

# How many options a question may show. did_you_mean.MAX_SUGGESTIONS = 3 for the
# same reason, stated there: a list of six is not a question, it is a
# search-results page — and the plan is parked while the user reads it. Four
# because a size/colour axis commonly has four real values.
MAX_CHOICE_OPTIONS = 4

# A candidate's label is the element's own visible text. Clipped only for
# display; the clip keeps whole words so an option is never cut mid-name (the
# rendering.py "clip BY ITEM" rule).
_LABEL_MAX_CHARS = 90

# Roles whose elements can be a chooseable ITEM. A grid card is often a link or
# a button; a form control never is.
_CHOOSABLE_ROLES = frozenset({"link", "button", "listitem", "article", "heading"})

# Chrome that appears in a product card's text and names no product.
_LABEL_NOISE_RE = re.compile(
    r"(?i)\b(add to (?:cart|bag|basket)|buy now|quick view|sale|new|sold out|"
    r"out of stock|wishlist|compare|free delivery|off)\b"
)

_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    """Lowercased alphanumeric tokens, in the order they were written. Single
    characters are dropped: a lone letter is a size code or a stray, never a
    name, and it would match far too much."""
    return [t for t in _WORD_RE.findall((text or "").lower()) if len(t) > 1]


def _joined_pairs(tokens: list[str]) -> list[str]:
    """Adjacent tokens glued together — "100 ml" as the page spells it, "100ml".

    The same trick `loop._names_only_the_destination` uses for a two-word brand
    that concatenates in its domain, and it is needed in both directions: a user
    may type "100 ml" against a label reading "100ml", or the reverse. Joining
    keeps this a token-vs-token comparison, so it adds no substring danger — a
    lone "oud" still cannot match inside "loud"."""
    return [a + b for a, b in zip(tokens, tokens[1:])]


def _words_with_pairs(text: str) -> list[str]:
    """A text's tokens PLUS its adjacent-joined pairs — the form both sides of a
    comparison must be in.

    Without this, "100 ml" (as a person types it) cannot match "100ml" (as the
    page writes it): neither "100" nor "ml" clears the length gate on its own, so
    the joined form is the only thing that can carry the match. `target_tokens`
    does this for the goal; anything else matching against a page label needs it
    for exactly the same reason."""
    words = _tokens(text)
    return words + [p for p in _joined_pairs(words) if p not in words]


def tokens_match(a: str, b: str) -> bool:
    """One token against another: equal, or the same word inflected.

    See the module docstring for why this is positional rather than a fuzzy
    ratio, and for the 38 measured pairs behind both constants."""
    if a == b:
        return True
    lo, hi = (a, b) if len(a) <= len(b) else (b, a)
    if len(lo) < _MIN_STEM_CHARS:
        return False
    return hi.startswith(lo) and (len(hi) - len(lo)) <= _MAX_INFLECTION_CHARS


@dataclass(frozen=True)
class Candidate:
    """One thing on the page the user's words could have meant. `label` is the
    element's own visible text, verbatim — never composed here."""

    label: str
    index: int
    href: str = ""
    price: str = ""

    def option(self) -> str:
        """The string shown as a clickable option, and matched back against the
        user's reply. Price is enrichment: it is what tells two identically
        named variants apart on a listing that repeats the title."""
        text = _clip(self.label)
        return f"{text} — {self.price}" if self.price else text


def _clip(text: str) -> str:
    text = " ".join((text or "").split())
    if len(text) <= _LABEL_MAX_CHARS:
        return text
    cut = text[:_LABEL_MAX_CHARS].rsplit(" ", 1)[0]
    return (cut or text[:_LABEL_MAX_CHARS]) + "…"


def target_tokens(goal: str, url: str = "", extra: Iterable[str] = ()) -> list[str]:
    """The user's own significant words — what a candidate is scored against.

    `extra` carries their later answers, so a reply to one question sharpens the
    next round rather than repeating it (the termination property: the answer
    changes the detector's input). The site's own name is dropped, because every
    candidate on a site mentions it and a token that scores equally everywhere
    cannot discriminate.

    ⚠️ THE HOST ONLY, never the whole URL. A results page carries the search in
    its query string (`/search?q=janan`), so tokenizing the URL would file the
    user's own word under "the site's name" and drop the one token that
    discriminates — the exact inverse of this function's job. Caught by the
    incident test, which is why it uses a realistic results URL."""
    site: set[str] = set()
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except ValueError:  # a malformed URL is simply no site context
        host = ""
    if host:
        site = set(_tokens(host))
    words: list[str] = []
    for text in (goal, *extra):
        kept = [
            t for t in _tokens(text)
            if t not in _GOAL_STOPWORDS and t not in site
        ]
        # Joined from the KEPT tokens, not the raw ones: "100 ml" -> "100ml" is
        # the case this exists for, and joining across the plumbing would only
        # manufacture noise ("tocart", "carton") that matches nothing.
        for token in kept + _joined_pairs(kept):
            if token not in words:
                words.append(token)
    return words


def _label_of(element: Any) -> str:
    """An element's own visible text, preferring the unclipped capture."""
    return str(getattr(element, "name_full", "") or getattr(element, "name", "") or "")


def _score(target: list[str], label: str) -> int:
    """How many DISTINCT target tokens this label carries. Distinct, so a label
    repeating one word ("Janan Janan Oud") cannot outrank one that genuinely
    matches more of the request."""
    label_tokens = _tokens(label)
    pool = set(label_tokens) | set(_joined_pairs(label_tokens))
    return sum(1 for t in target if any(tokens_match(t, l) for l in pool))


def _is_choosable(element: Any) -> bool:
    role = str(getattr(element, "role", "") or "").lower()
    if role in _CHOOSABLE_ROLES:
        return True
    return bool(str(getattr(element, "href", "") or ""))


def _dedupe_key(label: str, href: str) -> tuple:
    return (" ".join(_tokens(label)), (href or "").strip().rstrip("/").lower())


def candidates_of(
    elements: Iterable[Any], *, find_price: Optional[Any] = None
) -> list[Candidate]:
    """Every chooseable thing on the page, deduped, in DOM order.

    `find_price` is `extract.find_price`, injected so this module stays pure. A
    grid card's text is "title … price …" (the shape extract.py reads), so it is
    split the way extract._card_of does: everything before the first price is the
    title. Scoring on the TITLE and not the whole card is not cosmetic — "Rs.
    100" would otherwise let a price's digits match a target token like "100" and
    manufacture a leader out of a price tag."""
    out: list[Candidate] = []
    seen: set[tuple] = set()
    for element in elements or ():
        if not _is_choosable(element):
            continue
        raw = _label_of(element)
        price = ""
        title = raw
        if find_price is not None:
            try:
                match = find_price(raw)
                if match is not None:
                    price = match.group(0).strip()
                    head = raw[: match.start()].strip(" -–—|·,")
                    if head:
                        title = head
            except Exception:  # noqa: BLE001 — enrichment must never decide a pause
                price = ""
        label = _LABEL_NOISE_RE.sub(" ", title).strip(" -–—|·,")
        if not label:
            continue
        try:
            index = int(getattr(element, "index"))
        except (TypeError, ValueError):
            continue
        href = str(getattr(element, "href", "") or "")
        key = _dedupe_key(label, href)
        # First in DOM order wins a duplicate — the page's own ordering is the
        # only ranking signal we have, and re-ordering would promote a near-match
        # over the item the page itself puts first.
        if key in seen:
            continue
        seen.add(key)
        out.append(Candidate(label=label, index=index, href=href, price=price))
    return out


def tied_candidates(
    target: list[str],
    elements: Iterable[Any],
    *,
    limit: int = MAX_CHOICE_OPTIONS,
    find_price: Optional[Any] = None,
) -> list[Candidate]:
    """The candidates the user's words cannot tell apart, or [] when there is no
    ambiguity to raise.

    Returns [] — meaning "carry on exactly as before" — when there are no target
    tokens, when nothing on the page matches any of them, when ONE candidate
    leads, or when fewer than two things tie. That is the same
    answer-only-when-sure discipline `extract.py` and `folder_resolver` are built
    on: code never picks, and it never asks about an ambiguity it cannot show."""
    if not target:
        return []
    scored = [
        (_score(target, c.label), c)
        for c in candidates_of(elements, find_price=find_price)
    ]
    scored = [(s, c) for s, c in scored if s > 0]
    if len(scored) < 2:
        return []
    top = max(s for s, _ in scored)
    tied = [c for s, c in scored if s == top]
    if len(tied) < 2:
        return []  # one clear leader — the user's words were specific enough
    return tied[:limit]


def page_subject(title: str, url: str = "") -> str:
    """What THIS page is itself about, in its own words — its title plus the
    URL's path segments.

    ⚠️ NEVER THE QUERY STRING, and for the mirror image of `target_tokens`'
    host-only rule. A results page carries the user's own words in `?q=janan`,
    so counting them would let every search page claim to BE whatever was
    searched for — scoring the request against itself. The path is the site's
    own naming of the resource (`/products/janan-sport-30ml`); the query is the
    request."""
    parts = _tokens(title)
    try:
        path = urlparse(url or "").path or ""
    except ValueError:  # a malformed URL simply contributes no path
        path = ""
    parts += _tokens(path)
    kept: list[str] = []
    for token in parts:
        if token not in _GOAL_STOPWORDS and token not in kept:
            kept.append(token)
    return " ".join(kept)


def page_is_the_target(
    target: list[str], title: str, url: str, tied: Iterable[Candidate]
) -> bool:
    """True when this page IS the thing the user named — so the things merely
    LISTED on it are not a choice to put to them.

    ⚠️ THE DEFECT THIS EXISTS FOR (live 2026-08-02, traces 80da37b030ab →
    9ee36900e6e0). The user asked to add "janan" to the cart, was asked which
    one, answered "JANAN SPORT - 30ML", and the pick was enforced in code — the
    run landed on `/products/janan-sport-30ml`, exactly right. Then the product
    page's "you may also like" rail (200ML / GIFT SET / 100ML) tied 3-3-3 and it
    ASKED AGAIN, from a list that did not even contain the thing they had just
    chosen. Their answer WAS in the corpus and could not help: it discriminates
    the chosen item from its siblings, and those siblings are still equal to
    EACH OTHER.

    So the question "which of these did you mean?" is only meaningful while the
    run is SELECTING. Once it has selected — by navigating into a thing — it is
    ACTING, and a rail of related products is navigation, not a choice. The test
    for which situation we are in is direct rather than a proxy: score the
    page's own identity with the SAME scorer, and if the user's words point at
    it STRICTLY harder than at anything listed on it, the page is the answer.

    ⚠️ STRICTLY, and that is load-bearing — measured on the incident:

        search page  title 1 vs candidates 1  ->  not greater  ->  ASK   (right)
        product page title 5 vs candidates 3  ->  greater      ->  act   (right)

    A listing page's title echoes the query or the category, so it ties with the
    products it lists and never suppresses. `>=` would have silenced the search
    page too, which is the question that MUST be asked. Needs no ancestry, no
    geometry, no URL-shape list, and cannot disagree with the tie test it
    guards, because it is the same comparison."""
    tied = list(tied)
    if not target or len(tied) < 2:
        return False
    subject = page_subject(title, url)
    if not subject:
        return False
    return _score(target, subject) > max(_score(target, c.label) for c in tied)


def plain_words(target: Iterable[str]) -> list[str]:
    """The target tokens a person would recognise as their own words.

    `target_tokens` carries adjacent-joined pairs ("100 ml" -> "100ml") because
    a page may spell a name either way, but they are an internal matching device
    and reading them back is noise — live 2026-08-02 the question said it was
    matching 'find product named janan findproduct productnamed'. Display only;
    the verdict is never computed from this."""
    words = [t for t in target if t]
    glued = {a + b for a in words for b in words}
    return [t for t in words if t not in glued]


def locate(
    chosen: str, elements: Iterable[Any], *, find_price: Optional[Any] = None
) -> Optional[Candidate]:
    """The element on THIS page that the user's chosen option names, or None.

    ENFORCE, NEVER TRUST — the `_apply_site_correction` / folder_resolver rule.
    After the user answers, the resumed run must act on the thing they picked;
    re-asking the model would hand it the same ambiguous goal that produced the
    question. So the choice is turned back into an element in CODE, at zero LLM
    cost. None when the page no longer shows it, or when it still cannot be told
    apart — code never picks, and the loop then carries on normally."""
    words = _words_with_pairs(chosen)
    if not words:
        return None
    scored = [
        (_score(words, c.label), c)
        for c in candidates_of(elements, find_price=find_price)
    ]
    scored = [(s, c) for s, c in scored if s > 0]
    if not scored:
        return None
    top = max(s for s, _ in scored)
    leaders = [c for s, c in scored if s == top]
    return leaders[0] if len(leaders) == 1 else None


def pick_by_answer(answer: str, options: Iterable[str]) -> str:
    """The option the user's reply names, or "" when it names none.

    Fail-closed by construction, the `_match_site_choice` rule: an ambiguous or
    unrecognised reply returns "", and the caller stops honestly rather than
    guessing. An exact/normalized hit is the clicked-button path; otherwise the
    reply must single out exactly ONE option by its words."""
    text = " ".join(_tokens(answer))
    if not text:
        return ""
    words = _words_with_pairs(answer)
    choices = [str(o) for o in options if str(o).strip()]
    for option in choices:
        if " ".join(_tokens(option)) == text:
            return option
    hits = [o for o in choices if _score(words, o) > 0]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        # Several options carry the reply's words — take the one that carries
        # the MOST of them, but only when that leader is unique. A reply that
        # still cannot tell two options apart has not answered the question.
        scored = [(_score(words, o), o) for o in hits]
        top = max(s for s, _ in scored)
        leaders = [o for s, o in scored if s == top]
        if len(leaders) == 1:
            return leaders[0]
    return ""
