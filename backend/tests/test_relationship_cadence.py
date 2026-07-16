"""
Phase 11 Parts 1 & 2 — relationship & memory cadence
(app/core/relationship_cadence.py).
"""
from datetime import timedelta

import pytest_asyncio
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.relationship_cadence import memory_callbacks, people_cadence
from app.db.database import Base
from app.db.models import Contact, SemanticMemory, utc_now


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


async def _add_contact(factory, name, *, last_days_ago, count, rel=None):
    async with factory() as db:
        db.add(Contact(
            name=name,
            last_interaction=utc_now() - timedelta(days=last_days_ago),
            interaction_count=count,
            relationship_type=rel,
        ))
        await db.commit()


# ========================================================== people_cadence

async def test_stale_contact_surfaces(factory):
    await _add_contact(factory, "Jamil", last_days_ago=40, count=5, rel="friend")
    async with factory() as db:
        out = await people_cadence(db)
    assert len(out) == 1
    assert out[0]["name"] == "Jamil"
    assert out[0]["weeks_since"] >= 5
    assert out[0]["relationship_type"] == "friend"


async def test_recent_contact_excluded(factory):
    await _add_contact(factory, "Sara", last_days_ago=3, count=5)
    async with factory() as db:
        assert await people_cadence(db) == []


async def test_thin_history_excluded(factory):
    # Only one mention (below min_interactions) — not a real relationship yet.
    await _add_contact(factory, "Acquaintance", last_days_ago=60, count=1)
    async with factory() as db:
        assert await people_cadence(db) == []


async def test_null_last_interaction_excluded(factory):
    async with factory() as db:
        db.add(Contact(name="Never", last_interaction=None, interaction_count=5))
        await db.commit()
        assert await people_cadence(db) == []


async def test_ranked_by_longest_silence(factory):
    await _add_contact(factory, "Recentish", last_days_ago=30, count=5)
    await _add_contact(factory, "Ancient", last_days_ago=120, count=5)
    async with factory() as db:
        out = await people_cadence(db)
    assert [c["name"] for c in out] == ["Ancient", "Recentish"]


# ========================================================= memory_callbacks

async def _add_memory(factory, content, *, days_ago, subject="user"):
    async with factory() as db:
        db.add(SemanticMemory(
            content=content, subject=subject,
            created_at=utc_now() - timedelta(days=days_ago),
        ))
        await db.commit()


async def test_concern_in_window_surfaces(factory):
    await _add_memory(factory, "Worried about the project deadline next week", days_ago=10)
    async with factory() as db:
        out = await memory_callbacks(db)
    assert len(out) == 1 and "deadline" in out[0]["content"].lower()
    assert out[0]["days_ago"] >= 9


async def test_non_concern_ignored(factory):
    await _add_memory(factory, "Had pizza for lunch", days_ago=10)
    async with factory() as db:
        assert await memory_callbacks(db) == []


async def test_outside_window_ignored(factory):
    await _add_memory(factory, "Worried about the deadline", days_ago=1)    # too recent
    await _add_memory(factory, "Worried about the exam", days_ago=60)       # too old
    async with factory() as db:
        assert await memory_callbacks(db) == []
