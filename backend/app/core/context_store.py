"""
Furi OS — World Model / Context Store (Phase 8, Part 1)

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
# ------------------------------------------------------- screen-aware chat
#: How many recent OCR captures are kept for chat context (in-memory ring,
#: retention=none — wiped on restart like everything else sensed).
SCREEN_RING_MAX = 3
#: Per-capture cap on the fuller OCR text kept for chat (the condensed world
#: -model summary stays separately capped in screen_ocr.condense_ocr_text).
SCREEN_FULL_TEXT_MAX = 1500
#: The chat injection only fires when the LATEST capture is at most this old —
#: beyond it the screen context is history, not "what's on screen right now".
#: Unlike the world model's on_screen_context, the ring itself is never nulled
#: on staleness: it keeps last-known and exposes age instead.
SCREEN_CHAT_MAX_AGE_SECONDS = 300.0
#: The assembled model is memoized this long so Phase-9 consumers polling it do
#: not re-hit Google on every read; the expensive Google sections have their own
#: longer cache below. Tests pass use_cache=False for determinism.
WORLD_CACHE_TTL_SECONDS = 5.0
#: Calendar/inbox are network calls — cached longer than the model itself.
GOOGLE_CACHE_TTL_SECONDS = 60.0
#: How many unread messages to probe when counting (a digest, not the inbox).
UNREAD_PROBE = 25

# ------------------------------------------------------------- affective (Phase 13)
#: A posted affective summary (typing cadence / voice energy) is "current" this
#: long; after that it drops out of the derivation (staleness-gated like device).
AFFECTIVE_FRESH_SECONDS = 90.0
#: App-switch rate is computed over this trailing window of device signals.
ACTIVITY_WINDOW_SECONDS = 300.0
#: Cap the device-signal ring buffer (retention=none, in-memory, wiped on restart).
ACTIVITY_RING_MAX = 64
#: Coarse thresholds mapping raw signals → a 0..1 intensity/strain. All are
#: deliberately generous — this is an arousal/effort PROXY, never an emotion read.
TYPING_BUSY_CPM = 240.0            # sustained chars/min at/above this reads "busy"
BACKSPACE_STRAIN_RATE = 0.25       # this fraction of keys being deletes reads "effortful"
SWITCH_BUSY_PER_MIN = 4.0          # app switches/min at/above this reads "thrashing"
VOICE_AROUSAL = 0.5                # scaled RMS (0..1) at/above this reads "energized"


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


@dataclass(frozen=True)
class _ScreenCapture:
    """One entry of the screen-context ring (screen-aware chat): the fuller OCR
    text plus which app/window it was read from. In-memory only, wiped on
    restart — the Phase 8 retention=none posture."""
    full_text: str                    # capped at SCREEN_FULL_TEXT_MAX
    app: Optional[str]
    window_title: Optional[str]
    captured: float                   # time.monotonic() at capture (age)
    captured_at: str                  # utc iso (display)


@dataclass(frozen=True)
class _AffectiveState:
    """The latest client-posted affective summary (Phase 13). Each field is
    optional — a source that isn't sensing right now is simply absent."""
    typing_cpm: Optional[float]      # chars/min over the client's rolling window
    backspace_rate: Optional[float]  # fraction of keystrokes that were deletes (0..1)
    voice_energy: Optional[float]    # scaled mic RMS (0..1), an arousal proxy
    received: float                  # time.monotonic() at receipt (staleness)
    received_at: str                 # utc iso (display)


_device: Optional[_DeviceState] = None
_ocr: Optional[_OcrState] = None
_affective: Optional[_AffectiveState] = None
#: Ring of the last SCREEN_RING_MAX OCR captures (screen-aware chat) — newest
#: last. Kept even past staleness (last-known + age); retention=none.
_screen_ring: list[_ScreenCapture] = []
#: Rolling (monotonic_ts, app_key) of recent device signals — the app-switch
#: rate signal for activity intensity. Retention=none; wiped on restart.
_activity_ring: list[tuple[float, str]] = []
#: key ("calendar"/"unread") → (monotonic_ts, value-or-None)
_google_cache: dict[str, tuple[float, Any]] = {}
#: (monotonic_ts, WorldModel)
_world_cache: Optional[tuple[float, "WorldModel"]] = None


