"""
Jarvis OS — Google Integration Foundation (Phase 5, Part 1)

OAuth 2.0 for installed apps (loopback redirect on 127.0.0.1) plus local
token storage. This module is the ONLY place that reads or writes Google
credentials; email/calendar tools and the daily briefing consume them via
app/integrations/google_services.py.

Security rules, all enforced in code:
- Tokens live OUTSIDE the repo (~/.jarvis/google_token.json by default),
  written atomically with owner-only permissions, and NEVER logged — log
  lines carry exception class names and account email at most, never token
  material.
- Scopes are least-privilege and frozen at module level (gmail readonly/
  compose/send + calendar.events — never gmail.modify or full mail: Jarvis
  reads, drafts, and sends; it does not delete or relabel mail). A stored
  token missing ANY required scope counts as NOT connected — re-consent,
  never silent partial capability.
- A missing/revoked/unrefreshable token degrades to GoogleNotConnectedError
  with a stable user-facing message. Dependent features catch it and fail
  clean ("Google account not connected"), never crash — the graceful-
  degradation contract: no Google account is a normal state, not an error
  state. Ambiguity is never destructive: a failed refresh raises, it does
  not delete the token file (the failure may be transient network trouble).
- The consent flow runs in a worker thread (run_local_server blocks) with a
  hard timeout, one flow at a time; the loopback server binds 127.0.0.1
  only, matching the backend's own bind rule.

The module-level AUTH_MANAGER indirection follows the SESSION_FACTORY
pattern (memory_tools/task_runner): tests point it at a scratch token path
— a conftest autouse fixture guarantees the suite never touches the real
~/.jarvis token or the network.
"""
import asyncio
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from loguru import logger

from app.core.config import settings

# Least-privilege, requested once. Adding a scope later means every user
# reconnects — deliberate: new capability requires new consent.
SCOPES: tuple = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
)

# The consent flow can't be killed once started (blocking thread), so it must
# time itself out — otherwise an abandoned browser tab wedges connect forever.
OAUTH_FLOW_TIMEOUT_SECONDS = 300

_REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"

NOT_CONNECTED_MESSAGE = (
    "Google account not connected — connect it from Settings first."
)
RECONNECT_MESSAGE = (
    "Google session expired or was revoked — reconnect it from Settings."
)

# Key we add next to the credential fields in the token file so the status
# endpoint can show "Connected as x@gmail.com" without a network call.
_ACCOUNT_EMAIL_KEY = "_account_email"


class GoogleNotConnectedError(Exception):
    """No usable Google credentials. Message is stable and user-facing —
    tools surface it verbatim as a clean ToolResult error."""

    def __init__(self, detail: str = NOT_CONNECTED_MESSAGE) -> None:
        super().__init__(detail)


def _default_token_path() -> Path:
    if settings.GOOGLE_TOKEN_PATH:
        return Path(settings.GOOGLE_TOKEN_PATH).expanduser()
    return Path.home() / ".jarvis" / "google_token.json"


