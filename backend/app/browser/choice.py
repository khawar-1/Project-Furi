"""
Furi OS — "which one did you mean?" for the things ON a page (2026-08-02)

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
from urllib.parse import parse_qs, urlparse

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
# same reason, stated there: a list of sixty is not a question, it is a
# search-results page — and the plan is parked while the user reads it.
#
# RAISED 4 → 8 (2026-08-08), MEASURED on the live listing this feature exists
# for. `junaidjamshed.com/search?q=janan` returns TWENTY products whose names
# carry "janan", and a one-family subset is already three (JANAN SPORT - 30ML /
# - 200ML / - GIFT SET). At four, the user's own requirement — "if there are
# janan sports 100ml and janan sports 200ml it should tell me BOTH" — cannot be
# expressed for more than one family at a time. Eight shows every variant of two
# or three families, which is what a real "which one?" looks like.
#
# It is a SHOWN cap, not a matched one: `tied_matches` returns the whole tie and
# the caller reports how many there were, so a truncated list says so. A list
# that silently drops half the answer is the "record lied" failure this codebase
# keeps having to unpick — the user cannot tell an incomplete list from a
# complete one.
MAX_CHOICE_OPTIONS = 8

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


# What an option says when the store cannot sell it. Only ever appended when
# EVERY tied item is sold out — otherwise the unbuyable ones are not offered at
# all — so seeing it means "none of these can be added", not "this one".
SOLD_OUT_MARK = "(sold out)"


@dataclass(frozen=True)
class Candidate:
    """One thing on the page the user's words could have meant. `label` is the
    element's own visible text, verbatim — never composed here."""

    label: str
    index: int
    href: str = ""
    price: str = ""
    # 2026-08-09. False when the store says this cannot be bought. Read from the
    # element (observe.Element.sold_out, a walk of the card's own ancestry), so
    # like every other value here it is a fact the page stated.
    #
    # ⚠️ IT NEVER FILTERS `candidates_of`. `locate` is the answered-pick
    # enforcement, and a user who deliberately picks a sold-out product must
    # still have their answer honoured — dropping it there would silently ignore
    # what they said. Only the tie path reads it.
    available: bool = True

    def option(self) -> str:
        """The string shown as a clickable option, and matched back against the
        user's reply. Price is enrichment: it is what tells two identically
        named variants apart on a listing that repeats the title."""
        text = _clip(self.label)
        if self.price:
            text = f"{text} — {self.price}"
        return text if self.available else f"{text} {SOLD_OUT_MARK}"


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


def _concepts(target: Iterable[str]) -> list[str]:
    """The user's DISTINCT IDEAS — the target tokens with the joined-pair
    spellings folded away.

    `target_tokens` carries "100ml" alongside "100" and "ml" so a page may spell
    the same idea either way. That makes the pair an alternate SPELLING of two
    tokens, never a third idea, and everything that counts a match has to say so
    (see `_score`). The one computation, so display and scoring cannot drift —
    `plain_words` is this list."""
    words = [t for t in target if t]
    glued = {a + b for a in words for b in words}
    return [t for t in words if t not in glued]


def _score(target: list[str], label: str) -> int:
    """How many of the user's DISTINCT IDEAS this label carries.

    ⚠️ IT USED TO COUNT TARGET TOKENS, AND THAT MADE WORD ORDER DECIDE
    (live 2026-08-10). `target_tokens` appends adjacent-joined pairs, so
    "black kameez kurta" arrives as five tokens — black, kameez, kurta,
    blackkameez, kameezkurta — and a label that happens to spell two of them
    ADJACENTLY scored a bonus for it. MEASURED on the incident's own results
    page:

        Black Kameez Shalwar                 -> 3   ('blackkameez' is adjacent)
        Black Cotton Casual Kameez Shalwar   -> 2   (the same two words, apart)

    so `tied_matches` saw a unique leader where the user saw two equally good
    matches, and the "which one did you mean?" question was never asked — code
    picking between real equals, through an artefact of its own matching device.

    A joined pair is an alternate SPELLING of two ideas, so a pair match now
    covers exactly the two ideas it is made of, once. "100 ml" still matches
    "100ml" (neither half clears the length gate alone, so the pair is the only
    thing that can carry it — that is what this device is FOR); what it can no
    longer do is out-score a label that carries the same words in another order.

    Distinct, so a label repeating one word ("Janan Janan Oud") cannot outrank
    one that genuinely matches more of the request."""
    label_tokens = _tokens(label)
    pool = set(label_tokens) | set(_joined_pairs(label_tokens))
    concepts = _concepts(target)
    if not concepts:
        return 0

    def _hits(token: str) -> bool:
        return any(tokens_match(token, l) for l in pool)

    # Which joined spellings does this label carry, and which two ideas is each
    # made of? Exact composition, never a substring test — a pair is by
    # construction `a + b` of two concepts.
    carried_pairs = {t for t in target if t not in concepts and _hits(t)}
    covered_by_pair: set[str] = set()
    for pair in carried_pairs:
        for a in concepts:
            for b in concepts:
                if pair == a + b:
                    covered_by_pair.add(a)
                    covered_by_pair.add(b)
    return sum(1 for c in concepts if _hits(c) or c in covered_by_pair)


