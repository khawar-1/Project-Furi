"""
Jarvis OS — Birthday reminders (Phase 5, Part 4)

The first RECURRING proactive feature, built entirely on Parts 1-2 like the
Part 4 (phase 4) reminders: a contact's birthday becomes a scheduled_jobs row
that fires at 09:00 local on the day, pushes a Part 1 event (Part 3's toast
wiring reads title/body unchanged) AND persists a chat message (the push
channel has no queue), then RE-ARMS next year's job — recurrence without
touching the scheduler's one-shot core. SQLite stays the truth; a restart on
the birthday itself fires late-but-fires (existing scheduler behavior).

Design (all in code):
- One job per contact. Contact.birthday_job_id points at the pending
  scheduled_jobs row (mirrors Reminder.job_id). sync_contact_birthday_job is
  the single choke point every hook (create / update / clear / soft-delete /
  hard-delete) calls — it cancels the old job and arms the next occurrence.
- 09:00 LOCAL on the birthday, stored as naive UTC (the reminder convention:
  user-facing times are local, storage is naive UTC via to_naive_utc).
- Feb 29 birthdays fire Feb 28 in non-leap years — early beats missed for a
  "wish them" nudge. Deterministic, documented.
- The handler is defensive: a contact that vanished/deactivated, a stale job
  (not the contact's current one), or a birthday that changed since the job
  was scheduled all no-op WITHOUT re-arming — the sync path owns the value.
- ensure_birthday_jobs() reconciles at startup (self-healing): arms missing
  jobs (pre-Part-4 contacts, fire/re-arm crashes) and sweeps orphans (jobs
  whose contact is gone/inactive/birthday-less or that a contact no longer
  points at). Best-effort throughout — a scheduler hiccup never breaks a
  contact save (logged, like narration).
"""
import calendar as _calendar
from datetime import datetime, timezone
from typing import Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.push import push
from app.core.scheduler import FiredJob, register_job_handler, scheduler, to_naive_utc
from app.db.models import Contact, Message

BIRTHDAY_JOB_KIND = "birthday"
BIRTHDAY_HOUR = 9  # 09:00 local, the "wish them" nudge time


# --------------------------------------------------------- occurrence math

def _parse_month_day(birthday: str) -> Optional[tuple[int, int]]:
    """(month, day) from a canonical "MM-DD" or "YYYY-MM-DD" birthday, or None
    if it is neither (the value stored is already validated by
    normalize_birthday, so this is a light parse, not a re-validation)."""
    parts = str(birthday or "").strip().split("-")
    try:
        if len(parts) == 2:
            month, day = int(parts[0]), int(parts[1])
        elif len(parts) == 3:
            month, day = int(parts[1]), int(parts[2])
        else:
            return None
    except ValueError:
        return None
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return month, day


def _occurrence_in_year(year: int, month: int, day: int) -> Optional[datetime]:
    """The 09:00-local naive datetime of this birthday in `year`. Feb 29 in a
    non-leap year slides to Feb 28."""
    if month == 2 and day == 29 and not _calendar.isleap(year):
        day = 28
    try:
        return datetime(year, month, day, BIRTHDAY_HOUR, 0)
    except ValueError:
        return None


def next_birthday_run_at(
    birthday: str, now: Optional[datetime] = None
) -> Optional[datetime]:
    """The next 09:00-local occurrence of `birthday` strictly after `now`,
    returned as naive UTC (what schedule_at stores). `now` is naive LOCAL
    (the normalize_birthday(today=) injectable-clock pattern); an aware `now`
    is coerced to local naive. None when the birthday can't be parsed."""
    md = _parse_month_day(birthday)
    if md is None:
        return None
    month, day = md
    if now is None:
        now = datetime.now()  # naive local
    elif now.tzinfo is not None:
        now = now.astimezone().replace(tzinfo=None)

    candidate = _occurrence_in_year(now.year, month, day)
    if candidate is None or candidate <= now:
        candidate = _occurrence_in_year(now.year + 1, month, day)
    if candidate is None:
        return None
    # Local wall-clock 09:00 → aware (machine offset) → naive UTC.
    return to_naive_utc(candidate.astimezone())


# ------------------------------------------------------------- the choke point

async def sync_contact_birthday_job(db: AsyncSession, contact: Contact) -> None:
    """Cancel the contact's current birthday job (if any) and, iff the contact
    is active with a valid birthday, arm the next occurrence and store its id
    on the contact. The ONE function every contact-write hook calls. Wrapped
    so a scheduler failure never breaks the contact save that preceded it."""
    try:
        if contact.birthday_job_id:
            await scheduler.cancel(contact.birthday_job_id)
            contact.birthday_job_id = None

        run_at = None
        if contact.is_active and contact.birthday:
            run_at = next_birthday_run_at(contact.birthday)
        if run_at is not None:
            job_id = await scheduler.schedule_at(
                run_at,
                BIRTHDAY_JOB_KIND,
                {"contact_id": contact.id, "birthday": contact.birthday},
            )
            contact.birthday_job_id = job_id

        await db.commit()
    except Exception as e:  # best-effort — never break a contact save
        logger.warning(
            f"Birthday job sync failed for contact {contact.id} (non-critical): {e}"
        )


# ------------------------------------------------------------------- firing