async def _revoke_token(token: str) -> bool:
    """Best-effort revocation at Google. Module-level so tests can stub the
    network away; never raises."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                _REVOKE_ENDPOINT,
                params={"token": token},
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
        return response.status_code == 200
    except Exception as e:
        logger.warning(
            f"Google token revoke failed ({type(e).__name__}) — "
            "deleting the local token anyway"
        )
        return False


class GoogleAuthManager:
    """Owns token load/refresh/store and the connect/disconnect lifecycle."""

    def __init__(self, token_path: Optional[Path] = None) -> None:
        self._token_path = Path(token_path) if token_path else _default_token_path()
        # Guards _connecting across the event loop and the flow thread.
        self._state_lock = threading.Lock()
        self._connecting = False
        self._last_error: Optional[str] = None
        # Keeps the fire-and-forget flow task referenced (the _RUNNING rule
        # from task_runner: a bare create_task can be garbage-collected).
        self._flow_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------ properties

    @property
    def token_path(self) -> Path:
        return self._token_path

    @property
    def is_configured(self) -> bool:
        return bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET)

    @property
    def is_connecting(self) -> bool:
        with self._state_lock:
            return self._connecting

    # ---------------------------------------------------------- token file

    def _load_token_data(self) -> Optional[Dict[str, Any]]:
        """Parsed token file, or None. Corrupt file = not connected, never a
        crash (and never deleted here — diagnosis beats destruction)."""
        try:
            raw = self._token_path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("Google token file is unreadable — treating as not connected")
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _scopes_ok(data: Dict[str, Any]) -> bool:
        granted = data.get("scopes") or []
        return set(SCOPES).issubset(set(granted))

    def _save_credentials(self, creds: Any, account_email: Optional[str]) -> None:
        """Atomic write (temp + os.replace) with owner-only permissions —
        a crash mid-write must never leave a torn token file."""
        data = json.loads(creds.to_json())
        if account_email:
            data[_ACCOUNT_EMAIL_KEY] = account_email
        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._token_path.with_name(self._token_path.name + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)  # best-effort on Windows, meaningful elsewhere
        except OSError:
            pass
        os.replace(tmp, self._token_path)

    # -------------------------------------------------------------- status

    def status(self) -> Dict[str, Any]:
        """Purely local — reads config + the token file, never the network,
        so the UI can poll it freely."""
        data = self._load_token_data()
        connected = bool(
            data and data.get("refresh_token") and self._scopes_ok(data)
        )
        detail: Optional[str] = None
        if not self.is_configured:
            detail = (
                "Google OAuth is not configured — set GOOGLE_CLIENT_ID and "
                "GOOGLE_CLIENT_SECRET in .env, then restart the backend."
            )
        elif data and not connected:
            detail = (
                "The stored Google token is missing required permissions — "
                "reconnect to grant them."
            )
        elif self._last_error and not connected:
            detail = f"Last connection attempt failed: {self._last_error}"
        return {
            "configured": self.is_configured,
            "connected": connected,
            "connecting": self.is_connecting,
            "account_email": (data or {}).get(_ACCOUNT_EMAIL_KEY) if connected else None,
            "scopes": list(SCOPES),
            "detail": detail,
        }

    # --------------------------------------------------------- credentials

    async def get_credentials(self) -> Any:
        """Usable (silently refreshed) credentials, or GoogleNotConnectedError.
        The single choke point every Google API call goes through."""
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        data = self._load_token_data()
        if not data or not data.get("refresh_token") or not self._scopes_ok(data):
            raise GoogleNotConnectedError()

        creds = Credentials.from_authorized_user_info(data, scopes=list(SCOPES))
        if not creds.valid:
            try:
                # Network I/O — off the event loop.
                await asyncio.to_thread(creds.refresh, Request())
            except Exception as e:
                # Revoked, expired-beyond-refresh, or transient network — the
                # caller can't act on the difference, and deleting the file on
                # a maybe-transient failure would be destructive. Degrade.
                logger.warning(f"Google token refresh failed: {type(e).__name__}")
                raise GoogleNotConnectedError(RECONNECT_MESSAGE) from e
            self._save_credentials(creds, account_email=data.get(_ACCOUNT_EMAIL_KEY))
        return creds

    # ------------------------------------------------------------- connect

    async def start_connect(self) -> str:
        """Kick off the loopback consent flow in a worker thread.
        Returns a status string: "pending" (flow started — poll /status),
        "already_connected", "in_progress", or "not_configured"."""
        if not self.is_configured:
            return "not_configured"
        if self.status()["connected"]:
            return "already_connected"
        with self._state_lock:
            if self._connecting:
                return "in_progress"
            self._connecting = True
        self._flow_task = asyncio.create_task(asyncio.to_thread(self._run_flow_sync))
        return "pending"

    def _run_flow_sync(self) -> None:
        """Blocking consent flow — worker thread only. Owns clearing the
        _connecting flag; swallows every failure into _last_error (an
        abandoned or denied consent is a normal outcome, not a crash)."""
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow

            client_config = {
                "installed": {
                    "client_id": settings.GOOGLE_CLIENT_ID,
                    "client_secret": settings.GOOGLE_CLIENT_SECRET,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            }
            flow = InstalledAppFlow.from_client_config(client_config, scopes=list(SCOPES))
            creds = flow.run_local_server(
                host="127.0.0.1",  # loopback only — the backend's own bind rule
                port=0,  # ephemeral port; never collides with the backend
                open_browser=True,
                authorization_prompt_message="",
                success_message=(
                    "Jarvis is connected to your Google account — "
                    "you can close this tab."
                ),
                timeout_seconds=OAUTH_FLOW_TIMEOUT_SECONDS,
            )
            account_email = self._fetch_account_email(creds)
            self._save_credentials(creds, account_email=account_email)
            self._last_error = None
            logger.info(
                "✅ Google account connected"
                + (f" ({account_email})" if account_email else "")
            )
        except Exception as e:
            # Exception text here is flow plumbing (timeout, denied consent,
            # port trouble) — no token material.
            self._last_error = f"{type(e).__name__}: {e}"
            logger.warning(f"Google connect flow did not complete: {self._last_error}")
        finally:
            with self._state_lock:
                self._connecting = False

    @staticmethod
    def _fetch_account_email(creds: Any) -> Optional[str]:
        """Which account got connected — fetched ONCE at connect time (via
        gmail.readonly, no extra scopes) so status() stays network-free."""
        try:
            from googleapiclient.discovery import build

            service = build("gmail", "v1", credentials=creds, cache_discovery=False)
            profile = service.users().getProfile(userId="me").execute()
            return profile.get("emailAddress")
        except Exception as e:
            logger.warning(f"Could not fetch Google account email: {type(e).__name__}")
            return None

    # ---------------------------------------------------------- disconnect

    async def disconnect(self) -> Dict[str, bool]:
        """Revoke at Google (best-effort) and delete the local token. The
        local delete ALWAYS happens — a revoke outage must not trap the user
        in a connected state they asked to leave."""
        data = self._load_token_data()
        revoked = False
        token = (data or {}).get("refresh_token") or (data or {}).get("token")
        if token:
            revoked = await _revoke_token(token)
        try:
            self._token_path.unlink(missing_ok=True)
            disconnected = True
        except OSError as e:
            logger.warning(f"Could not delete Google token file: {type(e).__name__}")
            disconnected = False
        self._last_error = None
        return {"disconnected": disconnected, "revoked": revoked}


# Indirection so tests (and a future multi-account world) can swap the
# manager. Resolved at call time, never at import time — the SESSION_FACTORY
# pattern.
AUTH_MANAGER: Optional[GoogleAuthManager] = None


def auth_manager() -> GoogleAuthManager:
    global AUTH_MANAGER
    if AUTH_MANAGER is None:
        AUTH_MANAGER = GoogleAuthManager()
    return AUTH_MANAGER
