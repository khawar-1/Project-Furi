"""
Phase 4 Part 4 — reminders business logic (app/core/reminders.py).

Uses a real JarvisScheduler instance (same pattern as test_scheduler.py) so
create/cancel/fire exercise the actual scheduled_jobs row, not a mock.
Reminder rows live on the SAME shared in-memory database as the scheduler's
own tables, since a reminder fire opens its own session via
app.db.database.AsyncSessionLocal — so these tests patch AsyncSessionLocal
to point at the same engine.
"""
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.push import push_manager
from app.core.scheduler import JarvisScheduler
from app.db.database import Base
from app.db.models import Message, Reminder, utc_now
from tests.test_push_channel import FakeSocket


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
def _wire_reminders(factory, sched, monkeypatch):
    """Point both the reminder handler's own DB access (AsyncSessionLocal)
    and the app-wide `scheduler` singleton at this test's isolated engine —
    app.core.reminders imports `scheduler` directly, so the module-level
    object must be swapped, not just a local variable."""
    import app.core.reminders as reminders_module
    import app.core.scheduler as scheduler_module

    monkeypatch.setattr("app.db.database.AsyncSessionLocal", factory)
    monkeypatch.setattr(reminders_module, "scheduler", sched)
    monkeypatch.setattr(scheduler_module, "scheduler", sched)
    # The app-wide scheduler got the "reminder" handler at import time
    # (app.core.reminders.register()); this fresh test instance needs it too.
    sched.register_handler(reminders_module.REMINDER_JOB_KIND, reminders_module._reminder_job_handler)
    push_manager._connections.clear()
    yield
    push_manager._connections.clear()


async def get_reminder(factory, reminder_id: str) -> Reminder:
    async with factory() as session:
        return await session.get(Reminder, reminder_id)


# ================================================================= creation

async def test_create_reminder_basic(factory):
    from app.core.reminders import create_reminder
    from app.core.scheduler import ScheduledJob

    due = utc_now() + timedelta(hours=1)
    async with factory() as db:
        reminder = await create_reminder(db, "call jamil", due, session_id="s1")

    assert reminder.text == "call jamil"
    assert reminder.session_id == "s1"
    assert reminder.status == "pending"
    assert reminder.job_id is not None

    async with factory() as db:
        job = await db.get(ScheduledJob, reminder.job_id)
    assert job is not None
    assert job.kind == "reminder"
    assert job.status == "pending"


# =================================================================== cancel

async def test_cancel_pending_reminder_wins(factory, sched):
    from app.core.reminders import cancel_reminder, create_reminder

    due = utc_now() + timedelta(hours=1)
    async with factory() as db:
        reminder = await create_reminder(db, "call jamil", due, session_id="s1")

    async with factory() as db:
        assert await cancel_reminder(db, reminder.id) is True

    row = await get_reminder(factory, reminder.id)
    assert row.status == "cancelled"

    # A cancelled job's late timer must find nothing to run.
    await sched._fire(reminder.job_id)
    row_after = await get_reminder(factory, reminder.id)
    assert row_after.status == "cancelled"  # never flipped to fired


async def test_cancel_settles_exactly_once(factory):
    from app.core.reminders import cancel_reminder, create_reminder

    due = utc_now() + timedelta(hours=1)
    async with factory() as db:
        reminder = await create_reminder(db, "call jamil", due, session_id="s1")

    async with factory() as db:
        assert await cancel_reminder(db, reminder.id) is True
    async with factory() as db:
        assert await cancel_reminder(db, reminder.id) is False


async def test_cancel_unknown_reminder_returns_false(factory):
    from app.core.reminders import cancel_reminder

    async with factory() as db:
        assert await cancel_reminder(db, "nonexistent") is False


async def test_cancel_after_fire_returns_false(factory, sched):
    from app.core.reminders import cancel_reminder, create_reminder

    due = utc_now() + timedelta(seconds=0)
    async with factory() as db:
        reminder = await create_reminder(db, "call jamil", due, session_id="s1")

    await sched._fire(reminder.job_id)

    async with factory() as db:
        assert await cancel_reminder(db, reminder.id) is False
    row = await get_reminder(factory, reminder.id)
    assert row.status == "fired"


# ==================================================================== firing