def _is_choosable(element: Any) -> bool:
    role = str(getattr(element, "role", "") or "").lower()
    if role in _CHOOSABLE_ROLES:
        return True
    return bool(str(getattr(element, "href", "") or ""))


def _dedupe_key(candidate: "Candidate") -> str:
    """Two candidates are the same CHOICE when they would be shown identically.

    ⚠️ THIS USED TO KEY ON (label tokens, href), AND THAT OFFERED THE USER TWO
    IDENTICAL BUTTONS. Measured on the incident's own product page: the "you may
    also like" rail carries two different products both named `BLACK COTTON
    CASUAL KAMEEZ SHALWAR`, at different hrefs, so nothing merged them and both
    were offered — a question with two byte-identical answers, which
    `pick_by_answer` then resolves by taking the FIRST, i.e. code silently
    picking between real equals.

    An option the user cannot tell from another is not a second option. The
    price is part of the key because it is part of what they see, so two
    same-named variants at different prices stay distinct and stay answerable.
    """
    return " ".join(_tokens(candidate.option()))


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
        candidate = Candidate(
            label=label,
            index=index,
            href=href,
            price=price,
            # Tolerant read, the `_in_dialog` shape: any element without the
            # field — every fake in the suite, any older observation — is
            # available, which is the behaviour that predates the flag.
            available=not bool(getattr(element, "sold_out", False)),
        )
        key = _dedupe_key(candidate)
        # First in DOM order wins a duplicate — the page's own ordering is the
        # only ranking signal we have, and re-ordering would promote a near-match
        # over the item the page itself puts first.
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def tied_matches(
    target: list[str],
    elements: Iterable[Any],
    *,
    find_price: Optional[Any] = None,
) -> list[Candidate]:
    """EVERY candidate the user's words cannot tell apart — the whole tie, in DOM
    order, UNTRUNCATED.

    Returns [] — meaning "carry on exactly as before" — when there are no target
    tokens, when nothing on the page matches any of them, when ONE candidate
    leads, or when fewer than two things tie. That is the same
    answer-only-when-sure discipline `extract.py` and `folder_resolver` are built
    on: code never picks, and it never asks about an ambiguity it cannot show.

    The caller decides how many to SHOW (MAX_CHOICE_OPTIONS) and reports the rest
    as a count, so a truncated question is honest about being truncated."""
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
    return tied


def tied_candidates(
    target: list[str],
    elements: Iterable[Any],
    *,
    limit: int = MAX_CHOICE_OPTIONS,
    find_price: Optional[Any] = None,
) -> list[Candidate]:
    """`tied_matches`, capped at what a question may show. Kept as the shape most
    callers want; scoring happens once, in `tied_matches`."""
    return tied_matches(target, elements, find_price=find_price)[:limit]


