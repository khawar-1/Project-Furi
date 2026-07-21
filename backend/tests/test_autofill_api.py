"""
Phase 15.2 — /api/autofill HTTP behavior (the curated autofill profile).

httpx ASGITransport over the real app with an in-memory DB (the
test_threads_api.py pattern). Pins the CRUD surface, the 400s, and that a SECRET
value is never returned by the API (display-masked, write-through).
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
    r = await client.get("/api/autofill")
    assert r.status_code == 200 and r.json() == []


async def test_create_and_list(client):
    r = await client.post("/api/autofill", json={"label": "Email", "value": "me@x.com"})
    assert r.status_code == 200
    body = r.json()
    assert body["key"] == "email" and body["value"] == "me@x.com"
    assert body["kind"] == "text" and body["is_secret"] is False

    rows = (await client.get("/api/autofill")).json()
    assert [row["key"] for row in rows] == ["email"]


async def test_upsert_replaces_not_duplicates(client):
    await client.post("/api/autofill", json={"label": "Email", "value": "a@x.com"})
    await client.post("/api/autofill", json={"label": "Email", "value": "b@x.com"})
    rows = (await client.get("/api/autofill")).json()
    assert len(rows) == 1 and rows[0]["value"] == "b@x.com"


async def test_a_secret_value_is_never_returned(client):
    r = await client.post(
        "/api/autofill", json={"label": "Password", "value": "hunter2", "kind": "secret"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["is_secret"] is True
    assert body["value"] is None       # display-masked — never returned
    assert body["has_value"] is True   # but the UI knows one is set

    rows = (await client.get("/api/autofill")).json()
    assert rows[0]["value"] is None


async def test_bad_kind_is_400(client):
    r = await client.post("/api/autofill", json={"label": "X", "value": "v", "kind": "weird"})
    assert r.status_code == 400


async def test_unsafe_document_path_is_400(client):
    r = await client.post(
        "/api/autofill",
        json={"label": "Resume", "value": r"C:\nope\missing.pdf", "kind": "document"},
    )
    assert r.status_code == 400


async def test_delete(client):
    await client.post("/api/autofill", json={"label": "Phone", "value": "555"})
    r = await client.delete("/api/autofill/phone")
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert (await client.delete("/api/autofill/phone")).status_code == 404
