"""
Static auth token for the local API (2026-07-15).

The backend binds loopback-only, but loopback is not authorization: any local
process — or a malicious webpage firing simple-request POSTs at
http://127.0.0.1:8000 (CORS hides the *response*, it does not block the
*request*) — could otherwise drive an API that reads email, sends mail as the
user, and runs shell commands behind the approval gate. One static token
closes the class:

- The BACKEND owns the token: generated on first startup, persisted at
  ~/.jarvis/auth_token (atomic write, best-effort 0600 — the google_auth
  token hygiene), stable across restarts so WS reconnects never invalidate.
- Electron reads the file after the backend reports healthy and injects it
  into the renderer as window.__JARVIS_TOKEN__ (the __BACKEND_URL__ pattern).
- AuthMiddleware validates every HTTP request (X-Jarvis-Token header, or
  Authorization: Bearer) and every WebSocket handshake (?token= query param —
  browsers cannot set WS headers). /health stays open (liveness probe;
  Electron polls it BEFORE it can know the token) and OPTIONS passes (CORS
  preflight carries no custom headers by design).

The middleware is pure ASGI, not BaseHTTPMiddleware: it must see the
websocket scope (BaseHTTPMiddleware only wraps http) and must never buffer
the SSE / PCM streaming responses.

Tests: the suite imports the real app from main.py, so a conftest autouse
fixture flips ENABLED off (the hermetic-fixture pattern) — enforcement is
covered explicitly in test_auth.py. Import of this module has NO side
effects; the token file is only touched when a token is actually needed
(lifespan startup, or the first enforced request).
"""
import os
import secrets
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs

from loguru import logger

from app.core.config import settings

# Header the frontend sends on every HTTP call.
TOKEN_HEADER = "x-jarvis-token"

# Module-level seams (tests repoint/flip these — the google_auth pattern).
TOKEN_PATH = Path.home() / ".jarvis" / "auth_token"
ENABLED = True

_cached_token: Optional[str] = None


def reset_auth() -> None:
    """Clear the cached token (tests)."""
    global _cached_token
    _cached_token = None


def get_or_create_token() -> str:
    """The auth token: settings override > persisted file > freshly generated.

    Generation writes the file atomically (temp + os.replace, best-effort
    0600) so a crash mid-write never leaves a torn token, and the same token
    survives restarts — Electron and WS reconnects rely on it being stable.
    """
    global _cached_token
    if _cached_token:
        return _cached_token

    if settings.API_AUTH_TOKEN:
        _cached_token = settings.API_AUTH_TOKEN
        return _cached_token

    try:
        existing = TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        existing = ""
    if existing:
        _cached_token = existing
        return _cached_token

    token = secrets.token_urlsafe(32)
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOKEN_PATH.with_name(TOKEN_PATH.name + ".tmp")
    tmp.write_text(token, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)  # best-effort on Windows, meaningful elsewhere
    except OSError:
        pass
    os.replace(tmp, TOKEN_PATH)
    logger.info(f"🔐 Generated API auth token at {TOKEN_PATH}")
    _cached_token = token
    return _cached_token


def _expected_token() -> Optional[str]:
    """The token requests must present, or None when it cannot be resolved.

    A resolution failure (unreadable disk) FAILS CLOSED — the caller denies
    the request rather than waving everything through."""
    try:
        return get_or_create_token()
    except Exception as e:  # noqa: BLE001 — deny, never crash the request
        logger.error(f"Auth token resolution failed (denying requests): {e}")
        return None


def _header_token(scope: dict) -> str:
    """X-Jarvis-Token, falling back to Authorization: Bearer."""
    bearer = ""
    for name, value in scope.get("headers") or []:
        if name == TOKEN_HEADER.encode():
            return value.decode("latin-1").strip()
        if name == b"authorization":
            text = value.decode("latin-1").strip()
            if text.lower().startswith("bearer "):
                bearer = text[7:].strip()
    return bearer


def _query_token(scope: dict) -> str:
    params = parse_qs((scope.get("query_string") or b"").decode("latin-1"))
    values = params.get("token") or [""]
    return values[0]


def _token_ok(presented: str) -> bool:
    expected = _expected_token()
    if not expected or not presented:
        return False
    return secrets.compare_digest(presented, expected)


class AuthMiddleware:
    """Pure-ASGI static-token gate over the whole app.

    HTTP: X-Jarvis-Token (or Authorization: Bearer) → 401 on mismatch.
    WebSocket: ?token= query param → handshake rejected with 4401.
    Exempt: /health (liveness — polled before the token can be known) and
    OPTIONS (CORS preflight). Everything else — /chat, /memory, /api/*, /ws,
    /docs — requires the token.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket") or not ENABLED:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path == "/health" or path.startswith("/health/"):
            await self.app(scope, receive, send)
            return

        if scope["type"] == "http":
            if scope.get("method") == "OPTIONS":  # CORS preflight
                await self.app(scope, receive, send)
                return
            if _token_ok(_header_token(scope)):
                await self.app(scope, receive, send)
                return
            from starlette.responses import JSONResponse

            response = JSONResponse(
                {"detail": "Missing or invalid auth token"}, status_code=401
            )
            await response(scope, receive, send)
            return

        # WebSocket: consume the connect event, then reject the handshake.
        if _token_ok(_query_token(scope)):
            await self.app(scope, receive, send)
            return
        message = await receive()
        if message["type"] == "websocket.connect":
            await send({"type": "websocket.close", "code": 4401})
