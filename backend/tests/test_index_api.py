"""
Phase 6 Part 2 — /api/index HTTP behavior.

Real HTTP (httpx ASGITransport, the test_settings_api.py pattern) over an
in-memory DB. No test touches the real jarvis.db, the real Qdrant, or the
real home folders: file_index.SESSION_FACTORY is pointed at the test factory
and rebuild is a no-op here (no vector store initialized) — we assert the
config store + validation + the background trigger's bookkeeping.
"""
from pathlib import Path

import httpx
import pytest_asyncio
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import file_index
from app.core.dependencies import get_db
from app.db.database import Base
from main import app


@pytest_asyncio.fixture
async def client(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    monkeypatch.setattr("app.db.database.AsyncSessionLocal", factory)
    monkeypatch.setattr(file_index, "SESSION_FACTORY", factory)

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
    await file_index.wait_for_index()
    await engine.dispose()


async def test_get_defaults(client):
    r = await client.get("/api/index")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False          # opt-in
    assert isinstance(body["folders"], list)
    assert body["status"]["indexed_files"] == 0


async def test_put_config_persists(client, tmp_path):
    r = await client.put("/api/index/config", json={
        "enabled": True,
        "folders": [str(tmp_path)],
        "exclusions": [str(tmp_path / "skip")],
        "interval_minutes": 120,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["folders"] == [str(tmp_path)]
    assert body["interval_minutes"] == 120

    # Round-trips on the next GET.
    again = (await client.get("/api/index")).json()
    assert again["exclusions"] == [str(tmp_path / "skip")]


async def test_put_config_rejects_protected_folder(client):
    root = Path.home().anchor or "C:\\"   # filesystem root — never indexable
    r = await client.put("/api/index/config", json={
        "enabled": True, "folders": [root], "exclusions": [], "interval_minutes": 360,
    })
    assert r.status_code == 400


async def test_put_config_clamps_interval(client, tmp_path):
    r = await client.put("/api/index/config", json={
        "enabled": True, "folders": [str(tmp_path)], "exclusions": [], "interval_minutes": 1,
    })
    assert r.json()["interval_minutes"] == file_index_min()


async def test_rebuild_starts_background_pass(client):
    r = await client.post("/api/index/rebuild", json={"full": False})
    assert r.status_code == 200
    assert r.json()["started"] is True


async def test_status_endpoint(client):
    r = await client.get("/api/index/status")
    assert r.status_code == 200
    assert "indexed_files" in r.json()


def file_index_min():
    from app.core.app_settings import FILE_INDEX_MIN_INTERVAL
    return FILE_INDEX_MIN_INTERVAL
