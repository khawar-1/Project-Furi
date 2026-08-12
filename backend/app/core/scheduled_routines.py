"""
Furi OS — Scheduled routines (Phase 10, Part 2)

Gives a teachable routine an optional TIME TRIGGER that auto-runs it. This is
the one genuinely new autonomous surface in Phase 10 — everything else rides
the Initiative Engine. It follows the birthdays.py recurring-job pattern
verbatim, with a PER-ROW job pointer (Routine.schedule_job_id, the
Contact.birthday_job_id template) and interval math borrowed from reindex.py.

THE LOAD-BEARING SAFETY PROPERTY: a scheduled routine runs its goal_template
STRING through start_task — the plan is RE-DERIVED fresh through the agent
planner, so the structural approval gate, path guards, and recipient/event-id
locks all re-apply automatically. A scheduled routine that would WRITE pauses
and pushes a PlanCard for approval; a read-only routine completes autonomously.
"Scheduled" therefore means auto-PLAN, never auto-WRITE — a routine can never
smuggle a pre-approved destructive plan past the gate, even fired unattended.
No LLM call happens in the handler itself.

Occurrence math:
- interval → now + interval_minutes (the reindex pure-interval convention)
- daily    → next local HH:MM strictly after now (the next_briefing_run_at rule)
- weekly   → next local weekday@HH:MM strictly after now
All returned as naive UTC via to_naive_utc, what schedule_at stores.
"""
from datetime import datetime, timedelta
from typing import Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.birthdays import _latest_session_id  # reuse: newest chat session
from app.core.scheduler import FiredJob, register_job_handler, scheduler, to_naive_utc
from app.db.models import Routine

ROUTINE_JOB_KIND = "routine"

_VALID_TYPES = ("interval", "daily", "weekly")
_MIN_INTERVAL_MINUTES = 5
_MAX_INTERVAL_MINUTES = 10_080  # one week

_WEEKDAY_NAMES = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
)


def _fmt_time(hour: int, minute: int) -> str:
    suffix = "AM" if (hour or 0) < 12 else "PM"
    h12 = (hour or 0) % 12 or 12
    return f"{h12}:{(minute or 0):02d} {suffix}"


def describe_schedule(routine: Routine) -> str:
    """A human phrase for a routine's schedule ('every Friday at 4:00 PM'),
    or '' when unscheduled. Used in the teach acknowledgement + logging."""
    stype = routine.schedule_type
    if stype == "interval":
        n = routine.schedule_interval_minutes or 0
        if n and n % 60 == 0:
            hours = n // 60
            return f"every {hours} hour{'s' if hours != 1 else ''}"
        return f"every {n} minutes"
    if stype == "daily":
        return f"every day at {_fmt_time(routine.schedule_hour, routine.schedule_minute)}"
    if stype == "weekly" and routine.schedule_weekday is not None:
        day = _WEEKDAY_NAMES[routine.schedule_weekday]
        return f"every {day} at {_fmt_time(routine.schedule_hour, routine.schedule_minute)}"
    return ""


# --------------------------------------------------------- occurrence math

def _local_now(now: Optional[datetime]) -> datetime:
    """Naive LOCAL now (the next_birthday_run_at convention — an aware value is
    coerced to local naive)."""
    if now is None:
        return datetime.now()
    if now.tzinfo is not None:
        return now.astimezone().replace(tzinfo=None)
    return now


def _next_daily(now: datetime, hour: int, minute: int) -> Optional[datetime]:
    try:
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError:
        return None
    if candidate <= now:
        candidate += timedelta(days=1)
    return to_naive_utc(candidate.astimezone())


def _next_weekly(now: datetime, weekday: int, hour: int, minute: int) -> Optional[datetime]:
    if not (0 <= weekday <= 6):
        return None
    try:
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError:
        return None
    candidate += timedelta(days=(weekday - now.weekday()) % 7)
    if candidate <= now:
        candidate += timedelta(days=7)
    return to_naive_utc(candidate.astimezone())


