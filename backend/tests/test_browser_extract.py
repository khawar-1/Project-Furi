"""
Deterministic structured extraction (app/browser/extract.py) — 2026-07-26.

THE LIVE DEFECT THESE PIN. Given "search Daraz for Yonex badminton rackets,
extract the top 3 with prices, and tell me which is cheapest", the loop reached
the real results page with 158 elements on it and then ran the same `extract`
three times, each returning ZERO records, until the repeat cap killed the run and
reported "the page didn't respond to that action after several tries". The page
had responded perfectly. Two causes:

  - `page_text` was truncated to the PROMPT budget at CAPTURE, so extraction
    could only see the first 4000 chars of body.innerText — header, nav, category
    rail, filters. The products sat past it.
  - The products were in the ELEMENT LIST all along (Daraz's grid is <div> cards;
    observe.py's wide tier names each one with the card's own innerText, which is
    title + price + rating), and the extractor never looked there.

So the properties worth pinning are: the element list is read; a real grid yields
real rows; the reader DECLINES rather than guesses; and it cannot invent a value.
"""
from dataclasses import dataclass, field

from app.browser import extract as ex


# --------------------------------------------------------------------- fixtures
@dataclass
class FakeElement:
    """Only what extract.py touches. Mirrors the real Element's attribute names
    exactly, including `name_full` — the pre-prompt-clip text (observe.py)."""

    role: str = "item"
    name_full: str = ""
    name: str = ""
    href: str = ""


@dataclass
class FakeObservation:
    elements: list = field(default_factory=list)
    url: str = "https://www.daraz.pk/catalog/?q=yonex"
    title: str = "Buy Yonex badminton racket Online at Best Prices"
    page_text: str = "clipped prose"
    text_full: str = "clipped prose and rather more of it"


def _grid():
    """A results grid shaped like the one that failed live: a search box, product
    cards whose whole text is one element name, the same item ALSO listed as an
    inner link, and an action button."""
    return FakeObservation(
        elements=[
            FakeElement("searchbox", "Search in Daraz"),
            FakeElement(
                "item",
                "Yonex Astrox 99 Pro Badminton Racket Rs. 24,999 -15% Rs. 29,499 4.7 (128)",
                href="/products/astrox-99-pro",
            ),
            FakeElement(
                "item",
                "Yonex Nanoflare 001 Feel Racket Rs. 8,499 4.2 (31)",
                href="/products/nanoflare-001",
            ),
            # The same product, listed again as the link inside its own card.
            FakeElement(
                "link", "Yonex Nanoflare 001 Feel Racket Rs. 8,499", href="/products/nanoflare-001"
            ),
            FakeElement(
                "item", "Yonex Arcsaber 11 Pro Racket Rs. 41,500 4.9 (12)", href="/products/arc-11"
            ),
            FakeElement("button", "Add to Cart"),
        ]
    )


# ------------------------------------------------------------- price / rating
def test_a_price_needs_a_currency_token():
    """The precision property. A bare number is a rating, a review count, a page
    number or a discount at least as often as it is a price — so requiring a
    currency token is what keeps this reader from guessing, and declining is the
    designed outcome when a site prints prices without one."""
    assert ex.find_price("Yonex Astrox Rs. 24,999").group(0) == "Rs. 24,999"
    assert ex.find_price("Headphones 45,000 PKR").group(0) == "45,000 PKR"
    assert ex.find_price("Nike Pegasus $129.99").group(0) == "$129.99"
    assert ex.find_price("Save 20% on 100 items") is None
    assert ex.find_price("Page 3 of 12") is None
    assert ex.find_price("4.7 out of 5 stars, 128 reviews") is None


def test_a_letter_currency_code_is_word_bounded():
    """MEASURED, not hypothesised: without a leading \\b, case-insensitive `Rs`
    matches inside "hours", so "delivery in 24 hours 3 days" parsed as a price."""
    assert ex.find_price("delivery in 24 hours 3 days") is None
    assert ex.find_price("ships in 2 hours") is None


def test_the_currency_prefix_form_wins_over_a_stray_leading_number():
    """A single alternation is scanned by POSITION, so the suffix branch swallowed
    an unrelated number: "Nike Air Zoom Pegasus 41 $129.99" parsed as "41 $"."""
    assert ex.find_price("Nike Air Zoom Pegasus 41 $129.99").group(0) == "$129.99"


