"""
Phase 5 Part 1 — Integrations API over real HTTP (httpx ASGITransport
against the actual FastAPI app, the test_agent_api pattern). The conftest
autouse fixture already points the auth manager at a scratch token path, so
these tests exercise the real router + manager with no network and no real
token file.
"""
import json

import httpx
import pytest
import pytest_asyncio

from app.core.config import settings
from app.integrations import google_auth
from app.integrations.google_auth import SCOPES
from main import app


@pytest_asyncio.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _write_fake_token() -> None:
    manager = google_auth.auth_manager()
    manager.token_path.parent.mkdir(parents=True, exist_ok=True)
    manager.token_path.write_text(
        json.dumps(
            {
                "token": "t",
                "refresh_token": "r",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "c",
                "client_secret": "s",
                "scopes": list(SCOPES),
                "_account_email": "user@example.com",
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "client-secret")


@pytest.fixture
def unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "")


async def test_status_shape(client, configured):
    response = await client.get("/api/integrations/google/status")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "configured", "connected", "connecting", "account_email", "scopes", "detail",
    }
    assert body["configured"] is True
    assert body["connected"] is False


async def test_status_connected_shows_account(client, configured):
    _write_fake_token()
    body = (await client.get("/api/integrations/google/status")).json()
    assert body["connected"] is True
    assert body["account_email"] == "user@example.com"


async def test_connect_refused_when_not_configured(client, unconfigured):
    response = await client.post("/api/integrations/google/connect")
    assert response.status_code == 400
    assert "GOOGLE_CLIENT_ID" in response.json()["detail"]


async def test_connect_already_connected(client, configured):
    _write_fake_token()
    response = await client.post("/api/integrations/google/connect")
    assert response.status_code == 200
    assert response.json() == {"status": "already_connected"}


async def test_connect_starts_flow(client, configured, monkeypatch):
    manager = google_auth.auth_manager()

    def _fake_flow():
        try:
            _write_fake_token()
        finally:
            with manager._state_lock:
                manager._connecting = False

    monkeypatch.setattr(manager, "_run_flow_sync", _fake_flow)
    response = await client.post("/api/integrations/google/connect")
    assert response.status_code == 200
    assert response.json() == {"status": "pending"}
    await manager._flow_task
    body = (await client.get("/api/integrations/google/status")).json()
    assert body["connected"] is True


async def test_disconnect_end_to_end(client, configured, monkeypatch):
    _write_fake_token()

    async def _fake_revoke(token: str) -> bool:
        return True

    monkeypatch.setattr(google_auth, "_revoke_token", _fake_revoke)
    response = await client.post("/api/integrations/google/disconnect")
    assert response.status_code == 200
    assert response.json() == {"disconnected": True, "revoked": True}
    body = (await client.get("/api/integrations/google/status")).json()
    assert body["connected"] is False