def next_routine_run_at(
    routine: Routine, now: Optional[datetime] = None
) -> Optional[datetime]:
    """The next occurrence of this routine's schedule strictly after `now`
    (naive local), as naive UTC. None if the routine is unscheduled or its
    fields are unusable."""
    stype = routine.schedule_type
    if stype not in _VALID_TYPES:
        return None
    now = _local_now(now)

    if stype == "interval":
        interval = routine.schedule_interval_minutes
        if not interval or interval < 1:
            return None
        return to_naive_utc((now + timedelta(minutes=int(interval))).astimezone())
    if stype == "daily":
        return _next_daily(now, routine.schedule_hour or 0, routine.schedule_minute or 0)
    if stype == "weekly":
        if routine.schedule_weekday is None:
            return None
        return _next_weekly(
            now, routine.schedule_weekday, routine.schedule_hour or 0,
            routine.schedule_minute or 0,
        )
    return None


# ------------------------------------------------------- schedule validation

def normalize_schedule_spec(spec: Optional[dict]) -> dict:
    """Validate + clamp a schedule dict into clean Routine.schedule_* values.

    A None/empty/"none" schedule_type CLEARS the schedule (all fields reset).
    Raises ValueError with a user-facing message on an invalid spec (the API
    turns it into a 400; the offer/teach paths only ever pass known-good specs).
    """
    cleared = {
        "schedule_type": None,
        "schedule_minute": 0,
        "schedule_hour": 9,
        "schedule_weekday": None,
        "schedule_interval_minutes": None,
    }
    if not spec:
        return cleared
    stype = spec.get("schedule_type")
    if stype in (None, "", "none", "manual"):
        return cleared
    if stype not in _VALID_TYPES:
        raise ValueError(
            f"schedule_type must be one of {_VALID_TYPES} (or empty to clear)"
        )

    def _int(key: str, lo: int, hi: int, default: int) -> int:
        """Coerce + CLAMP into range (the app_settings _clamp_int rule — never
        crash on an out-of-range value; the store is the final authority)."""
        raw = spec.get(key, default)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = default
        return max(lo, min(value, hi))

    if stype == "interval":
        interval = _int("schedule_interval_minutes", _MIN_INTERVAL_MINUTES,
                        _MAX_INTERVAL_MINUTES, _MIN_INTERVAL_MINUTES)
        return {**cleared, "schedule_type": "interval",
                "schedule_interval_minutes": interval}

    hour = _int("schedule_hour", 0, 23, 9)
    minute = _int("schedule_minute", 0, 59, 0)
    if stype == "daily":
        return {**cleared, "schedule_type": "daily",
                "schedule_hour": hour, "schedule_minute": minute}
    # weekly
    weekday = _int("schedule_weekday", 0, 6, 0)
    return {**cleared, "schedule_type": "weekly", "schedule_weekday": weekday,
            "schedule_hour": hour, "schedule_minute": minute}


# ------------------------------------------------------------- choke point

async def sync_routine_schedule_job(db: AsyncSession, routine: Routine) -> None:
    """Cancel the routine's current schedule job (if any) and, iff it is active
    and scheduled, arm the next occurrence and store its id on the row. The ONE
    function every schedule hook (set / clear / re-arm / startup) calls.
    Best-effort — a scheduler failure never breaks the routine save."""
    try:
        if routine.schedule_job_id:
            await scheduler.cancel(routine.schedule_job_id)
            routine.schedule_job_id = None

        run_at = None
        if routine.is_active and routine.schedule_type:
            run_at = next_routine_run_at(routine)
        if run_at is not None:
            job_id = await scheduler.schedule_at(
                run_at,
                ROUTINE_JOB_KIND,
                {"routine_id": routine.id, "schedule_type": routine.schedule_type},
            )
            routine.schedule_job_id = job_id

        await db.commit()
    except Exception as e:  # best-effort — never break a routine save
        logger.warning(
            f"Routine schedule sync failed for {routine.id} (non-critical): {e}"
        )