def test_rating_shapes():
    assert ex._rating_of("Racket 4.7") == ""                # a bare number alone is not a rating
    assert ex._rating_of("Racket 4.7 (128)") == "4.7"       # score beside a review count IS
    assert ex._rating_of("2 left in stock (5)") == ""       # no decimal → not a rating
    assert ex._rating_of("Racket ★ 4.7") == "4.7"
    assert ex._rating_of("Rating: 4.2 Some Product") == "4.2"
    assert ex._rating_of("scored 4.5/5") == "4.5"
    assert ex._rating_of("4.5 out of 5") == "4.5"
    assert ex._rating_of("5 stars") == "5"
    assert ex._rating_of("no opinion here") == ""


# --------------------------------------------------------------------- records
def test_a_real_grid_yields_real_rows_under_the_requested_field_names():
    result = ex.structured_records(_grid(), ["name", "price"])
    assert result.covers_requested is True
    assert result.records == [
        {"name": "Yonex Astrox 99 Pro Badminton Racket", "price": "Rs. 24,999"},
        {"name": "Yonex Nanoflare 001 Feel Racket", "price": "Rs. 8,499"},
        {"name": "Yonex Arcsaber 11 Pro Racket", "price": "Rs. 41,500"},
    ]


def test_page_order_is_preserved():
    """Position is meaning on a results page — "the top 3 products" is a claim
    about order. De-nesting sorts by title length internally and must undo it."""
    prices = [r["price"] for r in ex.structured_records(_grid(), ["price"]).records]
    assert prices == ["Rs. 24,999", "Rs. 8,499", "Rs. 41,500"]


def test_the_same_item_listed_twice_is_one_row():
    """A grid lists both the card and the link inside it. The fuller text wins."""
    titles = [r["title"] for r in ex.structured_records(_grid(), []).records]
    assert titles.count("Yonex Nanoflare 001 Feel Racket") == 1
    assert "Yonex Nanoflare 001 Feel Racket 4.2 (31)" not in titles


def test_form_controls_and_unpriced_controls_are_not_items():
    """A price inside a "max price" filter box is not a product, and neither is
    the Add-to-Cart button sitting next to one."""
    rows = ex.structured_records(
        FakeObservation(
            elements=[
                FakeElement("input", "Max price Rs. 50,000 filter for you"),
                FakeElement("input", "Min price Rs. 1,000 filter for you"),
            ]
        ),
        [],
    )
    assert rows.records == []


def test_default_fields_include_rating_and_url_when_the_caller_names_nothing():
    first = ex.structured_records(_grid(), []).records[0]
    assert first["title"] == "Yonex Astrox 99 Pro Badminton Racket"
    assert first["price"] == "Rs. 24,999"
    assert first["rating"] == "4.7"
    assert first["url"] == "/products/astrox-99-pro"


def test_a_field_alias_is_honoured_verbatim():
    """A model that asked for `cost` gets a key named `cost`."""
    rows = ex.structured_records(_grid(), ["product", "cost"]).records
    assert set(rows[0]) == {"product", "cost"}
    assert rows[0]["cost"] == "Rs. 24,999"


# ------------------------------------------------------- when it must decline
def test_one_priced_line_is_not_a_list():
    """_MIN_CARDS: a page mentioning a single price is a page, not an
    enumeration. Declining hands the job to the LLM path."""
    one = FakeObservation(
        elements=[FakeElement("item", "Yonex Astrox 99 Pro Racket Rs. 24,999", href="/p/1")]
    )
    assert ex.structured_records(one, []).records == []


def test_a_prose_page_declines():
    assert (
        ex.structured_records(
            FakeObservation(elements=[FakeElement("link", "Some Article Title Here", href="/a")]),
            [],
        ).records
        == []
    )


def test_an_unparseable_field_still_returns_rows_but_reports_it_is_incomplete():
    """The caller runs the LLM for `seller` — and keeps these rows if the LLM
    finds nothing, because real page content beats reporting nothing."""
    result = ex.structured_records(_grid(), ["name", "price", "seller"])
    assert result.covers_requested is False
    assert len(result.records) == 3


def test_coverage_is_judged_on_the_ROWS_not_on_the_field_names():
    """The first version asked "can we parse a rating in principle" and answered
    yes for cards that print none — so the caller skipped the LLM and returned a
    ratings request with no ratings in it. Coverage is a property of the output."""
    unrated = FakeObservation(
        elements=[
            FakeElement("item", "Widget One Deluxe Model Rs. 100", href="/1"),
            FakeElement("item", "Widget Two Deluxe Model Rs. 200", href="/2"),
        ]
    )
    result = ex.structured_records(unrated, ["name", "rating"])
    assert result.records                      # the rows are real and kept
    assert result.covers_requested is False    # but the rating was not delivered


