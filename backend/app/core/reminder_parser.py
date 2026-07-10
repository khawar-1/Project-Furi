"""
Jarvis OS — Reminder Detection + Time Parsing (Phase 4, Part 4)

Deterministic (non-LLM) recognition of reminder requests ("remind me at 6
to call Jamil") and extraction of the due time + reminder text. Same
philosophy as search_files' date rules (app/tools/file_tools.py): a time
the code cannot confidently resolve is never guessed at — the caller gets
back a clarifying question instead of a scheduled reminder.

Why this can't be an LLM call: the extraction pipeline already proved that
letting a model interpret dates is unreliable, and reminders are worse —
a wrong guess here means a notification that never fires, or fires at the
wrong hour with nobody watching. Parsing time expressions is a solved,
testable problem in code; it doesn't need a model.

Supported time expressions (checked in this priority order):
  - Relative:   "in 20 minutes", "after 2 hours", "in 3 days"
  - Absolute date: "on 2026-07-10 at 5pm" (ISO date only — the search_files
    convention: no "07/10/2026" guessing)
  - Clock time, optionally with a day qualifier: "at 6", "at 6pm",
    "at 6:30am", "tomorrow at 9", "tonight at 10", "at 18:30" (24h)

A bare 12-hour clock hour with no am/pm ("at 6") is resolved by a
documented default, not asked about: whichever of {H:00 AM, H:00 PM} is
still ahead of now wins; if both are still ahead (i.e. it's very early in
the day), PM wins the tie — "remind me at 6" said over lunch means this
evening; if neither is left today, it rolls to tomorrow's PM occurrence.
Given the same input and the same "now", this always resolves to the same
output — it is a rule, not a coin flip.

Anything else — no time expression found at all, a bare day with no clock
time ("remind me tomorrow to call jamil"), an hour out of range, a time
that has already passed — comes back with `ambiguous=True` and a
ready-to-ask `question`; nothing is ever scheduled from an ambiguous parse.

An ambiguous parse IS parked across turns by the router
(app/api/reminder_router.py): the half that parsed (task text or time) is
kept in memory and the next message answers the missing half —
parse_time_reply() here interprets that bare answer ("in 5 mins", "6pm")
with the same never-guess rules. The parking is in-memory only (a backend
restart simply drops the question; the user asks again) — unlike contact
disambiguation it is not persisted to SQLite.
"""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

# Two-tier trigger. STRONG phrases are unmistakable reminder intent and
# fire on their own. "remind" alone (as in "reminds me of...") must never
# trip this. "reminders?" covers the plural ("set reminders for X and Y") —
# before that, "set reminders …" bypassed this router entirely and the LLM
# fabricated a confirmation (live bug, 2026-07-09).
# "remind me WHEN/ONCE/AFTER you're done" is event-conditioned, not timed —
# that's Part 5 background-task intent ("tell me when you're done" with a
# different verb) and must fall through to the task router, never park here
# asking "what time?" (live bug, 2026-07-09). "after <number>" stays a
# reminder ("remind me after 30 minutes to call mom"). The lookahead also
# covers "remind me after doing/finishing all this/these tasks" — completion
# of the message's own work, same class (live bug, 2026-07-10).
REMINDER_TRIGGER_RE = re.compile(
    r"\b(?:remind me(?!\s+(?:when(?:ever)?\b"
    r"|once\s+(?:you|it|this|that|everything)\b"
    r"|after\s+(?:you|it|this|that|these|those|everything"
    r"|(?:doing|finishing|completing|running|executing)\s+(?:all\s+)?(?:of\s+)?"
    r"(?:this|that|these|those|them|it|everything|the\s+tasks?))\b))"
    r"|(?:set|add|create|make|schedule) (?:a |an |another )?reminders?(?: to| for)?"
    r"|set (?:an |the )?alarm(?: for)?"
    r")\b",
    re.IGNORECASE,
)

