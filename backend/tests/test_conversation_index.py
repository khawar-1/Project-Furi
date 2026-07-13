"""
Phase 6 Part 4 — conversation index service (app/core/conversation_index.py).

Messages become searchable by meaning. These tests use an in-memory SQLite
(StaticPool, shared across sessions) with a FAKE qdrant that records upserts and
a stub embedder, so the suite never loads fastembed or touches a real vector
store. Covers: embed-on-write (upsert + embedded_at cursor set), the privacy
toggle (disabled → no-op), the skip rules (system / empty), incremental vs full
backfill, and the run_conversation_index enable/qdrant gates.
"""
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import conversation_index as ci
from app.core.app_settings import FileIndexConfig, set_file_index_config
from app.db.database import Base
from app.db.models import Message, utc_now


# ================================================================= fakes

class _FakePoint:
    def __init__(self, id, vector, payload):
        self.id = id
        self.vector = vector
        self.payload = payload


class _FakeQdrant:
    """Records every upsert; PointStruct-compatible enough for our code."""
    def __init__(self):
        self.upserts: list[tuple[str, list]] = []

    async def upsert(self, collection_name, points):
        self.upserts.append((collection_name, list(points)))

    @property
    def point_ids(self) -> list:
        return [p.id for _, pts in self.upserts for p in pts]

    @property
    def payloads(self) -> list[dict]:
        return [p.payload for _, pts in self.upserts for p in pts]


async def _fake_embed_batch(texts):
    return [[0.01] * 384 for _ in texts]


async def _fake_embed_text(text):
    return [0.02] * 384


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
async def db(factory, monkeypatch):
    # Point run_conversation_index's own-session lookups at this engine too.
    monkeypatch.setattr(ci, "SESSION_FACTORY", factory)
    async with factory() as session:
        yield session


async def _enable(db, enabled=True):
    await set_file_index_config(db, FileIndexConfig(
        enabled=enabled, folders=(r"C:\Users\me\Docs",),
        exclusions=(), interval_minutes=360,
    ))


async def _add(db, *, role="user", content="hello", embedded=False, age_days=0):
    msg = Message(session_id="s1", role=role, content=content)
    msg.created_at = utc_now() - timedelta(days=age_days)
    if embedded:
        msg.embedded_at = utc_now()
    db.add(msg)
    await db.commit()
    return msg


# ========================================================= embed on write

async def test_embed_on_write_upserts_and_marks(db, monkeypatch):
    await _enable(db)
    fake = _FakeQdrant()
    monkeypatch.setattr(ci, "get_qdrant_client", lambda: fake)
    monkeypatch.setattr(ci, "embed_text", _fake_embed_text)

    msg = await _add(db, role="user", content="the trip budget was tight")
    ok = await ci.embed_message_best_effort(db, msg)

    assert ok is True
    assert fake.point_ids == [msg.id]                 # id = message id
    assert fake.upserts[0][0] == "conversation_messages"
    assert msg.embedded_at is not None
    payload = fake.payloads[0]
    assert payload["message_id"] == msg.id
    assert payload["session_id"] == "s1"
    assert payload["role"] == "user"
    assert "trip budget" in payload["text"]


async def test_embed_on_write_noop_when_disabled(db, monkeypatch):
    await _enable(db, enabled=False)   # privacy toggle OFF
    fake = _FakeQdrant()
    monkeypatch.setattr(ci, "get_qdrant_client", lambda: fake)
    monkeypatch.setattr(ci, "embed_text", _fake_embed_text)

    msg = await _add(db)
    ok = await ci.embed_message_best_effort(db, msg)

    assert ok is False
    assert fake.upserts == []
    assert msg.embedded_at is None


async def test_embed_on_write_skips_system_and_empty(db, monkeypatch):
    await _enable(db)
    fake = _FakeQdrant()
    monkeypatch.setattr(ci, "get_qdrant_client", lambda: fake)
    monkeypatch.setattr(ci, "embed_text", _fake_embed_text)

    sys_msg = await _add(db, role="system", content="you are jarvis")
    empty = await _add(db, role="user", content="   ")

    assert await ci.embed_message_best_effort(db, sys_msg) is False
    assert await ci.embed_message_best_effort(db, empty) is False
    assert fake.upserts == []


async def test_embed_on_write_noop_without_qdrant(db, monkeypatch):
    await _enable(db)
    monkeypatch.setattr(ci, "get_qdrant_client", lambda: None)
    monkeypatch.setattr(ci, "embed_text", _fake_embed_text)

    msg = await _add(db)
    assert await ci.embed_message_best_effort(db, msg) is False
    assert msg.embedded_at is None