def reset_context_store() -> None:
    """Test/shutdown hook — drop all sensed state and caches (retention=none)."""
    global _device, _ocr, _affective, _world_cache
    _device = None
    _ocr = None
    _affective = None
    _activity_ring.clear()
    _screen_ring.clear()
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
    mono = time.monotonic()
    now_wall = datetime.now(timezone.utc).isoformat()
    _device = _DeviceState(
        active_app=active_app or None,
        window_title=window_title or None,
        idle_seconds=idle_seconds,
        received=mono,
        received_at=now_wall,
    )
    # Feed the app-switch-rate signal (Phase 13 activity intensity). Key on the
    # app, falling back to the window title; drop old entries beyond the window.
    key = (active_app or window_title or "").strip()
    if key:
        _activity_ring.append((mono, key))
        cutoff = mono - ACTIVITY_WINDOW_SECONDS
        while _activity_ring and (_activity_ring[0][0] < cutoff or len(_activity_ring) > ACTIVITY_RING_MAX):
            _activity_ring.pop(0)
    _world_cache = None  # a new signal invalidates the memoized model


def record_affective_signal(
    typing_cpm: Optional[float],
    backspace_rate: Optional[float],
    voice_energy: Optional[float],
) -> None:
    """Store the latest client-computed affective summary (Phase 13). Called by
    the /state endpoint only after the master + affective_sensing gate passes.
    Only TIMING/ENERGY summaries are posted — never keystroke content or audio."""
    global _affective, _world_cache
    _affective = _AffectiveState(
        typing_cpm=typing_cpm,
        backspace_rate=backspace_rate,
        voice_energy=voice_energy,
        received=time.monotonic(),
        received_at=datetime.now(timezone.utc).isoformat(),
    )
    _world_cache = None


def record_ocr_summary(
    summary: str,
    *,
    full_text: Optional[str] = None,
    app: Optional[str] = None,
    window_title: Optional[str] = None,
) -> None:
    """Store the rolling on-screen-context summary. Called by the /screen
    endpoint only after the master + screen_ocr gate passes. An empty summary
    clears the condensed state (the screen had no readable text).

    Screen-aware chat additions: `full_text` is the richer OCR text kept in the
    capture ring (capped SCREEN_FULL_TEXT_MAX; falls back to the summary), and
    `app`/`window_title` attribute the capture — defaulted from the current
    fresh device signal when not passed (the /screen endpoint has no device
    info of its own). An UNCHANGED capture refreshes the latest ring entry's
    timestamps in place instead of appending a duplicate, so age labels and
    freshness stay honest while the user sits on one window."""
    global _ocr, _world_cache
    mono = time.monotonic()
    now_wall = datetime.now(timezone.utc).isoformat()

    text = (summary or "").strip()
    _ocr = _OcrState(
        summary=text,
        captured=mono,
        captured_at=now_wall,
    ) if text else None

    # ----- the chat ring (kept last-known; never nulled on staleness) -----
    ring_text = (full_text or "").strip() or text
    if ring_text:
        ring_text = ring_text[:SCREEN_FULL_TEXT_MAX]
        if (app is None and window_title is None) and _device is not None and _device_fresh(mono):
            app = _device.active_app
            window_title = _device.window_title
        latest = _screen_ring[-1] if _screen_ring else None
        if latest is not None and latest.full_text == ring_text:
            # Dedupe: same screen text — refresh timestamps (and attribution)
            # in place rather than storing an identical capture.
            _screen_ring[-1] = _ScreenCapture(
                full_text=ring_text,
                app=app if app is not None else latest.app,
                window_title=window_title if window_title is not None else latest.window_title,
                captured=mono,
                captured_at=now_wall,
            )
        else:
            _screen_ring.append(_ScreenCapture(
                full_text=ring_text,
                app=app,
                window_title=window_title,
                captured=mono,
                captured_at=now_wall,
            ))
            del _screen_ring[:-SCREEN_RING_MAX]

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


# ------------------------------------------------------- affective (Phase 13)

def _affective_fresh(now: float) -> bool:
    return _affective is not None and (now - _affective.received) <= AFFECTIVE_FRESH_SECONDS


def _switches_per_min(now: float) -> Optional[float]:
    """App switches per minute over the trailing activity window, or None when
    there isn't enough history. A short span is floored to 60s so a burst can
    never explode the rate — this is a coarse read, not a precise metric."""
    cutoff = now - ACTIVITY_WINDOW_SECONDS
    window = [(t, k) for (t, k) in _activity_ring if t >= cutoff]
    if len(window) < 2:
        return None
    switches = sum(1 for i in range(1, len(window)) if window[i][1] != window[i - 1][1])
    span = max(window[-1][0] - window[0][0], 60.0)
    return switches / (span / 60.0)


