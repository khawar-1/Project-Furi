"""
Progress-aware chat: app/core/task_status.active_tasks_context — the
BACKGROUND WORK block injected so Jarvis answers "how's the browser task going?"
from live Task rows, never from imagination.
"""
from datetime import timedelta

import pytest_asyncio
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.task_status import active_tasks_context
from app.db.database import Base
from app.db.models import Task, utc_now


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


async def _add(db, **kw):
    task = Task(**kw)
    db.add(task)
    await db.commit()
    return task


async def test_no_session_returns_empty(factory):
    async with factory() as db:
        assert await active_tasks_context(db, None) == ""
        assert await active_tasks_context(db, "") == ""


async def test_no_tasks_returns_empty(factory):
    async with factory() as db:
        assert await active_tasks_context(db, "s1") == ""


async def test_active_task_is_reported_with_agent_and_status(factory):
    async with factory() as db:
        await _add(db, session_id="s1", goal="play jane on youtube",
                   status="running", domain="browser")
        block = await active_tasks_context(db, "s1")
    assert "BACKGROUND WORK" in block
    assert "Browser agent" in block
    assert "running now" in block
    assert "play jane on youtube" in block


async def test_awaiting_statuses_are_phrased_for_the_user(factory):
    async with factory() as db:
        await _add(db, session_id="s1", goal="email jamil", status="awaiting_approval", domain="email")
        await _add(db, session_id="s1", goal="which file?", status="awaiting_choice", domain="file")
        block = await active_tasks_context(db, "s1")
    assert "waiting for your approval" in block
    assert "waiting for your answer" in block


async def test_other_sessions_are_not_reported(factory):
    async with factory() as db:
        await _add(db, session_id="other", goal="secret task", status="running", domain="file")
        assert await active_tasks_context(db, "s1") == ""


async def test_recent_terminal_included_old_terminal_excluded(factory):
    async with factory() as db:
        fresh = await _add(db, session_id="s1", goal="fresh done", status="completed", domain="file")
        fresh.finished_at = utc_now() - timedelta(minutes=5)
        stale = await _add(db, session_id="s1", goal="ancient done", status="completed", domain="file")
        stale.finished_at = utc_now() - timedelta(hours=3)
        await db.commit()
        block = await active_tasks_context(db, "s1")
    assert "fresh done" in block
    assert "completed" in block
    assert "ancient done" not in block


async def test_missing_domain_falls_to_general_label(factory):
    async with factory() as db:
        await _add(db, session_id="s1", goal="legacy task", status="running", domain=None)
        block = await active_tasks_context(db, "s1")
    assert "Jarvis" in block  # general agent display name
    assert "legacy task" in block
