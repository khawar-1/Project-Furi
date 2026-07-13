"""
Jarvis OS — Reminders (Phase 4, Part 4)

The phase's "moment": "remind me at 6 to call Jamil" becomes a Reminder row
plus a Part 2 scheduled job. When the job fires: a Part 1 push event goes
out (a live window shows it in chat and, if unfocused or closed to tray,
Part 3 raises the native notification); and — because the push channel has
no queue — a chat message is ALSO persisted into the reminder's session
directly, so the reminder is visible next time the user opens Jarvis even
if no window ever caught the push.

Reminder is the user-facing record (text, due_at, session_id, status); the
scheduled_jobs row it points at (job_id) is the timer. The fire-vs-cancel
race is settled once, at the scheduler level (JarvisScheduler's own atomic
row claim) — this module mirrors that outcome onto the Reminder row, it
never re-arbitrates it. A reminder whose job settles as "failed" (a bug, or
a kind that lost its handler) is mirrored as failed too, never silently
left "pending" forever.
"""
from datetime import datetime
from typing import Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.push import push
from app.core.scheduler import FiredJob, register_job_handler, scheduler, to_naive_utc
from app.db.models import Message, Reminder, utc_now

REMINDER_JOB_KIND = "reminder"


async def create_reminder(
    db: AsyncSession, text: str, due_at: datetime, session_id: Optional[str] = None
) -> Reminder:
    """Persist the reminder row, THEN arm its timer — row before timer, same
    order as schedule_at's own row-before-timer rule, so a crash between the
    two steps leaves a real row (job_id=None) instead of an orphaned timer
    nobody can list or cancel."""
    reminder = Reminder(text=text, session_id=session_id, due_at=to_naive_utc(due_at))
    db.add(reminder)
    await db.commit()
    await db.refresh(reminder)

    job_id = await scheduler.schedule_at(due_at, REMINDER_JOB_KIND, {"reminder_id": reminder.id})
    reminder.job_id = job_id
    await db.commit()
    await db.refresh(reminder)
    return reminder


async def cancel_reminder(db: AsyncSession, reminder_id: str) -> bool:
    """True only if this call actually settled the reminder. Cancelling the
    underlying job is the real race-settler; this row is only updated when
    that succeeds, so a reminder that already fired (or was already
    cancelled) correctly returns False."""
    reminder = await db.get(Reminder, reminder_id)
    if reminder is None or reminder.status != "pending":
        return False
    if reminder.job_id:
        won = await scheduler.cancel(reminder.job_id)
        if not won:
            return False  # it fired (or was cancelled) between the read and now
    reminder.status = "cancelled"
    await db.commit()
    return True


async def list_reminders(db: AsyncSession, status: Optional[str] = None, limit: int = 50) -> list[Reminder]:
    query = select(Reminder).order_by(Reminder.due_at).limit(limit)
    if status is not None:
        query = query.where(Reminder.status == status)
    result = await db.execute(query)
    return list(result.scalars().all())


def _fallback_title(text: str) -> str:
    return text if len(text) <= 60 else text[:57] + "…"


def _describe_lateness(seconds: float) -> str:
    """Human wording for how far past due a late fire is ("5 minutes",
    "2 hours", "3 days"). Coarse on purpose — the exact due time is on the
    Reminder row; this is for a chat message, not an audit."""
    minutes = max(1, int(seconds // 60))
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''}"


async def _reminder_job_handler(job: FiredJob) -> None:
    """Runs once the scheduler has already won the fire-vs-cancel race for
    this job. Marks the Reminder fired, pushes the live event, and persists
    a chat message into its session so a closed window still gets the
    reminder next time it opens Jarvis."""
    from app.db.database import AsyncSessionLocal

    reminder_id = job.payload.get("reminder_id")
    if not isinstance(reminder_id, str) or not reminder_id:
        raise ValueError("reminder job payload needs a non-empty 'reminder_id' string")

    async with AsyncSessionLocal() as db:
        reminder = await db.get(Reminder, reminder_id)
        if reminder is None:
            logger.warning(f"Reminder job fired for missing reminder {reminder_id}")
            return
        if reminder.status != "pending":
            return  # already cancelled — the scheduled_jobs claim should have prevented this fire
        reminder.status = "fired"
        reminder.fired_at = utc_now()
        await db.commit()

        if job.late:
            # Honest about the gap: this fire is >60s past due (typically a
            # backend restart), never presented as if it fired on time.
            late_by = _describe_lateness((utc_now() - reminder.due_at).total_seconds())
            body = (
                f"Reminder (missed while Jarvis was offline — was due "
                f"{late_by} ago): {reminder.text}"
            )
        else:
            body = f"Reminder: {reminder.text}"
        await push("reminder", {
            "reminder_id": reminder.id,
            "title": "Jarvis",
            "body": body,
            "text": reminder.text,
            "session_id": reminder.session_id,
            "late": job.late,
        })

        if reminder.session_id:
            # Best-effort: the toast already fired and the reminder is marked
            # fired — a failed history write must not fail the job.
            from app.db.persist import persist_message_best_effort
            await persist_message_best_effort(
                db, reminder.session_id, "assistant", body,
                what="fired-reminder message",
            )


def register() -> None:
    """Idempotent-in-effect registration — called at import time so the
    scheduler always has a handler for 'reminder' jobs, mirroring how the
    built-in 'push' kind self-registers in scheduler.py."""
    register_job_handler(REMINDER_JOB_KIND, _reminder_job_handler)


register()
