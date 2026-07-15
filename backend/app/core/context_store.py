"""
Jarvis OS — World Model / Context Store (Phase 8, Part 1)

The Context Layer's aggregate: a single, in-memory, WRITE-ONLY picture of what
the user is doing right now, and the ONE read seam — get_world_model(db) — that
every future proactive feature (Phase 9) will consume. Phase 8 only WRITES this
model; nothing reads it to change behavior yet.

Privacy is structural here:
- Retention = NONE. Nothing sensed is persisted to SQLite (only ContextConfig
  is). Device state and the rolling OCR summary live in these module globals,
  are staleness-gated, and vanish on restart.
- The MASTER kill switch (ContextConfig.enabled) gates the whole model: when
  off, get_world_model returns an empty model and every record_* call is a
  no-op the API refuses upstream — reads and writes both go dark.
- Each aggregated section is INDEPENDENTLY best-effort (the daily-briefing
  gather rule): Google not connected, an API down, or a query error just drops
  that section, never raises.

State mutation happens on the backend's single asyncio loop (API handlers), so
plain module globals are race-free. `time.monotonic()` drives staleness.
"""
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Awaitable

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.app_settings import ContextConfig, get_context_config
from app.db.models import FileIndex

# ------------------------------------------------------------- tunables
#: A device heartbeat arrives ~every 30s; after this long with no signal the
#: user is treated as "away" (laptop asleep, Electron gone, sensing off).
DEVICE_FRESH_SECONDS = 90.0
#: The rolling OCR summary is "current" for a few capture intervals, floored so
#: a long interval still leaves the summary visible between captures.
OCR_FRESH_MIN_SECONDS = 120.0
OCR_FRESH_INTERVAL_MULTIPLIER = 3
#: The assembled model is memoized this long so Phase-9 consumers polling it do
#: not re-hit Google on every read; the expensive Google sections have their own
#: longer cache below. Tests pass use_cache=False for determinism.
WORLD_CACHE_TTL_SECONDS = 5.0
#: Calendar/inbox are network calls — cached longer than the model itself.
GOOGLE_CACHE_TTL_SECONDS = 60.0
#: How many unread messages to probe when counting (a digest, not the inbox).
UNREAD_PROBE = 25


# --------------------------------------------------------------- in-mem state

@dataclass(frozen=True)
class _DeviceState:
    active_app: Optional[str]
    window_title: Optional[str]
    idle_seconds: Optional[float]
    received: float          # time.monotonic() at receipt (staleness)
    received_at: str         # utc iso (display)


@dataclass(frozen=True)
class _OcrState:
    summary: str
    captured: float          # time.monotonic() at capture (staleness)
    captured_at: str         # utc iso (display)


_device: Optional[_DeviceState] = None
_ocr: Optional[_OcrState] = None
#: key ("calendar"/"unread") → (monotonic_ts, value-or-None)
_google_cache: dict[str, tuple[float, Any]] = {}
#: (monotonic_ts, WorldModel)
_world_cache: Optional[tuple[float, "WorldModel"]] = None


def reset_context_store() -> None:
    """Test/shutdown hook — drop all sensed state and caches (retention=none)."""
    global _device, _ocr, _world_cache
    _device = None
    _ocr = None
    _world_cache = None
    _google_cache.clear()


# ------------------------------------------------------------- write entry pts

def record_device_signal(
    active_app: Optional[str],
    window_title: Optional[str],
    idle_seconds: Optional[float],
) -> None:
    """Store the latest device signal (active app/window + idle time). Called by
    the /device endpoint only after the master + device_sensing gate passes."""
    global _device, _world_cache
    now_wall = datetime.now(timezone.utc).isoformat()
    _device = _DeviceState(
        active_app=active_app or None,
        window_title=window_title or None,
        idle_seconds=idle_seconds,
        received=time.monotonic(),
        received_at=now_wall,
    )
    _world_cache = None  # a new signal invalidates the memoized model


