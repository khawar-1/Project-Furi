"""
Phase 4 Part 2 — Scheduler / event bus.

Unit tests drive JarvisScheduler instances directly (schedule/claim/cancel
races, handler failure isolation, rehydration, the built-in push bridge);
API tests run the /api/schedule router through TestClient with the app
scheduler pointed at the test database. Real-timer tests use short delays
with generous wait deadlines so they can't flake on a slow machine.
"""
import asyncio
import json
from datetime import timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.testclient import TestClient

from app.core.push import push_manager
from app.core.scheduler import JarvisScheduler, ScheduledJob
from app.db.database import Base
from app.db.models import utc_now
from main import app
from tests.test_push_channel import FakeSocket


@pytest_asyncio.fixture
async def job_factory():
    """Session factory over ONE shared in-memory connection — the scheduler
    opens its own sessions per fire, so every session must see one database."""
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
async def sched(job_factory):
    s = JarvisScheduler(session_factory=job_factory)
    yield s
    await s.shutdown()


async def get_row(job_factory, job_id: str) -> ScheduledJob:
    async with job_factory() as session:
        return await session.get(ScheduledJob, job_id)


async def insert_row(job_factory, **kwargs) -> str:
    row = ScheduledJob(**{"payload": "{}", **kwargs})
    async with job_factory() as session:
        session.add(row)
        await session.commit()
        return row.id


# ================================================================ scheduling

async def test_schedule_at_persists_pending_row(sched, job_factory):
    fired = []
    sched.register_handler("t", lambda job: fired.append(job))  # never runs here

    job_id = await sched.schedule_at(utc_now() + timedelta(hours=1), "t", {"a": 1})

    row = await get_row(job_factory, job_id)
    assert row.status == "pending"
    assert row.kind == "t"
    assert json.loads(row.payload) == {"a": 1}
    assert row.run_at.tzinfo is None  # stored naive UTC
    assert not fired


async def test_schedule_unknown_kind_is_refused_up_front(sched, job_factory):
    with pytest.raises(ValueError, match="ghost"):
        await sched.schedule_at(utc_now(), "ghost", {})
    async with job_factory() as session:
        result = await session.execute(select(ScheduledJob))
        assert result.scalars().all() == []  # refused means no row either


async def test_aware_run_at_is_normalized_to_utc(sched, job_factory):
    sched.register_handler("t", _noop)
    plus_five = timezone(timedelta(hours=5))
    aware = (utc_now() + timedelta(hours=2)).replace(tzinfo=plus_five)

    job_id = await sched.schedule_at(aware, "t")

    row = await get_row(job_factory, job_id)
    assert row.run_at == aware.astimezone(timezone.utc).replace(tzinfo=None)


# ==================================================================== firing

async def _noop(job) -> None:
    pass


async def test_fire_runs_handler_and_marks_fired(sched, job_factory):
    received = []
    sched.register_handler("t", lambda job: _record(received, job))
    job_id = await sched.schedule_at(utc_now(), "t", {"who": "jamil"})

    await sched._fire(job_id)

    assert len(received) == 1
    assert received[0].payload == {"who": "jamil"}
    assert received[0].late is False
    row = await get_row(job_factory, job_id)
    assert row.status == "fired"
    assert row.fired_at is not None


async def _record(bucket, job) -> None:
    bucket.append(job)


async def test_handler_failure_is_recorded_and_isolated(sched, job_factory):
    async def bad(job):
        raise RuntimeError("handler exploded")

    received = []
    sched.register_handler("bad", bad)
    sched.register_handler("good", lambda job: _record(received, job))
    bad_id = await sched.schedule_at(utc_now(), "bad")
    good_id = await sched.schedule_at(utc_now(), "good")

    await sched._fire(bad_id)
    await sched._fire(good_id)  # one bad job never takes the bus down

    bad_row = await get_row(job_factory, bad_id)
    assert bad_row.status == "failed"
    assert "handler exploded" in bad_row.error
    assert (await get_row(job_factory, good_id)).status == "fired"
    assert len(received) == 1


async def test_fire_without_handler_marks_failed(sched, job_factory):
    job_id = await insert_row(job_factory, kind="ghost", run_at=utc_now())

    await sched._fire(job_id)

    row = await get_row(job_factory, job_id)
    assert row.status == "failed"
    assert "no handler" in row.error


async def test_fire_is_claimed_exactly_once(sched, job_factory):
    received = []
    sched.register_handler("t", lambda job: _record(received, job))
    job_id = await sched.schedule_at(utc_now(), "t")

    await sched._fire(job_id)
    await sched._fire(job_id)  # second fire loses the claim

    assert len(received) == 1


async def test_late_flag_set_when_fired_long_after_run_at(sched, job_factory):
    received = []
    sched.register_handler("t", lambda job: _record(received, job))
    job_id = await insert_row(
        job_factory, kind="t", run_at=utc_now() - timedelta(minutes=10)
    )

    await sched._fire(job_id)

    assert received[0].late is True


# ================================================================== cancel

async def test_cancel_pending_job_wins_and_fire_is_noop(sched, job_factory):
    received = []
    sched.register_handler("t", lambda job: _record(received, job))
    job_id = await sched.schedule_at(utc_now() + timedelta(hours=1), "t")

    assert await sched.cancel(job_id) is True
    await sched._fire(job_id)  # a late timer must find nothing to run

    assert received == []
    assert (await get_row(job_factory, job_id)).status == "cancelled"
    assert await sched.cancel(job_id) is False  # one settle per job


async def test_cancel_after_fire_returns_false(sched, job_factory):
    sched.register_handler("t", _noop)
    job_id = await sched.schedule_at(utc_now(), "t")
    await sched._fire(job_id)

    assert await sched.cancel(job_id) is False
    assert (await get_row(job_factory, job_id)).status == "fired"


