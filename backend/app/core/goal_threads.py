"""
Jarvis OS — Goal threads (Phase 11, Part 3 — ongoing-concern tracking)

The ONE accessor for the `goal_threads` table (the reminders rule — the
extractor, the initiative gatherer, and the API all go through here; none
touch the table directly). A goal thread is a lightweight ongoing concern —
"the deadline I was worried about", "prepping for the interview" — that Jarvis
can proactively follow up on ("last week you were worried about X; did it
land?").

Creation is best-effort from the extractor (source="extractor") or manual
(source="manual"). Re-mentioning the same concern UPSERTS the open thread
(dedupe by normalized title) instead of piling up duplicates. `next_check_at`
is when a nudge becomes due; the initiative engine reads `due_threads`, nudges,
and calls `mark_nudged` to push the next check out so a concern is never nagged
on every heartbeat.
"""
from datetime import datetime, timedelta
from typing import Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.routines import normalize_name  # shared title/dedupe normalizer
from app.db.models import GoalThread, utc_now

# Days after creation to first nudge when there is no explicit event_date.
DEFAULT_CHECK_DAYS = 7
# Days to push the next check out after a nudge (so it isn't nagged repeatedly).
RENUDGE_DAYS = 7

_VALID_STATUS = ("open", "resolved", "dropped")


def normalize_title(title: str) -> str:
    """Dedupe key for a concern — reuses the routine/goal normalizer."""
    return normalize_name(title)


def _default_next_check(event_date, created: datetime) -> datetime:
    """When to first nudge: the day AFTER a known event_date ('did it land?'),
    otherwise DEFAULT_CHECK_DAYS after creation. Naive UTC (day-granular)."""
    if event_date is not None:
        base = datetime(event_date.year, event_date.month, event_date.day)
        return base + timedelta(days=1)
    return created + timedelta(days=DEFAULT_CHECK_DAYS)


async def _find_open(db: AsyncSession, key: str) -> Optional[GoalThread]:
    if not key:
        return None
    result = await db.execute(
        select(GoalThread).where(
            GoalThread.normalized_title == key,
            GoalThread.status == "open",
            GoalThread.is_active.is_(True),
        )
    )
    return result.scalar_one_or_none()


async def upsert_thread(
    db: AsyncSession,
    title: str,
    *,
    description: Optional[str] = None,
    event_date=None,
    contact_id: Optional[str] = None,
    source: str = "extractor",
    next_check_at: Optional[datetime] = None,
) -> Optional[GoalThread]:
    """Create a thread, or UPDATE the existing open thread with the same
    normalized title (re-mention → refresh, not duplicate). Returns the row, or
    None when the title is empty. Best-effort friendly — the extractor wraps its
    call, so a failure here never breaks extraction."""
    key = normalize_title(title)
    if not key:
        return None

    now = utc_now()
    check = next_check_at or _default_next_check(event_date, now)

    existing = await _find_open(db, key)
    if existing is not None:
        if description:
            existing.description = description
        if event_date is not None:
            existing.event_date = event_date
        if contact_id is not None:
            existing.contact_id = contact_id
        existing.next_check_at = check
        existing.is_active = True
        await db.commit()
        await db.refresh(existing)
        return existing

    thread = GoalThread(
        title=title.strip()[:512],
        normalized_title=key,
        description=description,
        status="open",
        contact_id=contact_id,
        event_date=event_date,
        next_check_at=check,
        source=source,
    )
    db.add(thread)
    await db.commit()
    await db.refresh(thread)
    logger.info(f"Goal thread opened: '{thread.title[:60]}' (source={source})")
    return thread


async def list_threads(
    db: AsyncSession, status: Optional[str] = None, limit: int = 50
) -> list[GoalThread]:
    """Active threads, newest first, optionally filtered by status."""
    query = select(GoalThread).where(GoalThread.is_active.is_(True))
    if status:
        query = query.where(GoalThread.status == status)
    query = query.order_by(GoalThread.created_at.desc()).limit(limit)
    return list((await db.execute(query)).scalars().all())


async def get_thread(db: AsyncSession, thread_id: str) -> Optional[GoalThread]:
    return await db.get(GoalThread, thread_id)


async def _set_status(db: AsyncSession, thread_id: str, status: str) -> Optional[GoalThread]:
    thread = await db.get(GoalThread, thread_id)
    if thread is None:
        return None
    thread.status = status
    thread.updated_at = utc_now()
    await db.commit()
    await db.refresh(thread)
    return thread


async def resolve_thread(db: AsyncSession, thread_id: str) -> Optional[GoalThread]:
    """The concern landed / is done — stop nudging."""
    return await _set_status(db, thread_id, "resolved")


async def drop_thread(db: AsyncSession, thread_id: str) -> Optional[GoalThread]:
    """The user dismissed the concern — stop nudging (distinct from resolved)."""
    return await _set_status(db, thread_id, "dropped")


async def due_threads(
    db: AsyncSession, now: Optional[datetime] = None, limit: int = 10
) -> list[GoalThread]:
    """Open, active threads whose next_check_at has passed — nudge candidates.
    Best-effort → [] (never breaks a heartbeat)."""
    now = now or utc_now()
    try:
        result = await db.execute(
            select(GoalThread)
            .where(
                GoalThread.status == "open",
                GoalThread.is_active.is_(True),
                GoalThread.next_check_at.isnot(None),
                GoalThread.next_check_at <= now,
            )
            .order_by(GoalThread.next_check_at.asc())
            .limit(limit)
        )
        return list(result.scalars().all())
    except Exception as e:  # pragma: no cover — defensive
        logger.warning(f"due_threads query failed (non-critical): {e}")
        return []


async def mark_nudged(
    db: AsyncSession, thread: GoalThread, now: Optional[datetime] = None
) -> None:
    """Record that a thread was just nudged and push its next check out so it is
    not nagged on the next heartbeat. Best-effort."""
    try:
        now = now or utc_now()
        thread.last_nudged_at = now
        thread.next_check_at = now + timedelta(days=RENUDGE_DAYS)
        await db.commit()
    except Exception as e:
        logger.warning(f"mark_nudged failed (non-critical): {e}")
        try:
            await db.rollback()
        except Exception:
            pass
