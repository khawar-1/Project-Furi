"""
Phase 10 Part 2 — scheduled routines (app/core/scheduled_routines.py).

The test_reindex.py / test_birthdays.py shape: a real JarvisScheduler on a
shared in-memory DB with the app-wide `scheduler` singleton swapped, the
"routine" handler registered on it, and start_task / planner_memory_context
stubbed so firing a job exercises the guards + re-arm without invoking a real
planner.

The load-bearing safety property (a scheduled run re-derives the plan through
the approval gate) is covered structurally by the routines suite (start_task
from a goal STRING); here we verify the SCHEDULING machinery.
"""
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.routines import create_routine
from app.core.scheduled_routines import (
    ROUTINE_JOB_KIND,
    describe_schedule,
    ensure_routine_schedule_jobs,
    next_routine_run_at,
    normalize_schedule_spec,
    set_routine_schedule,
    sync_routine_schedule_job,
)
from app.core.scheduler import JarvisScheduler, to_naive_utc, utc_now
from app.db.database import Base
from app.db.models import Routine


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
    import app.core.scheduled_routines as sr
    import app.core.scheduler as scheduler_module

    monkeypatch.setattr("app.db.database.AsyncSessionLocal", factory)
    monkeypatch.setattr(sr, "scheduler", sched)
    monkeypatch.setattr(scheduler_module, "scheduler", sched)
    sched.register_handler(ROUTINE_JOB_KIND, sr._routine_job_handler)


@pytest.fixture
def started(monkeypatch):
    """Stub the background-task launch so a fired routine records its goal
    instead of invoking a real planner."""
    calls = []

    async def fake_start_task(db, goal, session_id, conversation="", memory="", provider=None):
        calls.append(goal)
        return None

    async def fake_pmc(db, goal, qdrant=None):
        return ""

    monkeypatch.setattr("app.agents.start_task", fake_start_task)
    monkeypatch.setattr("app.agents.planner_memory_context", fake_pmc)
    return calls


# ------------------------------------------------------------------- helpers

def _local(naive_utc: datetime) -> datetime:
    """A stored naive-UTC run_at → machine-local aware time (for wall-clock asserts)."""
    return naive_utc.replace(tzinfo=timezone.utc).astimezone()


async def _weekly(factory, weekday=4, hour=16, minute=0):
    async with factory() as db:
        r = await create_routine(db, "compile week", "compile the week")
        await set_routine_schedule(db, r.id, {
            "schedule_type": "weekly", "schedule_weekday": weekday,
            "schedule_hour": hour, "schedule_minute": minute,
        })
        return r.id


async def _pending(sched):
    return await sched.list_jobs(status="pending", kind=ROUTINE_JOB_KIND, limit=100)


async def _get(factory, routine_id):
    async with factory() as db:
        return await db.get(Routine, routine_id)


# ============================================================ occurrence math

def test_next_run_none_when_unscheduled():
    r = Routine(name="x", normalized_name="x", goal_template="g", schedule_type=None)
    assert next_routine_run_at(r) is None


def test_next_run_interval():
    now = datetime(2026, 7, 16, 10, 0)
    r = Routine(name="x", normalized_name="x", goal_template="g",
                schedule_type="interval", schedule_interval_minutes=30)
    assert next_routine_run_at(r, now) == to_naive_utc((now + timedelta(minutes=30)).astimezone())


def test_next_run_daily_wallclock():
    now = datetime(2026, 7, 16, 7, 0)  # before 08:00
    r = Routine(name="x", normalized_name="x", goal_template="g",
                schedule_type="daily", schedule_hour=8, schedule_minute=0)
    run = next_routine_run_at(now=now, routine=r)
    local = _local(run)
    assert local.hour == 8 and local.minute == 0
    assert local.replace(tzinfo=None) > now


def test_next_run_daily_rolls_to_tomorrow():
    now = datetime(2026, 7, 16, 9, 0)  # past 08:00
    r = Routine(name="x", normalized_name="x", goal_template="g",
                schedule_type="daily", schedule_hour=8, schedule_minute=0)
    local = _local(next_routine_run_at(now=now, routine=r))
    assert local.replace(tzinfo=None) > now
    assert local.hour == 8


def test_next_run_weekly_wallclock():
    now = datetime(2026, 7, 16, 10, 0)  # a Thursday
    r = Routine(name="x", normalized_name="x", goal_template="g",
                schedule_type="weekly", schedule_weekday=4, schedule_hour=16, schedule_minute=0)
    local = _local(next_routine_run_at(now=now, routine=r))
    assert local.weekday() == 4 and local.hour == 16
    assert local.replace(tzinfo=None) > now


# ============================================================ normalize spec

