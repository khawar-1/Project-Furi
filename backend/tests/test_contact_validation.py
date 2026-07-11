"""
Phase 5 Part 2 — deterministic contact-field validation net.

contact_validation.py is the safety net behind the LLM extractor for email
and birthday (the normalize_future_phrasing philosophy): the prompt states
the format contract, these tests pin the Python enforcement — a hallucinated
address or an impossible date must NEVER survive normalization.
"""
from datetime import date

import pytest

from app.memory.contact_validation import normalize_birthday, normalize_email
from app.memory.extraction_schema import PersonMentioned

TODAY = date(2026, 7, 11)


# ============================================================= email: valid
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("jamil@example.com", "jamil@example.com"),
        ("  jamil@example.com  ", "jamil@example.com"),  # whitespace stripped
        ("Jamil.Ali@Example.COM", "Jamil.Ali@example.com"),  # domain lowered, local preserved
        ("mailto:jamil@example.com", "jamil@example.com"),
        ("MAILTO:jamil@example.com", "jamil@example.com"),
        ("<jamil@example.com>", "jamil@example.com"),
        ("a+tag@sub.example.co.uk", "a+tag@sub.example.co.uk"),
        ("first_last%x@ex-ample.io", "first_last%x@ex-ample.io"),
    ],
)
def test_normalize_email_valid(raw, expected):
    assert normalize_email(raw) == expected


# =========================================================== email: invalid
@pytest.mark.parametrize(
    "raw",
    [
        None,
        42,
        ["jamil@example.com"],
        "",
        "   ",
        "null",
        "None",
        "unknown",
        "N/A",
        "null or string",  # prompt placeholder echoed back
        "plainaddress",
        "a@b",  # no dot in domain
        "a@@b.com",
        "a b@c.com",  # space in local part
        "a@b..com",  # consecutive dots
        "a@.b.com",  # leading dot in domain
        "a@b.com.",  # trailing dot
        "a@b.123",  # numeric TLD
        "x" * 65 + "@example.com",  # local part over 64
        "a@" + "b" * 250 + ".com",  # total over 254
    ],
)
def test_normalize_email_invalid(raw):
    assert normalize_email(raw) is None


# ========================================================= birthday: valid
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1990-03-04", "1990-03-04"),
        ("03-04", "03-04"),
        ("3-4", "03-04"),  # zero-pad
        ("1990-3-4", "1990-03-04"),
        ("1990/03/04", "1990-03-04"),  # slash separator
        ("03/04", "03-04"),
        ("March 4", "03-04"),  # month name, no year — the spec's case
        ("march 4", "03-04"),
        ("Mar 4th", "03-04"),  # abbreviation + ordinal
        ("Sept 9", "09-09"),  # common 4-letter abbreviation
        ("4 March", "03-04"),  # day-first
        ("4th of March", "03-04"),
        ("March 4, 1990", "1990-03-04"),
        ("March 4 1990", "1990-03-04"),
        ("4 March 1990", "1990-03-04"),
        ("December 25", "12-25"),
        ("02-29", "02-29"),  # leap-day birthday, year unknown
        ("1992-02-29", "1992-02-29"),  # real leap year
        ("2026-01-01", "2026-01-01"),  # this year, already past — a newborn
    ],
)
def test_normalize_birthday_valid(raw, expected):
    assert normalize_birthday(raw, today=TODAY) == expected


# ======================================================= birthday: invalid
@pytest.mark.parametrize(
    "raw",
    [
        None,
        42,
        "",
        "null",
        "unknown",
        "YYYY-MM-DD",  # prompt placeholder echoed back
        "MM-DD",
        "02-30",  # no such day
        "13-04",  # month 13 — never DD-MM-flipped to April 13
        "00-05",
        "04-00",
        "1990-02-29",  # not a leap year
        "2027-03-04",  # future year
        "2026-12-25",  # this year but still ahead of today
        "1899-03-04",  # absurd-year floor
        "Mach 4",  # not a month
        "next tuesday",
        "in march",
        "March",  # month alone is not a birthday
    ],
)
def test_normalize_birthday_invalid(raw):
    assert normalize_birthday(raw, today=TODAY) is None


# ============================================== the belt: PersonMentioned
def test_person_mentioned_normalizes_valid_values():
    p = PersonMentioned(name="Jamil", email="Jamil@Example.COM", birthday="March 4")
    assert p.email == "Jamil@example.com"
    assert p.birthday == "03-04"


def test_person_mentioned_drops_garbage_silently():
    """Extraction never surfaces errors: junk becomes None, not a crash —
    so it never reaches store_contact and never parks a resolution."""
    p = PersonMentioned(name="Jamil", email="not-an-email", birthday="02-30")
    assert p.email is None
    assert p.birthday is None


def test_person_mentioned_drops_placeholder_echoes():
    p = PersonMentioned(name="Jamil", email="null or string", birthday="YYYY-MM-DD")
    assert p.email is None
    assert p.birthday is None