@dataclass(frozen=True)
class ItemChoice:
    """What to do about the things on this page the user's words cannot separate.

    Exactly one of `tied` / `settled` is meaningful, the `AxisChoice` shape one
    layer up: either there is a question to ask or there is a value to take."""

    tied: tuple[Candidate, ...] = ()   # ask about these (>= 2), already stock-filtered
    settled: Optional[Candidate] = None  # the only one that can be bought — take it
    dropped: int = 0                   # how many the tie lost to stock
    none_buyable: bool = False         # every tied item is sold out
    # ⚠️ THE TIE BEFORE STOCK, and callers testing "is this page itself the
    # thing?" MUST use it. `page_is_the_target` needs two or more candidates to
    # compare against and answers False for any shorter list — so once stock
    # narrows a tie to a single survivor, a caller passing `tied`/`settled` gets
    # False unconditionally and the suppression silently stops working. Found in
    # self-review: a product page scoring 5 against a rail of 3-3-3 suppresses
    # correctly, and would have started CLICKING a rail item the moment two of
    # those three sold out. Whether a page IS what the user asked for is a fact
    # about the page and their words; the store's stock has nothing to do with it.
    all_tied: tuple[Candidate, ...] = ()


def item_choice(
    target: list[str],
    elements: Iterable[Any],
    *,
    find_price: Optional[Any] = None,
) -> Optional[ItemChoice]:
    """The whole "which one did you mean?" verdict, or None to carry on.

    `tied_matches` answers "can the user's words separate these?". This adds the
    second question the live incident showed was missing — "can the STORE even
    sell them?" — and the rules are `unresolved_axis`'s, applied one layer up
    because they are the same rules about who is entitled to choose:

    1. AN UNBUYABLE ITEM IS NOT AN OPTION. MEASURED on the incident: of the 20
       things matching "janan", SIX were sold out and all six were offered. The
       user picking one would have been sent to a page that cannot add it.
    2. IF EXACTLY ONE SURVIVES, IT IS FORCED, so it is taken rather than asked
       about — `unresolved_axis` rule 3 verbatim ("six sizes, one in stock:
       offering all six offers five dead ends"). The user's words could not
       separate these; availability did.
    3. IF NOTHING SURVIVES, STILL ASK — but say so. Silently returning "no
       question" there would hand the page to the model with every option dead,
       and silently returning the buyable set (empty) would be a question with
       no answers. The options carry SOLD_OUT_MARK and the caller says plainly
       that none can be added.

    None means "carry on exactly as before" — no target words, nothing matched,
    or one clear leader — which is `tied_matches`' own contract, unchanged."""
    tied_all = tied_matches(target, elements, find_price=find_price)
    if len(tied_all) < 2:
        return None
    every = tuple(tied_all)
    buyable = tuple(c for c in tied_all if c.available)
    dropped = len(tied_all) - len(buyable)
    if not buyable:
        return ItemChoice(
            tied=every, dropped=dropped, none_buyable=True, all_tied=every
        )
    if len(buyable) == 1:
        return ItemChoice(settled=buyable[0], dropped=dropped, all_tied=every)
    return ItemChoice(tied=buyable, dropped=dropped, all_tied=every)


def page_subject(title: str, url: str = "") -> str:
    """What THIS page is itself about, in its own words — its title plus the
    URL's path segments.

    ⚠️ NEVER THE QUERY STRING, and for the mirror image of `target_tokens`'
    host-only rule. A results page carries the user's own words in `?q=janan`,
    so counting them would let every search page claim to BE whatever was
    searched for — scoring the request against itself. The path is the site's
    own naming of the resource (`/products/janan-sport-30ml`); the query is the
    request.

    ⚠️ AND THE TITLE IS THE SAME REQUEST ARRIVING BY ANOTHER CHANNEL
    (live 2026-08-10). Refusing the query string was not enough, because a
    storefront prints the query INTO its title: Shopify serves
    `Search: 1000 results found for "black kameez kurta"`. MEASURED on the
    incident, against a forced three-way tie:

        subject 5   vs   best candidate 2   ->  page_is_the_target = True

    so the belt that means "this page IS the thing, do not ask about the items
    listed on it" fired on a SEARCH RESULTS PAGE and suppressed the one question
    that had to be asked. The guard's own recorded measurement ("search page 1 vs
    candidates 1 -> asks") held only because that query was ONE WORD; a
    multi-word query always wins, because the title echoes every word while no
    single product carries them all.

    So the request's words are dropped WHEREVER they appear, and the query
    string is where the request is knowable — a comparator, not a `Search:`
    keyword list. Harmless params contribute nothing: on the incident's product
    URL the values are `1`, `ca55448a0`, `r`, none of which is in its title."""
    parts = _tokens(title)
    try:
        parsed = urlparse(url or "")
        path = parsed.path or ""
        query = parsed.query or ""
    except ValueError:  # a malformed URL simply contributes no path
        path, query = "", ""
    parts += _tokens(path)
    request: set[str] = set()
    for values in parse_qs(query).values():
        for value in values:
            request.update(_tokens(value))
    kept: list[str] = []
    for token in parts:
        if token in _GOAL_STOPWORDS or token in request or token in kept:
            continue
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


