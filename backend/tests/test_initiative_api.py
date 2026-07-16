"""
Phase 9 — /api/initiative HTTP behavior (the test_settings_api.py pattern).

A throwaway in-memory scheduler is wired into every module that references the
app-wide `scheduler` by name (initiative core + the initiative router both
`from app.core.scheduler import scheduler`); app_settings, scheduled_jobs, and
suggestions share the request DB. No test touches the real jarvis.db, arms a
real timer, or hits an LLM/Google.
"""
import httpx
import pytest_asyncio
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import suggestions as sug
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

    import app.api.initiative as initiative_router
    import app.core.initiative as initiative_core
    import app.core.scheduler as scheduler_module

    monkeypatch.setattr("app.db.database.AsyncSessionLocal", factory)
    monkeypatch.setattr(initiative_core, "scheduler", sched)
    monkeypatch.setattr(initiative_router, "scheduler", sched)
    monkeypatch.setattr(scheduler_module, "scheduler", sched)
    sched.register_handler(
        initiative_core.INITIATIVE_JOB_KIND, initiative_core._initiative_job_handler
    )

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
        c._sched = sched
        c._factory = factory
        yield c
    app.dependency_overrides.clear()
    await sched.shutdown()
    await engine.dispose()


async def _pending(client):
    return await client._sched.list_jobs(status="pending", kind="initiative", limit=100)


# ------------------------------------------------------------------ settings

async def test_get_defaults_off_ask(client):
    r = await client.get("/api/initiative/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["autonomy"] == "ask"
    assert "act" in body["autonomy_levels"]


async def test_put_enables_and_arms_job(client):
    r = await client.put("/api/initiative/settings", json={
        "enabled": True, "autonomy": "ask", "interval_minutes": 30, "daily_budget": 4,
        "quiet_start_hour": 22, "quiet_end_hour": 8, "min_gap_minutes": 20,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True and body["interval_minutes"] == 30
    assert body["next_run_at"] is not None
    assert len(await _pending(client)) == 1


async def test_put_rejects_bad_autonomy(client):
    r = await client.put("/api/initiative/settings", json={
        "enabled": True, "autonomy": "yolo", "interval_minutes": 30, "daily_budget": 4,
        "quiet_start_hour": 22, "quiet_end_hour": 8, "min_gap_minutes": 20,
    })
    assert r.status_code == 400
    assert "autonomy" in r.json()["detail"]


async def test_put_clamps_out_of_range_interval(client):
    # interval below the floor (15) is clamped, not rejected.
    r = await client.put("/api/initiative/settings", json={
        "enabled": True, "autonomy": "ask", "interval_minutes": 1, "daily_budget": 4,
        "quiet_start_hour": 22, "quiet_end_hour": 8, "min_gap_minutes": 20,
    })
    assert r.status_code == 200
    assert r.json()["interval_minutes"] == 15


async def test_put_disabled_cancels_job(client):
    await client.put("/api/initiative/settings", json={
        "enabled": True, "autonomy": "ask", "interval_minutes": 30, "daily_budget": 4,
        "quiet_start_hour": 22, "quiet_end_hour": 8, "min_gap_minutes": 20,
    })
    assert len(await _pending(client)) == 1
    r = await client.put("/api/initiative/settings", json={
        "enabled": False, "autonomy": "ask", "interval_minutes": 30, "daily_budget": 4,
        "quiet_start_hour": 22, "quiet_end_hour": 8, "min_gap_minutes": 20,
    })
    assert r.status_code == 200
    assert r.json()["next_run_at"] is None
    assert await _pending(client) == []


# --------------------------------------------------------------- suggestions

async def test_list_suggestions(client):
    async with client._factory() as db:
        await sug.create_suggestion(db, category="general", title="A", body="do a")
    r = await client.get("/api/initiative/suggestions")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1 and rows[0]["title"] == "A"


async def test_list_filters_by_status(client):
    async with client._factory() as db:
        await sug.create_suggestion(db, category="general", title="P", body="x")
    r = await client.get("/api/initiative/suggestions?status=dismissed")
    assert r.status_code == 200 and r.json() == []


async def test_accept_endpoint(client):
    async with client._factory() as db:
        row = await sug.create_suggestion(db, category="calendar_prep", title="A", body="x")
    r = await client.post(f"/api/initiative/suggestions/{row.id}/accept")
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"


async def test_dismiss_endpoint(client):
    async with client._factory() as db:
        row = await sug.create_suggestion(db, category="file_cleanup", title="A", body="x")
    r = await client.post(f"/api/initiative/suggestions/{row.id}/dismiss")
    assert r.status_code == 200
    assert r.json()["status"] == "dismissed"


async def test_accept_missing_is_404(client):
    r = await client.post("/api/initiative/suggestions/nope/accept")
    assert r.status_code == 404


async def test_dismiss_already_settled_is_404(client):
    async with client._factory() as db:
        row = await sug.create_suggestion(db, category="general", title="A", body="x")
        await sug.dismiss_suggestion(db, row.id)
    r = await client.post(f"/api/initiative/suggestions/{row.id}/dismiss")
    assert r.status_code == 404


# ------------------------------------------------------------------- run-now

async def test_run_now_empty_slate(client):
    # No Google, no data → nothing surfaced, no LLM call.
    r = await client.post("/api/initiative/run-now")
    assert r.status_code == 200
    assert r.json()["surfaced"] == 0
