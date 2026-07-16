"""
Phase 11 Part 3 — /api/threads HTTP behavior (goal threads).

httpx ASGITransport over the real app with an in-memory DB (the
test_routines_api.py pattern). No scheduler/LLM/Google touched.
"""
import httpx
import pytest_asyncio
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.dependencies import get_db
from app.db.database import Base
from main import app


@pytest_asyncio.fixture
async def client():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _get_db():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = _get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
    await engine.dispose()


async def test_list_empty(client):
    r = await client.get("/api/threads")
    assert r.status_code == 200 and r.json() == []


async def test_create_and_list(client):
    r = await client.post("/api/threads", json={
        "title": "The project deadline", "description": "worried about it",
        "event_date": "2026-08-01",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "The project deadline"
    assert body["status"] == "open" and body["source"] == "manual"
    assert body["event_date"] == "2026-08-01"
    assert body["created_at"].endswith("+00:00")

    listed = (await client.get("/api/threads")).json()
    assert len(listed) == 1


async def test_create_bad_date_400(client):
    r = await client.post("/api/threads", json={"title": "x", "event_date": "08/01/2026"})
    assert r.status_code == 400


async def test_resolve(client):
    created = (await client.post("/api/threads", json={"title": "waiting to hear back"})).json()
    r = await client.post(f"/api/threads/{created['id']}/resolve")
    assert r.status_code == 200 and r.json()["status"] == "resolved"
    # Resolved threads drop out of the open filter.
    assert (await client.get("/api/threads?status=open")).json() == []


async def test_dismiss(client):
    created = (await client.post("/api/threads", json={"title": "a concern"})).json()
    r = await client.post(f"/api/threads/{created['id']}/dismiss")
    assert r.status_code == 200 and r.json()["status"] == "dropped"


async def test_resolve_missing_404(client):
    r = await client.post("/api/threads/nope/resolve")
    assert r.status_code == 404
