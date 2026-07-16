"""
Jarvis OS — Pattern mining (Phase 10, Part 1)

Detects RECURRING goals and their temporal cadence from the completed-Task
history, ON DEMAND — the file_intelligence.frequent_folders precedent: reads
existing rows, no new table, no background job, best-effort → []. Nothing here
can act; it only produces a read-only signal.

Two consumers:
- core/routines.maybe_offer_routine ENRICHES its offer when a cadence is found,
  spelling out a *scheduled* teach phrase ("...usually every Friday around
  4:00 PM — save this as a routine called "X" that runs every Friday at 4pm").
- The Initiative Engine surfaces mined patterns as a signal so the composer can
  propose automating them.

Cadence detection is DETERMINISTIC (no LLM). Over a goal's completion
timestamps (converted to the machine's LOCAL time), a strong weekday+hour
cluster → weekly; a strong hour cluster spread across several weekdays → daily;
otherwise None (frequency only). Thresholds are conservative — a false "you do
this every Friday" is worse than silence, exactly like the reminder parser's
never-guess rule.

The pure detector `detect_cadence(local_datetimes)` operates on already-local
datetimes so it is hermetically testable regardless of the machine timezone;
`mine_task_patterns` does the UTC→local conversion.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.routines import ROUTINE_OFFER_THRESHOLD, normalize_goal
from app.db.models import Task

# A goal must have recurred at least this many times to be a pattern — reuse the
# offer-to-save threshold so "recurring" means the same thing everywhere.
MIN_OCCURRENCES = ROUTINE_OFFER_THRESHOLD

# Fraction of occurrences that must agree for a cadence to be declared.
CADENCE_MAJORITY = 0.6

# Half-width (hours) of the "same time of day" band. 2h tolerates ordinary
# drift ("late afternoon") without inventing precision the data lacks.
HOUR_BAND = 2

# A "daily" cadence must be spread across at least this many distinct weekdays,
# so a Saturday+Sunday habit is never mislabelled "every day".
DAILY_MIN_WEEKDAYS = 3

DEFAULT_LIMIT = 10

_WEEKDAY_NAMES = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
)


@dataclass(frozen=True)
class PatternCadence:
    """A detected temporal cadence. `hour`/`minute` are a representative local
    time; `weekday` is 0=Mon..6=Sun for weekly, None for daily."""
    kind: str            # "weekly" | "daily"
    hour: int            # 0-23
    minute: int          # 0-59
    weekday: Optional[int] = None


@dataclass(frozen=True)
class PatternCandidate:
    """One recurring goal, its occurrence count, and its cadence (or None)."""
    normalized_goal: str
    sample_goal: str
    count: int
    cadence: Optional[PatternCadence]


# ---------------------------------------------------------------- detection

def _hour_distance(a: int, b: int) -> int:
    """Circular distance between two clock hours (23 and 1 are 2 apart)."""
    diff = abs(a - b)
    return min(diff, 24 - diff)


def _median_time(dts: list[datetime]) -> tuple[int, int]:
    """Representative (hour, minute) — the median minute-of-day of the group."""
    minutes = sorted(d.hour * 60 + d.minute for d in dts)
    mid = minutes[len(minutes) // 2]
    return mid // 60, mid % 60


def _best_hour_band(dts: list[datetime]) -> tuple[float, int, int]:
    """The tightest same-time-of-day cluster: try each occurrence's hour as a
    band center, count how many fall within ±HOUR_BAND (circular), and return
    (coverage_fraction, representative_hour, representative_minute) for the best
    center."""
    if not dts:
        return 0.0, 0, 0
    total = len(dts)
    best_count, best_center = 0, dts[0].hour
    for candidate in dts:
        center = candidate.hour
        count = sum(1 for d in dts if _hour_distance(d.hour, center) <= HOUR_BAND)
        if count > best_count:
            best_count, best_center = count, center
    covered = [d for d in dts if _hour_distance(d.hour, best_center) <= HOUR_BAND]
    hour, minute = _median_time(covered)
    return best_count / total, hour, minute


def detect_cadence(local_dts: list[datetime]) -> Optional[PatternCadence]:
    """Deterministic cadence detection over LOCAL datetimes. Returns a weekly or
    daily cadence when the occurrences agree strongly, else None. Pure and
    timezone-agnostic (operates only on .weekday()/.hour/.minute), so tests can
    pass controlled local datetimes."""
    dts = [d for d in local_dts if d is not None]
    total = len(dts)
    if total < MIN_OCCURRENCES:
        return None

    # Weekly: a dominant weekday AND a tight hour cluster within that weekday.
    by_weekday: dict[int, list[datetime]] = {}
    for d in dts:
        by_weekday.setdefault(d.weekday(), []).append(d)
    weekday, group = max(by_weekday.items(), key=lambda kv: len(kv[1]))
    if len(group) / total >= CADENCE_MAJORITY:
        frac, hour, minute = _best_hour_band(group)
        if frac >= CADENCE_MAJORITY:
            return PatternCadence(kind="weekly", weekday=weekday, hour=hour, minute=minute)

    # Daily: a tight hour cluster spread across several distinct weekdays.
    frac, hour, minute = _best_hour_band(dts)
    distinct_weekdays = len({d.weekday() for d in dts})
    if frac >= CADENCE_MAJORITY and distinct_weekdays >= DAILY_MIN_WEEKDAYS:
        return PatternCadence(kind="daily", weekday=None, hour=hour, minute=minute)

    return None


# ---------------------------------------------------------------- rendering

def _fmt_time(hour: int, minute: int) -> str:
    """12-hour clock label, e.g. '4:00 PM'."""
    suffix = "AM" if hour < 12 else "PM"
    h12 = hour % 12 or 12
    return f"{h12}:{minute:02d} {suffix}"


def describe_cadence(cadence: Optional[PatternCadence]) -> str:
    """Human phrase for an offer/UI, e.g. 'every Friday around 4:00 PM'."""
    if cadence is None:
        return ""
    when = _fmt_time(cadence.hour, cadence.minute)
    if cadence.kind == "weekly" and cadence.weekday is not None:
        return f"every {_WEEKDAY_NAMES[cadence.weekday]} around {when}"
    return f"every day around {when}"


def teach_phrase_cadence(cadence: Optional[PatternCadence]) -> str:
    """A recurrence phrase parse_routine_recurrence understands, so the offer's
    teach line applies the schedule when the user says it back verbatim, e.g.
    'every friday at 4:00pm'."""
    if cadence is None:
        return ""
    suffix = "am" if cadence.hour < 12 else "pm"
    h12 = cadence.hour % 12 or 12
    clock = f"{h12}:{cadence.minute:02d}{suffix}"
    if cadence.kind == "weekly" and cadence.weekday is not None:
        return f"every {_WEEKDAY_NAMES[cadence.weekday].lower()} at {clock}"
    return f"every day at {clock}"


def cadence_to_schedule(cadence: Optional[PatternCadence]) -> Optional[dict]:
    """Map a cadence to the Routine schedule-field dict (for pre-filling a
    scheduled routine), or None."""
    if cadence is None:
        return None
    if cadence.kind == "weekly":
        return {
            "schedule_type": "weekly",
            "schedule_weekday": cadence.weekday,
            "schedule_hour": cadence.hour,
            "schedule_minute": cadence.minute,
        }
    if cadence.kind == "daily":
        return {
            "schedule_type": "daily",
            "schedule_hour": cadence.hour,
            "schedule_minute": cadence.minute,
        }
    return None


def format_task_patterns(candidates: list[PatternCandidate]) -> str:
    """Render mined patterns as a plain-text list for the Initiative signal
    block. '' when there is nothing worth automating (the section is omitted)."""
    usable = [c for c in candidates if c.count >= MIN_OCCURRENCES]
    if not usable:
        return ""
    lines = []
    for c in usable[:5]:
        goal = " ".join(c.sample_goal.split())
        if len(goal) > 80:
            goal = goal[:79] + "…"
        when = f", {describe_cadence(c.cadence)}" if c.cadence else ""
        lines.append(f'- "{goal}" (done {c.count} times{when})')
    return "\n".join(lines)


# ---------------------------------------------------------------- queries

def _to_local(dt: datetime) -> datetime:
    """A naive-UTC stored timestamp → machine-local aware time."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()