def page_covers_target(target: list[str], title: str, url: str) -> bool:
    """True when this page's OWN identity already carries every word the user
    used — so it is the thing they named, and searching for it would navigate
    away from the answer.

    The sibling of `page_is_the_target`, for the case where there is nothing to
    compare against: that one asks "does the page beat the items listed ON it?"
    and needs a tie; this asks the simpler "is the page already it?" and is used
    before any listing exists — by the deterministic search leg, which fires on
    the START URL (2026-08-09).

    Uses the SAME scorer and the SAME page_subject (title + URL PATH, never the
    query string — a results page carries the user's own words in `?q=` and would
    otherwise always claim to BE whatever was searched for). Requires EVERY idea
    rather than a majority: a partial match is exactly the ambiguous case where
    searching is the right move ("janan perfume" on the Janan Sport page still
    wants the search, because "perfume" is not on it).

    ⚠️ AGAINST `_concepts`, NOT THE RAW TARGET. `_score` counts the user's
    distinct IDEAS, so its ceiling is the number of concepts; comparing to
    `len(target)` — which also holds the joined-pair spellings — would make this
    unsatisfiable for any multi-word request and silently switch the belt off."""
    words = [t for t in target if t]
    if not words:
        return False
    subject = page_subject(title, url)
    if not subject:
        return False
    return _score(words, subject) == len(_concepts(words))


def answered_here(chosen: str, title: str, url: str) -> bool:
    """True when this page IS the item the user already picked — so the things
    listed ON it are not a question to put to them a second time.

    ⚠️ THE DEFECT THIS EXISTS FOR (live 2026-08-09). Asked for a "black plain
    shalwar kameez", the user was shown two products, picked BLACK COTTON CASUAL
    KAMEEZ SHALWAR, the run navigated into it — and the product page's related
    rail was put to them as a fresh question. `page_is_the_target` should have
    suppressed it and could not: MEASURED, the page scores 9 and the rail scores
    9, because the rail holds two other garments carrying the page's own name,
    and that test needs STRICTLY greater (rightly — `>=` would silence the
    search page, where the question must be asked).

    So this asks the question directly instead of by proxy. `page_is_the_target`
    infers "have we selected yet?" from scores; once the user has ANSWERED, the
    answer is a fact and nothing needs inferring. It is also why the same run
    did NOT re-ask on `janan leather` — there the proxy happened to work (page 2,
    rail 1) — which is precisely the inconsistency the user reported: the proxy's
    verdict depends on how specific their words happened to be.

    MEASURED on both incidents: True on the kameez page and on the janan-leather
    page, and False on a DIFFERENT product's page (janan gold, chosen janan
    sport) — where the question must still be asked.

    Reuses `page_covers_target`, so it cannot disagree with the search leg that
    already decides "this page IS what was asked for"."""
    words = target_tokens(chosen or "")
    if not words:
        return False
    return page_covers_target(words, title, url)


def plain_words(target: Iterable[str]) -> list[str]:
    """The target tokens a person would recognise as their own words.

    `target_tokens` carries adjacent-joined pairs ("100 ml" -> "100ml") because
    a page may spell a name either way, but they are an internal matching device
    and reading them back is noise — live 2026-08-02 the question said it was
    matching 'find product named janan findproduct productnamed'.

    This IS `_concepts`, and delegating rather than repeating it is the point:
    since 2026-08-10 the same list decides scoring, so a second copy here could
    drift from the one the verdict is computed with — the second-copy-of-a-fact
    hole this codebase has recorded eight times."""
    return _concepts(target)