# The completion condition can also come BEFORE the trigger: "…create the
# file, after doing all this remind me" (live bug 2026-07-10 — the
# forward-order lookahead above never sees it, so the router parked a
# "What time should I remind you?" for what is background-task intent, and
# the parked question then swallowed the user's retry). Matched against the
# text immediately preceding the trigger. Deliberately completion-flavoured
# only: "after 30 minutes remind me…" (a number) and concrete user
# activities ("after dinner remind me…") never match — those stay reminders.
_PRE_COMPLETION_RE = re.compile(
    r"(?:"
    r"(?:when(?:ever)?|once|after)\s+"
    r"(?:you(?:'re|'ve|\s+are|\s+have)?\s+|it(?:'s|\s+is)?\s+|(?:this|that)(?:'s|\s+is)\s+"
    r"|everything(?:'s|\s+is)?\s+|all\s+(?:is\s+)?)?"
    r"(?:all\s+)?(?:done|finish(?:ed)?|complete[d]?|ready)"
    r"|after\s+(?:doing|finishing|completing|running|executing)\s+"
    r"(?:all\s+(?:of\s+)?)?(?:this|that|these|those|them|it|everything|the\s+tasks?|all)"
    r")[\s,;.!—–-]*$",
    re.IGNORECASE,
)

# WEAK phrases are reminder-INTENT-shaped but common in ordinary chat
# ("let me know what you think", "alert me if anything breaks"), so they
# only count as a trigger when the message ALSO carries a recognizable time
# expression — "alert me at 6 to take my medicine" is a reminder, "alert me
# if anything happens" is conversation. Guard rails against neighbouring
# features: "tell me" needs "to" and "let me know" must not be followed by
# "when" — "tell me when you're done" / "let me know when it's finished"
# are Part 5 BACKGROUND-TASK phrases, never reminders; and no "remember"
# phrasing belongs here at all ("remember that X" is the MEMORY engine's
# territory — "don't let me forget" is the one forget-flavoured phrase
# that's reminder intent).
WEAK_TRIGGER_RE = re.compile(
    r"\b(?:alert me|notify me|ping me|buzz me"
    r"|wake me(?: up)?"
    r"|(?:give me|gimme) (?:a )?heads?[- ]?ups?"
    r"|let me know(?!\s+(?:when|if|once|after|whether|what|how|about)\b)"
    r"|tell me(?=\s+to\b)"
    r"|(?:don'?t|do not) let me forget"
    r")\b",
    re.IGNORECASE,
)

# A weak trigger that IS the whole task ("wake me up at 7") leaves no text
# behind once its span is removed — give it its obvious default instead of
# asking "What should I remind you to do?".
_WAKE_RE = re.compile(r"^wake me(?: up)?$", re.IGNORECASE)


def _default_text_for_trigger(trigger_text: str) -> Optional[str]:
    t = trigger_text.strip().lower()
    if _WAKE_RE.match(t):
        return "wake up"
    if "alarm" in t:
        return "alarm"
    if "head" in t:  # "gimme a heads up at 11" — the heads-up IS the task
        return "heads up"
    return None

_RELATIVE_RE = re.compile(
    r"\b(?:in|after)\s+(\d+)\s*(minutes?|mins?|hours?|hrs?|days?)\b", re.IGNORECASE
)
# Minutes accept ":", "." or a space as the separator ("6:04", "6.04",
# "6 04 pm") — always exactly two digits, so "at 6 to call" never misreads.
_CLOCK_RE = re.compile(
    r"\b(today|tonight|tomorrow)?\s*at\s+(\d{1,2})(?:[:. ](\d{2}))?\s*(am|pm)?\b",
    re.IGNORECASE,
)
_ISO_DATE_RE = re.compile(
    r"\bon\s+(\d{4})-(\d{2})-(\d{2})(?:\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?)?\b",
    re.IGNORECASE,
)

_UNIT_SECONDS = {
    "minute": 60, "minutes": 60, "min": 60, "mins": 60,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600,
    "day": 86400, "days": 86400,
}


@dataclass(frozen=True)
class ReminderParseResult:
    """matched=False means the message isn't a reminder request at all (no
    trigger phrase) — the caller should fall through to normal routing.
    matched=True + ambiguous=True carries a question and no due_at; nothing
    should be scheduled from it. matched=True + ambiguous=False is ready to
    schedule."""
    matched: bool
    text: Optional[str] = None
    due_at: Optional[datetime] = None  # aware, caller's local tz
    ambiguous: bool = False
    question: Optional[str] = None