async def _completed_goal_times(db: AsyncSession) -> list[tuple[str, Optional[datetime]]]:
    rows = (
        await db.execute(
            select(Task.goal, Task.finished_at).where(Task.status == "completed")
        )
    ).all()
    return [(g, f) for g, f in rows]


async def mine_task_patterns(
    db: AsyncSession, *, min_occurrences: int = MIN_OCCURRENCES, limit: int = DEFAULT_LIMIT
) -> list[PatternCandidate]:
    """Recurring completed goals with their cadence, most-significant first
    (cadence-bearing then most-frequent). Best-effort — any failure yields []."""
    try:
        rows = await _completed_goal_times(db)
    except Exception as e:  # pragma: no cover — defensive, never break a caller
        logger.warning(f"mine_task_patterns query failed (non-critical): {e}")
        return []

    groups: dict[str, list[Optional[datetime]]] = {}
    samples: dict[str, str] = {}
    for goal, finished_at in rows:
        if not goal:
            continue
        key = normalize_goal(goal)
        if not key:
            continue
        groups.setdefault(key, []).append(finished_at)
        samples.setdefault(key, goal.strip())

    candidates: list[PatternCandidate] = []
    for key, times in groups.items():
        count = len(times)
        if count < min_occurrences:
            continue
        local_dts = [_to_local(t) for t in times if t is not None]
        candidates.append(PatternCandidate(
            normalized_goal=key,
            sample_goal=samples[key],
            count=count,
            cadence=detect_cadence(local_dts),
        ))

    candidates.sort(key=lambda c: (c.cadence is not None, c.count), reverse=True)
    return candidates[: max(0, limit)]


async def cadence_for_goal(db: AsyncSession, goal: str) -> Optional[PatternCadence]:
    """The detected cadence for ONE goal's completed runs, or None. Used by the
    offer-to-save path to enrich its wording. Best-effort → None."""
    key = normalize_goal(goal)
    if not key:
        return None
    try:
        rows = await _completed_goal_times(db)
    except Exception as e:  # pragma: no cover — defensive
        logger.warning(f"cadence_for_goal query failed (non-critical): {e}")
        return None
    times = [f for g, f in rows if g and normalize_goal(g) == key and f is not None]
    return detect_cadence([_to_local(t) for t in times])
