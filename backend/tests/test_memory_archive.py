"""The reversible memory archive (2026-08-03, Tier 2 item 7).

This is the only part of the item that HIDES anything, so most of these tests
are about what it must NOT do. The rule the whole design rests on: **nothing is
ever destroyed by an automatic process.** User-initiated deletion is a hard
delete and always has been; automatic maintenance only ever sets `archived_at`,
which retrieval skips and one click undoes.
"""
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.database import Base

from app.db.models import SemanticMemory, utc_now
from app.memory.archive import (
    ARCHIVE_AFTER_DAYS,
    ARCHIVE_MAX_PER_PASS,
    PROTECTED_CATEGORIES,
    archive_stale_memories,
    list_archived,
    restore_memory,
)
from app.memory.engine import MemoryEngine


@pytest_asyncio.fixture
async def swept_memory_db(tmp_path_factory, monkeypatch):
    """A database the housekeeping sweep can reach — it opens its OWN session,
    so a test that only calls the purge functions never touches the wiring."""
    db_dir = tmp_path_factory.mktemp("memory-archive-sweep")
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_dir / 'a.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    import app.db.database as database

    monkeypatch.setattr(database, "AsyncSessionLocal", factory)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _add(db, **kw) -> SemanticMemory:
    age_days = kw.pop("age_days", 0)
    row = SemanticMemory(subject=kw.pop("subject", "user"), **kw)
    row.created_at = utc_now() - timedelta(days=age_days)
    db.add(row)
    await db.commit()
    return row


async def _rows(db) -> list[SemanticMemory]:
    return list((await db.execute(select(SemanticMemory))).scalars().all())


# --------------------------------------------------------- what it must NOT do


@pytest.mark.asyncio
async def test_a_recent_memory_is_never_archived(db_session):
    await _add(db_session, content="fresh", age_days=3)
    assert await archive_stale_memories(db_session) == 0


@pytest.mark.asyncio
async def test_a_memory_that_has_been_used_is_never_archived(db_session):
    """`last_used_at` is stamped when a fact is actually RENDERED into a prompt.
    Anything the conversation has needed is live by definition."""
    row = await _add(db_session, content="used", age_days=ARCHIVE_AFTER_DAYS + 90)
    row.last_used_at = utc_now() - timedelta(days=ARCHIVE_AFTER_DAYS + 30)
    await db_session.commit()

    assert await archive_stale_memories(db_session) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("category", sorted(PROTECTED_CATEGORIES))
async def test_core_identity_categories_are_never_archived(db_session, category):
    """⚠️ THE CASE THAT JUSTIFIES A DENY-LIST. "I am allergic to penicillin" can
    go a year unmentioned and must still be there the day it matters. Being
    unused is exactly what these facts look like when everything is fine."""
    await _add(
        db_session, content=f"a {category} fact", category=category,
        age_days=ARCHIVE_AFTER_DAYS * 4,
    )
    assert await archive_stale_memories(db_session) == 0


@pytest.mark.asyncio
async def test_a_superseded_memory_is_left_alone(db_session):
    """⚠️ `is_active=False` IS A DIFFERENT STATE and must not be blurred into
    this one. It is a soft DELETE written by dedup and supersede — a claim the
    content is now WRONG. `archived_at` claims only "nothing has needed this".
    Marking a superseded row would present a correction as a tidy-up."""
    await _add(
        db_session, content="superseded", is_active=False,
        age_days=ARCHIVE_AFTER_DAYS * 2,
    )
    assert await archive_stale_memories(db_session) == 0


@pytest.mark.asyncio
async def test_it_never_deletes_a_row(db_session):
    """THE LOAD-BEARING PROPERTY. Archiving is reversible; deletion is not."""
    await _add(db_session, content="ancient", age_days=ARCHIVE_AFTER_DAYS * 3)
    before = len(await _rows(db_session))

    await archive_stale_memories(db_session)

    after = await _rows(db_session)
    assert len(after) == before, "the archive pass deleted a row"
    assert after[0].content == "ancient"
    assert after[0].is_active is True, "archiving must not touch is_active"
    assert after[0].qdrant_id == after[0].qdrant_id  # vector reference untouched