async def test_schedule_message_embed_runs_detached(db, factory, monkeypatch):
    """The chat hook (latency 2026-07-13): _persist_message no longer awaits
    the embed — schedule_message_embed runs it as a detached task with its
    OWN session and the same guards/cursor semantics."""
    await _enable(db)
    fake = _FakeQdrant()
    monkeypatch.setattr(ci, "get_qdrant_client", lambda: fake)
    monkeypatch.setattr(ci, "embed_text", _fake_embed_text)

    msg = await _add(db, role="assistant", content="deferred embed works")
    ci.schedule_message_embed(msg.id)          # returns immediately
    await ci.wait_for_conversation_index()     # drain the detached task

    assert fake.point_ids == [msg.id]
    async with factory() as check:
        fresh = await check.get(Message, msg.id)
        assert fresh.embedded_at is not None   # cursor stamped by the task


async def test_schedule_message_embed_missing_row_is_noop(db, monkeypatch):
    await _enable(db)
    fake = _FakeQdrant()
    monkeypatch.setattr(ci, "get_qdrant_client", lambda: fake)
    monkeypatch.setattr(ci, "embed_text", _fake_embed_text)

    ci.schedule_message_embed("no-such-message-id")
    await ci.wait_for_conversation_index()
    assert fake.upserts == []


# ============================================================ backfill

async def test_backfill_embeds_unembedded_only(db):
    fresh_a = await _add(db, role="user", content="alpha")
    fresh_b = await _add(db, role="assistant", content="beta")
    await _add(db, role="user", content="already done", embedded=True)
    await _add(db, role="system", content="scaffolding")   # never indexed

    fake = _FakeQdrant()
    stats = await ci.index_conversations(db, fake, full=False, embed=_fake_embed_batch)

    assert stats.embedded == 2
    assert set(fake.point_ids) == {fresh_a.id, fresh_b.id}
    # The freshly-embedded rows are now marked.
    for m in (fresh_a, fresh_b):
        await db.refresh(m)
        assert m.embedded_at is not None


async def test_backfill_full_reembeds_all_turns(db):
    a = await _add(db, role="user", content="alpha", embedded=True)
    b = await _add(db, role="assistant", content="beta", embedded=True)
    await _add(db, role="system", content="scaffolding", embedded=True)

    fake = _FakeQdrant()
    stats = await ci.index_conversations(db, fake, full=True, embed=_fake_embed_batch)

    # full=True re-embeds the user+assistant turns (system still skipped).
    assert stats.embedded == 2
    assert set(fake.point_ids) == {a.id, b.id}


# =================================================== run_conversation_index

async def test_run_gated_on_enabled(db, monkeypatch):
    await _enable(db, enabled=False)
    fake = _FakeQdrant()
    monkeypatch.setattr(ci, "get_qdrant_client", lambda: fake)
    monkeypatch.setattr(ci, "embed_batch", _fake_embed_batch)
    await _add(db)

    stats = await ci.run_conversation_index()
    assert stats.embedded == 0
    assert "disabled" in (stats.error or "").lower()
    assert fake.upserts == []


async def test_run_noop_without_qdrant(db, monkeypatch):
    await _enable(db)
    monkeypatch.setattr(ci, "get_qdrant_client", lambda: None)
    monkeypatch.setattr(ci, "embed_batch", _fake_embed_batch)
    await _add(db)

    stats = await ci.run_conversation_index()
    assert stats.embedded == 0
    assert "unavailable" in (stats.error or "").lower()


async def test_run_embeds_when_enabled(db, monkeypatch):
    await _enable(db)
    fake = _FakeQdrant()
    monkeypatch.setattr(ci, "get_qdrant_client", lambda: fake)
    monkeypatch.setattr(ci, "embed_batch", _fake_embed_batch)
    m = await _add(db, role="user", content="the langgraph notes")

    stats = await ci.run_conversation_index()
    assert stats.embedded == 1
    assert fake.point_ids == [m.id]


async def test_summary_counts(db):
    await _add(db, role="user", content="one", embedded=True)
    await _add(db, role="assistant", content="two")
    await _add(db, role="system", content="ignored")

    summary = await ci.get_conversation_index_summary(db)
    assert summary["indexed_messages"] == 1
    assert summary["unindexed_messages"] == 1