def record_ocr_summary(summary: str) -> None:
    """Store the rolling on-screen-context summary. Called by the /screen
    endpoint only after the master + screen_ocr gate passes. An empty summary
    clears the state (the screen had no readable text)."""
    global _ocr, _world_cache
    text = (summary or "").strip()
    _ocr = _OcrState(
        summary=text,
        captured=time.monotonic(),
        captured_at=datetime.now(timezone.utc).isoformat(),
    ) if text else None
    _world_cache = None


# --------------------------------------------------------------- freshness

def _device_fresh(now: float) -> bool:
    return _device is not None and (now - _device.received) <= DEVICE_FRESH_SECONDS


def _ocr_fresh(now: float, config: ContextConfig) -> bool:
    if _ocr is None:
        return False
    window = max(
        OCR_FRESH_MIN_SECONDS,
        config.ocr_interval_seconds * OCR_FRESH_INTERVAL_MULTIPLIER,
    )
    return (now - _ocr.captured) <= window


def _derive_presence(config: ContextConfig, now: float) -> str:
    """active / idle / away / unknown from the device signal + idle threshold."""
    if _device is None:
        return "unknown"
    if not _device_fresh(now):
        return "away"
    idle = _device.idle_seconds or 0.0
    return "idle" if idle >= config.idle_threshold_seconds else "active"


# --------------------------------------------------------------- world model

@dataclass(frozen=True)
class WorldModel:
    """The aggregate the read seam returns. Every field is best-effort — a
    None/empty section means "not sensed / not available", never an error."""
    presence: str = "unknown"                      # active | idle | away | unknown
    active_app: Optional[str] = None
    window_title: Optional[str] = None
    idle_seconds: Optional[float] = None
    next_calendar_event: Optional[dict] = None
    unread: Optional[dict] = None                   # {count, has_urgent}
    recent_file_focus: Optional[dict] = None        # {filename, path, modified}
    on_screen_context: Optional[str] = None
    sensing: dict = field(default_factory=dict)     # {enabled, device_sensing, ...}
    captured_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------- best-effort Google sections

async def _google_section(key: str, fetch: Callable[[], Awaitable[Any]]) -> Any:
    """Return a Google-derived section, cached GOOGLE_CACHE_TTL_SECONDS. A
    failure (not connected, API down) caches None so reads don't retry every
    few seconds — the section simply stays absent until the cache expires."""
    now = time.monotonic()
    hit = _google_cache.get(key)
    if hit is not None and (now - hit[0]) < GOOGLE_CACHE_TTL_SECONDS:
        return hit[1]
    value: Any = None
    try:
        value = await fetch()
    except Exception as e:  # not connected / API error — drop the section
        logger.debug(f"World-model section '{key}' unavailable: {e}")
        value = None
    _google_cache[key] = (now, value)
    return value


async def _fetch_next_event() -> Optional[dict]:
    from app.tools.calendar_tools import _api, _event_row, format_event_when
    from app.integrations.google_services import get_calendar_service
    from app.tools.calendar_tools import CALENDAR_ID
    from datetime import datetime as _dt

    service = await get_calendar_service()
    listing = await _api(
        service.events().list(
            calendarId=CALENDAR_ID,
            singleEvents=True,
            orderBy="startTime",
            timeMin=_dt.now().astimezone().isoformat(),
            maxResults=1,
        )
    )
    items = listing.get("items") or []
    if not items:
        return None
    row = _event_row(items[0])
    row["when"] = format_event_when(row)
    return row


async def _fetch_unread() -> Optional[dict]:
    from app.tools.email_tools import build_gmail_query
    from app.integrations.google_services import get_gmail_service

    service = await get_gmail_service()

    def _list(query: str, cap: int) -> list:
        result = service.users().messages().list(
            userId="me", q=query, maxResults=cap
        ).execute()
        return result.get("messages") or []

    import asyncio

    unread = await asyncio.to_thread(
        _list, build_gmail_query({"unread_only": True}), UNREAD_PROBE
    )
    urgent = await asyncio.to_thread(
        _list, build_gmail_query({"unread_only": True}) + " is:important", 1
    )
    return {"count": len(unread), "has_urgent": bool(urgent)}