# ------------------------------------------------------------- what it DOES do


@pytest.mark.asyncio
async def test_a_long_unused_memory_is_archived(db_session):
    row = await _add(db_session, content="stale", age_days=ARCHIVE_AFTER_DAYS + 1)

    assert await archive_stale_memories(db_session) == 1

    await db_session.refresh(row)
    assert row.archived_at is not None


@pytest.mark.asyncio
async def test_a_pass_is_capped_so_a_first_run_is_gradual(db_session):
    """A first run over years of history must be observable, not a single
    silent sweep of thousands of rows."""
    for i in range(ARCHIVE_MAX_PER_PASS + 25):
        await _add(db_session, content=f"old {i}", age_days=ARCHIVE_AFTER_DAYS + 10)

    assert await archive_stale_memories(db_session) == ARCHIVE_MAX_PER_PASS


@pytest.mark.asyncio
async def test_archiving_is_idempotent(db_session):
    await _add(db_session, content="stale", age_days=ARCHIVE_AFTER_DAYS + 1)
    assert await archive_stale_memories(db_session) == 1
    assert await archive_stale_memories(db_session) == 0


# ------------------------------------------------------------------- the undo


@pytest.mark.asyncio
async def test_an_archived_memory_is_listed_and_restorable(db_session):
    """Without the undo the archive is a delete with extra steps."""
    row = await _add(db_session, content="stale", age_days=ARCHIVE_AFTER_DAYS + 1)
    await archive_stale_memories(db_session)

    listed = await list_archived(db_session)
    assert [m.content for m in listed] == ["stale"]

    assert await restore_memory(db_session, row.id) is True
    await db_session.refresh(row)
    assert row.archived_at is None
    assert row.last_used_at is not None, "a restore must not look instantly stale again"
    assert await list_archived(db_session) == []


@pytest.mark.asyncio
async def test_restoring_something_that_is_not_archived_reports_it(db_session):
    row = await _add(db_session, content="live")
    assert await restore_memory(db_session, row.id) is False
    assert await restore_memory(db_session, "no-such-id") is False


@pytest.mark.asyncio
async def test_a_restored_memory_does_not_immediately_re_archive(db_session):
    """`restore_memory` stamps `last_used_at`, so the very next sweep does not
    undo the user's undo."""
    row = await _add(db_session, content="stale", age_days=ARCHIVE_AFTER_DAYS * 2)
    await archive_stale_memories(db_session)
    await restore_memory(db_session, row.id)

    assert await archive_stale_memories(db_session) == 0


# ---------------------------------------------------- archived leaves the block


@pytest.mark.asyncio
async def test_an_archived_memory_is_excluded_from_retrieval(db_session):
    """The no-Qdrant fallback path is the one exercised here; the vector path
    filters on the same column in the same query."""
    await _add(db_session, content="live fact")
    stale = await _add(db_session, content="stale fact", age_days=ARCHIVE_AFTER_DAYS + 1)
    await archive_stale_memories(db_session)
    await db_session.refresh(stale)
    assert stale.archived_at is not None

    engine = MemoryEngine(db_session, provider=None, qdrant=None)
    found = await engine.search_semantic_memory("anything", limit=10)

    contents = [m.content for m in found]
    assert "live fact" in contents
    assert "stale fact" not in contents


# --------------------------------------------------------------- sweep wiring


@pytest.mark.asyncio
async def test_the_periodic_sweep_runs_the_archive(swept_memory_db):
    """⚠️ THE WIRING TEST — the 2026-08-03 lesson: a test that calls the
    function directly proves the FUNCTION works and says nothing about whether
    the timer calls it."""
    from app.core import housekeeping

    row = SemanticMemory(content="stale", subject="user")
    row.created_at = utc_now() - timedelta(days=ARCHIVE_AFTER_DAYS + 5)
    swept_memory_db.add(row)
    await swept_memory_db.commit()

    await housekeeping.run_housekeeping_pass()

    await swept_memory_db.refresh(row)
    assert row.archived_at is not None, "the memory archive is not on the sweep"