def test_a_discount_line_is_not_a_product():
    """"Rs. 500 off" has a price and no title — below _MIN_TITLE_WORDS."""
    rows = ex.structured_records(
        FakeObservation(
            elements=[FakeElement("item", "Rs. 500 off"), FakeElement("item", "Rs. 300 off")]
        ),
        [],
    )
    assert rows.records == []


def test_a_malformed_observation_degrades_to_nothing_rather_than_raising():
    assert ex.structured_records(object(), ["price"]).records == []
    assert ex.structured_records(None, None).records == []
    assert ex.structured_records(FakeObservation(elements=[None, 7, "x"]), []).records == []


def test_an_element_with_only_the_clipped_name_still_works():
    """`name_full` is defaulted for older observation shapes and every fake
    element in the suite — callers fall back to `name`."""
    rows = ex.structured_records(
        FakeObservation(
            elements=[
                FakeElement("item", name_full="", name="Widget One Deluxe Rs. 100"),
                FakeElement("item", name_full="", name="Widget Two Deluxe Rs. 200"),
            ]
        ),
        ["name", "price"],
    )
    assert [r["price"] for r in rows.records] == ["Rs. 100", "Rs. 200"]


# ------------------------------------------------ the prose path (the real daraz)
# MEASURED on daraz.pk's live results page, and it corrected the assumption this
# module was first built on: 155 elements, 125 of them links, and ZERO carrying a
# price — daraz puts the title inside the <a> and the price in a SIBLING node. The
# page's PROSE held all of it: 57 price tokens in a clean Title / Rs. X sequence.
# This is the verbatim shape.
_DARAZ_PROSE = """SAVE MORE ON APP SELL ON DARAZ HELP & SUPPORT LOGIN SIGN UP
SEARCH
watch for boys
Categories
Yonex badminton rackets
2594 items found for "Yonex badminton rackets"
Sort By:
Best Match
View:
Yonex, Hi Qua badminton Racket (single) premium quality
Rs. 1,750
33% Off
Coins save Rs. 18
501 sold
(95)
Punjab
Yonex Badminton Racket Astrox Smash with Carrey bag and one special shuttlecock
Rs. 2,290
24% Off
Coins save Rs. 23
7 sold
Punjab
Yonex, VS badminton Racket (single) premium quality
Rs. 1,544
48% Off
Coins save Rs. 46
34 sold
(6)
"""


def _daraz():
    """The real page: link elements carrying only titles, prices only in prose."""
    return FakeObservation(
        elements=[
            FakeElement("link", "LOGIN"),
            FakeElement("searchbox", "Search in Daraz"),
            FakeElement("link", "Yonex, Hi Qua badminton Racket (single) premium quality", href="/p/1"),
            FakeElement("link", "Yonex Badminton Racket Astrox Smash with Carrey bag and one special shuttlecock", href="/p/2"),
        ],
        page_text=_DARAZ_PROSE[:120],
        text_full=_DARAZ_PROSE,
    )


def test_the_prose_is_read_when_the_elements_carry_no_prices():
    result = ex.structured_records(_daraz(), ["name", "price"])
    assert result.source == "page text"
    assert result.covers_requested is True
    assert result.records == [
        {"name": "Yonex, Hi Qua badminton Racket (single) premium quality", "price": "Rs. 1,750"},
        {
            "name": "Yonex Badminton Racket Astrox Smash with Carrey bag and one special shuttlecock",
            "price": "Rs. 2,290",
        },
        {"name": "Yonex, VS badminton Racket (single) premium quality", "price": "Rs. 1,544"},
    ]


def test_a_price_with_no_title_pending_is_not_an_item():
    """"Coins save Rs. 18" sits between a product and the next one. Pairing it with
    the title above would invent an item; it must simply be skipped."""
    titles = [r["title"] for r in ex.structured_records(_daraz(), []).records]
    assert not any("Coins" in t for t in titles)
    assert all("Rs. 18" != r["price"] for r in ex.structured_records(_daraz(), []).records)


def test_a_page_heading_does_not_capture_a_distant_price():
    """A title goes stale after _PROSE_GAP lines, so a heading at the top of a page
    never pairs with the first price far below it."""
    prose = "\n".join(
        ["Some Page Heading Here"] + [f"filler line {i}" for i in range(12)] + ["Rs. 999"]
    )
    observation = FakeObservation(elements=[], page_text="", text_full=prose)
    assert ex.structured_records(observation, []).records == []


