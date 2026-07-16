"""
Phase 8 — /api/context HTTP behavior: settings CRUD + validation, the device
gate (accepted-vs-ignored), the screen-OCR hard gate (403) + the OCR path with
a fake engine, and the /world + /status reads. Real HTTP over an in-memory DB
(the test_index_api.py pattern); no network, no real OCR model.
"""
import httpx
import pytest_asyncio
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import screen_ocr
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


async def _enable(client, **fields):
    body = {
        "enabled": True,
        "device_sensing": True,
        "screen_ocr": True,
        "ocr_interval_seconds": 30,
        "idle_threshold_seconds": 300,
        "affective_sensing": False,
    }
    body.update(fields)
    return await client.put("/api/context/settings", json=body)


# --------------------------------------------------------------- settings

async def test_settings_defaults_opt_in(client):
    r = await client.get("/api/context/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False         # master OFF by default
    assert body["screen_ocr"] is False      # OCR opt-in on top


async def test_settings_put_roundtrip(client):
    r = await _enable(client, idle_threshold_seconds=120)
    assert r.status_code == 200
    assert r.json()["enabled"] is True
    assert r.json()["idle_threshold_seconds"] == 120
    again = await client.get("/api/context/settings")
    assert again.json()["idle_threshold_seconds"] == 120


async def test_settings_roundtrips_affective(client):
    r = await _enable(client, affective_sensing=True)
    assert r.status_code == 200
    assert r.json()["affective_sensing"] is True
    again = await client.get("/api/context/settings")
    assert again.json()["affective_sensing"] is True


async def test_settings_defaults_affective_off(client):
    body = (await client.get("/api/context/settings")).json()
    assert body["affective_sensing"] is False


async def test_settings_put_rejects_bad_interval(client):
    r = await _enable(client, ocr_interval_seconds=1)   # below the floor
    assert r.status_code == 400


async def test_settings_put_rejects_bad_idle(client):
    r = await _enable(client, idle_threshold_seconds=5)  # below the floor
    assert r.status_code == 400


# ----------------------------------------------------------- device gate

async def test_device_stored_when_enabled(client):
    await _enable(client)
    r = await client.post("/api/context/device", json={
        "active_app": "Code.exe", "window_title": "main.py", "idle_seconds": 3,
    })
    assert r.status_code == 200
    assert r.json()["stored"] is True
    world = (await client.get("/api/context/world")).json()
    assert world["active_app"] == "Code.exe"
    assert world["presence"] == "active"


async def test_device_ignored_when_master_off(client):
    await _enable(client, enabled=False)
    r = await client.post("/api/context/device", json={"active_app": "X"})
    assert r.status_code == 200
    assert r.json()["stored"] is False


async def test_device_ignored_when_device_sensing_off(client):
    await _enable(client, device_sensing=False)
    r = await client.post("/api/context/device", json={"active_app": "X"})
    assert r.json()["stored"] is False


async def test_device_truncates_long_strings(client):
    await _enable(client)
    r = await client.post("/api/context/device", json={
        "active_app": "A" * 5000, "window_title": "B" * 5000, "idle_seconds": 1,
    })
    assert r.json()["stored"] is True
    world = (await client.get("/api/context/world")).json()
    assert len(world["active_app"]) <= 256
    assert len(world["window_title"]) <= 512


# ---------------------------------------------------- affective /state gate

async def test_state_stored_and_surfaced_when_enabled(client):
    await _enable(client, affective_sensing=True)
    r = await client.post("/api/context/state", json={
        "typing_cpm": 400, "backspace_rate": 0.05,
    })
    assert r.status_code == 200
    assert r.json()["stored"] is True
    world = (await client.get("/api/context/world")).json()
    assert world["user_state"] is not None
    assert world["user_state"]["load"] == "busy"


async def test_state_ignored_when_affective_off(client):
    await _enable(client, affective_sensing=False)   # master on, affective off
    r = await client.post("/api/context/state", json={"typing_cpm": 400})
    assert r.status_code == 200
    assert r.json()["stored"] is False
    world = (await client.get("/api/context/world")).json()
    assert world["user_state"] is None


async def test_state_ignored_when_master_off(client):
    await _enable(client, enabled=False, affective_sensing=True)
    r = await client.post("/api/context/state", json={"typing_cpm": 400})
    assert r.json()["stored"] is False


async def test_state_clamps_out_of_range(client):
    await _enable(client, affective_sensing=True)
    # A wild backspace_rate is clamped, not rejected — coarse, never a crash.
    r = await client.post("/api/context/state", json={
        "typing_cpm": 300, "backspace_rate": 9.0,
    })
    assert r.status_code == 200
    world = (await client.get("/api/context/world")).json()
    assert world["user_state"]["signals"]["backspace_rate"] == 1.0


# ------------------------------------------------------- screen OCR gate

async def test_screen_403_when_ocr_disabled(client):
    await _enable(client, screen_ocr=False)
    r = await client.post(
        "/api/context/screen",
        files={"file": ("frame.png", b"\x89PNG", "image/png")},
    )
    assert r.status_code == 403


async def test_screen_403_when_master_off(client):
    await _enable(client, enabled=False, screen_ocr=True)
    r = await client.post(
        "/api/context/screen",
        files={"file": ("frame.png", b"\x89PNG", "image/png")},
    )
    assert r.status_code == 403


async def test_screen_ocr_path(client):
    await _enable(client, screen_ocr=True)
    screen_ocr.OCR_ENGINE_FACTORY = lambda: (
        lambda data: "Inbox\n3 unread\nCompose a message"
    )
    r = await client.post(
        "/api/context/screen",
        files={"file": ("frame.png", b"\x89PNGfake", "image/png")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["stored"] is True
    assert "Inbox" in body["summary"]
    world = (await client.get("/api/context/world")).json()
    assert world["on_screen_context"] == body["summary"]


async def test_screen_rejects_empty_frame(client):
    await _enable(client, screen_ocr=True)
    r = await client.post(
        "/api/context/screen",
        files={"file": ("frame.png", b"", "image/png")},
    )
    assert r.status_code == 400


# --------------------------------------------------------------- reads

async def test_status_reflects_flags(client):
    await _enable(client, screen_ocr=False)
    await client.post("/api/context/device", json={"active_app": "X", "idle_seconds": 1})
    status = (await client.get("/api/context/status")).json()
    assert status["enabled"] is True
    assert status["device_sensing"] is True
    assert status["screen_ocr"] is False
    assert status["device_fresh"] is True


async def test_world_dark_when_master_off(client):
    r = await client.get("/api/context/world")   # default master off
    body = r.json()
    assert body["sensing"]["enabled"] is False
    assert body["presence"] == "unknown"