def locate(
    chosen: str, elements: Iterable[Any], *, find_price: Optional[Any] = None
) -> Optional[Candidate]:
    """The element on THIS page that the user's chosen option names, or None.

    ENFORCE, NEVER TRUST — the `_apply_site_correction` / folder_resolver rule.
    After the user answers, the resumed run must act on the thing they picked;
    re-asking the model would hand it the same ambiguous goal that produced the
    question. So the choice is turned back into an element in CODE, at zero LLM
    cost. None when the page no longer shows it, or when it still cannot be told
    apart — code never picks, and the loop then carries on normally.

    The SOLD_OUT_MARK is stripped first: it is something WE appended to the
    option, not part of the page's own name for the thing, and leaving it in
    would score two stray tokens against every label."""
    words = _words_with_pairs(str(chosen or "").replace(SOLD_OUT_MARK, " "))
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


# ------------------------------------------------------- the axes a form offers
# 2026-08-08. The tie test above answers "WHICH ITEM did you mean?" from a
# listing. This answers the question one page later: the item is settled, and the
# form that adds it to the cart still needs a SIZE.
#
# ⚠️ IT IS ANCHORED TO THE ARMED FORM, NEVER TO THE PAGE, and that is measured
# rather than cautious: the only visible <select> on a real product page here is
# the REVIEW-SORT dropdown, so a page-anchored gate would ask the user to choose
# "Most recent". The form is also the only thing that knows which controls a
# submit would actually carry.
#
# NOTHING HERE IS PER-SITE OR PER-CATEGORY. An axis is any named group of form
# controls offering more than one value, so "Size" on a kurta, "50ml/100ml" on a
# perfume and "Waist" on a trouser are the same rule — the factors come from the
# form, never from a list this module keeps.

# A name a person cannot read is worse than no name at all: the question says
# "this option" instead. Measured on a real storefront — the MAIN product form
# names its axes `Size` / `Color` / `Style`, while the related-product cards name
# the identical axis `option-15623440335008-1`.
_OPAQUE_AXIS_RE = re.compile(
    r"""^(?:
          option[-_]?\d          # option-15623440335008-1
        | id | variant(?:[-_]?id)?
        | [0-9a-f]{8,}           # a bare hash/id
    )""",
    re.I | re.X,
)


@dataclass(frozen=True)
class AxisOption:
    """One value a form control offers. `label` is the page's own text for it;
    `value` is what a submit would carry. Both come from the DOM verbatim."""

    value: str
    label: str = ""
    chosen: bool = False
    available: bool = True

    def option(self) -> str:
        """What a question shows and matches a reply against — the human label
        where the page has one, else the raw value (a swatch's <label> is often
        empty because the size is drawn as a styled box)."""
        return _clip(self.label or self.value)


@dataclass(frozen=True)
class Axis:
    """One choice a form offers, with every value it lists."""

    name: str
    options: tuple[AxisOption, ...] = ()

    @property
    def buyable(self) -> tuple[AxisOption, ...]:
        return tuple(o for o in self.options if o.available)

    @property
    def chosen(self) -> Optional[AxisOption]:
        for option in self.options:
            if option.chosen:
                return option
        return None


@dataclass(frozen=True)
class AxisChoice:
    """What to do about one axis: take `settled` in code, or offer `options`."""

    axis: str
    field: str = ""          # a readable name, "" when the form's is opaque
    settled: str = ""        # a value CODE may apply without asking
    options: tuple[str, ...] = ()
    why: str = ""


def readable_axis_name(name: str) -> str:
    """The axis name to SHOW, or "" when the form's own name is machine noise."""
    text = str(name or "").strip()
    inner = re.match(r"^properties\[\s*_?(.+?)\s*\]$", text)
    if inner:
        text = inner.group(1)
    if not text or _OPAQUE_AXIS_RE.match(text):
        return ""
    text = " ".join(text.replace("_", " ").replace("-", " ").split())
    return _clip(text) if len(text) > 1 else ""


