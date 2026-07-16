"""
Phase 11 Part 3 — goal threads (app/core/goal_threads.py).

Ongoing-concern store: upsert-dedupe, lifecycle (resolve/drop), and the
nudge cadence (due_threads / mark_nudged) on a shared in-memory DB.
"""
from datetime import date, timedelta

import pytest_asyncio
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.goal_threads import (
    RENUDGE_DAYS,
    drop_thread,
    due_threads,
    get_thread,
    list_threads,
    mark_nudged,
    resolve_thread,
    upsert_thread,
)
from app.db.database import Base
from app.db.models import GoalThread, utc_now


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


# ================================================================ upsert

async def test_create_thread(factory):
    async with factory() as db:
        t = await upsert_thread(db, "The project deadline", description="worried about it")
        assert t is not None
        assert t.status == "open" and t.source == "extractor"
        assert t.next_check_at is not None  # a default check was scheduled


async def test_empty_title_returns_none(factory):
    async with factory() as db:
        assert await upsert_thread(db, "   ") is None


async def test_upsert_dedupes_open_thread(factory):
    async with factory() as db:
        a = await upsert_thread(db, "The Deadline!")
        b = await upsert_thread(db, "the deadline", description="still stressing")
        assert a.id == b.id  # same normalized title → updated, not duplicated
        assert b.description == "still stressing"
        assert len(await list_threads(db)) == 1


async def test_event_date_drives_next_check(factory):
    async with factory() as db:
        d = date.today() + timedelta(days=10)
        t = await upsert_thread(db, "Interview at Acme", event_date=d)
        # Nudge the day AFTER the event date ("did it land?").
        assert t.next_check_at.date() == d + timedelta(days=1)


# ============================================================== lifecycle

async def test_resolve_and_drop(factory):
    async with factory() as db:
        t = await upsert_thread(db, "waiting to hear back")
        resolved = await resolve_thread(db, t.id)
        assert resolved.status == "resolved"

        t2 = await upsert_thread(db, "another concern")
        dropped = await drop_thread(db, t2.id)
        assert dropped.status == "dropped"


async def test_list_filters_by_status(factory):
    async with factory() as db:
        a = await upsert_thread(db, "open one")
        b = await upsert_thread(db, "resolved one")
        await resolve_thread(db, b.id)
        assert [t.id for t in await list_threads(db, status="open")] == [a.id]


async def test_resolve_missing_returns_none(factory):
    async with factory() as db:
        assert await resolve_thread(db, "nope") is None


# ============================================================== nudging

async def test_due_threads_only_past_due(factory):
    async with factory() as db:
        due = await upsert_thread(db, "past due concern")
        due.next_check_at = utc_now() - timedelta(days=1)
        future = await upsert_thread(db, "future concern")
        future.next_check_at = utc_now() + timedelta(days=5)
        await db.commit()

        ids = [t.id for t in await due_threads(db)]
        assert due.id in ids and future.id not in ids


async def test_due_threads_excludes_resolved(factory):
    async with factory() as db:
        t = await upsert_thread(db, "resolved but past due")
        t.next_check_at = utc_now() - timedelta(days=1)
        await db.commit()
        await resolve_thread(db, t.id)
        assert await due_threads(db) == []


async def test_mark_nudged_pushes_next_check(factory):
    async with factory() as db:
        t = await upsert_thread(db, "concern")
        t.next_check_at = utc_now() - timedelta(days=1)
        await db.commit()

        await mark_nudged(db, t)
        refreshed = await get_thread(db, t.id)
        assert refreshed.last_nudged_at is not None
        # Pushed out ~RENUDGE_DAYS so it isn't re-nudged next heartbeat.
        assert refreshed.next_check_at > utc_now() + timedelta(days=RENUDGE_DAYS - 1)
        assert refreshed.id not in [x.id for x in await due_threads(db)]
