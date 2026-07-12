"""
Phase 6 Part 3 — incremental reindex scheduler (app/core/reindex.py).

The test_birthdays.py shape: a real JarvisScheduler on a shared in-memory DB,
with the app-wide `scheduler` singleton swapped so app.core.reindex (which
imports it directly) uses the test one, and the handler's own AsyncSessionLocal
pointed at the same engine. run_index is a safe no-op here (no Qdrant is
initialized in unit tests → it returns early), so firing a job exercises the
guards + re-arm without touching the filesystem or a vector store.

Covers: occurrence math (now + interval), sync arm/no-arm/replace, fire →
re-arm, the stale-job and disabled-now no-ops, and ensure_reindex_job
arm/sweep/cancel.
"""
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.app_settings import (
    FileIndexConfig,
    get_file_index_job_id,
    set_file_index_config,
)
from app.core.reindex import (
    REINDEX_JOB_KIND,
    ensure_reindex_job,
    next_reindex_run_at,
    sync_reindex_job,
)
from app.core.scheduler import JarvisScheduler, utc_now
from app.db.database import Base


# ================================================================= fixtures

@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def sched(factory):
    s = JarvisScheduler(session_factory=factory)
    yield s
    await s.shutdown()


@pytest.fixture(autouse=True)
def _wire(factory, sched, monkeypatch):
    """Point the handler's AsyncSessionLocal and the app-wide `scheduler`
    singleton (app.core.reindex imports it directly) at this test's engine,
    and give the fresh scheduler the reindex handler."""
    import app.core.reindex as reindex
    import app.core.scheduler as scheduler_module

    monkeypatch.setattr("app.db.database.AsyncSessionLocal", factory)
    monkeypatch.setattr(reindex, "scheduler", sched)
    monkeypatch.setattr(scheduler_module, "scheduler", sched)
    sched.register_handler(REINDEX_JOB_KIND, reindex._reindex_job_handler)


# ------------------------------------------------------------------- helpers

async def _set_config(factory, *, enabled=True, interval=360):
    async with factory() as db:
        await set_file_index_config(db, FileIndexConfig(
            enabled=enabled, folders=(r"C:\Users\me\Docs",),
            exclusions=(), interval_minutes=interval,
        ))


async def _pointer(factory):
    async with factory() as db:
        return await get_file_index_job_id(db)


async def _sync(factory):
    async with factory() as db:
        await sync_reindex_job(db)


async def _pending(sched):
    return await sched.list_jobs(status="pending", kind=REINDEX_JOB_KIND, limit=100)


# ============================================================ occurrence math

def test_next_reindex_run_at_is_now_plus_interval():
    now = datetime(2026, 7, 12, 10, 0)
    assert next_reindex_run_at(360, now) == now + timedelta(minutes=360)


def test_next_reindex_run_at_defaults_to_now():
    before = utc_now()
    run = next_reindex_run_at(30)
    delta = run - before
    assert timedelta(minutes=29) <= delta <= timedelta(minutes=31)


def test_next_reindex_run_at_floors_interval():
    now = datetime(2026, 7, 12, 10, 0)
    # A zero/negative interval is clamped to at least 1 minute.
    assert next_reindex_run_at(0, now) == now + timedelta(minutes=1)


# =================================================================== sync

async def test_sync_arms_when_enabled(factory, sched):
    await _set_config(factory, enabled=True, interval=360)
    await _sync(factory)
    jobs = await _pending(sched)
    assert len(jobs) == 1
    assert await _pointer(factory) == jobs[0]["id"]
    assert jobs[0]["payload"]["interval_minutes"] == 360


async def test_sync_no_arm_when_disabled(factory, sched):
    await _set_config(factory, enabled=False)
    await _sync(factory)
    assert await _pending(sched) == []
    assert await _pointer(factory) is None


async def test_sync_cancels_and_replaces_existing(factory, sched):
    await _set_config(factory, enabled=True)
    await _sync(factory)
    first = await _pointer(factory)
    await _sync(factory)
    jobs = await _pending(sched)
    assert len(jobs) == 1
    second = await _pointer(factory)
    assert second and second != first
    assert jobs[0]["id"] == second
    cancelled = await sched.list_jobs(status="cancelled", kind=REINDEX_JOB_KIND, limit=100)
    assert first in [j["id"] for j in cancelled]


# =================================================================== fire

async def test_fire_reindexes_and_rearms(factory, sched):
    await _set_config(factory, enabled=True, interval=360)
    await _sync(factory)
    job_id = (await _pending(sched))[0]["id"]

    await sched._fire(job_id)  # run_index is a no-op (no Qdrant) → just re-arm

    jobs = await _pending(sched)
    assert len(jobs) == 1
    assert jobs[0]["id"] != job_id
    assert await _pointer(factory) == jobs[0]["id"]


async def test_stale_job_does_not_rearm(factory, sched):
    await _set_config(factory, enabled=True)
    await _sync(factory)
    pointer = await _pointer(factory)

    # A stray job of the same kind that is NOT the current pointer.
    stale = await sched.schedule_at(
        utc_now(), REINDEX_JOB_KIND, {"interval_minutes": 360}
    )
    await sched._fire(stale)  # guard 2: pointer != job.id → return, no re-arm

    assert await _pointer(factory) == pointer  # unchanged
    pending_ids = [j["id"] for j in await _pending(sched)]
    assert pointer in pending_ids
    assert stale not in pending_ids  # it fired, no new job spawned


async def test_disabled_now_guard(factory, sched):
    await _set_config(factory, enabled=True)
    await _sync(factory)
    job_id = (await _pending(sched))[0]["id"]

    # Turned off between scheduling and firing (config only — no sync/cancel).
    await _set_config(factory, enabled=False)
    await sched._fire(job_id)  # guard 1: not enabled → return, no re-arm

    assert await _pending(sched) == []


# =========================================================== ensure_reindex_job

async def test_ensure_arms_when_enabled_and_none_live(factory, sched):
    await _set_config(factory, enabled=True)
    await ensure_reindex_job()
    jobs = await _pending(sched)
    assert len(jobs) == 1
    assert await _pointer(factory) == jobs[0]["id"]


async def test_ensure_sweeps_strays(factory, sched):
    await _set_config(factory, enabled=True)
    await _sync(factory)
    pointer = await _pointer(factory)
    s1 = await sched.schedule_at(utc_now(), REINDEX_JOB_KIND, {"interval_minutes": 360})
    s2 = await sched.schedule_at(utc_now(), REINDEX_JOB_KIND, {"interval_minutes": 360})

    await ensure_reindex_job()

    pending_ids = [j["id"] for j in await _pending(sched)]
    assert pending_ids == [pointer]
    assert s1 not in pending_ids and s2 not in pending_ids


async def test_ensure_disabled_cancels_pointer(factory, sched):
    await _set_config(factory, enabled=True)
    await _sync(factory)
    assert await _pointer(factory) is not None

    await _set_config(factory, enabled=False)
    await ensure_reindex_job()

    assert await _pointer(factory) is None
    assert await _pending(sched) == []
