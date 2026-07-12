"""
Phase 5 Part 4 — birthday reminders (app/core/birthdays.py).

Uses a real JarvisScheduler on a shared in-memory DB (the test_reminders.py
pattern): contacts, scheduled_jobs, and the handler's own AsyncSessionLocal
all point at the same engine, and the app-wide `scheduler` singleton is
swapped so app.core.birthdays (which imports it directly) uses the test one.

Covers: the occurrence math matrix; sync on create/update/clear/soft-delete/
hard-delete (job cancelled + re-armed, ids swapped); handler fire → push +
persisted Message + re-armed next-year job; the stale-job and changed-birthday
no-ops; ensure_birthday_jobs arming + orphan sweep; late/off-day wording.
"""
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.birthdays import (
    BIRTHDAY_JOB_KIND,
    ensure_birthday_jobs,
    next_birthday_run_at,
    sync_contact_birthday_job,
)
from app.core.push import push_manager
from app.core.scheduler import JarvisScheduler, to_naive_utc, utc_now
from app.db.database import Base
from app.db.models import Contact, Message
from app.memory.engine import MemoryEngine
from tests.test_push_channel import FakeSocket


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
    singleton (app.core.birthdays imports it directly) at this test's engine,
    and give the fresh scheduler the birthday handler."""
    import app.core.birthdays as bday
    import app.core.scheduler as scheduler_module

    monkeypatch.setattr("app.db.database.AsyncSessionLocal", factory)
    monkeypatch.setattr(bday, "scheduler", sched)
    monkeypatch.setattr(scheduler_module, "scheduler", sched)
    sched.register_handler(BIRTHDAY_JOB_KIND, bday._birthday_job_handler)
    push_manager._connections.clear()
    yield
    push_manager._connections.clear()


# ------------------------------------------------------------------- helpers

async def _get_contact(factory, contact_id):
    async with factory() as db:
        return await db.get(Contact, contact_id)


async def _pending_jobs(sched):
    return await sched.list_jobs(status="pending", kind=BIRTHDAY_JOB_KIND, limit=100)


async def _make_engine_contact(factory, name="Jamil Ali", birthday="07-14"):
    """Create a contact through the engine (its create hook schedules the
    birthday job), returning the fresh contact id."""
    async with factory() as db:
        engine = MemoryEngine(db=db, qdrant=None)
        details = {"birthday": birthday} if birthday else {}
        contact = await engine.create_contact_manual(name, details)
        return contact.id


async def _insert_contact_with_job(factory, sched, name="Jamil Ali",
                                    birthday="07-14", run_at=None):
    """Insert a contact and arm a birthday job at a SPECIFIC run_at (so firing
    tests control on-time / late / off-day deterministically)."""
    run_at = run_at or utc_now()
    async with factory() as db:
        contact = Contact(name=name, birthday=birthday, is_active=True)
        db.add(contact)
        await db.commit()
        await db.refresh(contact)
        job_id = await sched.schedule_at(
            run_at, BIRTHDAY_JOB_KIND,
            {"contact_id": contact.id, "birthday": birthday},
        )
        contact.birthday_job_id = job_id
        await db.commit()
        await db.refresh(contact)
        return contact.id, job_id


# ============================================================ occurrence math

def test_next_birthday_upcoming_this_year():
    now = datetime(2026, 7, 11, 12, 0)
    run_at = next_birthday_run_at("07-14", now)
    # 09:00 local on 2026-07-14, stored as naive UTC.
    assert run_at == to_naive_utc(datetime(2026, 7, 14, 9, 0).astimezone())


def test_next_birthday_rolls_to_next_year_when_passed():
    now = datetime(2026, 7, 11, 12, 0)
    run_at = next_birthday_run_at("03-04", now)
    assert run_at.year == 2027 or run_at.month == 3  # next March
    assert run_at == to_naive_utc(datetime(2027, 3, 4, 9, 0).astimezone())


def test_next_birthday_before_9am_on_the_day_is_today():
    now = datetime(2026, 7, 11, 8, 0)  # before 9am
    run_at = next_birthday_run_at("07-11", now)
    assert run_at == to_naive_utc(datetime(2026, 7, 11, 9, 0).astimezone())


def test_next_birthday_after_9am_on_the_day_rolls_forward():
    now = datetime(2026, 7, 11, 10, 0)  # after 9am
    run_at = next_birthday_run_at("07-11", now)
    assert run_at == to_naive_utc(datetime(2027, 7, 11, 9, 0).astimezone())


def test_next_birthday_year_known_form():
    now = datetime(2026, 1, 1, 0, 0)
    assert next_birthday_run_at("1990-07-14", now) == to_naive_utc(
        datetime(2026, 7, 14, 9, 0).astimezone()
    )


def test_feb29_falls_back_to_feb28_in_non_leap_year():
    now = datetime(2027, 1, 1, 0, 0)  # 2027 is not a leap year
    assert next_birthday_run_at("02-29", now) == to_naive_utc(
        datetime(2027, 2, 28, 9, 0).astimezone()
    )


def test_feb29_stays_feb29_in_leap_year():
    now = datetime(2028, 1, 1, 0, 0)  # 2028 is a leap year
    assert next_birthday_run_at("02-29", now) == to_naive_utc(
        datetime(2028, 2, 29, 9, 0).astimezone()
    )


def test_unparseable_birthday_returns_none():
    assert next_birthday_run_at("", datetime(2026, 7, 11)) is None
    assert next_birthday_run_at("not-a-date", datetime(2026, 7, 11)) is None


# ================================================================ sync hooks

async def test_create_contact_with_birthday_schedules_a_job(factory, sched):
    contact_id = await _make_engine_contact(factory, birthday="07-14")
    contact = await _get_contact(factory, contact_id)
    assert contact.birthday_job_id is not None
    jobs = await _pending_jobs(sched)
    assert len(jobs) == 1
    assert jobs[0]["id"] == contact.birthday_job_id
    assert jobs[0]["payload"]["contact_id"] == contact_id


async def test_create_contact_without_birthday_schedules_nothing(factory, sched):
    contact_id = await _make_engine_contact(factory, birthday=None)
    contact = await _get_contact(factory, contact_id)
    assert contact.birthday_job_id is None
    assert await _pending_jobs(sched) == []


async def test_adding_a_birthday_later_schedules_the_job(factory, sched):
    contact_id = await _make_engine_contact(factory, birthday=None)
    async with factory() as db:
        engine = MemoryEngine(db=db, qdrant=None)
        await engine.update_contact(contact_id, {"birthday": "March 4"})
    contact = await _get_contact(factory, contact_id)
    assert contact.birthday == "03-04"
    assert contact.birthday_job_id is not None
    assert len(await _pending_jobs(sched)) == 1


async def test_changing_the_birthday_reschedules(factory, sched):
    contact_id = await _make_engine_contact(factory, birthday="07-14")
    first = (await _get_contact(factory, contact_id)).birthday_job_id
    async with factory() as db:
        engine = MemoryEngine(db=db, qdrant=None)
        await engine.update_contact(contact_id, {"birthday": "08-15"})
    contact = await _get_contact(factory, contact_id)
    assert contact.birthday_job_id != first  # ids swapped on the row
    # Exactly one pending job — the old one was cancelled.
    jobs = await _pending_jobs(sched)
    assert len(jobs) == 1 and jobs[0]["id"] == contact.birthday_job_id


async def test_clearing_the_birthday_cancels_the_job(factory, sched):
    contact_id = await _make_engine_contact(factory, birthday="07-14")
    async with factory() as db:
        engine = MemoryEngine(db=db, qdrant=None)
        await engine.update_contact(contact_id, {"birthday": ""}, clear_empty=True)
    contact = await _get_contact(factory, contact_id)
    assert contact.birthday is None
    assert contact.birthday_job_id is None
    assert await _pending_jobs(sched) == []


async def test_unrelated_edit_leaves_the_job_untouched(factory, sched):
    contact_id = await _make_engine_contact(factory, birthday="07-14")
    first = (await _get_contact(factory, contact_id)).birthday_job_id
    async with factory() as db:
        engine = MemoryEngine(db=db, qdrant=None)
        await engine.update_contact(contact_id, {"organization": "Acme"})
    contact = await _get_contact(factory, contact_id)
    assert contact.birthday_job_id == first  # no churn
    assert len(await _pending_jobs(sched)) == 1


async def test_soft_delete_cancels_the_job(factory, sched):
    contact_id = await _make_engine_contact(factory, birthday="07-14")
    async with factory() as db:
        contact = await db.get(Contact, contact_id)
        contact.is_active = False
        await db.commit()
        await sync_contact_birthday_job(db, contact)
    contact = await _get_contact(factory, contact_id)
    assert contact.birthday_job_id is None
    assert await _pending_jobs(sched) == []


async def test_hard_delete_cancels_the_job(factory, sched):
    contact_id = await _make_engine_contact(factory, name="Temp Person", birthday="07-14")
    async with factory() as db:
        engine = MemoryEngine(db=db, qdrant=None)
        assert await engine.delete_contact_by_name("Temp Person") is True
    assert await _pending_jobs(sched) == []


# ==================================================================== firing

async def test_fire_pushes_event_and_persists_message(factory, sched):
    sock = FakeSocket()
    await push_manager.connect(sock)
    async with factory() as db:
        db.add(Message(session_id="s-chat", role="user", content="hi"))
        await db.commit()

    contact_id, job_id = await _insert_contact_with_job(
        factory, sched, name="Jamil Ali", birthday="07-11", run_at=utc_now(),
    )
    await sched._fire(job_id)

    assert len(sock.sent) == 1
    event = sock.sent[0]
    assert event["type"] == "birthday"
    assert event["payload"]["contact_id"] == contact_id
    assert "Jamil Ali" in event["payload"]["body"]

    async with factory() as db:
        result = await db.execute(select(Message).where(Message.role == "assistant"))
        messages = result.scalars().all()
    assert len(messages) == 1
    assert "Jamil Ali" in messages[0].content
    assert messages[0].session_id == "s-chat"  # newest session


async def test_fire_reschedules_next_year(factory, sched):
    contact_id, job_id = await _insert_contact_with_job(
        factory, sched, birthday="07-11", run_at=utc_now(),
    )
    await sched._fire(job_id)
    contact = await _get_contact(factory, contact_id)
    # The fired job is re-armed: a NEW pending job, a new id on the row.
    assert contact.birthday_job_id is not None and contact.birthday_job_id != job_id
    jobs = await _pending_jobs(sched)
    assert len(jobs) == 1 and jobs[0]["id"] == contact.birthday_job_id


async def test_fire_with_known_year_states_the_age(factory, sched):
    sock = FakeSocket()
    await push_manager.connect(sock)
    # Fire ON the day (run_at = now) so this is not a late/off-day fire — the
    # age branch only applies to an on-time birthday. Date-relative so a day
    # rollover never flips it into the "missed while offline" wording.
    now = datetime.now()
    _, job_id = await _insert_contact_with_job(
        factory, sched, name="Jamil Ali",
        birthday=f"1990-{now.month:02d}-{now.day:02d}",
        run_at=to_naive_utc(now.replace(hour=9, minute=0, second=0, microsecond=0).astimezone()),
    )
    await sched._fire(job_id)
    assert f"turn {now.year - 1990}" in sock.sent[0]["payload"]["body"]


async def test_off_day_fire_is_honest(factory, sched):
    sock = FakeSocket()
    await push_manager.connect(sock)
    # Scheduled two days ago (backend was offline over the birthday).
    run_at = to_naive_utc((datetime.now() - timedelta(days=2)).astimezone())
    _, job_id = await _insert_contact_with_job(
        factory, sched, name="Jamil Ali", birthday="07-09", run_at=run_at,
    )
    await sched._fire(job_id)
    body = sock.sent[0]["payload"]["body"]
    assert "offline" in body
    assert "was on" in body


async def test_stale_job_does_not_fire_or_fork(factory, sched):
    contact_id, job_id = await _insert_contact_with_job(
        factory, sched, birthday="07-11", run_at=utc_now(),
    )
    # Point the contact at a DIFFERENT current job — this one is now stale.
    async with factory() as db:
        contact = await db.get(Contact, contact_id)
        contact.birthday_job_id = "some-other-job"
        await db.commit()

    sock = FakeSocket()
    await push_manager.connect(sock)
    await sched._fire(job_id)
    assert sock.sent == []  # guard 2: stale job never fires or re-arms


async def test_changed_birthday_since_scheduling_does_not_fire(factory, sched):
    contact_id, job_id = await _insert_contact_with_job(
        factory, sched, birthday="07-11", run_at=utc_now(),
    )
    async with factory() as db:
        contact = await db.get(Contact, contact_id)
        contact.birthday = "12-25"  # changed since the job was scheduled
        await db.commit()

    sock = FakeSocket()
    await push_manager.connect(sock)
    await sched._fire(job_id)
    assert sock.sent == []  # guard 3: payload birthday != current birthday


async def test_inactive_contact_does_not_fire(factory, sched):
    contact_id, job_id = await _insert_contact_with_job(
        factory, sched, birthday="07-11", run_at=utc_now(),
    )
    async with factory() as db:
        contact = await db.get(Contact, contact_id)
        contact.is_active = False
        await db.commit()

    sock = FakeSocket()
    await push_manager.connect(sock)
    await sched._fire(job_id)
    assert sock.sent == []  # guard 1


# =================================================== startup reconciliation

async def test_ensure_arms_missing_jobs(factory, sched):
    # Two active contacts with birthdays but NO job (pre-Part-4 contacts).
    async with factory() as db:
        for name, bd in (("A One", "07-14"), ("B Two", "03-04")):
            db.add(Contact(name=name, birthday=bd, is_active=True))
        await db.commit()

    await ensure_birthday_jobs()

    jobs = await _pending_jobs(sched)
    assert len(jobs) == 2
    async with factory() as db:
        result = await db.execute(select(Contact))
        for c in result.scalars().all():
            assert c.birthday_job_id is not None


async def test_ensure_sweeps_orphan_jobs(factory, sched):
    # A pending birthday job whose contact no longer exists.
    orphan = await sched.schedule_at(
        utc_now() + timedelta(days=30), BIRTHDAY_JOB_KIND,
        {"contact_id": "ghost", "birthday": "07-14"},
    )
    await ensure_birthday_jobs()
    remaining = {j["id"] for j in await _pending_jobs(sched)}
    assert orphan not in remaining


async def test_ensure_leaves_a_live_job_alone(factory, sched):
    contact_id = await _make_engine_contact(factory, birthday="07-14")
    before = (await _get_contact(factory, contact_id)).birthday_job_id
    await ensure_birthday_jobs()
    after = (await _get_contact(factory, contact_id)).birthday_job_id
    assert after == before  # a valid live job is not re-armed
    assert len(await _pending_jobs(sched)) == 1
