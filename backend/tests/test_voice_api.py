"""
Phase 7 Part 1 — /api/settings/voice + /api/voice HTTP behavior.

Exercised over real HTTP (httpx ASGITransport, the test_settings_api.py
pattern). The conftest _hermetic_voice_stt fixture guarantees no test can load
a real whisper model; each test swaps in its own fake factory where needed.
"""
import threading

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core import voice_stt
from app.core.dependencies import get_db
from app.db.database import Base
from main import app


class FakeInfo:
    language = "en"
    duration = 1.2


class FakeSegment:
    def __init__(self, text: str):
        self.text = text


class FakeModel:
    def transcribe(self, audio, **kwargs):
        return iter([FakeSegment("Turn on"), FakeSegment("the lights.")]), FakeInfo()


@pytest.fixture(autouse=True)
def fake_factory():
    voice_stt.STT_MODEL_FACTORY = lambda name: FakeModel()


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


def _upload(data: bytes = b"fake-opus-bytes"):
    return {"file": ("utterance.webm", data, "audio/webm")}


# ------------------------------------------------------- /api/settings/voice


async def test_settings_default_to_disabled(client):
    r = await client.get("/api/settings/voice")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["stt_model"] == "small"
    assert body["review_before_send"] is False
    assert body["stt_status"]["status"] == "not_loaded"
    assert "small" in body["stt_models"]


async def test_put_round_trips_without_kicking_a_load(client):
    r = await client.put("/api/settings/voice", json={
        "enabled": False, "stt_model": "base", "review_before_send": True,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["stt_model"] == "base" and body["review_before_send"] is True
    # Disabled: no download was started.
    assert body["stt_status"]["status"] == "not_loaded"
    r = await client.get("/api/settings/voice")
    assert r.json()["stt_model"] == "base"


async def test_put_round_trips_listen_on_summon(client):
    # Part 5: the field persists, and a PUT that omits it (a Part-3/4-shaped
    # frontend) still succeeds and defaults it OFF.
    r = await client.put("/api/settings/voice", json={
        "enabled": False, "stt_model": "small", "listen_on_summon": True,
    })
    assert r.status_code == 200
    assert r.json()["listen_on_summon"] is True
    r = await client.get("/api/settings/voice")
    assert r.json()["listen_on_summon"] is True
    r = await client.put("/api/settings/voice", json={
        "enabled": False, "stt_model": "small",
    })
    assert r.status_code == 200
    assert r.json()["listen_on_summon"] is False


async def test_put_rejects_unknown_model(client):
    r = await client.put("/api/settings/voice", json={
        "enabled": True, "stt_model": "gigantic-v9",
    })
    assert r.status_code == 400
    assert "stt_model" in r.json()["detail"]


async def test_put_enable_kicks_the_model_load(client):
    r = await client.put("/api/settings/voice", json={"enabled": True, "stt_model": "base"})
    assert r.status_code == 200
    assert r.json()["stt_status"]["status"] in ("loading", "ready")
    await voice_stt.wait_for_load()
    r = await client.get("/api/voice/status")
    body = r.json()
    assert body["enabled"] is True
    assert body["stt"]["status"] == "ready"
    assert body["stt"]["model"] == "base"
    assert body["stt"]["configured_model"] == "base"


# --------------------------------------------------------------- /api/voice


async def test_status_when_disabled(client):
    r = await client.get("/api/voice/status")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["stt"]["status"] == "not_loaded"


async def test_transcribe_refused_when_disabled(client):
    r = await client.post("/api/voice/transcribe", files=_upload())
    assert r.status_code == 400
    assert "disabled" in r.json()["detail"]


async def test_transcribe_returns_text_when_ready(client):
    await client.put("/api/settings/voice", json={"enabled": True, "stt_model": "small"})
    await voice_stt.wait_for_load()
    r = await client.post("/api/voice/transcribe", files=_upload())
    assert r.status_code == 200
    body = r.json()
    assert body["text"] == "Turn on the lights."
    assert body["language"] == "en"
    assert body["duration"] == 1.2


async def test_transcribe_while_model_is_loading_is_409(client):
    release = threading.Event()

    def slow_factory(name: str) -> FakeModel:
        release.wait(timeout=5)
        return FakeModel()

    voice_stt.STT_MODEL_FACTORY = slow_factory
    await client.put("/api/settings/voice", json={"enabled": True, "stt_model": "small"})
    try:
        r = await client.post("/api/voice/transcribe", files=_upload())
        assert r.status_code == 409
        assert "not ready" in r.json()["detail"]
    finally:
        release.set()
        await voice_stt.wait_for_load()
    # Once the load settles, the same request succeeds.
    r = await client.post("/api/voice/transcribe", files=_upload())
    assert r.status_code == 200


async def test_transcribe_empty_audio_is_400(client):
    await client.put("/api/settings/voice", json={"enabled": True, "stt_model": "small"})
    await voice_stt.wait_for_load()
    r = await client.post("/api/voice/transcribe", files=_upload(b""))
    assert r.status_code == 400
    assert "empty" in r.json()["detail"]


async def test_transcribe_decode_failure_is_400_not_500(client):
    class BrokenModel:
        def transcribe(self, audio, **kwargs):
            raise ValueError("could not decode container")

    voice_stt.STT_MODEL_FACTORY = lambda name: BrokenModel()
    await client.put("/api/settings/voice", json={"enabled": True, "stt_model": "small"})
    await voice_stt.wait_for_load()
    r = await client.post("/api/voice/transcribe", files=_upload(b"not-audio"))
    assert r.status_code == 400
    assert "Could not transcribe" in r.json()["detail"]