def _derive_user_state(config: ContextConfig, now: float) -> Optional[dict]:
    """A COARSE load bucket from independently-best-effort signals: activity
    intensity (app-switch rate + idle), typing cadence + backspace strain, and
    voice energy. Returns None when no fresh signal contributes — never a guess.

    Deliberately conservative and transparent: `signals` echoes the raw inputs
    so the audit UI shows exactly what fed the read, and `confidence` scales with
    how many independent source families contributed (adaptive behavior requires
    both a busy/stressed bucket AND enough confidence — see high_load())."""
    intensity_parts: list[float] = []
    strain_parts: list[float] = []
    signals: dict[str, Any] = {}
    sources = 0

    # Source 1 — activity intensity (device). Present iff a fresh device signal.
    if _device is not None and _device_fresh(now):
        sources += 1
        idle = _device.idle_seconds or 0.0
        if idle >= config.idle_threshold_seconds:
            intensity_parts.append(0.0)   # idle overrides prior thrash → calm
            signals["idle_seconds"] = round(idle, 1)
        else:
            rate = _switches_per_min(now)
            if rate is not None:
                signals["app_switches_per_min"] = round(rate, 2)
                intensity_parts.append(min(1.0, rate / SWITCH_BUSY_PER_MIN))
            else:
                intensity_parts.append(0.0)

    # Source 2 — typing cadence + backspace strain (client-posted).
    if _affective_fresh(now) and _affective is not None:
        a = _affective
        if a.typing_cpm is not None:
            sources += 1
            signals["typing_cpm"] = round(a.typing_cpm, 1)
            intensity_parts.append(min(1.0, a.typing_cpm / TYPING_BUSY_CPM))
            if a.backspace_rate is not None:
                signals["backspace_rate"] = round(a.backspace_rate, 2)
                # rate == BACKSPACE_STRAIN_RATE maps to 0.5 strain; 2× → 1.0.
                strain_parts.append(min(1.0, a.backspace_rate / (BACKSPACE_STRAIN_RATE * 2)))
        # Source 3 — voice energy (arousal proxy), only while it's being sensed.
        if a.voice_energy is not None:
            sources += 1
            signals["voice_energy"] = round(a.voice_energy, 2)
            intensity_parts.append(min(1.0, a.voice_energy / VOICE_AROUSAL))

    if sources == 0:
        return None

    intensity = sum(intensity_parts) / len(intensity_parts) if intensity_parts else 0.0
    strain = max(strain_parts) if strain_parts else 0.0

    if strain >= 0.5 and intensity >= 0.4:
        load = "stressed"
    elif intensity >= 0.6:
        load = "busy"
    elif intensity >= 0.3:
        load = "steady"
    else:
        load = "calm"

    return {
        "load": load,
        "confidence": round(min(1.0, sources / 3.0), 2),
        "signals": signals,
    }


#: A load read only steers behavior (initiative bar, chat brevity, output router)
#: when it is high AND confident enough — a single weak signal never re-tunes
#: Furi. Kept as one predicate so every consumer agrees on the threshold.
HIGH_LOAD_MIN_CONFIDENCE = 0.33


def high_load(user_state: Optional[dict]) -> bool:
    """True iff the user reads as busy/stressed with enough confidence to act on.
    The ONE gate every adaptive consumer (backend + mirrored on the frontend)
    shares, so a low-confidence blip never changes behavior."""
    if not user_state:
        return False
    return (
        user_state.get("load") in ("busy", "stressed")
        and float(user_state.get("confidence") or 0.0) >= HIGH_LOAD_MIN_CONFIDENCE
    )


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
    user_state: Optional[dict] = None               # {load, confidence, signals} (Phase 13)
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
                "affective_sensing": bool(config.affective_sensing) if config else False,
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

    # Affective load — only when its own opt-in is on; best-effort, dark otherwise.
    user_state = _derive_user_state(config, now) if config.affective_sensing else None

    model = WorldModel(
        presence=presence,
        active_app=active_app,
        window_title=window_title,
        idle_seconds=idle_seconds,
        next_calendar_event=next_event,
        unread=unread,
        recent_file_focus=recent_file,
        on_screen_context=on_screen,
        user_state=user_state,
        sensing={
            "enabled": True,
            "device_sensing": config.device_sensing,
            "screen_ocr": config.screen_ocr,
            "affective_sensing": config.affective_sensing,
            "device_fresh": device_fresh,
            "ocr_fresh": ocr_fresh,
        },
        captured_at=datetime.now(timezone.utc).isoformat(),
    )
    _world_cache = (now, model)
    return model