async def set_routine_schedule(
    db: AsyncSession, routine_id: str, spec: Optional[dict]
) -> Optional[Routine]:
    """Apply a validated schedule to a routine and (re)arm its job in one call.
    Returns the routine, or None if it doesn't exist. Raises ValueError on an
    invalid spec (caller maps to 400)."""
    routine = await db.get(Routine, routine_id)
    if routine is None:
        return None
    clean = normalize_schedule_spec(spec)
    for field, value in clean.items():
        setattr(routine, field, value)
    await db.commit()
    await db.refresh(routine)
    await sync_routine_schedule_job(db, routine)  # commits the pointer too
    return routine


# ------------------------------------------------------------------- firing

async def _routine_job_handler(job: FiredJob) -> None:
    """Fires once the scheduler won the fire-vs-cancel race. Re-derives and runs
    the routine's goal in the background (approval gate re-applies), then
    RE-ARMS the next occurrence. Guards reject a vanished/unscheduled routine or
    a stale job so a recurrence chain never forks. No LLM call here."""
    from app.db.database import AsyncSessionLocal

    routine_id = job.payload.get("routine_id")
    if not isinstance(routine_id, str) or not routine_id:
        raise ValueError("routine job payload needs a non-empty 'routine_id' string")

    async with AsyncSessionLocal() as db:
        routine = await db.get(Routine, routine_id)
        # Guard 1: gone / deactivated / schedule cleared — no re-arm.
        if routine is None or not routine.is_active or not routine.schedule_type:
            return
        # Guard 2: a stale job (not the routine's current pointer) never forks
        # a second recurrence chain.
        if routine.schedule_job_id != job.id:
            return
        # Guard 3: the schedule kind changed since scheduling — the sync path
        # owns the new value; this job is obsolete.
        payload_type = job.payload.get("schedule_type")
        if payload_type and payload_type != routine.schedule_type:
            return

        goal = routine.goal_template
        session_id = await _latest_session_id(db)
        try:
            from app.agents import planner_memory_context, start_task
            memory = await planner_memory_context(db, goal)
            await start_task(
                db, goal, session_id, conversation="", memory=memory, provider=None
            )
            logger.info(f"Scheduled routine fired: '{routine.name}' ({routine.id})")
        except Exception as e:
            # A failed launch must never break the re-arm below (recurrence
            # discipline) — the next occurrence still gets a chance.
            logger.warning(
                f"Scheduled routine '{routine.id}' failed to start (non-critical): {e}"
            )

        # Recurrence: arm the next occurrence (cancels this fired job — a
        # harmless no-op — and stores the new id on the row).
        await sync_routine_schedule_job(db, routine)


# --------------------------------------------------- startup reconciliation

async def ensure_routine_schedule_jobs() -> None:
    """Self-healing startup pass (the ensure_birthday_jobs shape): arm missing
    schedule jobs and sweep orphans (jobs whose routine is gone/inactive/
    unscheduled or that a routine no longer points at). Runs after
    scheduler.start(); best-effort."""
    from app.db.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        routines = list((
            await db.execute(select(Routine).where(Routine.is_active == True))  # noqa: E712
        ).scalars().all())

        pending = await scheduler.list_jobs(
            status="pending", kind=ROUTINE_JOB_KIND, limit=10_000
        )

        valid_job_ids = {
            r.schedule_job_id
            for r in routines
            if r.schedule_job_id and r.schedule_type and next_routine_run_at(r)
        }

        swept = 0
        for j in pending:
            if j["id"] not in valid_job_ids:
                await scheduler.cancel(j["id"])
                swept += 1
        live_ids = {j["id"] for j in pending if j["id"] in valid_job_ids}

        armed = 0
        for r in routines:
            if not r.schedule_type or not next_routine_run_at(r):
                continue
            if r.schedule_job_id and r.schedule_job_id in live_ids:
                continue
            await sync_routine_schedule_job(db, r)
            armed += 1

    logger.info(f"Routine schedule jobs reconciled: {armed} armed, {swept} orphan(s) swept")


def register() -> None:
    """Register the 'routine' job handler at import (the reminders pattern)."""
    register_job_handler(ROUTINE_JOB_KIND, _routine_job_handler)


register()