def test_normalize_clear():
    assert normalize_schedule_spec(None)["schedule_type"] is None
    assert normalize_schedule_spec({"schedule_type": ""})["schedule_type"] is None


def test_normalize_weekly():
    out = normalize_schedule_spec({
        "schedule_type": "weekly", "schedule_weekday": 4,
        "schedule_hour": 16, "schedule_minute": 30,
    })
    assert out == {
        "schedule_type": "weekly", "schedule_weekday": 4, "schedule_hour": 16,
        "schedule_minute": 30, "schedule_interval_minutes": None,
    }


def test_normalize_interval_clamped():
    out = normalize_schedule_spec({"schedule_type": "interval", "schedule_interval_minutes": 2})
    assert out["schedule_interval_minutes"] == 5  # clamped to the floor


def test_normalize_invalid_type_raises():
    with pytest.raises(ValueError):
        normalize_schedule_spec({"schedule_type": "hourly"})


def test_normalize_clamps_out_of_range():
    # Numerics clamp (never crash), only an invalid schedule_type raises.
    out = normalize_schedule_spec({"schedule_type": "daily", "schedule_hour": 30})
    assert out["schedule_hour"] == 23


def test_describe_schedule():
    r = Routine(name="x", normalized_name="x", goal_template="g",
                schedule_type="weekly", schedule_weekday=4, schedule_hour=16, schedule_minute=0)
    assert describe_schedule(r) == "every Friday at 4:00 PM"
    r.schedule_type = "interval"
    r.schedule_interval_minutes = 120
    assert describe_schedule(r) == "every 2 hours"


# ========================================================= set / sync / arm

async def test_set_schedule_arms_job(factory, sched):
    rid = await _weekly(factory)
    jobs = await _pending(sched)
    assert len(jobs) == 1
    r = await _get(factory, rid)
    assert r.schedule_type == "weekly" and r.schedule_job_id == jobs[0]["id"]


async def test_clear_schedule_cancels_job(factory, sched):
    rid = await _weekly(factory)
    async with factory() as db:
        await set_routine_schedule(db, rid, {"schedule_type": None})
    assert await _pending(sched) == []
    assert (await _get(factory, rid)).schedule_job_id is None


async def test_sync_replaces_existing(factory, sched):
    rid = await _weekly(factory)
    first = (await _get(factory, rid)).schedule_job_id
    async with factory() as db:
        r = await db.get(Routine, rid)
        await sync_routine_schedule_job(db, r)
    jobs = await _pending(sched)
    assert len(jobs) == 1 and jobs[0]["id"] != first


# =================================================================== fire

async def test_fire_runs_and_rearms(factory, sched, started):
    rid = await _weekly(factory)
    job_id = (await _pending(sched))[0]["id"]

    await sched._fire(job_id)

    assert started == ["compile the week"]  # start_task called with the goal STRING
    jobs = await _pending(sched)
    assert len(jobs) == 1 and jobs[0]["id"] != job_id
    assert (await _get(factory, rid)).schedule_job_id == jobs[0]["id"]


async def test_stale_job_no_rearm(factory, sched, started):
    rid = await _weekly(factory)
    pointer = (await _get(factory, rid)).schedule_job_id
    stale = await sched.schedule_at(utc_now(), ROUTINE_JOB_KIND, {"routine_id": rid})

    await sched._fire(stale)  # guard 2: not the routine's pointer → no run, no re-arm

    assert started == []
    assert (await _get(factory, rid)).schedule_job_id == pointer


async def test_inactive_routine_no_rearm(factory, sched, started):
    rid = await _weekly(factory)
    job_id = (await _pending(sched))[0]["id"]
    async with factory() as db:
        r = await db.get(Routine, rid)
        r.is_active = False
        await db.commit()

    await sched._fire(job_id)  # guard 1: inactive → no run, no re-arm

    assert started == []
    assert await _pending(sched) == []


# =========================================================== ensure reconcile

async def test_ensure_arms_missing(factory, sched):
    # A scheduled routine whose job was never armed (fields set directly).
    async with factory() as db:
        r = await create_routine(db, "digest", "make the digest")
        r.schedule_type = "daily"
        r.schedule_hour = 8
        r.schedule_minute = 0
        await db.commit()

    await ensure_routine_schedule_jobs()

    jobs = await _pending(sched)
    assert len(jobs) == 1


async def test_ensure_sweeps_strays(factory, sched):
    rid = await _weekly(factory)
    pointer = (await _get(factory, rid)).schedule_job_id
    stray = await sched.schedule_at(utc_now(), ROUTINE_JOB_KIND, {"routine_id": rid})

    await ensure_routine_schedule_jobs()

    ids = [j["id"] for j in await _pending(sched)]
    assert pointer in ids and stray not in ids