# -------------------------------------------------------- screen-aware chat

def _age_label(seconds: float) -> str:
    """A short human age for a capture — '~Ns ago' under two minutes, '~Nm ago'
    beyond. Deterministic (no locale, no fuzz) so tests can assert on it."""
    s = max(0, int(seconds))
    if s < 120:
        return f"~{s}s ago"
    return f"~{s // 60}m ago"


def _capture_location(entry: _ScreenCapture) -> str:
    """'in <app> — "<window title>"' with either part degrading gracefully."""
    app = (entry.app or "").strip()
    title = (entry.window_title or "").strip()
    if app and title:
        return f'in {app} — "{title}"'
    if app:
        return f"in {app}"
    if title:
        return f'in "{title}"'
    return "app unknown"


#: Injected instead of screen text when the user OPTED IN to screen-aware chat
#: but no fresh capture exists (fresh launch with Furi focused, sensing
#: paused, backend just restarted — the ring is in-memory by design). Without
#: this the LLM sees nothing and INVENTS rituals ("just say 'take a
#: screenshot'" — live fabrication 2026-07-16); an honest system-authored
#: explanation is the structural fix, not a hopeful prompt rule.
SCREEN_NO_CAPTURE_NOTE = (
    "NO FRESH SCREEN CAPTURE: screen-aware chat is enabled, but there is no "
    "recent capture right now. Captures happen AUTOMATICALLY while screen "
    "sensing runs; Furi never captures its own window, so right after "
    "startup nothing exists until the user views another window for a moment. "
    "If the user asks about their screen: say you don't have a fresh view yet "
    "and ask them to bring the screen they mean to the front for a couple of "
    "seconds, then ask again. There is NO command, request, or trigger phrase "
    "for this — NEVER tell the user to say 'take a screenshot' or any other "
    "magic words."
)


async def screen_context_for_chat(db: AsyncSession) -> str:
    """The screen-context block for chat injection (screen-aware chat), or "".

    Gated on master + screen_ocr + screen_in_chat ALL being on — the chat LLM
    only ever sees on-screen text the user separately consented to sharing with
    it; the gate off returns "". When the gate is ON but the ring is empty or
    the latest capture is older than SCREEN_CHAT_MAX_AGE_SECONDS (last-known is
    kept in memory, but stale screen text must not masquerade as "right now"),
    the HONEST SCREEN_NO_CAPTURE_NOTE is returned instead of "" — an opted-in
    user asking "what's on my screen" must get a truthful "no capture yet",
    never an LLM improvising trigger phrases. Config read is best-effort — any
    failure reads as gate-off, never raises."""
    try:
        config = await get_context_config(db)
    except Exception as e:
        logger.debug(f"Screen chat context: config read failed (non-critical): {e}")
        return ""
    if not (config.enabled and config.screen_ocr and config.screen_in_chat):
        return ""
    if not _screen_ring:
        return SCREEN_NO_CAPTURE_NOTE

    now = time.monotonic()
    current = _screen_ring[-1]
    if (now - current.captured) > SCREEN_CHAT_MAX_AGE_SECONDS:
        return SCREEN_NO_CAPTURE_NOTE

    parts = [
        f"CURRENT SCREEN ({_age_label(now - current.captured)}, "
        f"{_capture_location(current)}):\n{current.full_text}"
    ]
    for prior in reversed(_screen_ring[:-1][-2:]):  # up to 2, newest first
        parts.append(
            f"EARLIER ({_age_label(now - prior.captured)}, "
            f"{_capture_location(prior)}):\n{prior.full_text}"
        )
    return "\n\n".join(parts)


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
            "affective_sensing": False, "device_fresh": False, "ocr_fresh": False,
        }
    return {
        "enabled": config.enabled,
        "device_sensing": config.device_sensing,
        "screen_ocr": config.screen_ocr,
        "affective_sensing": config.affective_sensing,
        "device_fresh": config.enabled and _device_fresh(now),
        "ocr_fresh": config.enabled and _ocr_fresh(now, config),
    }