def test_the_element_path_still_wins_when_the_elements_do_carry_prices():
    """The prose path is a FALLBACK, not a replacement — a card grid must still be
    read from its cards, which is more precise (it carries the href)."""
    result = ex.structured_records(_grid(), ["name", "price"])
    assert result.source == "elements"


def test_the_prose_reader_reads_the_captured_text_not_the_clipped_text():
    """On the measured page that is 7332 chars vs 4000 — about half the products."""
    observation = FakeObservation(
        elements=[],
        page_text="Widget One Deluxe Model\nRs. 100\n",
        text_full="Widget One Deluxe Model\nRs. 100\nWidget Two Deluxe Model\nRs. 200\n",
    )
    assert len(ex.structured_records(observation, []).records) == 2


# ------------------------------------------------------ the budget follows data
def test_the_element_share_drops_when_the_prose_holds_the_prices():
    """THE 5-WIDE TRAP, walked into once already. A fixed 60% to elements made the
    measured page WORSE: 5400 chars went to daraz's priceless titles and the prose
    holding every price was cut to 3485 — less room than before the element list
    was added at all."""
    priceless = ['- link: "Yonex, Hi Qua badminton Racket (single) premium quality"']
    assert ex._element_share_fraction(priceless, _DARAZ_PROSE) == ex._ELEMENT_SHARE_MIN

    priced = ['- item: "Widget A Rs. 10"', '- item: "Widget B Rs. 20"']
    assert ex._element_share_fraction(priced, _DARAZ_PROSE) == ex._ELEMENT_SHARE_MAX


def test_the_element_list_always_keeps_a_floor():
    """Starving it entirely trades one blindness for another — the element list is
    also how the model knows what it can click next."""
    assert ex._ELEMENT_SHARE_MIN > 0
    source = ex.record_source(_daraz(), limit=3000)
    assert "ITEMS ON THE PAGE" in source
    assert "PAGE TEXT:" in source


def test_the_prose_gets_the_room_it_needs_on_a_priceless_element_list():
    source = ex.record_source(_daraz(), limit=3000)
    body = source.split("PAGE TEXT:", 1)[1]
    assert "Rs. 1,750" in body and "Rs. 1,544" in body


# ------------------------------------------------------- the anti-fabrication
def test_every_emitted_value_is_copied_from_the_page():
    """THE GUARANTEE that makes this path better than a prompt, not just faster.
    "Copy values EXACTLY" was already in the extract prompt when the web round
    produced 112 invented country names. Here it is a property of the code: every
    value in every record is a substring of some element's own text (or its
    href)."""
    observation = _grid()
    corpus = "   ".join(
        (e.name_full or e.name) + "   " + e.href for e in observation.elements
    )
    for record in ex.structured_records(observation, []).records:
        for key, value in record.items():
            assert value in corpus, f"{key}={value!r} is not present on the page"


# --------------------------------------------------------------- LLM fallback
def test_has_content_is_asked_separately_from_the_source_string():
    """record_source ALWAYS emits the URL and title, so "the source is non-empty"
    is not the same question as "there is something to read" — answering it that
    way spends an LLM call on a blank page."""
    blank = FakeObservation(elements=[], page_text="", text_full="")
    assert ex.has_content(blank) is False
    assert ex.record_source(blank, limit=500) != ""      # still names the page
    assert ex.has_content(FakeObservation(elements=[], page_text="", text_full="words")) is True
    assert (
        ex.has_content(
            FakeObservation(elements=[FakeElement("item", "a thing")], page_text="", text_full="")
        )
        is True
    )


def test_record_source_puts_the_element_list_before_the_prose():
    """The dense, actionable half must never be crowded out by prose — the same
    rule render() follows (the 5-wide trap, restated in observe.py)."""
    source = ex.record_source(_grid(), limit=4000)
    assert source.index("ITEMS ON THE PAGE") < source.index("PAGE TEXT:")
    assert "Yonex Astrox 99 Pro Badminton Racket Rs. 24,999" in source
    assert "/products/astrox-99-pro" in source


def test_record_source_prefers_the_full_prose_over_the_clipped_prose():
    observation = FakeObservation(elements=[], page_text="CLIPPED", text_full="THE WHOLE THING")
    assert "THE WHOLE THING" in ex.record_source(observation, limit=2000)


def test_record_source_honours_its_budget():
    source = ex.record_source(_grid(), limit=200)
    assert len(source) <= 200
    assert ex.record_source(_grid(), limit=0) == ""