async def test_fire_pushes_event_and_persists_chat_message(factory, sched):
    from app.core.reminders import create_reminder

    sock = FakeSocket()
    await push_manager.connect(sock)

    due = utc_now() + timedelta(seconds=0)
    async with factory() as db:
        reminder = await create_reminder(db, "call jamil", due, session_id="s-chat")

    await sched._fire(reminder.job_id)

    row = await get_reminder(factory, reminder.id)
    assert row.status == "fired"
    assert row.fired_at is not None

    assert len(sock.sent) == 1
    event = sock.sent[0]
    assert event["type"] == "reminder"
    assert event["payload"]["reminder_id"] == reminder.id
    assert "call jamil" in event["payload"]["body"]
    assert event["payload"]["session_id"] == "s-chat"

    async with factory() as db:
        result = await db.execute(select(Message).where(Message.session_id == "s-chat"))
        messages = result.scalars().all()
    assert len(messages) == 1
    assert messages[0].role == "assistant"
    assert "call jamil" in messages[0].content


async def test_fire_without_session_id_still_pushes_no_message(factory, sched):
    from app.core.reminders import create_reminder

    sock = FakeSocket()
    await push_manager.connect(sock)

    due = utc_now() + timedelta(seconds=0)
    async with factory() as db:
        reminder = await create_reminder(db, "water the plants", due, session_id=None)

    await sched._fire(reminder.job_id)

    assert len(sock.sent) == 1
    async with factory() as db:
        result = await db.execute(select(Message))
        assert result.scalars().all() == []


async def test_fire_with_no_window_connected_still_settles(factory, sched):
    """Best-effort delivery by design: nothing connected is not a failure."""
    from app.core.reminders import create_reminder

    due = utc_now() + timedelta(seconds=0)
    async with factory() as db:
        reminder = await create_reminder(db, "call jamil", due, session_id="s1")

    await sched._fire(reminder.job_id)

    row = await get_reminder(factory, reminder.id)
    assert row.status == "fired"


async def test_late_reminder_flagged_in_push_payload(factory, sched):
    from app.core.reminders import create_reminder

    sock = FakeSocket()
    await push_manager.connect(sock)

    due = utc_now() - timedelta(hours=1)  # in the past — a late fire
    async with factory() as db:
        reminder = await create_reminder(db, "call jamil", due, session_id="s1")

    await sched._fire(reminder.job_id)

    assert sock.sent[0]["payload"]["late"] is True


async def test_late_fire_body_says_how_late(factory, sched):
    """A late fire is honest in the user-facing text — push body AND the
    persisted chat message — never presented as an on-time reminder."""
    from app.core.reminders import create_reminder

    sock = FakeSocket()
    await push_manager.connect(sock)

    due = utc_now() - timedelta(minutes=5)
    async with factory() as db:
        reminder = await create_reminder(db, "take shower", due, session_id="s-late")

    await sched._fire(reminder.job_id)

    body = sock.sent[0]["payload"]["body"]
    assert "missed while Jarvis was offline" in body
    assert "was due 5 minutes ago" in body
    assert "take shower" in body

    async with factory() as db:
        result = await db.execute(select(Message).where(Message.session_id == "s-late"))
        messages = result.scalars().all()
    assert len(messages) == 1
    assert messages[0].content == body


async def test_on_time_fire_body_has_no_late_wording(factory, sched):
    from app.core.reminders import create_reminder

    sock = FakeSocket()
    await push_manager.connect(sock)

    due = utc_now() + timedelta(seconds=0)
    async with factory() as db:
        reminder = await create_reminder(db, "take shower", due, session_id="s-ontime")

    await sched._fire(reminder.job_id)

    body = sock.sent[0]["payload"]["body"]
    assert body == "Reminder: take shower"
    assert "missed" not in body


def test_describe_lateness_units():
    from app.core.reminders import _describe_lateness

    assert _describe_lateness(65) == "1 minute"
    assert _describe_lateness(5 * 60) == "5 minutes"
    assert _describe_lateness(2 * 3600) == "2 hours"
    assert _describe_lateness(3 * 86400) == "3 days"
    # Defensive clamp: late means >60s, but never say "0 minutes".
    assert _describe_lateness(10) == "1 minute"


# ================================================================== listing

async def test_list_reminders_soonest_first_and_status_filter(factory):
    from app.core.reminders import cancel_reminder, create_reminder, list_reminders

    now = utc_now()
    async with factory() as db:
        far = await create_reminder(db, "far", now + timedelta(hours=5), "s1")
        near = await create_reminder(db, "near", now + timedelta(hours=1), "s1")

    async with factory() as db:
        rows = await list_reminders(db)
    assert [r.id for r in rows] == [near.id, far.id]

    async with factory() as db:
        await cancel_reminder(db, near.id)

    async with factory() as db:
        pending = await list_reminders(db, status="pending")
        cancelled = await list_reminders(db, status="cancelled")
    assert [r.id for r in pending] == [far.id]
    assert [r.id for r in cancelled] == [near.id]