# ======================================================= timers + rehydration

async def test_real_timer_fires_the_job(sched, job_factory):
    done = asyncio.Event()
    sched.register_handler("t", lambda job: _set(done))
    await sched.start()

    job_id = await sched.schedule_at(utc_now() + timedelta(seconds=0.05), "t")

    await asyncio.wait_for(done.wait(), timeout=5)
    await _wait_for_status(job_factory, job_id, "fired")


async def _set(event: asyncio.Event) -> None:
    event.set()


async def _wait_for_status(job_factory, job_id, expected, timeout=5.0):
    """The row update lands right after the handler — poll briefly."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        row = await get_row(job_factory, job_id)
        if row.status == expected:
            return
        await asyncio.sleep(0.02)
    assert row.status == expected


async def test_rehydration_fires_past_due_job_late(sched, job_factory):
    received = []
    done = asyncio.Event()

    async def handler(job):
        received.append(job)
        done.set()

    sched.register_handler("t", handler)
    await insert_row(job_factory, kind="t", run_at=utc_now() - timedelta(hours=3))

    rehydrated = await sched.start()

    assert rehydrated == 1
    await asyncio.wait_for(done.wait(), timeout=5)
    assert received[0].late is True  # missed while "down" — fired anyway


async def test_rehydration_arms_future_job(sched, job_factory):
    done = asyncio.Event()
    sched.register_handler("t", lambda job: _set(done))
    await insert_row(job_factory, kind="t", run_at=utc_now() + timedelta(seconds=0.1))

    rehydrated = await sched.start()

    assert rehydrated == 1
    await asyncio.wait_for(done.wait(), timeout=5)


async def test_startup_purges_old_settled_rows_only(sched, job_factory):
    old = utc_now() - timedelta(days=40)
    purged_id = await insert_row(
        job_factory, kind="t", run_at=old, status="fired", fired_at=old, created_at=old
    )
    old_pending_id = await insert_row(
        job_factory, kind="t", run_at=utc_now() + timedelta(days=1), created_at=old
    )
    fresh_fired_id = await insert_row(
        job_factory, kind="t", run_at=old, status="fired", fired_at=utc_now()
    )
    sched.register_handler("t", _noop)

    await sched.start()

    assert await get_row(job_factory, purged_id) is None
    assert (await get_row(job_factory, old_pending_id)).status == "pending"  # pending NEVER purged
    assert (await get_row(job_factory, fresh_fired_id)).status == "fired"


# ============================================================ the push bridge

@pytest.fixture(autouse=True)
def _clean_push_manager():
    push_manager._connections.clear()
    yield
    push_manager._connections.clear()


async def test_push_kind_delivers_event_over_the_channel(sched, job_factory):
    sock = FakeSocket()
    await push_manager.connect(sock)
    job_id = await sched.schedule_at(
        utc_now(), "push", {"event_type": "reminder", "payload": {"text": "call Jamil"}}
    )

    await sched._fire(job_id)

    assert len(sock.sent) == 1
    event = sock.sent[0]
    assert event["type"] == "reminder"
    assert event["payload"]["text"] == "call Jamil"
    assert event["payload"]["late"] is False
    assert "scheduled_for" in event["payload"]
    assert (await get_row(job_factory, job_id)).status == "fired"


async def test_push_kind_without_event_type_fails_the_job(sched, job_factory):
    job_id = await sched.schedule_at(utc_now(), "push", {"payload": {"x": 1}})

    await sched._fire(job_id)

    row = await get_row(job_factory, job_id)
    assert row.status == "failed"
    assert "event_type" in row.error


async def test_push_kind_with_no_window_connected_still_settles(sched, job_factory):
    """Best-effort by design: nothing connected is not a failure."""
    job_id = await sched.schedule_at(utc_now(), "push", {"event_type": "reminder"})

    await sched._fire(job_id)

    assert (await get_row(job_factory, job_id)).status == "fired"


# ================================================================== API tests

@pytest.fixture
def api_client(job_factory):
    """TestClient with the app-wide scheduler pointed at the test database.
    No lifespan — APScheduler queues timers as pending jobs, which is all
    these routing tests need."""
    from app.core.scheduler import scheduler as app_scheduler

    original = app_scheduler._session_factory
    app_scheduler._session_factory = job_factory
    yield TestClient(app)
    app_scheduler._session_factory = original
    try:
        app_scheduler._aps.remove_all_jobs()
    except Exception:
        pass


def test_schedule_test_endpoint_creates_pending_push_job(api_client):
    response = api_client.post(
        "/api/schedule/test",
        json={"delay_seconds": 60, "event_type": "demo", "payload": {"a": 1}},
    )
    assert response.status_code == 200
    job_id = response.json()["job_id"]

    listed = api_client.get("/api/schedule", params={"status": "pending"}).json()
    assert [j["id"] for j in listed] == [job_id]
    assert listed[0]["kind"] == "push"
    assert listed[0]["payload"] == {"event_type": "demo", "payload": {"a": 1}}


def test_cancel_endpoint_settles_job_exactly_once(api_client):
    job_id = api_client.post("/api/schedule/test", json={"delay_seconds": 60}).json()["job_id"]

    assert api_client.delete(f"/api/schedule/{job_id}").json() == {"cancelled": True}
    assert api_client.delete(f"/api/schedule/{job_id}").json() == {"cancelled": False}

    listed = api_client.get("/api/schedule", params={"status": "cancelled"}).json()
    assert [j["id"] for j in listed] == [job_id]


def test_list_endpoint_rejects_bogus_status(api_client):
    assert api_client.get("/api/schedule", params={"status": "sideways"}).status_code == 422
