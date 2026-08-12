"""
Furi OS — Home Assistant integration (Feature 1: Home & IoT control)

The ONLY module that holds the Home Assistant credential and the only place
that speaks HTTP to the hub. `app/tools/home_tools.py` goes through
`get_home_client()`; nothing else does.

Why Home Assistant rather than per-vendor SDKs
----------------------------------------------
One self-hosted HTTP API covers ~2000 device brands (Hue, LIFX, Tuya, Z-Wave,
Zigbee, Nest, Sonos, TP-Link…). A long-lived access token, a REST call, done.
Integrating vendors one at a time would mean N OAuth flows, N token stores and
N failure modes for one capability. HA also exposes SCENES, which map onto the
routines Furi already has.

Degradation contract (the google_services rule)
-----------------------------------------------
A missing/invalid/unreachable hub raises `HomeNotConnectedError` from ONE choke
point — `get_home_client()` — with a stable, user-facing message. Every tool
catches it and returns a clean failed ToolResult. No hub configured is a NORMAL
state, not an error state: Furi says so and carries on.

⚠️ WHY THERE IS NO SSRF EXEMPTION HERE, AND WHY THAT IS THE SAFER DESIGN
------------------------------------------------------------------------
A Home Assistant hub lives on the LAN (`http://homeassistant.local:8123`,
`http://192.168.1.x:8123`), which `browser_tools._host_is_blocked` refuses by
design. The obvious move is to punch a hole in that guard. We do NOT, because
the guard is not in this path at all and adding an exemption to it would weaken
the web tools for no benefit here.

What actually bounds this client is stricter: **the base URL can only ever come
from the user's own configuration, never from a model.** No tool in
`home_tools.py` accepts a URL, a host, or a path — they take an `entity_id` and
nothing else, and the URL is composed in code from the stored config. So there
is no reachable path by which a plan, a web page, an email or a page of screen
text can aim this client anywhere. `validate_base_url()` (called at config-set
time, from the API layer) is the one gate, and it runs against a value a human
typed.

Nothing here is cached across a config change: `reset_home_client()` drops the
client and the entity cache, and the settings PUT calls it.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import httpx
from loguru import logger

from app.core.config import settings

# ------------------------------------------------------------------ messages

NOT_CONNECTED_MESSAGE = (
    "Home Assistant is not connected. Add your hub's address and a long-lived "
    "access token in Settings → Home & devices, then try again."
)

# ------------------------------------------------------------------ limits

REQUEST_TIMEOUT_SECONDS = 10.0
# A hub with hundreds of entities is normal; a tool result that lists all of
# them is not. Reads clip, and say they clipped.
STATES_MAX_ROWS = 200
# The entity list changes rarely — a short cache keeps list_devices cheap when a
# plan reads it, filters it, then reads it again on a revise round.
ENTITY_CACHE_SECONDS = 60.0

# Domains this integration will talk to at all. Everything outside this set is
# refused before a request is built — see `_DOMAIN_SERVICES` below for why the
# allowlist is the safety property rather than a convenience.
SUPPORTED_DOMAINS = (
    "light", "switch", "fan", "lock", "cover", "climate", "media_player",
    "input_boolean", "scene", "script", "binary_sensor", "sensor",
    "vacuum", "humidifier", "water_heater",
)


class HomeNotConnectedError(Exception):
    """No usable Home Assistant configuration or the hub is unreachable.
    Message is stable and user-facing — tools surface it verbatim."""

    def __init__(self, detail: str = NOT_CONNECTED_MESSAGE) -> None:
        super().__init__(detail)


class HomeApiError(Exception):
    """The hub answered, but not with success. Carries a clean message —
    never a raw traceback or a token."""


# ------------------------------------------------------------------ token I/O

def _default_token_path() -> Path:
    """`~/.jarvis/home_token.json` — outside the repo, beside the Google token
    and the auth token. `HOME_ASSISTANT_TOKEN_PATH` overrides (tests)."""
    override = getattr(settings, "HOME_ASSISTANT_TOKEN_PATH", "")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".jarvis" / "home_token.json"


# Module-level so tests can point it at a scratch path (the AUTH_MANAGER
# convention). Resolved at call time, never at import time.
TOKEN_PATH: Optional[Path] = None


def _token_path() -> Path:
    return TOKEN_PATH if TOKEN_PATH is not None else _default_token_path()


def read_token() -> str:
    """The stored long-lived access token, or "" when absent/corrupt.

    A corrupt file reads as absent — never a crash (the `get_setting`
    discipline). `.env` is consulted as a fallback so a scripted/dev install can
    set `HOME_ASSISTANT_TOKEN` without going through the UI.
    """
    path = _token_path()
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            token = str(data.get("access_token") or "").strip()
            if token:
                return token
    except Exception as e:  # noqa: BLE001 — a bad file is "not connected"
        logger.warning(f"Home Assistant token file unreadable ({type(e).__name__})")
    return str(getattr(settings, "HOME_ASSISTANT_TOKEN", "") or "").strip()


def write_token(token: str) -> None:
    """Persist the token atomically with owner-only permissions (the
    google_auth hygiene). An empty token DELETES the file — that is how
    disconnect works, and it must not leave a stale credential behind."""
    path = _token_path()
    token = str(token or "").strip()
    if not token:
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning(f"Could not remove Home Assistant token: {type(e).__name__}")
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".home_token", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"access_token": token}, fh)
        try:
            os.chmod(tmp, 0o600)  # best-effort on Windows, meaningful elsewhere
        except OSError:
            pass
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def has_token() -> bool:
    return bool(read_token())


# ------------------------------------------------------------------ base URL

def validate_base_url(raw: str) -> str:
    """Normalize and validate a user-typed hub address, or raise ValueError.

    The ONE gate on where this client can point. It runs against a value a
    human typed in Settings — never against anything a model produced — which
    is why a LAN address is acceptable here and would not be in `browser_tools`.
    Returns the URL with any trailing slash removed so path joins are simple.
    """
    text = str(raw or "").strip()
    if not text:
        raise ValueError("Hub address is required, e.g. http://homeassistant.local:8123")
    if "://" not in text:
        text = f"http://{text}"  # bare host/IP is the common case
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"Hub address must start with http:// or https:// — got '{parsed.scheme}://'"
        )
    if not parsed.hostname:
        raise ValueError(f"Hub address has no host: '{raw}'")
    if parsed.query or parsed.fragment:
        raise ValueError("Hub address must be a plain base URL, with no query or fragment")
    return text.rstrip("/")


# ------------------------------------------------------------------ the client

@dataclass(frozen=True)
class Device:
    """One entity, flattened to what a person (and an approval card) needs."""
    entity_id: str
    name: str
    domain: str
    state: str
    area: str
    attributes: dict


def _friendly_name(entity_id: str, attributes: dict) -> str:
    name = str(attributes.get("friendly_name") or "").strip()
    if name:
        return name
    # `light.kitchen_main` → "Kitchen Main" — a readable last resort.
    tail = entity_id.split(".", 1)[-1]
    return tail.replace("_", " ").strip().title() or entity_id


def domain_of(entity_id: str) -> str:
    return str(entity_id or "").split(".", 1)[0].strip().lower()


class HomeAssistantClient:
    """A thin async REST client over the Home Assistant API.

    Deliberately small: `states`, `state`, and `call_service`. There is no
    generic request method taking a caller-supplied path, because that is the
    escape hatch this module exists to not have.
    """

    def __init__(self, base_url: str, token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._client: Optional[httpx.AsyncClient] = None
        self._entities: Optional[list[Device]] = None
        self._entities_at = 0.0

    # -- lifecycle ---------------------------------------------------------

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=REQUEST_TIMEOUT_SECONDS,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001 — closing must never raise
                pass
            self._client = None

    # -- transport ---------------------------------------------------------

    async def _request(self, method: str, path: str, *, json_body: Any = None) -> Any:
        """One API call. Path is composed in code by the callers below — never
        by a caller outside this module, and never from tool parameters."""
        try:
            response = await self._http().request(method, path, json=json_body)
        except httpx.HTTPError as e:
            # Unreachable hub reads as "not connected" rather than an API error:
            # from the user's chair a hub that is off and a hub never configured
            # are the same situation, and the message names the fix.
            raise HomeNotConnectedError(
                f"Couldn't reach Home Assistant at {self._base_url} "
                f"({type(e).__name__}). Check the hub is on and the address is right."
            ) from e

        if response.status_code in (401, 403):
            raise HomeNotConnectedError(
                "Home Assistant rejected the access token. Create a new "
                "long-lived access token and paste it in Settings → Home & devices."
            )
        if response.status_code == 404:
            raise HomeApiError("Home Assistant did not recognise that entity or service.")
        if response.status_code >= 400:
            raise HomeApiError(
                f"Home Assistant API error (HTTP {response.status_code}): "
                f"{response.text[:200]}"
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    # -- reads -------------------------------------------------------------

    async def ping(self) -> str:
        """Verify the hub answers and the token works. Returns its version, or
        "" when it did not report one. Raises the usual errors otherwise."""
        body = await self._request("GET", "/api/config")
        if isinstance(body, dict):
            return str(body.get("version") or "")
        return ""

    async def states(self, *, force: bool = False) -> list[Device]:
        """Every entity the hub exposes, in a flattened shape. Cached briefly —
        a plan commonly reads, filters, then reads again on a revise round."""
        now = time.monotonic()
        if (
            not force
            and self._entities is not None
            and (now - self._entities_at) < ENTITY_CACHE_SECONDS
        ):
            return self._entities

        raw = await self._request("GET", "/api/states")
        devices: list[Device] = []
        for row in raw or []:
            if not isinstance(row, dict):
                continue
            entity_id = str(row.get("entity_id") or "").strip()
            if not entity_id or "." not in entity_id:
                continue
            attributes = row.get("attributes")
            attributes = attributes if isinstance(attributes, dict) else {}
            devices.append(
                Device(
                    entity_id=entity_id,
                    name=_friendly_name(entity_id, attributes),
                    domain=domain_of(entity_id),
                    state=str(row.get("state") or "unknown"),
                    area=str(attributes.get("area") or "").strip(),
                    attributes=attributes,
                )
            )
        self._entities = devices
        self._entities_at = now
        return devices

    async def state(self, entity_id: str) -> Optional[Device]:
        """One entity's live state, straight from the hub (never the cache — a
        state question must not be answered from a minute-old snapshot)."""
        raw = await self._request("GET", f"/api/states/{entity_id}")
        if not isinstance(raw, dict):
            return None
        attributes = raw.get("attributes")
        attributes = attributes if isinstance(attributes, dict) else {}
        return Device(
            entity_id=str(raw.get("entity_id") or entity_id),
            name=_friendly_name(entity_id, attributes),
            domain=domain_of(entity_id),
            state=str(raw.get("state") or "unknown"),
            area=str(attributes.get("area") or "").strip(),
            attributes=attributes,
        )

    # -- writes ------------------------------------------------------------

    async def call_service(self, domain: str, service: str, data: dict) -> Any:
        """Invoke one HA service. `domain`/`service` are chosen in
        `home_tools.py` from a fixed map — they are never free text from a
        model."""
        self._entities = None  # a write invalidates the cached snapshot
        return await self._request(
            "POST", f"/api/services/{domain}/{service}", json_body=data
        )


# ------------------------------------------------------------- factory seam

# Zero-arg callable (sync or async) returning a client. None = build the real
# one. The GMAIL_SERVICE_FACTORY pattern: tests assign this and the suite never
# touches a real hub.
HOME_SERVICE_FACTORY: Optional[Callable[[], Any]] = None

_CLIENT: Optional[HomeAssistantClient] = None
_CLIENT_KEY: tuple[str, str] = ("", "")


def reset_home_client() -> None:
    """Drop the cached client (and with it the entity cache). Called by the
    settings PUT so a changed address or token takes effect immediately, and by
    the test fixture between tests."""
    global _CLIENT, _CLIENT_KEY
    client, _CLIENT, _CLIENT_KEY = _CLIENT, None, ("", "")
    if client is not None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop to close on — the process is going away anyway
        loop.create_task(client.aclose())


async def get_home_client(base_url: str = "", token: str = "") -> Any:
    """The ONE way business code gets a hub client.

    Raises HomeNotConnectedError when there is no usable configuration — the
    single choke point every tool catches. `base_url` is passed in by the
    caller from the stored config; `token` defaults to the stored one.
    """
    if HOME_SERVICE_FACTORY is not None:
        client = HOME_SERVICE_FACTORY()
        if inspect.isawaitable(client):
            client = await client
        return client

    url = str(base_url or "").strip().rstrip("/")
    if not url:
        raise HomeNotConnectedError()
    key_token = str(token or "").strip() or read_token()
    if not key_token:
        raise HomeNotConnectedError()

    global _CLIENT, _CLIENT_KEY
    if _CLIENT is None or _CLIENT_KEY != (url, key_token):
        reset_home_client()
        _CLIENT = HomeAssistantClient(url, key_token)
        _CLIENT_KEY = (url, key_token)
    return _CLIENT