def _find_trigger(message: str, now: datetime) -> Optional[re.Match]:
    """The one place trigger policy lives: a strong phrase fires alone; a
    weak phrase fires only alongside a recognizable time expression. A
    "remind me" preceded by a completion condition ("after doing all this
    remind me") with NO time expression anywhere is event-conditioned
    background intent, not a timed reminder — return None so it falls
    through to the task router. An explicit time wins ("after doing all
    this remind me at 6pm to leave" is still a reminder)."""
    m = REMINDER_TRIGGER_RE.search(message)
    if m:
        if (
            m.group(0).lower().startswith("remind me")
            and _PRE_COMPLETION_RE.search(message[: m.start()])
            and _match_time(message, now) is None
        ):
            return None
        return m
    w = WEAK_TRIGGER_RE.search(message)
    if w and _match_time(message, now) is not None:
        return w
    return None


def looks_like_reminder(message: str, now: Optional[datetime] = None) -> bool:
    """Cheap deterministic gate — no LLM call. Mirrors looks_like_task's
    role in task_router.py."""
    return _find_trigger(message, now or datetime.now().astimezone()) is not None


def _remove_spans(message: str, spans: list) -> str:
    """Blank out each span (trigger phrase, time expression) and return
    what's left — the reminder's own text."""
    for start, end in sorted(spans, key=lambda s: s[0], reverse=True):
        message = message[:start] + " " + message[end:]
    return message


# Leading greetings/vocatives that survive trigger+time removal ("hey
# jarvis, remind me i have a meeting at 6" would otherwise store the text
# "hey jarvis , i have a meeting" — noise, not the reminder).
_GREETING_PREFIX_RE = re.compile(
    r"^(?:(?:hey|hi|hello|yo|ok|okay|oh|um+|uh+|please|jarvis)(?:[\s,!.]+|$))+",
    re.IGNORECASE,
)


