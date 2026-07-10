"""
Jarvis OS — Scheduler / Event Bus (Phase 4, Part 2)

Timed work for every proactive feature (reminders, background tasks, ...).
The design mirrors plan_store: SQLite is the truth, the in-process timers
are the hot layer.

- Every job is a `scheduled_jobs` row FIRST; the APScheduler timer is only
  the wake-up call. A restart rebuilds all timers from pending rows
  (`start()`), so a reminder set before a reboot still fires — one whose
  run_at passed while the backend was down fires immediately on boot,
  marked late, because for proactive features late is better than never.
- The event bus is the handler registry: features register a coroutine per
  job `kind` (`register_job_handler`). Firing looks the handler up by kind;
  handler failures are recorded on the row (status=failed + error) and
  NEVER propagate — one bad job can't take the scheduler down.
- Firing CLAIMS the row (UPDATE ... WHERE status='pending', rowcount
  settles races) so a job fires at most once, ever — cancel vs. fire can
  never both win.
- Built-in kind "push": payload {"event_type": str, "payload": dict} —
  schedule any Part 1 push event for later. Delivery is best-effort like
  the channel itself; features needing guarantees own their state.

Everything runs on the backend's single asyncio loop; handlers can
`await push(...)` and open DB sessions directly.
"""
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from loguru import logger
from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import ScheduledJob, utc_iso, utc_now

#: A fire more than this many seconds after run_at is reported late=True.
LATE_AFTER_SECONDS = 60

#: Settled rows (fired/failed/cancelled) are purged after this at startup.
SETTLED_RETENTION_DAYS = 30

#: Errors stored on the row are truncated to this length.
MAX_ERROR_LEN = 2000


@dataclass(frozen=True)
class FiredJob:
    """What a job handler receives when its moment arrives."""
    id: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    run_at: datetime = field(default_factory=utc_now)  # naive UTC, as stored
    late: bool = False  # fired > LATE_AFTER_SECONDS past run_at (e.g. after a restart)


JobHandler = Callable[[FiredJob], Awaitable[None]]


