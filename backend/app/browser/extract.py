"""
Furi OS — deterministic structured extraction from an Observation.

WHY THIS EXISTS (the defect it repairs, 2026-07-26)
--------------------------------------------------
`browser_loop._extract_data` asked an LLM to read records off `obs.page_text`.
Live on daraz.pk's real search-results page it returned ZERO records three times
in a row and the run died — on a page holding 158 product cards. Two causes,
both structural:

1. `page_text` was truncated to the PROMPT budget at CAPTURE, so extraction could
   only ever see the first 4000 chars of `body.innerText`: header, nav, category
   rail, filters. The products were past it. (Fixed in observe.py — see CAPTURE
   vs RENDER there.)
2. The products were never in the prose to begin with, and never needed to be.
   Daraz's grid is `<div>` cards; observe.py's wide tier lists each one as an
   element whose accessible name IS the card's own innerText — title, price and
   rating together. **The extractor never looked at the element list.** The loop
   could see the grid and could not read it.

So this module reads the ELEMENT LIST, which is where a results grid actually
lives, and it does it in code.

WHY DETERMINISTIC, not another LLM call
--------------------------------------
Three reasons, in the order they matter:

- **It cannot fabricate.** Every value it emits is a slice of the observation.
  That is a stronger guarantee than any prompt ("copy values EXACTLY" was already
  in the extract prompt when the FIFA round produced 112 invented countries), and
  a test asserts it: every value in every record is a substring of its source
  element's text.
- **It costs nothing.** The measured extract step was 24-26s wall-clock, ~15s of
  it that one LLM call. On a grid page this path answers in microseconds.
- **It is site-agnostic.** Currency-shaped and rating-shaped tokens are generic
  text patterns — the same class of thing as `loop._top_result_action`'s
  title-token ranking. There is no per-site DOM knowledge here and there must
  never be.

WHEN IT DECLINES
----------------
It answers only when it is sure, and defers otherwise — the "code never picks
when unclear" rule that `placeholder_resolver` and `folder_resolver` are built
on:

- fewer than `_MIN_CARDS` cards found ⇒ `[]` (one priced line is a page, not a
  list);
- a requested field it cannot parse (`seller`, `delivery time`) ⇒ records still
  come back but `covers_requested` is False, so the caller runs the LLM and keeps
  these only if that returns nothing (evidence is not a deletion);
- no currency token in a card ⇒ not a card. A bare number is a rating, a review
  count, a page number or a discount as often as it is a price, and guessing
  which would be exactly the fabrication this module exists to prevent.

Non-ASCII characters in the patterns below are written as \\u escapes on purpose:
this file is edited by tools that match source text exactly, and a literal thin
space or currency glyph is invisible to that matching.

This module is PURE — it imports nothing from `app.*` (the observe.py rule), so
it cannot be a cycle and needs no fixture to test.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# A card needs at least this many words of non-price text to count as an item —
# "Rs. 500 off" is not a product.
_MIN_TITLE_WORDS = 3
# One priced line is not a list. Two of the same shape is the weakest honest
# evidence that this page enumerates items.
_MIN_CARDS = 2
_MAX_RECORDS = 60
_TITLE_MAX = 200
# How many non-price lines a title may survive before it stops being the title of
# the next price it sees. Sized from the measured daraz sequence (title, price,
# then "% Off" / "Coins save" / "N sold" / "(95)" / a city before the next title):
# generous enough for that, tight enough that a page heading never pairs with a
# price further down the page.
_PROSE_GAP = 6
# The element list's share of the LLM-path budget. It keeps a floor even when the
# prose plainly holds the data, because the element list is also how the model
# knows what it can CLICK next — starving it entirely would trade one blindness
# for another.
_ELEMENT_SHARE_MAX = 0.6
_ELEMENT_SHARE_MIN = 0.2

# Form controls: a price inside a filter box or a "max price" input is not an
# item. Buttons are deliberately NOT excluded — plenty of grids make each card a
# button.
_SKIP_ROLES = frozenset(
    {"input", "textbox", "searchbox", "password", "checkbox", "radio", "file", "option"}
)

# --------------------------------------------------------------- text patterns
# Longest alternatives first so "US$" beats "$" and "Rs." beats "Rs".
#
# The LETTER codes carry a leading \b and that is not decoration: without it,
# case-insensitive `Rs` matches INSIDE "hours", so "delivery in 24 hours 3 days"
# parsed as a price. Symbols need no boundary — they are never inside a word.
_CURRENCY_WORD = r"(?:US\$|Rs\.?|PKR|USD|EUR|GBP|INR|AED|SAR|CAD|AUD)"
# ₨ Rs sign, ₹ rupee, £ pound, € euro, ¥ yen, ₩ won
_CURRENCY_SYMBOL = "(?:₨|₹|£|€|\\$|¥|₩)"
_CURRENCY = r"(?:\b" + _CURRENCY_WORD + "|" + _CURRENCY_SYMBOL + ")"
# Grouped thousands (comma, plain space,   thin space,   narrow no-break
# space) or a plain number, either with up to two decimals.
_AMOUNT = (
    "\\d{1,3}(?:[,   ]\\d{3})+(?:\\.\\d{1,2})?"
    "|\\d+(?:\\.\\d{1,2})?"
)
# A PRICE REQUIRES A CURRENCY TOKEN, before or after the amount. This is the
# precision property that keeps ratings, review counts, discount percentages and
# page numbers out — and the reason a page whose prices carry no currency token
# makes this module DECLINE rather than guess which number is the price.
#
# The two forms are SEPARATE patterns, tried prefix-first, because a single
# alternation is scanned by POSITION and the suffix branch then swallows an
# unrelated leading number: "Nike Pegasus 41 $129.99" parsed as "41 $" (measured,
# not hypothesised). Prefix-first also matches how the currency is written on the
# overwhelming majority of pages, so the suffix form only ever runs on pages that
# genuinely use it ("45,000 PKR").
_PRICE_PREFIX_RE = re.compile(
    "(?:" + _CURRENCY + ")\\s*(?:" + _AMOUNT + ")", re.IGNORECASE
)
_PRICE_SUFFIX_RE = re.compile(
    "(?:" + _AMOUNT + ")\\s*(?:" + _CURRENCY + ")", re.IGNORECASE
)
# ★ black star, ⭐ star emoji, ☆ white star
#
# The third alternative is the retail-card idiom "4.7 (128)" — a score beside its
# review count, which is how essentially every product grid prints a rating and
# which the first two forms miss entirely (measured on the daraz cards). It is
# kept narrow on purpose: the DECIMAL is required, so a size or a quantity
# ("5 (2 left)") does not match, and the parenthesis must hold a number.
_RATING_RE = re.compile(
    "(?:★|⭐|☆|\\brating\\s*[:\\-]?\\s*)\\s*([0-5](?:[.,]\\d)?)\\b"
    "|\\b([0-5](?:[.,]\\d)?)\\s*(?:/\\s*5|out\\s+of\\s+5|stars?\\b)"
    "|\\b([0-5][.,]\\d)\\s*\\(\\s*\\d[\\d,.]*\\s*\\)",
    re.IGNORECASE,
)
# – en dash, — em dash, · middle dot, • bullet
_TITLE_TRIM = " \t-|,:;/\\–—·•"

# --------------------------------------------------------------- field mapping
# Requested field name → the canonical thing we can parse for it. Anything absent
# from here is a field this module cannot answer, and saying so is the point.
_CANONICAL: dict[str, str] = {
    "title": "title", "name": "title", "product": "title", "product_name": "title",
    "item": "title", "item_name": "title", "heading": "title", "model": "title",
    "description": "title",
    "price": "price", "cost": "price", "amount": "price", "current_price": "price",
    "sale_price": "price", "prices": "price",
    "rating": "rating", "ratings": "rating", "stars": "rating",
    "star_rating": "rating", "score": "rating", "review_score": "rating",
    "url": "url", "link": "url", "href": "url", "product_url": "url",
}
_DEFAULT_FIELDS = ("title", "price", "rating", "url")


@dataclass(frozen=True)
class StructuralResult:
    """`records` is what was found; `covers_requested` says whether it answers
    every field the caller named. They are separate because a partial answer is
    still evidence — the caller runs the LLM and falls back to these if it
    returns nothing, rather than throwing real rows away."""

    records: list[dict] = field(default_factory=list)
    covers_requested: bool = False
    # WHICH reader produced them: "elements" or "page text". Carried so the log can
    # name the path that actually ran — a message that says "from the element list"
    # when the prose answered is the same species of misleading record this whole
    # round exists to remove.
    source: str = ""

    def __bool__(self) -> bool:
        return bool(self.records)


# ------------------------------------------------------------------ extraction
def structured_records(
    observation: Any, fields: Optional[Iterable[str]] = None
) -> StructuralResult:
    """Read item records off the observation's ELEMENT LIST, deterministically.

    Never raises: extraction is best-effort everywhere in this stack, and a
    malformed observation must degrade to "found nothing", not to a crash."""
    try:
        return _structured_records(observation, fields)
    except Exception:  # noqa: BLE001 — a parse must never break a browse
        return StructuralResult()


def _structured_records(
    observation: Any, fields: Optional[Iterable[str]]
) -> StructuralResult:
    requested = [str(f).strip() for f in (fields or []) if str(f or "").strip()]
    wanted = _canonical_fields(requested)

    cards: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for element in getattr(observation, "elements", None) or []:
        parsed = _card_of(element)
        if parsed is None:
            continue
        key = (parsed["title"].casefold(), parsed.get("price", ""))
        if key in seen:
            continue
        seen.add(key)
        cards.append(parsed)
        if len(cards) >= _MAX_RECORDS:
            break

    cards = _drop_nested(cards)
    source = "elements"
    # THE ELEMENT LIST IS NOT ALWAYS WHERE THE DATA IS, and assuming it was cost a
    # whole round. MEASURED on daraz.pk's real results page: 155 elements, 125 of
    # them links, and ZERO carrying a price — the site puts the title inside the
    # <a> and the price in a SIBLING node, so "one element = one card" is simply
    # false there. The same page's prose holds all of it, 57 price tokens in a
    # clean `Title / Rs. X / ...` sequence. So when the elements do not answer,
    # read the prose the same way.
    if len(cards) < _MIN_CARDS:
        cards = _prose_cards(observation)
        source = "page text"

    if len(cards) < _MIN_CARDS:
        return StructuralResult()

    records = [r for r in (_project(c, requested, wanted) for c in cards) if r]
    if len(records) < _MIN_CARDS:
        return StructuralResult()
    return StructuralResult(
        records=records, covers_requested=_covers(records, requested), source=source
    )


def _covers(records: list[dict], requested: list[str]) -> bool:
    """Did we actually DELIVER every field the caller named, on every row?

    Asked of the OUTPUT, not of the field names. The first version asked "do we
    know how to parse these fields in principle", which claimed a rating column
    was answered on cards that carried no readable rating — a claim the caller
    then acts on by skipping the LLM. Only what is on the rows counts."""
    if not requested:
        return bool(records)
    for name in requested:
        if not _CANONICAL.get(_key(name)):
            return False            # a field this module cannot parse at all
        if any(not record.get(name) for record in records):
            return False            # parseable, but missing on at least one row
    return True


def _key(name: str) -> str:
    return name.lower().replace(" ", "_").replace("-", "_")


def _canonical_fields(requested: list[str]) -> list[str]:
    """The canonical fields to fill. An unrecognised request falls back to the
    default set rather than emitting nothing — the rows are still useful, and
    `covers_requested` is what tells the caller they are incomplete."""
    out: list[str] = []
    for name in requested:
        canonical = _CANONICAL.get(_key(name))
        if canonical and canonical not in out:
            out.append(canonical)
    return out or list(_DEFAULT_FIELDS)


def _card_of(element: Any) -> Optional[dict]:
    """One element → a card dict, or None when it is not an item.

    The title is the text BEFORE the first price, because that is where a card
    puts it on essentially every grid ever shipped. When that head is too short
    (a card leading with a badge or a discount), fall back to the whole text with
    the priced and rated spans removed.

    HONEST LIMIT: `price` is the FIRST currency token in the card, not the
    cheapest. A discounted card reads "Rs. 24,999  Rs. 29,499" — current price
    first — so first is right far more often than min would be ("Rs. 500 off"
    would win a min). It is always a verbatim slice of the page either way."""
    role = str(getattr(element, "role", "") or "").lower()
    if role in _SKIP_ROLES:
        return None
    text = _element_text(element)
    if not text:
        return None

    price_match = find_price(text)
    if price_match is None:
        return None

    head = text[: price_match.start()].strip(_TITLE_TRIM)
    title = head if _word_count(head) >= _MIN_TITLE_WORDS else _strip_tokens(text)
    if _word_count(title) < _MIN_TITLE_WORDS:
        return None

    card = {"title": title[:_TITLE_MAX], "price": price_match.group(0).strip()}
    rating = _rating_of(text)
    if rating:
        card["rating"] = rating
    href = str(getattr(element, "href", "") or "").strip()
    if href:
        card["url"] = href
    return card


def _prose_cards(observation: Any) -> list[dict]:
    """Item records read off the page's PROSE, as a line sequence.

    The generic "list page" idiom, and it is remarkably consistent across the web:
    a product/listing renders as its title on one line and its price a line or two
    later, with badges and counts in between. daraz.pk:

        Yonex, Hi Qua badminton Racket (single) premium quality
        Rs. 1,750
        33% Off
        Coins save Rs. 18
        501 sold
        (95)
        Punjab
        Yonex Badminton Racket Astrox Smash with Carrey bag ...
        Rs. 2,290

    So: remember the most recent title-shaped line; when a priced line arrives
    within a few lines of it, that pair is a record. Details that matter —

      - the NEAREST preceding title wins, so headings further up cannot capture a
        price that is not theirs;
      - a price with no title pending is ignored, which is what keeps "Coins save
        Rs. 18" from becoming an item;
      - a title goes stale after `_PROSE_GAP` lines, so a heading at the top of a
        page never pairs with the first price far below it.

    Reads `text_full` — the prose as CAPTURED, not the prompt-clipped `page_text`.
    On the measured page that is the difference between 4000 and 7332 chars, i.e.
    about half the products."""
    text = str(
        getattr(observation, "text_full", "")
        or getattr(observation, "page_text", "")
        or ""
    )
    if not text:
        return []

    records: list[dict] = []
    title = ""
    distance = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        price = find_price(line)
        if price is not None:
            if title and distance <= _PROSE_GAP:
                record = {"title": title[:_TITLE_MAX], "price": price.group(0).strip()}
                rating = _rating_of(line)
                if rating:
                    record["rating"] = rating
                records.append(record)
                title = ""
            continue
        if _word_count(line) >= _MIN_TITLE_WORDS and not _PRICE_PREFIX_RE.search(line):
            title = line
            distance = 0
        elif title:
            distance += 1
            if distance > _PROSE_GAP:
                title = ""
        if len(records) >= _MAX_RECORDS:
            break
    return records


def _element_text(element: Any) -> str:
    """The element's fullest name. `name_full` is the pre-prompt-clip text (see
    observe.py); `name` is the fallback for a fake element or an older shape."""
    return str(
        getattr(element, "name_full", "") or getattr(element, "name", "") or ""
    ).strip()


def find_price(text: str) -> Optional[re.Match]:
    """The first price in `text`, currency-before-amount preferred. Public because
    the browse benchmark asserts a reported price against the page's own text and
    must use exactly this notion of "a price"."""
    return _PRICE_PREFIX_RE.search(text or "") or _PRICE_SUFFIX_RE.search(text or "")


def _rating_of(text: str) -> str:
    match = _RATING_RE.search(text)
    if match is None:
        return ""
    # Whichever alternative matched — both branches capture the number itself, so
    # the emitted value is always a substring of the page's own text.
    return (match.group(1) or match.group(2) or match.group(3) or "").strip()


def _strip_tokens(text: str) -> str:
    """The text with every priced and rated span removed — the fallback title."""
    without = _PRICE_PREFIX_RE.sub(" ", text)
    without = _PRICE_SUFFIX_RE.sub(" ", without)
    without = _RATING_RE.sub(" ", without)
    return re.sub(r"\s{2,}", " ", without).strip(_TITLE_TRIM)


def _word_count(text: str) -> int:
    return len([w for w in re.split(r"\s+", text or "") if w])


def _drop_nested(cards: list[dict]) -> list[dict]:
    """A grid frequently lists both a card and a link inside it, so the same item
    arrives twice with one title contained in the other. Keep the longer title
    (the fuller text) and drop the contained one when the price agrees."""
    order = {id(card): position for position, card in enumerate(cards)}
    keep: list[dict] = []
    for card in sorted(cards, key=lambda c: len(c["title"]), reverse=True):
        title = card["title"].casefold()
        if any(
            card.get("price") == other.get("price")
            and title in other["title"].casefold()
            for other in keep
        ):
            continue
        keep.append(card)
    # Restore the page's own order — position is meaning on a results page ("the
    # top 3 products"), and sorting by title length destroyed it.
    return sorted(keep, key=lambda c: order.get(id(c), 0))


def _project(card: dict, requested: list[str], wanted: list[str]) -> dict:
    """The card under the field names the CALLER asked for. A model that asked for
    `cost` gets `cost`; one that named nothing gets the default set."""
    if not requested:
        return {k: card[k] for k in wanted if card.get(k)}
    out: dict = {}
    for name in requested:
        canonical = _CANONICAL.get(_key(name))
        if canonical and card.get(canonical):
            out[name] = card[canonical]
    # A request we could not map at all (only `seller`, say) leaves nothing to
    # project — fall back to what we did find so the row is not empty.
    return out or {k: card[k] for k in wanted if card.get(k)}


# --------------------------------------------------------------- LLM fallback
def has_content(observation: Any) -> bool:
    """Is there anything on this page an extractor could read at all?

    Asked separately from `record_source` on purpose: that function always emits
    the URL and title, so "the source string is non-empty" is not the same
    question and answering it that way would spend an LLM call on a blank page.
    Content means a named element or some prose — nothing else counts."""
    prose = str(
        getattr(observation, "text_full", "")
        or getattr(observation, "page_text", "")
        or ""
    ).strip()
    if prose:
        return True
    return any(
        _element_text(element)
        for element in (getattr(observation, "elements", None) or [])
    )


def record_source(observation: Any, *, limit: int) -> str:
    """The corpus for the LLM extraction path: the ELEMENT LIST first, then the
    page's full prose.

    Elements first and within their own share, for the same reason `render()` does
    it — the dense, actionable half must never be crowded out by prose (the
    5-wide trap, restated in observe.py). Prose inherits whatever the elements did
    not use, so a text-heavy article page still gets the whole budget.

    THE SHARE FOLLOWS THE EVIDENCE, and it has to. A fixed 60% to elements was the
    first cut and it made the measured page WORSE: daraz's 155 elements carry no
    prices at all, so 5400 chars went to bare titles and the prose that held every
    price was cut to 3485 — less room than it had before the element list was
    added. Widening one channel without checking which channel holds the data is
    the 5-wide trap wearing a different hat. So: when the prose is visibly where
    the priced content is and the elements are not, the prose leads."""
    limit = max(0, int(limit or 0))
    if not limit:
        return ""

    element_lines = [
        line for line in (_source_line(e) for e in getattr(observation, "elements", None) or [])
        if line
    ]
    prose_all = str(
        getattr(observation, "text_full", "")
        or getattr(observation, "page_text", "")
        or ""
    ).strip()
    element_share = int(limit * _element_share_fraction(element_lines, prose_all))

    lines: list[str] = []
    used = 0
    for line in element_lines:
        if used + len(line) + 1 > element_share:
            break
        lines.append(line)
        used += len(line) + 1

    parts: list[str] = []
    url = str(getattr(observation, "url", "") or "")
    if url:
        parts.append(f"URL: {url}")
    title = str(getattr(observation, "title", "") or "")
    if title:
        parts.append(f"TITLE: {title}")
    if lines:
        parts.append("")
        parts.append(f"ITEMS ON THE PAGE ({len(lines)}):")
        parts.extend(lines)

    head = "\n".join(parts)
    if prose_all:
        room = limit - len(head) - len("\n\nPAGE TEXT:\n")
        if room > 0:
            head += "\n\nPAGE TEXT:\n" + prose_all[:room]
    return head[:limit]


def _element_share_fraction(element_lines: list[str], prose: str) -> float:
    """How much of the budget the element list earns.

    Counted, not assumed: if the prose carries priced content and the element list
    does not, the elements are titles and navigation — useful context, not the
    answer — so they get a floor and the prose gets the rest."""
    if not element_lines:
        return 0.0
    if not prose:
        return _ELEMENT_SHARE_MAX
    priced_elements = sum(1 for line in element_lines if find_price(line) is not None)
    priced_prose = len(_PRICE_PREFIX_RE.findall(prose)) + len(_PRICE_SUFFIX_RE.findall(prose))
    if priced_prose >= _MIN_CARDS and priced_elements < _MIN_CARDS:
        return _ELEMENT_SHARE_MIN
    return _ELEMENT_SHARE_MAX


def _source_line(element: Any) -> str:
    name = _element_text(element)
    if not name:
        return ""
    role = str(getattr(element, "role", "") or "element")
    href = str(getattr(element, "href", "") or "").strip()
    line = f'- {role}: "{name}"'
    if href:
        line += f" → {href}"
    return line
