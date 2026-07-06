"""Shared fixtures for the Jarvis OS backend test suite."""
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.db.database import Base
from app.memory.engine import MemoryEngine
from app.memory.conversation_state import CONVERSATION_SESSIONS


@pytest_asyncio.fixture
async def db_session():
    """In-memory SQLite session for each test."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
def engine(db_session):
    """MemoryEngine with no Qdrant (SQLite-only mode)."""
    return MemoryEngine(db=db_session, qdrant=None)


@pytest.fixture
def session_id():
    """Fresh conversation session id, cleaned up after the test."""
    sid = str(uuid.uuid4())
    yield sid
    CONVERSATION_SESSIONS.pop(sid, None)
