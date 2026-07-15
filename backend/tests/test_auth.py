"""
API auth token (app/core/auth.py, 2026-07-15).

The conftest autouse _hermetic_auth fixture disables enforcement for the
whole suite; these tests re-enable it explicitly with a known token and a
scratch token path, and exercise the middleware through the REAL app
(TestClient, no lifespan — the push-channel tests' pattern).
"""
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core import auth
from app.core.config import settings
from main import app

TOKEN = "test-token-123"


@pytest.fixture
def auth_enabled(tmp_path, monkeypatch):
    """Enforcement ON with a known token at a scratch path. The autouse
    _hermetic_auth fixture restores the module seams afterwards."""
    monkeypatch.setattr(settings, "API_AUTH_TOKEN", "")
    auth.ENABLED = True
    auth.TOKEN_PATH = tmp_path / "auth_token"
    auth.TOKEN_PATH.write_text(TOKEN, encoding="utf-8")
    auth.reset_auth()
    yield TOKEN
    auth.ENABLED = False
    auth.reset_auth()


# ================================================================ middleware

def test_request_without_token_is_401(auth_enabled):
    client = TestClient(app)
    response = client.get("/api/reminders")
    assert response.status_code == 401
    assert response.json() == {"detail": "Missing or invalid auth token"}


def test_wrong_token_is_401(auth_enabled):
    client = TestClient(app)
    response = client.get("/api/reminders", headers={"X-Jarvis-Token": "nope"})
    assert response.status_code == 401


def test_header_token_passes(auth_enabled):
    client = TestClient(app)
    # /ws/test is DB-free — a clean 200 without touching jarvis.db.
    response = client.post(
        "/ws/test", json={"message": "hi"}, headers={"X-Jarvis-Token": TOKEN}
    )
    assert response.status_code == 200


def test_bearer_token_passes(auth_enabled):
    client = TestClient(app)
    response = client.post(
        "/ws/test", json={"message": "hi"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 200


def test_chat_surface_is_guarded(auth_enabled):
    """The chat routes live OUTSIDE /api — the middleware must cover them."""
    client = TestClient(app)
    response = client.post("/chat/stream", json={"messages": []})
    assert response.status_code == 401


def test_health_is_exempt(auth_enabled):
    client = TestClient(app)
    assert client.get("/health").status_code == 200


def test_cors_preflight_is_exempt(auth_enabled):
    """A preflight OPTIONS carries no custom headers by design — it must
    reach CORSMiddleware, not die on a 401."""
    client = TestClient(app)
    response = client.options(
        "/api/reminders",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-jarvis-token",
        },
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin")


def test_disabled_flag_waves_requests_through():
    """The conftest default: ENABLED=False means no enforcement (and the
    token file is never created as a side effect of a request)."""
    client = TestClient(app)
    response = client.post("/ws/test", json={"message": "hi"})
    assert response.status_code == 200
    assert not auth.TOKEN_PATH.exists()


# ================================================================ websocket

def test_ws_without_token_is_rejected_4401(auth_enabled):
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws"):
            pass
    assert exc_info.value.code == 4401


def test_ws_with_wrong_token_is_rejected(auth_enabled):
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws?token=nope"):
            pass
    assert exc_info.value.code == 4401


def test_ws_with_token_connects(auth_enabled):
    client = TestClient(app)
    with client.websocket_connect(f"/ws?token={TOKEN}") as websocket:
        hello = websocket.receive_json()
        assert hello["type"] == "connected"


# ============================================================ token lifecycle

def test_generate_persists_and_rereads(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "API_AUTH_TOKEN", "")
    auth.TOKEN_PATH = tmp_path / "auth_token"
    auth.reset_auth()

    first = auth.get_or_create_token()
    assert first and auth.TOKEN_PATH.read_text(encoding="utf-8").strip() == first
    # No torn temp file left behind by the atomic write.
    assert not auth.TOKEN_PATH.with_name("auth_token.tmp").exists()

    auth.reset_auth()  # a "restart" re-reads the same token
    assert auth.get_or_create_token() == first


def test_settings_override_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "API_AUTH_TOKEN", "from-env")
    auth.TOKEN_PATH = tmp_path / "auth_token"
    auth.TOKEN_PATH.write_text("from-file", encoding="utf-8")
    auth.reset_auth()
    assert auth.get_or_create_token() == "from-env"


def test_existing_file_is_never_regenerated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "API_AUTH_TOKEN", "")
    auth.TOKEN_PATH = tmp_path / "auth_token"
    auth.TOKEN_PATH.write_text("stable-token\n", encoding="utf-8")
    auth.reset_auth()
    assert auth.get_or_create_token() == "stable-token"
