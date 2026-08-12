"""
Furi OS — Deterministic contact-field validation.

The safety net behind the LLM extractor for contact attributes (email,
birthday) — same species as normalize_future_phrasing in engine.py: the
extraction prompt states the format contract (belt), these pure functions
enforce it in Python (suspenders) so a hallucinated address or an impossible
date never lands in the DB, no matter what the model emits.

Applied in three places:
  - extraction_schema.PersonMentioned field validators (capture time —
    junk never even parks in a PendingResolution),
  - MemoryEngine.update_contact / create_contact_manual (write time —
    covers every path, including parked-resolution merges),
  - the /api/contacts POST/PUT handlers (manual edits get an explicit 400
    instead of the extractor's silent drop).

Pure functions: no I/O, no LLM, deterministic. Canonical birthday storage is
"MM-DD" when the year is unknown ("Jamil's birthday is March 4") and
"YYYY-MM-DD" when it is known — matching the extraction prompt's contract.
"""
import calendar
import re
from datetime import date
from typing import Optional

# Values the LLM echoes back from the prompt's placeholder text, or uses to
# mean "not stated" — never real data (same class as
# UserProfileEnrichment._strip_null_strings).
_PLACEHOLDER_VALUES = {
    "", "null", "none", "unknown", "n/a", "na",
    "null or string", "yyyy-mm-dd", "mm-dd",
}

_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}$"
)

MIN_BIRTH_YEAR = 1900

_MONTHS = {}
for _i, _name in enumerate(calendar.month_name):
    if _name:
        _MONTHS[_name.lower()] = _i
for _i, _abbr in enumerate(calendar.month_abbr):
    if _abbr:
        _MONTHS[_abbr.lower()] = _i
_MONTHS["sept"] = 9

# "March 4", "Mar 4th, 1990", "March 4 1990"
_MONTH_FIRST_RE = re.compile(
    r"^(?P<month>[a-z]+)\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?"
    r"(?:,?\s+(?P<year>\d{4}))?$"
)
# "4 March", "4th of March 1990"
_DAY_FIRST_RE = re.compile(
    r"^(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<month>[a-z]+)\.?"
    r"(?:,?\s+(?P<year>\d{4}))?$"
)
# "1990-03-04", "1990/3/4"
_FULL_DATE_RE = re.compile(r"^(?P<year>\d{4})[-/](?P<month>\d{1,2})[-/](?P<day>\d{1,2})$")
# "03-04", "3/4"
_MONTH_DAY_RE = re.compile(r"^(?P<month>\d{1,2})[-/](?P<day>\d{1,2})$")


def normalize_email(value: object) -> Optional[str]:
    """Return a canonical email string, or None if value is not a plausible address."""
    if not isinstance(value, str):
        return None
    email = value.strip()
    if email.lower().startswith("mailto:"):
        email = email[len("mailto:"):].strip()
    if email.startswith("<") and email.endswith(">"):
        email = email[1:-1].strip()
    if email.lower() in _PLACEHOLDER_VALUES:
        return None
    if len(email) > 254 or not _EMAIL_RE.match(email):
        return None
    local, domain = email.rsplit("@", 1)
    if len(local) > 64 or ".." in email:
        return None
    # Canonical form: domain is case-insensitive, the local part is not.
    return f"{local}@{domain.lower()}"


def _valid_month_day(month: int, day: int) -> bool:
    if not 1 <= month <= 12:
        return False
    # Year unknown: Feb 29 is a real birthday (leap years exist).
    max_day = 29 if month == 2 else calendar.monthrange(2001, month)[1]
    return 1 <= day <= max_day


def normalize_birthday(value: object, today: Optional[date] = None) -> Optional[str]:
    """
    Return canonical "MM-DD" (year unknown) or "YYYY-MM-DD" (year known),
    or None when the value is not a sane, non-future calendar date.
    """
    if not isinstance(value, str):
        return None
    today = today or date.today()
    text = re.sub(r"\s+", " ", value.strip())
    if text.lower() in _PLACEHOLDER_VALUES:
        return None

    year: Optional[int] = None
    month: Optional[int] = None
    day: Optional[int] = None

    m = _FULL_DATE_RE.match(text)
    if m:
        year, month, day = int(m["year"]), int(m["month"]), int(m["day"])
    elif (m := _MONTH_DAY_RE.match(text)):
        month, day = int(m["month"]), int(m["day"])
    else:
        m = _MONTH_FIRST_RE.match(text.lower()) or _DAY_FIRST_RE.match(text.lower())
        if not m:
            return None
        month = _MONTHS.get(m["month"])
        if month is None:
            return None
        day = int(m["day"])
        year = int(m["year"]) if m["year"] else None

    if year is not None:
        if not MIN_BIRTH_YEAR <= year <= today.year:
            return None
        try:
            born = date(year, month, day)  # rejects Feb 29 in non-leap years
        except ValueError:
            return None
        if born > today:
            return None
        return f"{year:04d}-{month:02d}-{day:02d}"

    if not _valid_month_day(month, day):
        return None
    return f"{month:02d}-{day:02d}"
