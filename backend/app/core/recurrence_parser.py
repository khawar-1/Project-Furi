"""
Furi OS — Recurrence parser (Phase 10, Part 2 — scheduled routines)

Deterministic, conservative parsing of a recurring-schedule phrase in a teach
command ("save this as a routine called X that runs every Friday at 4pm").
Same never-guess philosophy as reminder_parser: only a few well-formed shapes
are recognized, and anything else returns None so the routine is simply saved
WITHOUT a schedule (the user can set one in Settings) — a wrong schedule that
fires unbidden is worse than no schedule.

Recognized shapes:
- weekly:   "every friday at 4pm", "each tuesday at 16:30", "on mondays at 9am"
- daily:    "every day at 8am", "daily at 7", "every morning at 6:30"
- interval: "every 30 minutes", "every 2 hours", "every 90 mins"

Bare-hour rule (no am/pm, hour 1–12): a documented default band so a common
phrase still resolves — 7–11 → AM, 12 → noon, 1–6 → PM; 0 and 13–23 are taken
as literal 24-hour. A morning/evening/night hint overrides the band. Every
value ends up visible and editable in the Routines panel, so a mis-read is
correctable, never silent.
"""
import re
from dataclasses import dataclass
from typing import Optional

_WEEKDAY_TO_INT = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

# interval: "every 30 minutes" / "every 2 hours"
_INTERVAL_RE = re.compile(
    r"\bevery\s+(\d+)\s*(minutes?|mins?|hours?|hrs?)\b", re.IGNORECASE
)
# weekly: "every friday at 4pm" / "each tues at 16:30" / "on mondays at 9"
_WEEKLY_RE = re.compile(
    r"\b(?:every|each|on)\s+"
    r"(monday|mon|tuesday|tues|tue|wednesday|wed|thursday|thurs|thur|thu|friday|fri|saturday|sat|sunday|sun)s?\s+"
    r"(?:at\s+)?(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?\b",
    re.IGNORECASE,
)
# daily: "every day at 8am" / "daily at 7" / "every morning at 6:30"
_DAILY_RE = re.compile(
    r"\b(?:every\s*day|everyday|daily|each\s+day|every\s+(morning|afternoon|evening|night))\s+"
    r"(?:at\s+)?(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?\b",
    re.IGNORECASE,
)

# All three patterns, so strip_recurrence can excise whichever matched.
_ALL_PATTERNS = (_INTERVAL_RE, _WEEKLY_RE, _DAILY_RE)

_MIN_INTERVAL_MINUTES = 5
_MAX_INTERVAL_MINUTES = 10_080  # one week


@dataclass(frozen=True)
class ScheduleSpec:
    """A parsed schedule, ready to become Routine.schedule_* fields."""
    schedule_type: str  # "interval" | "daily" | "weekly"
    schedule_hour: int = 9
    schedule_minute: int = 0
    schedule_weekday: Optional[int] = None
    schedule_interval_minutes: Optional[int] = None

    def as_dict(self) -> dict:
        return {
            "schedule_type": self.schedule_type,
            "schedule_hour": self.schedule_hour,
            "schedule_minute": self.schedule_minute,
            "schedule_weekday": self.schedule_weekday,
            "schedule_interval_minutes": self.schedule_interval_minutes,
        }


def _resolve_clock(
    hour_s: str, minute_s: Optional[str], meridiem: Optional[str],
    hint: Optional[str] = None,
) -> Optional[tuple[int, int]]:
    """(hour24, minute) from clock parts, or None if out of range."""
    try:
        hour = int(hour_s)
        minute = int(minute_s) if minute_s else 0
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    if meridiem:
        m = meridiem.lower()
        if hour < 1 or hour > 12:
            return None
        if hour == 12:
            hour = 0 if m == "am" else 12
        elif m == "pm":
            hour += 12
        return hour, minute

    # No am/pm — a morning/evening hint sets the band, else literal 24h for
    # 0 and 13–23, else the documented default band for a bare 1–12.
    if hint:
        h = hint.lower()
        if h == "morning" and 1 <= hour <= 12:
            return (hour % 12), minute
        if h in ("evening", "night", "afternoon") and 1 <= hour <= 12:
            return (12 if hour == 12 else hour % 12 + 12), minute
    if hour == 0 or hour >= 13:
        return hour, minute
    if 7 <= hour <= 11:
        return hour, minute       # AM
    if hour == 12:
        return 12, minute         # noon
    return hour + 12, minute      # 1–6 → PM


def _clamp_interval(value: int) -> int:
    return max(_MIN_INTERVAL_MINUTES, min(value, _MAX_INTERVAL_MINUTES))


def parse_recurrence(text: str) -> Optional[ScheduleSpec]:
    """Parse a recurring-schedule phrase, or None. Interval is tried first (its
    'every N units' shape can't collide with the day/time shapes)."""
    if not text:
        return None

    m = _INTERVAL_RE.search(text)
    if m:
        try:
            amount = int(m.group(1))
        except ValueError:
            amount = 0
        unit = m.group(2).lower()
        minutes = amount * 60 if unit.startswith(("hour", "hr")) else amount
        if minutes >= 1:
            return ScheduleSpec(
                schedule_type="interval",
                schedule_interval_minutes=_clamp_interval(minutes),
            )

    m = _WEEKLY_RE.search(text)
    if m:
        weekday = _WEEKDAY_TO_INT.get(m.group(1).lower())
        clock = _resolve_clock(m.group(2), m.group(3), m.group(4))
        if weekday is not None and clock is not None:
            return ScheduleSpec(
                schedule_type="weekly",
                schedule_weekday=weekday,
                schedule_hour=clock[0],
                schedule_minute=clock[1],
            )

    m = _DAILY_RE.search(text)
    if m:
        hint = m.group(1)  # morning/afternoon/evening/night, or None
        clock = _resolve_clock(m.group(2), m.group(3), m.group(4), hint=hint)
        if clock is not None:
            return ScheduleSpec(
                schedule_type="daily",
                schedule_hour=clock[0],
                schedule_minute=clock[1],
            )

    return None


def strip_recurrence(text: str) -> tuple[str, Optional[ScheduleSpec]]:
    """Return (text with the recurrence phrase removed, spec-or-None). Also
    strips a leading 'that runs'/'that run'/'running'/'and run(s)' connector
    left dangling once the phrase is excised, so a routine NAME captured from
    "...called X that runs every Friday at 4pm" comes back as just "X"."""
    spec = parse_recurrence(text)
    if spec is None:
        return text.strip(), None
    cleaned = text
    for pattern in _ALL_PATTERNS:
        cleaned = pattern.sub(" ", cleaned)
    cleaned = re.sub(
        r"\b(?:that\s+runs?|which\s+runs?|running|and\s+runs?|to\s+run)\b\s*$",
        "",
        cleaned.strip(),
        flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,;:-")
    return cleaned, spec
