"""
Phase 5 Part 6 — /api/settings/briefing HTTP behavior.

Exercised over real HTTP (httpx ASGITransport, the test_contacts_api.py
pattern). A throwaway in-memory scheduler is wired into every module that
references the app-wide `scheduler` by name (daily_briefing + the settings
router both `from app.core.scheduler import scheduler`), and app_settings +
scheduled_jobs share the request DB so the pointer and the job stay consistent.
No test touches the real jarvis.db, arms a real timer, or hits an LLM/Google.
"""
import httpx
import pytest_asyncio
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.dependencies import get_db
from app.core.scheduler import JarvisScheduler
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
    sched = JarvisScheduler(session_factory=factory)

    import app.api.settings as settings_router
    import app.core.daily_briefing as briefing
    import app.core.scheduler as scheduler_module

    monkeypatch.setattr("app.db.database.AsyncSessionLocal", factory)
    monkeypatch.setattr(briefing, "scheduler", sched)
    monkeypatch.setattr(settings_router, "scheduler", sched)
    monkeypatch.setattr(scheduler_module, "scheduler", sched)
    sched.register_handler(briefing.BRIEFING_JOB_KIND, briefing._briefing_job_handler)

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
        c._sched = sched  # expose for job assertions
        yield c
    app.dependency_overrides.clear()
    await sched.shutdown()
    await engine.dispose()


async def _pending(client):
    return await client._sched.list_jobs(status="pending", kind="daily_briefing", limit=100)


async def test_get_defaults_to_on_at_0800(client):
    r = await client.get("/api/settings/briefing")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["time"] == "08:00"


async def test_put_updates_and_arms_the_job(client):
    r = await client.put("/api/settings/briefing", json={"enabled": True, "time": "09:30"})
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True and body["time"] == "09:30"
    assert body["next_run_at"] is not None
    assert len(await _pending(client)) == 1


async def test_put_rejects_bad_time(client):
    r = await client.put("/api/settings/briefing", json={"enabled": True, "time": "9am"})
    assert r.status_code == 400
    assert "HH:MM" in r.json()["detail"]


async def test_put_rejects_out_of_range_time(client):
    r = await client.put("/api/settings/briefing", json={"enabled": True, "time": "24:00"})
    assert r.status_code == 400


async def test_put_disabled_cancels_the_job(client):
    await client.put("/api/settings/briefing", json={"enabled": True, "time": "08:00"})
    assert len(await _pending(client)) == 1
    r = await client.put("/api/settings/briefing", json={"enabled": False, "time": "08:00"})
    assert r.status_code == 200
    assert r.json()["next_run_at"] is None
    assert await _pending(client) == []


async def test_run_now_delivers(client):
    # Empty slate (no Google, no data) → the deterministic empty briefing, no LLM.
    r = await client.post("/api/settings/briefing/run-now")
    assert r.status_code == 200
    body = r.json()
    assert body["delivered"] is True
    assert isinstance(body["message"], str) and body["message"]
