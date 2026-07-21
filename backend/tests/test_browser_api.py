"""
Phase 14 — /api/browser HTTP behavior: the media stop-control surface and the
one-time sign-in flow. Real HTTP over an in-memory DB (the test_context_api.py
pattern); the browser is never actually launched — open_login is patched, and
the media registry is exercised through the in-memory fakes.
"""
import httpx
import pytest_asyncio
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import browser_session
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


async def test_media_status_empty(client):
    resp = await client.get("/api/browser/media")
    assert resp.status_code == 200
    assert resp.json() == {
        "playing": False,
        "title": "",
        "url": "",
        "window_open": False,
        "window_title": "",
        "window_url": "",
    }


async def test_stop_media_idempotent(client):
    resp = await client.post("/api/browser/stop-media")
    assert resp.status_code == 200
    assert resp.json() == {"stopped": False}


async def test_close_window_idempotent(client):
    """Closing a result window when none is open is fine, not an error."""
    resp = await client.post("/api/browser/close-window")
    assert resp.status_code == 200
    assert resp.json() == {"closed": False}


async def test_media_status_reports_an_open_result_window(client):
    """A kept-open commit result window (14.6) surfaces in the same status the
    StatusBar polls, and POST /close-window clears it."""

    class _FakeWindow:
        async def close(self):
            pass

    await browser_session.register_result_window(
        _FakeWindow(), title="File Uploaded!", url="https://the-internet.herokuapp.com/upload"
    )
    try:
        status = (await client.get("/api/browser/media")).json()
        assert status["window_open"] is True
        assert status["window_title"] == "File Uploaded!"
        assert status["window_url"] == "https://the-internet.herokuapp.com/upload"

        closed = (await client.post("/api/browser/close-window")).json()
        assert closed == {"closed": True}
        assert browser_session.active_result_window() is None
    finally:
        await browser_session.close_result_window()


async def test_account_status_reports_login_closed(client):
    resp = await client.get("/api/browser/account")
    assert resp.status_code == 200
    assert resp.json() == {"login_open": False}


async def test_login_opens_window(client, monkeypatch):
    calls = {}

    async def _fake_open(url):
        calls["url"] = url

    monkeypatch.setattr(browser_session, "open_login_window", _fake_open)
    resp = await client.post("/api/browser/login", json={})
    assert resp.status_code == 200
    assert resp.json() == {"login_open": True}
    assert calls["url"] == browser_session.DEFAULT_LOGIN_URL


async def test_login_passes_through_a_custom_url(client, monkeypatch):
    calls = {}

    async def _fake_open(url):
        calls["url"] = url

    monkeypatch.setattr(browser_session, "open_login_window", _fake_open)
    resp = await client.post("/api/browser/login", json={"url": "https://www.youtube.com"})
    assert resp.status_code == 200
    assert calls["url"] == "https://www.youtube.com"


async def test_login_reports_503_when_no_browser(client, monkeypatch):
    async def _unavailable(url):
        raise browser_session.BrowserUnavailable("Install Playwright")

    monkeypatch.setattr(browser_session, "open_login_window", _unavailable)
    resp = await client.post("/api/browser/login", json={})
    assert resp.status_code == 503
    assert "Playwright" in resp.json()["detail"]


async def test_close_login_idempotent(client):
    resp = await client.post("/api/browser/close-login")
    assert resp.status_code == 200
    assert resp.json() == {"closed": False}


# ----------------------------------------------------- vision fallback (15.3)
async def test_vision_defaults_off(client):
    """The DOM-first vision fallback is opt-in — off before the user touches it."""
    resp = await client.get("/api/browser/vision")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert "configured" in body and "provider" in body and "model" in body


async def test_vision_put_round_trips(client, monkeypatch):
    """PUT flips the toggle and persists; GET reflects it. `configured` mirrors
    whether a key is present in .env (forced True here so the state is stable)."""
    from app.api import browser as browser_api

    monkeypatch.setattr(browser_api, "_vision_configured", lambda: True)

    put = await client.put("/api/browser/vision", json={"enabled": True})
    assert put.status_code == 200
    assert put.json()["enabled"] is True
    assert put.json()["configured"] is True

    got = await client.get("/api/browser/vision")
    assert got.json()["enabled"] is True

    # And back off.
    off = await client.put("/api/browser/vision", json={"enabled": False})
    assert off.json()["enabled"] is False