def _clean_task_text(text: str) -> str:
    t = re.sub(r"\s+", " ", text).strip(" ,.!")
    t = _GREETING_PREFIX_RE.sub("", t)
    t = re.sub(r"^(?:to|for|that i (?:should|need to)|that|about)\s+", "", t, flags=re.IGNORECASE)
    t = _GREETING_PREFIX_RE.sub("", t)  # "to please call mom" → "call mom"
    t = re.sub(r"[\s,]*\bplease\b[\s,!.]*$", "", t, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", t).strip(" ,.!")


def _no_time_question(hint: str = "") -> str:
    suffix = f" {hint}" if hint else ""
    return (
        f"What time should I remind you{suffix}? "
        '(e.g. "at 6pm", "in 20 minutes", "tomorrow at 9am")'
    )


def _no_task_question() -> str:
    return "What should I remind you to do?"


def _past_time_question(due_at: datetime) -> str:
    return (
        f"That time ({due_at.strftime('%I:%M %p on %A, %B %d').lstrip('0')}) "
        "has already passed — what time did you mean?"
    )


def _bad_date_question(y: str, mo: str, d: str) -> str:
    return f'"{y}-{mo}-{d}" isn\'t a valid date — please give it as YYYY-MM-DD.'


def _bad_time_question(hour: int, minute: int) -> str:
    return (
        f'"{hour}:{minute:02d}" isn\'t a time I can use — please give an hour '
        "0-23 (or 1-12 with am/pm) and minutes 0-59."
    )


def _valid_minute(minute_s: Optional[str]) -> Optional[int]:
    minute = int(minute_s) if minute_s else 0
    return minute if 0 <= minute <= 59 else None


def _resolve_bare_hour(
    now: datetime, target_date: datetime, hour12: int, minute: int
) -> datetime:
    """No am/pm given: pick whichever of {AM, PM} on target_date is still
    ahead of `now`; PM breaks a tie; roll a day forward if both have
    already passed. See module docstring for the reasoning."""
    h = hour12 % 12
    am = target_date.replace(hour=h, minute=minute, second=0, microsecond=0)
    pm = target_date.replace(hour=h + 12, minute=minute, second=0, microsecond=0)
    future = [c for c in (am, pm) if c > now]
    if len(future) == 1:
        return future[0]
    if len(future) == 2:
        return pm
    return pm + timedelta(days=1)


def _apply_12h(target_date: datetime, hour12: int, minute: int, meridiem: str) -> datetime:
    h = hour12 % 12
    if meridiem.lower() == "pm":
        h += 12
    return target_date.replace(hour=h, minute=minute, second=0, microsecond=0)


def _resolve_clock(
    now: datetime, target_date: datetime, hour: int, minute: int, meridiem: Optional[str]
) -> Optional[datetime]:
    """Resolve an hour/minute (+ optional am/pm) onto target_date's calendar
    date. None when the hour is out of any usable range (caller turns that
    into a question). 24-hour hours (13-23, or 0 for midnight) need no
    am/pm — unambiguous. Does NOT decide whether a past result should roll
    forward — callers with different conventions (explicit clock time vs.
    an explicit ISO date) make that call themselves."""
    if meridiem:
        if not (1 <= hour <= 12):
            return None
        return _apply_12h(target_date, hour, minute, meridiem)

    if 0 <= hour <= 23:
        if 13 <= hour <= 23 or hour == 0:
            return target_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return _resolve_bare_hour(now, target_date, hour, minute)

    return None


@dataclass(frozen=True)
class _TimeMatch:
    """One recognized time expression in a message. Exactly one of due_at /
    question is set: a clean resolution, or the clarifying question for a
    time-LIKE expression the rules refuse ('on 2026-02-30', a passed
    'today at 6am')."""
    span: tuple
    due_at: Optional[datetime] = None
    question: Optional[str] = None


def _match_time(message: str, now: datetime) -> Optional[_TimeMatch]:
    """Find and resolve the message's time expression; None when there is
    none at all. Shared by parse_reminder (full requests) and
    parse_time_reply (bare answers to 'What time…?')."""
    # 1. Relative — always unambiguous, checked first so "in 20 minutes"
    # never gets misread by the clock-time pattern.
    m = _RELATIVE_RE.search(message)
    if m:
        amount, unit = int(m.group(1)), m.group(2).lower()
        return _TimeMatch(span=m.span(), due_at=now + timedelta(seconds=amount * _UNIT_SECONDS[unit]))

    # 2. Absolute ISO date, optionally with a time.
    m = _ISO_DATE_RE.search(message)
    if m:
        y, mo, d, hour_s, minute_s, meridiem = m.groups()
        try:
            base = now.replace(year=int(y), month=int(mo), day=int(d), second=0, microsecond=0)
        except ValueError:
            return _TimeMatch(span=m.span(), question=_bad_date_question(y, mo, d))
        if hour_s is None:
            return _TimeMatch(span=m.span(), question=_no_time_question(f"on {y}-{mo}-{d}"))
        hour = int(hour_s)
        minute = _valid_minute(minute_s)
        if minute is None:
            return _TimeMatch(span=m.span(), question=_bad_time_question(hour, 0))
        due_at = _resolve_clock(now, base, hour, minute, meridiem)
        if due_at is None:
            return _TimeMatch(span=m.span(), question=_bad_time_question(hour, minute))
        # An explicit date is never silently rolled a day forward — a past
        # moment on a stated date is a mistake, not a "next occurrence".
        if due_at <= now:
            return _TimeMatch(span=m.span(), question=_past_time_question(due_at))
        return _TimeMatch(span=m.span(), due_at=due_at)

    # 3. Clock time with an optional day qualifier.
    m = _CLOCK_RE.search(message)
    if m:
        qualifier, hour_s, minute_s, meridiem = m.groups()
        hour = int(hour_s)
        minute = _valid_minute(minute_s)
        if minute is None:
            return _TimeMatch(span=m.span(), question=_bad_time_question(hour, 0))
        target_date = now + timedelta(days=1) if (qualifier or "").lower() == "tomorrow" else now
        due_at = _resolve_clock(now, target_date, hour, minute, meridiem)
        if due_at is None:
            return _TimeMatch(span=m.span(), question=_bad_time_question(hour, minute))
        # No explicit day word and it's already past: assume the next
        # occurrence (the ordinary "remind me at 6pm" convention). An
        # explicit "today"/"tonight" is NOT silently rolled — a stated day
        # that's already passed is a mistake, not a next-occurrence request.
        if meridiem and qualifier is None and due_at <= now:
            due_at += timedelta(days=1)
        if due_at <= now:
            return _TimeMatch(span=m.span(), question=_past_time_question(due_at))
        return _TimeMatch(span=m.span(), due_at=due_at)

    return None


def _time_right_after(message: str, start: int, now: datetime) -> Optional[_TimeMatch]:
    """Probe the text starting at `start` with an implicit 'at' — only a
    time expression sitting IMMEDIATELY there counts (anything later in the
    message would already have matched the normal grammar). The returned
    span is mapped back onto the original message."""
    rest = message[start:]
    if not rest.strip():
        return None
    pad = "at" if rest.startswith(" ") else "at "
    m = _match_time(pad + rest, now)
    if m is None or m.span[0] != 0:
        return None
    return _TimeMatch(
        span=(start, start + m.span[1] - len(pad)), due_at=m.due_at, question=m.question
    )


def parse_reminder(message: str, now: Optional[datetime] = None) -> Optional[ReminderParseResult]:
    """Returns None when the message has no reminder trigger at all (fall
    through to normal chat/task routing). Otherwise always returns a
    ReminderParseResult — ambiguous ones carry a question, never a guess.
    Ambiguous results still carry whatever WAS extractable (text without a
    time, due_at without a task) so the router can park the half it has and
    only ask for the missing half."""
    now = now or datetime.now().astimezone()
    trigger = _find_trigger(message, now)
    if not trigger:
        return None

    tm = _match_time(message, now)
    if tm is None and "alarm" in trigger.group(0).lower():
        # "set an alarm for 7am" — the natural alarm phrasing has no "at"
        # for the clock grammar to anchor on.
        tm = _time_right_after(message, trigger.end(), now)

    if tm is None:
        # No time expression recognized at all — but the task text is still
        # extractable ("remind me to call mom" → "call mom"), so carry it.
        hint = ""
        for word in ("tomorrow", "tonight", "today"):
            if re.search(rf"\b{word}\b", message, re.IGNORECASE):
                hint = word
                break
        text = _clean_task_text(_remove_spans(message, [trigger.span()])) or None
        return ReminderParseResult(
            matched=True, text=text, ambiguous=True, question=_no_time_question(hint)
        )

    text = _clean_task_text(_remove_spans(message, [trigger.span(), tm.span])) or None
    if text is None:
        text = _default_text_for_trigger(trigger.group(0))
    if tm.question:
        return ReminderParseResult(matched=True, text=text, ambiguous=True, question=tm.question)
    if not text:
        # Time resolved but nothing to be reminded OF — carry the time so
        # the router can park it and only ask for the task.
        return ReminderParseResult(
            matched=True, due_at=tm.due_at, ambiguous=True, question=_no_task_question()
        )
    return ReminderParseResult(matched=True, text=text, due_at=tm.due_at)


# ------------------------------------------------- multiple reminders in one ask
#
# "set reminders for calling ceo at 6:04 pm and a reminder for meeting with
# cto at 7" is TWO reminders. The split is deterministic and conservative:
# candidate segments are cut on "and", and a cut is only kept when the piece
# accumulated so far carries its OWN time expression — so "call mom and dad
# at 6pm" never splits (no time before the "and"), while "call mom and dad
# at 6pm and take pills at 7pm" splits exactly once. If ANY segment can't be
# resolved cleanly (unusable time, no task text), the whole message falls
# back to the single-reminder parse — ask, never half-schedule.

_SEGMENT_SPLIT_RE = re.compile(r"\s*,?\s+and\s+", re.IGNORECASE)

# A later reminder anchored to the previous one's time. Three shapes:
# "30 mins after it" (pronoun required after "after" — '30 mins after
# DINNER' must never anchor), "30 mins later", and the reversed
# "(at) after 30 mins".
_ANCHOR_RE = re.compile(
    r"\b(?:(\d+)\s*(minutes?|mins?|hours?|hrs?|days?)\s+"
    r"(?:after\s+(?:it|that|this|the (?:first|previous|last) one)|later)"
    r"|(?:at\s+)?after\s+(\d+)\s*(minutes?|mins?|hours?|hrs?|days?))\b",
    re.IGNORECASE,
)

# A later segment often restates the ask ("and a reminder for …", "and
# remind me to …") — strip that restatement, keep the task.
_RETRIGGER_PREFIX_RE = re.compile(
    r"^(?:(?:set\s+)?(?:a|an|another|one)\s+reminders?\s+(?:for|to|about)?\s*"
    r"|set\s+reminders?\s+(?:for|to|about)?\s*"
    r"|remind me\s+(?:to|about)?\s*"
    r"|also\s+)+",
    re.IGNORECASE,
)


def _segment_has_time(segment: str, now: datetime, is_first: bool) -> bool:
    if _match_time(segment, now) is not None:
        return True
    return not is_first and _ANCHOR_RE.search(segment) is not None


def _parse_segment(
    segment: str, now: datetime, is_first: bool, prev_due: Optional[datetime]
) -> Optional[ReminderParseResult]:
    """One clean reminder from one segment, or None (→ caller falls back to
    the single-reminder parse and its ask-don't-guess questions)."""
    if is_first:
        trig = _find_trigger(segment, now)
        if not trig:
            return None
        work = segment
        extra_spans = [trig.span()]
    else:
        work = _RETRIGGER_PREFIX_RE.sub("", segment.strip())
        extra_spans = []

    # In a LATER segment, "after 30 mins" means after the PREVIOUS reminder,
    # so the anchor is consulted before the plain grammar (which would read
    # it as "30 minutes from now").
    anchor = None if (is_first or prev_due is None) else _ANCHOR_RE.search(work)
    if anchor is not None:
        amount = int(anchor.group(1) or anchor.group(3))
        unit = (anchor.group(2) or anchor.group(4)).lower()
        due_at = prev_due + timedelta(seconds=amount * _UNIT_SECONDS[unit])
        time_span = anchor.span()
    else:
        tm = _match_time(work, now)
        if tm is None or tm.due_at is None:  # absent, or time-like but unusable
            return None
        due_at, time_span = tm.due_at, tm.span

    text = _clean_task_text(_remove_spans(work, extra_spans + [time_span]))
    if not text and is_first:
        text = _default_text_for_trigger(trig.group(0)) or ""
    if not text:
        return None
    return ReminderParseResult(matched=True, text=text, due_at=due_at)


def _try_multi(message: str, now: datetime) -> Optional[list[ReminderParseResult]]:
    parts = _SEGMENT_SPLIT_RE.split(message)
    if len(parts) < 2:
        return None

    # Merge-forward segmentation: a part with no time expression of its own
    # belongs with what follows ("call mom" + "dad at 6pm").
    segments: list[str] = []
    current = ""
    for part in parts:
        current = f"{current} and {part}" if current else part
        if _segment_has_time(current, now, is_first=not segments):
            segments.append(current)
            current = ""
    if current or len(segments) < 2:
        return None

    results: list[ReminderParseResult] = []
    prev_due: Optional[datetime] = None
    for i, segment in enumerate(segments):
        r = _parse_segment(segment, now, is_first=i == 0, prev_due=prev_due)
        if r is None:
            return None
        results.append(r)
        prev_due = r.due_at
    return results


def parse_reminders(message: str, now: Optional[datetime] = None) -> Optional[list[ReminderParseResult]]:
    """Multi-aware entry point for the router. None when the message has no
    reminder trigger. A list of 2+ results is only ever returned when EVERY
    reminder in it resolved cleanly (text + future due time) — otherwise a
    one-element list wrapping parse_reminder's result, ambiguity and all."""
    now = now or datetime.now().astimezone()
    if _find_trigger(message, now) is None:
        return None
    multi = _try_multi(message, now)
    if multi:
        return multi
    return [parse_reminder(message, now)]


def parse_time_reply(message: str, now: Optional[datetime] = None) -> Optional[ReminderParseResult]:
    """Interpret a bare reply to 'What time should I remind you?' — no
    trigger phrase required. Accepts everything parse_reminder's time
    grammar does ("in 5 mins", "at 6pm", "tomorrow at 9", "on 2026-07-10 at
    5pm") plus prefix-less short forms ("5 mins", "6pm", "6:30"). Returns
    None when the reply contains no recognizable time expression at all;
    ambiguous=True + question when it is time-like but unusable (past,
    invalid) — same never-guess rules as the full parser."""
    now = now or datetime.now().astimezone()
    stripped = message.strip()

    tm = _match_time(stripped, now)
    if tm is None and len(stripped) <= 32:
        # Short bare forms omit the preposition the grammar anchors on.
        tm = _match_time(f"in {stripped}", now) or _match_time(f"at {stripped}", now)
    if tm is None:
        return None
    if tm.question:
        return ReminderParseResult(matched=True, ambiguous=True, question=tm.question)
    return ReminderParseResult(matched=True, due_at=tm.due_at)
