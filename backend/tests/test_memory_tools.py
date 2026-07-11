"""
Phase 3.5 — Memory tools (recall_memory / lookup_contact).
Read-level registry tools that give the agent planner the same memory the
chat path has. lookup_contact must inherit the chat path's discipline:
ambiguous names come back status='ambiguous' with candidates (the planner
asks — rule 11), never a silent pick.
"""
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.tools  # noqa: F401 — registers every tool
from app.db.database import Base
from app.db.models import Contact, ContactInteraction, SemanticMemory
from app.tools import memory_tools
from app.tools.registry import registry


@pytest_asyncio.fixture
async def mem_db(tmp_path_factory, monkeypatch):
    """File-backed DB the tools' own sessions point at (SESSION_FACTORY)."""
    db_dir = tmp_path_factory.mktemp("memtools-db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_dir / 'mem.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(memory_tools, "SESSION_FACTORY", factory)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _seed(db) -> None:
    jamil = Contact(name="Jamil Ali", relationship_type="friend")
    db.add_all([
        jamil,
        Contact(name="Jamil Khan", relationship_type="colleague"),
        Contact(name="Sara", email="sara@example.com", organization="Acme", birthday="03-04"),
    ])
    await db.flush()
    db.add(ContactInteraction(
        contact_id=jamil.id, description="Played Tekken with the user", category="social",
    ))
    db.add(SemanticMemory(content="Planning to go fishing on 2026-07-12", subject="user"))
    db.add(SemanticMemory(content="Uses D:\\Projects for all code", subject="user"))
    await db.commit()


# ------------------------------------------------------------- recall_memory

async def test_recall_memory_returns_stored_facts(mem_db):
    await _seed(mem_db)
    result = await registry.get("recall_memory").execute(query="fishing plans")
    assert result.success is True
    contents = [m["content"] for m in result.output["memories"]]
    assert "Planning to go fishing on 2026-07-12" in contents
    assert result.output["count"] >= 2


async def test_recall_memory_requires_a_query(mem_db):
    result = await registry.get("recall_memory").execute(query="   ")
    assert result.success is False
    assert "query" in result.error


async def test_recall_memory_is_read_level(mem_db):
    assert registry.get("recall_memory").permission_level.value == "read"
    assert registry.get("lookup_contact").permission_level.value == "read"


# ------------------------------------------------------------ lookup_contact

async def test_lookup_contact_resolves_unique_name(mem_db):
    await _seed(mem_db)
    result = await registry.get("lookup_contact").execute(name="sara")
    assert result.success is True
    assert result.output["status"] == "resolved"
    assert result.output["contact"]["name"] == "Sara"
    assert result.output["contact"]["email"] == "sara@example.com"
    # Phase 5 Part 2: the planner resolves "email Jamil" / birthday briefings
    # from this payload — both fields must surface
    assert result.output["contact"]["birthday"] == "03-04"


async def test_lookup_contact_includes_recent_facts(mem_db):
    await _seed(mem_db)
    result = await registry.get("lookup_contact").execute(name="jamil ali")
    assert result.output["status"] == "resolved"
    facts = result.output["recent_facts"]
    assert facts and facts[0]["description"] == "Played Tekken with the user"


async def test_lookup_contact_ambiguous_never_guesses(mem_db):
    await _seed(mem_db)
    result = await registry.get("lookup_contact").execute(name="jamil")
    assert result.success is True
    assert result.output["status"] == "ambiguous"
    assert set(result.output["candidates"]) == {"Jamil Ali", "Jamil Khan"}
    assert "never guess" in result.output["note"].lower()


async def test_lookup_contact_not_found(mem_db):
    await _seed(mem_db)
    result = await registry.get("lookup_contact").execute(name="zorro")
    assert result.output["status"] == "not_found"


async def test_lookup_contact_requires_a_name(mem_db):
    result = await registry.get("lookup_contact").execute(name="")
    assert result.success is False
    assert "name" in result.error