def axes_of(contract: Any) -> list[Axis]:
    """The choice axes out of a form-contract read. Tolerant by construction: a
    malformed entry is skipped, never raised — a contract that cannot be parsed
    must degrade to "this form offers no choices", which is the behaviour that
    existed before axes were read at all."""
    raw = []
    if isinstance(contract, dict):
        raw = contract.get("axes") or []
    elif isinstance(contract, (list, tuple)):
        raw = contract
    axes: list[Axis] = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        options: list[AxisOption] = []
        for opt in entry.get("options") or []:
            if not isinstance(opt, dict):
                continue
            value = str(opt.get("value") or "")
            label = str(opt.get("label") or "").strip()
            if not value and not label:
                continue
            options.append(
                AxisOption(
                    value=value,
                    label=label,
                    chosen=bool(opt.get("chosen")),
                    # ABSENT MEANS AVAILABLE. A contract read by an older build,
                    # or a control whose markup carries no stock signal at all,
                    # must not read as "everything is sold out" — that would
                    # silently switch the gate off.
                    available=bool(opt.get("available", True)),
                )
            )
        if name and len(options) > 1:
            axes.append(Axis(name=name, options=tuple(options)))
    return axes


def _compact(text: str) -> str:
    """Lowercased alphanumerics only: "100 ml" and "100ml" and "100-ML" all read
    the same. Used ONLY for exact answer matching, never for scoring."""
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


# CLOTHING SIZES, BOTH WAYS ROUND (2026-08-10). MEASURED on the incident's own
# product page: the axis is a radio group offering XS / S / M / L / XL / XXL,
# and the user said "size large". Nothing could match those — `_answered` needs
# an exact string and `_tokens("L")` is EMPTY, so the prose path cannot see a
# one-letter option either. The user's own answer was therefore ignored and they
# were asked a question they had just answered.
#
# A closed table of the words people say for the letters a size control uses.
# Both sides are canonicalised, so "large" matches "L" AND "L" matches "Large".
# It is deliberately ONLY consulted for AXIS OPTIONS — a short, known set of
# values the page itself offers — never for product names, where "small" is an
# ordinary adjective.
_SIZE_WORDS = {
    "extrasmall": "xs", "xsmall": "xs", "xs": "xs",
    "small": "s", "sm": "s", "s": "s",
    "medium": "m", "med": "m", "m": "m",
    "large": "l", "lg": "l", "l": "l",
    "extralarge": "xl", "xlarge": "xl", "xl": "xl",
    "extraextralarge": "xxl", "xxlarge": "xxl", "2xl": "xxl", "xxl": "xxl",
    "3xl": "xxxl", "xxxl": "xxxl",
}


def _size_key(text: str) -> str:
    """A size written any of the ways people write it, in one canonical form.
    Anything that is not a size comes back as its own compact spelling, so this
    can only ever ADD a match between two spellings of the same size."""
    compact = _compact(text)
    return _SIZE_WORDS.get(compact, compact)


def _named_size(words: Iterable[str], options: tuple[AxisOption, ...]) -> str:
    """The one option whose SIZE the user named, or "" when they named none or
    could not separate two. Fail-closed, the `pick_by_answer` rule: if two
    options canonicalise the same way, nothing is chosen."""
    keys = {_size_key(w) for w in words if _size_key(w) in set(_SIZE_WORDS.values())}
    if not keys:
        return ""
    hits = [
        o for o in options
        if _size_key(o.value) in keys or _size_key(o.label) in keys
    ]
    return hits[0].value if len(hits) == 1 else ""


def _mentions_axis(words: Iterable[str], axis_name: str) -> bool:
    """Did the user's text name THIS axis — "select SIZE large"?

    ⚠️ THE GUARD ON THE PROSE PATH, and it is what keeps a real rule from
    becoming a loose one. A size word in a sentence is not always an answer
    about the size: in "add the small grey shirt to my cart" it describes the
    product, and settling S off it would answer a question the user never
    addressed. A direct REPLY needs no such proof (they were asked "which
    one?"); prose does, and the axis's own name is that proof."""
    wanted = {t for t in _tokens(readable_axis_name(axis_name))} | {
        t for t in _tokens(axis_name)
    }
    if not wanted:
        return False
    said = {t for t in words}
    return any(any(tokens_match(w, s) for s in said) for w in wanted)