def _fired_on_later_day(job: FiredJob) -> bool:
    """True when this job fired on a calendar day AFTER the birthday (the
    backend was offline past it) — distinct from merely a few minutes late on
    the day itself, which is still 'today is their birthday'."""
    run_local = job.run_at.replace(tzinfo=timezone.utc).astimezone()
    return datetime.now().date() > run_local.date()


def _age_turning(birthday: str, run_at: datetime) -> Optional[int]:
    """The age the contact turns on this occurrence, when the birth year is
    known ("YYYY-MM-DD"), else None."""
    parts = str(birthday or "").split("-")
    if len(parts) != 3:
        return None
    try:
        birth_year = int(parts[0])
    except ValueError:
        return None
    run_local = run_at.replace(tzinfo=timezone.utc).astimezone()
    age = run_local.year - birth_year
    return age if age > 0 else None


def _birthday_body(contact: Contact, job: FiredJob) -> str:
    """The chat/toast text. Honest when fired on a later day (offline gap)."""
    name = contact.name
    if _fired_on_later_day(job):
        run_local = job.run_at.replace(tzinfo=timezone.utc).astimezone()
        when = f"{run_local.strftime('%B')} {run_local.day}"
        return (
            f"Birthday reminder (missed while Jarvis was offline — {name}'s "
            f"birthday was on {when}) 🎂"
        )
    age = _age_turning(contact.birthday, job.run_at)
    if age is not None:
        return f"Today is {name}'s birthday 🎂 — they turn {age} today."
    return f"Today is {name}'s birthday 🎂"


async def _latest_session_id(db: AsyncSession) -> Optional[str]:
    """The newest chat session, so a fired birthday is visible even if no
    window caught the push. None when the user has never chatted."""
    result = await db.execute(
        select(Message.session_id)
        .where(Message.session_id.isnot(None))
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _birthday_job_handler(job: FiredJob) -> None:
    """Runs once the scheduler has won the fire-vs-cancel race. Pushes the
    event, persists a chat message, and RE-ARMS next year. Guards reject a
    stale/superseded job so a recurrence chain never forks. Handler failures
    land on the job row per the scheduler contract — never propagate."""
    from app.db.database import AsyncSessionLocal

    contact_id = job.payload.get("contact_id")
    if not isinstance(contact_id, str) or not contact_id:
        raise ValueError("birthday job payload needs a non-empty 'contact_id' string")

    async with AsyncSessionLocal() as db:
        contact = await db.get(Contact, contact_id)
        # Guard 1: contact gone, deactivated, or birthday cleared — no re-arm.
        if contact is None or not contact.is_active or not contact.birthday:
            return
        # Guard 2: a stale job (not the contact's current pointer) never forks
        # a second recurrence chain.
        if contact.birthday_job_id != job.id:
            return
        # Guard 3: the birthday changed since this job was scheduled — the sync
        # path owns the new value; this job is obsolete.
        payload_birthday = job.payload.get("birthday")
        if payload_birthday and payload_birthday != contact.birthday:
            return

        body = _birthday_body(contact, job)
        await push("birthday", {
            "contact_id": contact.id,
            "title": "Jarvis",
            "body": body,
            "text": body,
            "late": job.late,
        })

        session_id = await _latest_session_id(db)
        if session_id:
            db.add(Message(session_id=session_id, role="assistant", content=body))
            await db.commit()

        # Recurrence: arm next year's one-shot job (cancels this fired one,
        # which is a harmless no-op, and stores the new id on the contact).
        await sync_contact_birthday_job(db, contact)


# --------------------------------------------------- startup reconciliation

async def ensure_birthday_jobs() -> None:
    """Self-healing startup pass: arm missing birthday jobs and sweep orphans.
    Covers contacts whose birthdays predate Part 4, a crash between fire and
    re-arm, and stray jobs whose contact is gone/inactive/birthday-less. Runs
    after scheduler.start(); best-effort (a failure is logged, non-critical)."""
    from app.db.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Contact).where(Contact.is_active == True))  # noqa: E712
        contacts = list(result.scalars().all())

        pending = await scheduler.list_jobs(
            status="pending", kind=BIRTHDAY_JOB_KIND, limit=10_000
        )

        # A pending job is VALID only if it is the current pointer of an active
        # contact that still has a schedulable birthday.
        valid_job_ids = {
            c.birthday_job_id
            for c in contacts
            if c.birthday_job_id and c.birthday and next_birthday_run_at(c.birthday)
        }

        swept = 0
        for j in pending:
            if j["id"] not in valid_job_ids:
                await scheduler.cancel(j["id"])
                swept += 1
        live_ids = {j["id"] for j in pending if j["id"] in valid_job_ids}

        armed = 0
        for c in contacts:
            if not c.birthday or not next_birthday_run_at(c.birthday):
                continue
            if c.birthday_job_id and c.birthday_job_id in live_ids:
                continue  # already has a live pending job
            await sync_contact_birthday_job(db, c)
            armed += 1

    logger.info(f"Birthday jobs reconciled: {armed} armed, {swept} orphan(s) swept")


def register() -> None:
    """Register the 'birthday' job handler at import time, mirroring how
    app.core.reminders self-registers."""
    register_job_handler(BIRTHDAY_JOB_KIND, _birthday_job_handler)


register()