def to_naive_utc(dt: datetime) -> datetime:
    """Normalize to the naive-UTC convention the DB uses. Aware datetimes
    are converted; naive ones are trusted to already be UTC. Public so other
    features (reminders) that build datetimes for schedule_at can store the
    same value they hand the scheduler."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


class JarvisScheduler:
    """AsyncIOScheduler wrapped so nothing outside this module touches
    APScheduler — the interface is schedule_at / cancel / handlers, and the
    persistence rules live here, not in call sites."""

    def __init__(
        self,
        session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
    ) -> None:
        self._session_factory = session_factory
        self._handlers: dict[str, JobHandler] = {}
        self._aps = AsyncIOScheduler(timezone=timezone.utc)
        self.register_handler("push", _push_job_handler)

    # ------------------------------------------------------------ plumbing

    def _factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            from app.db.database import AsyncSessionLocal  # late: tests inject their own
            self._session_factory = AsyncSessionLocal
        return self._session_factory

    @property
    def running(self) -> bool:
        return bool(self._aps.running)

    # ------------------------------------------------------------ event bus

    def register_handler(self, kind: str, handler: JobHandler) -> None:
        """Register the coroutine that runs when a job of this kind fires."""
        if kind in self._handlers:
            logger.warning(f"Job handler for kind '{kind}' replaced")
        self._handlers[kind] = handler

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> int:
        """Start the timers and rebuild them from SQLite. Returns the number
        of pending jobs rehydrated. Past-due jobs fire immediately (late)."""
        if not self._aps.running:
            self._aps.start()

        await self._purge_settled()

        async with self._factory()() as session:
            result = await session.execute(
                select(ScheduledJob.id, ScheduledJob.run_at)
                .where(ScheduledJob.status == "pending")
                .order_by(ScheduledJob.run_at)
            )
            pending = result.all()
        for job_id, run_at in pending:
            self._schedule_timer(job_id, run_at)
        if pending:
            logger.info(f"Scheduler rehydrated {len(pending)} pending job(s) from SQLite")
        return len(pending)

    async def shutdown(self) -> None:
        if self._aps.running:
            self._aps.shutdown(wait=False)

    # ------------------------------------------------------------ the API

    async def schedule_at(
        self,
        run_at: datetime,
        kind: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> str:
        """Persist a one-shot job and set its timer. Returns the job id.
        run_at may be aware or naive-UTC; a past run_at fires immediately.
        Unknown kinds are refused up front — a typo should fail at schedule
        time, not silently at 6pm."""
        if kind not in self._handlers:
            raise ValueError(f"No handler registered for job kind '{kind}'")
        run_at = to_naive_utc(run_at)

        row = ScheduledJob(kind=kind, payload=json.dumps(payload or {}, default=str), run_at=run_at)
        async with self._factory()() as session:
            session.add(row)
            await session.commit()
            job_id = row.id

        # Row before timer: if this add fails the job still exists and the
        # next startup rehydration will arm it.
        self._schedule_timer(job_id, run_at)
        return job_id

    async def cancel(self, job_id: str) -> bool:
        """Cancel a pending job. True only if this call settled it — a job
        already fired/failed/cancelled returns False (rowcount decides)."""
        async with self._factory()() as session:
            result = await session.execute(
                update(ScheduledJob)
                .where(ScheduledJob.id == job_id, ScheduledJob.status == "pending")
                .values(status="cancelled")
            )
            await session.commit()
        try:
            self._aps.remove_job(job_id)
        except JobLookupError:
            pass  # timer already gone (fired, or never armed in this process)
        return bool(result.rowcount)

    async def list_jobs(self, status: Optional[str] = None, limit: int = 50) -> list[dict]:
        """Jobs as plain dicts, soonest run_at first."""
        async with self._factory()() as session:
            query = select(ScheduledJob).order_by(ScheduledJob.run_at).limit(limit)
            if status is not None:
                query = query.where(ScheduledJob.status == status)
            result = await session.execute(query)
            rows = result.scalars().all()
        return [
            {
                "id": r.id,
                "kind": r.kind,
                "payload": _payload_dict(r.payload),
                "run_at": utc_iso(r.run_at),
                "status": r.status,
                "error": r.error,
                "created_at": utc_iso(r.created_at),
                "fired_at": utc_iso(r.fired_at),
            }
            for r in rows
        ]

    # ------------------------------------------------------------ internals

    def _schedule_timer(self, job_id: str, run_at: datetime) -> None:
        """Arm the in-process timer for a persisted row. misfire_grace_time
        None = a late wake-up (or a past run_at) still fires, never skips."""
        try:
            self._aps.add_job(
                self._fire,
                trigger=DateTrigger(run_date=run_at.replace(tzinfo=timezone.utc)),
                args=[job_id],
                id=job_id,
                replace_existing=True,
                misfire_grace_time=None,
                coalesce=True,
            )
        except Exception as e:
            logger.error(f"Timer for job {job_id} could not be armed (row kept; "
                         f"rehydrates on next startup): {e}")

    async def _fire(self, job_id: str) -> None:
        """Run one due job. Never raises — APScheduler must never see an
        exception from us, and one job's failure is its own."""
        try:
            async with self._factory()() as session:
                row = await session.get(ScheduledJob, job_id)
                if row is None:
                    return
                kind, payload_raw, run_at = row.kind, row.payload, row.run_at
                # The claim: only one path ever wins a pending row.
                result = await session.execute(
                    update(ScheduledJob)
                    .where(ScheduledJob.id == job_id, ScheduledJob.status == "pending")
                    .values(status="fired", fired_at=utc_now())
                )
                await session.commit()
            if result.rowcount == 0:
                return  # cancelled or already fired — not ours to run

            handler = self._handlers.get(kind)
            if handler is None:
                await self._mark_failed(job_id, f"no handler registered for kind '{kind}'")
                return
            try:
                payload = json.loads(payload_raw)
                if not isinstance(payload, dict):
                    raise ValueError("payload is not a JSON object")
            except (ValueError, TypeError) as e:
                await self._mark_failed(job_id, f"payload not valid JSON: {e}")
                return

            late = (utc_now() - run_at).total_seconds() > LATE_AFTER_SECONDS
            try:
                await handler(FiredJob(id=job_id, kind=kind, payload=payload,
                                       run_at=run_at, late=late))
            except Exception as e:
                logger.error(f"Job {job_id} (kind '{kind}') handler failed: {e}")
                await self._mark_failed(job_id, str(e)[:MAX_ERROR_LEN])
        except Exception as e:
            logger.error(f"Firing job {job_id} crashed outside the handler: {e}")

    async def _mark_failed(self, job_id: str, error: str) -> None:
        try:
            async with self._factory()() as session:
                await session.execute(
                    update(ScheduledJob)
                    .where(ScheduledJob.id == job_id)
                    .values(status="failed", error=error[:MAX_ERROR_LEN])
                )
                await session.commit()
        except Exception as e:
            logger.error(f"Could not record failure for job {job_id}: {e}")

    async def _purge_settled(self) -> None:
        """Drop settled rows older than the retention window (startup)."""
        cutoff = utc_now() - timedelta(days=SETTLED_RETENTION_DAYS)
        try:
            async with self._factory()() as session:
                result = await session.execute(
                    delete(ScheduledJob).where(
                        ScheduledJob.status != "pending",
                        or_(
                            ScheduledJob.fired_at <= cutoff,
                            (ScheduledJob.fired_at.is_(None)) & (ScheduledJob.created_at <= cutoff),
                        ),
                    )
                )
                await session.commit()
            if result.rowcount:
                logger.info(f"Purged {result.rowcount} settled scheduled job(s)")
        except Exception as e:
            logger.warning(f"Settled-job purge failed (non-critical): {e}")


def _payload_dict(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw  # opaque — return as stored


async def _push_job_handler(job: FiredJob) -> None:
    """Built-in kind 'push': deliver a Part 1 push event at run_at.
    payload = {"event_type": str, "payload": dict}. Best-effort like the
    channel itself — no window connected means no delivery, by design."""
    from app.core.push import push

    event_type = job.payload.get("event_type")
    if not isinstance(event_type, str) or not event_type:
        raise ValueError("push job payload needs a non-empty 'event_type' string")
    body = dict(job.payload.get("payload") or {})
    body.setdefault("scheduled_for", job.run_at.isoformat())
    body.setdefault("late", job.late)
    await push(event_type, body)


# The application-wide scheduler. Features register their kinds on this at
# import time; main.py starts/stops it in the lifespan.
scheduler = JarvisScheduler()


def register_job_handler(kind: str, handler: JobHandler) -> None:
    """Module-level convenience for features: register on the app scheduler."""
    scheduler.register_handler(kind, handler)