async def _fetch_recent_file(db: AsyncSession) -> Optional[dict]:
    row = (
        await db.execute(
            select(FileIndex)
            .where(FileIndex.is_active.is_(True))
            .order_by(FileIndex.mtime.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    modified = ""
    try:
        modified = datetime.fromtimestamp(row.mtime, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        modified = ""
    return {"filename": row.filename, "path": row.path, "modified": modified}


# --------------------------------------------------------------- the read seam

async def get_world_model(db: AsyncSession, *, use_cache: bool = True) -> WorldModel:
    """The ONE read seam — the current world model. Best-effort throughout: any
    section that fails is simply absent. When the master switch is off, returns
    an empty model (sensing dark). Memoized WORLD_CACHE_TTL_SECONDS."""
    global _world_cache
    now = time.monotonic()

    if use_cache and _world_cache is not None and (now - _world_cache[0]) < WORLD_CACHE_TTL_SECONDS:
        return _world_cache[1]

    try:
        config = await get_context_config(db)
    except Exception as e:
        logger.warning(f"World model: config read failed (non-critical): {e}")
        config = None

    if config is None or not config.enabled:
        # Master kill switch off — the model goes dark (no device/Google/OCR).
        model = WorldModel(
            presence="unknown",
            sensing={
                "enabled": bool(config.enabled) if config else False,
                "device_sensing": bool(config.device_sensing) if config else False,
                "screen_ocr": bool(config.screen_ocr) if config else False,
                "device_fresh": False,
                "ocr_fresh": False,
            },
            captured_at=datetime.now(timezone.utc).isoformat(),
        )
        _world_cache = (now, model)
        return model

    device_fresh = _device_fresh(now)
    ocr_fresh = _ocr_fresh(now, config)

    # Device-derived sections (nulled when the signal is stale).
    presence = _derive_presence(config, now)
    active_app = _device.active_app if (device_fresh and _device) else None
    window_title = _device.window_title if (device_fresh and _device) else None
    idle_seconds = _device.idle_seconds if (device_fresh and _device) else None

    # Google sections — best-effort + cached (never block a read on the network).
    next_event = await _google_section("calendar", _fetch_next_event)
    unread = await _google_section("unread", _fetch_unread)

    # Recent-file focus — best-effort read of the file index.
    try:
        recent_file = await _fetch_recent_file(db)
    except Exception as e:
        logger.debug(f"World-model recent-file section unavailable: {e}")
        recent_file = None

    on_screen = _ocr.summary if (ocr_fresh and _ocr) else None

    model = WorldModel(
        presence=presence,
        active_app=active_app,
        window_title=window_title,
        idle_seconds=idle_seconds,
        next_calendar_event=next_event,
        unread=unread,
        recent_file_focus=recent_file,
        on_screen_context=on_screen,
        sensing={
            "enabled": True,
            "device_sensing": config.device_sensing,
            "screen_ocr": config.screen_ocr,
            "device_fresh": device_fresh,
            "ocr_fresh": ocr_fresh,
        },
        captured_at=datetime.now(timezone.utc).isoformat(),
    )
    _world_cache = (now, model)
    return model


async def context_status(db: AsyncSession) -> dict:
    """A cheap, purely-local sensing status for the StatusBar indicator — no
    Google I/O. Just the config flags + whether the two signals are fresh."""
    now = time.monotonic()
    try:
        config = await get_context_config(db)
    except Exception:
        config = None
    if config is None:
        return {
            "enabled": False, "device_sensing": False, "screen_ocr": False,
            "device_fresh": False, "ocr_fresh": False,
        }
    return {
        "enabled": config.enabled,
        "device_sensing": config.device_sensing,
        "screen_ocr": config.screen_ocr,
        "device_fresh": config.enabled and _device_fresh(now),
        "ocr_fresh": config.enabled and _ocr_fresh(now, config),
    }
