"""
Furi OS — Incremental reindex scheduler (Phase 6, Part 3)

Keeps the Part 2 semantic file index fresh on a cadence, without the user ever
pressing "index now". A recurring "reindex" scheduler job re-runs the INCREMENTAL
indexing pass (file_index.run_index — unchanged files are skipped by size+mtime)
every `interval_minutes`, then re-arms itself for the next interval.

This is the app/core/daily_briefing.py singleton pattern verbatim, with two
differences:
- The cadence is an INTERVAL, not a wall-clock time, so next_reindex_run_at is
  just `now + interval` (no local-timezone dance).
- The singleton's config AND its current job-id pointer already exist from
  Part 2 (FileIndexConfig + get/set_file_index_job_id, key "file_index.job_id")
  — reused as-is, no new app_settings code.

SQLite is the truth: a reindex due while the backend slept fires late-but-fires
(the pass is idempotent, so a late run is harmless). Guards mirror the briefing
handler — a disabled-now or stale job no-ops WITHOUT re-arming.
"""
import asyncio
from datetime import datetime, timedelta
from typing import Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.app_settings import (
    get_file_index_config,
    get_file_index_job_id,
    set_file_index_job_id,
)
from app.core.scheduler import FiredJob, register_job_handler, scheduler, utc_now

REINDEX_JOB_KIND = "reindex"


# --------------------------------------------------------- occurrence math

def next_reindex_run_at(
    interval_minutes: int, now: Optional[datetime] = None
) -> datetime:
    """The next run, `interval_minutes` after `now`, as naive UTC (what
    schedule_at stores). A pure interval — no wall-clock/timezone conversion,
    unlike next_briefing_run_at. `now` defaults to the current naive-UTC time."""
    base = now if now is not None else utc_now()
    return base + timedelta(minutes=max(1, int(interval_minutes)))


# ------------------------------------------------------------- the choke point

async def sync_reindex_job(db: AsyncSession) -> None:
    """Cancel the current reindex job (if any) and, iff the file index is
    enabled, arm the next occurrence and store its id. The ONE function every
    hook calls (config change, re-arm after fire, startup reconcile).
    Best-effort so a scheduler hiccup never breaks a config save."""
    try:
        job_id = await get_file_index_job_id(db)
        if job_id:
            await scheduler.cancel(job_id)
            await set_file_index_job_id(db, None)

        config = await get_file_index_config(db)
        if config.enabled:
            run_at = next_reindex_run_at(config.interval_minutes)
            new_id = await scheduler.schedule_at(
                run_at, REINDEX_JOB_KIND,
                {"interval_minutes": config.interval_minutes},
            )
            await set_file_index_job_id(db, new_id)
    except Exception as e:  # best-effort — never break a config save
        logger.warning(f"Reindex job sync failed (non-critical): {e}")


# ------------------------------------------------------------------- firing

async def _reindex_job_handler(job: FiredJob) -> None:
    """Runs once the scheduler has won the fire-vs-cancel race. Guards reject a
    disabled/superseded job (never re-arming), then runs one incremental index
    pass and RE-ARMS the next interval. Handler failures land on the job row per
    the scheduler contract — never propagate."""
    from app.core.file_index import run_index
    from app.db.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        config = await get_file_index_config(db)
        # Guard 1: turned off between scheduling and firing — do not re-arm.
        if not config.enabled:
            return
        # Guard 2: a stale job (not the current pointer) never fires or forks a
        # second recurrence chain.
        pointer = await get_file_index_job_id(db)
        if pointer != job.id:
            return

        try:
            # run_index opens its OWN session + resolves qdrant (a missing vector
            # store is a safe no-op); it is the incremental pass — unchanged files
            # are skipped by size+mtime.
            await run_index(full=False)

            # Phase 6 Part 4 — the same cadence backfills newly-written messages
            # (task/reminder/briefing turns the write-path hook doesn't cover, plus
            # any the hook missed). Incremental (embedded_at cursor), best-effort.
            from app.core.conversation_index import run_conversation_index
            await run_conversation_index(full=False)
        except asyncio.CancelledError:
            # A backend shutdown cancels the in-flight pass mid-file (the heavy
            # extract runs in a thread). That is expected teardown, not a crash —
            # log it cleanly instead of letting a scary CancelledError traceback
            # surface through APScheduler (2026-07-20). Skip the re-arm; startup's
            # ensure_reindex_job re-arms next boot. Swallow (do not re-raise): the
            # only thing cancelling a running pass is shutdown.
            logger.info("reindex pass cancelled (backend shutting down)")
            return

        # Recurrence: arm the next interval (cancels this fired job — a harmless
        # no-op — and stores the new id).
        await sync_reindex_job(db)


# --------------------------------------------------- startup reconciliation

async def ensure_reindex_job() -> None:
    """Self-healing startup pass (mirrors ensure_briefing_job): sweep stray
    reindex jobs, and — if the index is enabled — arm the job when none is live.
    Heals a crash between fire and re-arm. Best-effort; runs after
    scheduler.start()."""
    from app.db.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        config = await get_file_index_config(db)
        pointer = await get_file_index_job_id(db)
        pending = await scheduler.list_jobs(
            status="pending", kind=REINDEX_JOB_KIND, limit=10_000
        )

        # Sweep every pending job that is not the current pointer (strays /
        # duplicates from a crashed re-arm).
        swept = 0
        pointer_is_live = False
        for j in pending:
            if j["id"] == pointer:
                pointer_is_live = True
            else:
                await scheduler.cancel(j["id"])
                swept += 1

        if not config.enabled:
            # Disabled: make sure nothing is armed.
            if pointer:
                await scheduler.cancel(pointer)
                await set_file_index_job_id(db, None)
            logger.info(f"Reindex disabled ({swept} stray job(s) swept)")
            return

        if not pointer_is_live:
            await sync_reindex_job(db)
            logger.info(f"Reindex job armed ({swept} stray job(s) swept)")
        else:
            logger.info(f"Reindex job already live ({swept} stray job(s) swept)")


def register() -> None:
    """Register the 'reindex' job handler at import time, mirroring how
    app.core.reminders / app.core.birthdays / app.core.daily_briefing
    self-register."""
    register_job_handler(REINDEX_JOB_KIND, _reindex_job_handler)


register()