def _answered(answer: str, options: tuple[AxisOption, ...]) -> str:
    """The option a DIRECT REPLY names, by exact (normalised) match.

    ⚠️ THE TOKEN SCORER CANNOT SEE A CLOTHING SIZE. `_tokens("M")` is EMPTY —
    the module's token rule drops anything under _MIN_STEM_CHARS because it was
    built to match product NAMES, where a stray letter is noise. Sizes are
    single letters, so "M" scores 0 against every option and a reply of "M"
    could never settle the question it was answering.

    An ANSWER is a different kind of text from a GOAL and earns a different rule:
    it is a direct reply to "which one?", so an exact hit on the page's own label
    or value is unambiguous. Prose is still scored by tokens, because scanning a
    sentence for the bare letter "s" would match almost anything.
    """
    key = _compact(answer)
    if not key:
        return ""
    for option in options:
        if _compact(option.value) == key or _compact(option.label) == key:
            return option.value
    # …and the same size said another way ("large" for an option labelled "L").
    return _named_size([answer], options)


def _sole_match(
    target: list[str], options: tuple[AxisOption, ...], *, axis_name: str = ""
) -> str:
    """The one option the user's own words name, or "" when their words name
    none or cannot separate two. Fail-closed, the `pick_by_answer` rule."""
    scored = [(_score(target, o.option()), o) for o in options]
    hits = [(s, o) for s, o in scored if s > 0]
    if not hits:
        # A SIZE THE TOKEN SCORER CANNOT SEE. "select size large and add to
        # cart" reaches here as prose, and an option labelled "L" is a single
        # character — below the token rule's length gate, so it scores 0 against
        # everything, and the user's own answer was ignored (live 2026-08-10).
        #
        # Only when they NAMED this axis, so a size word describing the product
        # ("the small grey shirt") is not read as an answer about the size.
        if axis_name and _mentions_axis(target, axis_name):
            return _named_size(target, options)
        return ""
    top = max(s for s, _ in hits)
    leaders = [o for s, o in hits if s == top]
    return leaders[0].value if len(leaders) == 1 else ""


def unresolved_axis(
    axes: Iterable[Axis],
    target: list[str],
    *,
    answer: str = "",
    limit: int = MAX_CHOICE_OPTIONS,
) -> Optional[AxisChoice]:
    """The first axis that still needs settling — or None when the form is
    already fully specified and may be submitted as it stands.

    The rules, in order, and each one is a decision about who is entitled to
    make the choice:

    1. THE USER'S OWN WORDS WIN, always. If they said "100ml" and something else
       is selected, code takes 100ml — enforce, never trust (the 2026-08-02
       select_option rule, which exists because the model picked "50 ML" for a
       user who had said 100ml). Their direct REPLY to a previous ask is matched
       exactly (see `_answered`), their prose by tokens.
    2. AN UNBUYABLE AXIS IS NOT A QUESTION. Every value sold out is the site's
       answer, not the user's; leave the form alone and let the submit report
       what the site says.
    3. ONE BUYABLE VALUE IS NOT A QUESTION EITHER — it is forced, so code takes
       it. MEASURED: a real kurta lists six sizes with exactly ONE in stock, so
       a gate that offered "every value" would have offered five dead ends.
    4. AN AXIS THE PAGE HAS ALREADY SETTLED is disclosed on the approval card
       (which names it in words since 2026-08-02) and is not re-litigated here.
    5. Otherwise ASK, offering only what can actually be bought.
    """
    for axis in axes or ():
        if len(axis.options) < 2:
            continue
        buyable = axis.buyable
        pool = buyable or axis.options
        wanted = _answered(answer, pool) or _sole_match(
            target, pool, axis_name=axis.name
        )
        if wanted:
            current = axis.chosen
            if current is not None and current.value == wanted:
                continue  # already what they asked for
            return AxisChoice(
                axis=axis.name,
                field=readable_axis_name(axis.name),
                settled=wanted,
                why="your own words name it",
            )
        if not buyable:
            continue
        if axis.chosen is not None:
            continue
        if len(buyable) == 1:
            return AxisChoice(
                axis=axis.name,
                field=readable_axis_name(axis.name),
                settled=buyable[0].value,
                why="it is the only one in stock",
            )
        return AxisChoice(
            axis=axis.name,
            field=readable_axis_name(axis.name),
            options=tuple(o.option() for o in buyable[:limit]),
            why="nothing you said picks one",
        )
    return None


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
