"""
Jarvis OS — App settings store (Phase 5, Part 6)

The ONE accessor for runtime-configurable app settings (the AppSetting table).
A small key/value store with JSON-encoded values — the home for settings the
user toggles at runtime rather than in .env (which needs a restart). Part 6's
daily briefing config + its singleton scheduler-job pointer live here; future
settings can too.

Typed helpers (get_briefing_config / set_briefing_config) sit on top so call
sites never re-parse: a missing key yields the DEFAULT, which is what makes the
daily briefing "on by default at 08:00" true before the user ever opens
Settings.
"""
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AppSetting

# ------------------------------------------------------------------ keys
BRIEFING_CONFIG_KEY = "daily_briefing.config"
BRIEFING_JOB_ID_KEY = "daily_briefing.job_id"
FILE_INDEX_CONFIG_KEY = "file_index.config"
FILE_INDEX_JOB_ID_KEY = "file_index.job_id"
VOICE_CONFIG_KEY = "voice.config"
CONTEXT_CONFIG_KEY = "context.config"
INITIATIVE_CONFIG_KEY = "initiative.config"
INITIATIVE_JOB_ID_KEY = "initiative.job_id"


# --------------------------------------------------------- generic accessor

async def get_setting(db: AsyncSession, key: str, default: Any = None) -> Any:
    """The JSON-decoded value for `key`, or `default` when absent/corrupt.
    A corrupt row reads as absent (never a crash) — the same defensive stance
    the scheduler takes on a bad payload."""
    row = await db.get(AppSetting, key)
    if row is None:
        return default
    try:
        return json.loads(row.value)
    except (ValueError, TypeError):
        logger.warning(f"App setting '{key}' holds invalid JSON — using default")
        return default


async def set_setting(db: AsyncSession, key: str, value: Any) -> None:
    """Upsert `key` with a JSON-encoded value and commit."""
    encoded = json.dumps(value, default=str)
    row = await db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=encoded))
    else:
        row.value = encoded
    await db.commit()


# ----------------------------------------------------- daily-briefing config

@dataclass(frozen=True)
class BriefingConfig:
    """The user-facing daily-briefing settings. hour/minute are LOCAL wall
    clock (the reminder/birthday convention)."""
    enabled: bool
    hour: int
    minute: int

    @property
    def time_str(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"


#: Default before the user configures anything — ON at 08:00 local (the
#: confirmed product decision). ensure_briefing_job() arms this on first boot.
DEFAULT_BRIEFING = BriefingConfig(enabled=True, hour=8, minute=0)


def _coerce_briefing(raw: Any) -> BriefingConfig:
    """A stored dict → BriefingConfig, falling back to DEFAULT fields on any
    missing/invalid part (never a crash from a hand-edited row)."""
    if not isinstance(raw, dict):
        return DEFAULT_BRIEFING
    try:
        hour = int(raw.get("hour", DEFAULT_BRIEFING.hour))
        minute = int(raw.get("minute", DEFAULT_BRIEFING.minute))
    except (TypeError, ValueError):
        hour, minute = DEFAULT_BRIEFING.hour, DEFAULT_BRIEFING.minute
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        hour, minute = DEFAULT_BRIEFING.hour, DEFAULT_BRIEFING.minute
    return BriefingConfig(
        enabled=bool(raw.get("enabled", DEFAULT_BRIEFING.enabled)),
        hour=hour,
        minute=minute,
    )


async def get_briefing_config(db: AsyncSession) -> BriefingConfig:
    """The persisted briefing config, or DEFAULT_BRIEFING when never set."""
    raw = await get_setting(db, BRIEFING_CONFIG_KEY, default=None)
    if raw is None:
        return DEFAULT_BRIEFING
    return _coerce_briefing(raw)


async def set_briefing_config(db: AsyncSession, config: BriefingConfig) -> None:
    await set_setting(db, BRIEFING_CONFIG_KEY, {
        "enabled": config.enabled,
        "hour": config.hour,
        "minute": config.minute,
    })


# ------------------------------------------ singleton job pointer helpers

async def get_briefing_job_id(db: AsyncSession) -> Optional[str]:
    value = await get_setting(db, BRIEFING_JOB_ID_KEY, default=None)
    return value if isinstance(value, str) and value else None


async def set_briefing_job_id(db: AsyncSession, job_id: Optional[str]) -> None:
    await set_setting(db, BRIEFING_JOB_ID_KEY, job_id)


# ------------------------------------------------- file-index config (Phase 6)

# Bounds for the reindex interval (used in Part 3's scheduler; validated here so
# a hand-edited row can never arm an absurd timer).
FILE_INDEX_MIN_INTERVAL = 15        # minutes
FILE_INDEX_MAX_INTERVAL = 7 * 24 * 60


def _default_index_folders() -> list[str]:
    """The three suggested folders (Desktop/Documents/Downloads under home) —
    prefilled so enabling the index 'just works', but NEVER whole drives. Only
    the ones that exist on this machine are offered."""
    home = Path.home()
    return [str(home / name) for name in ("Desktop", "Documents", "Downloads")
            if (home / name).is_dir()]


@dataclass(frozen=True)
class FileIndexConfig:
    """Which folders the semantic file index covers, plus its reindex cadence.
    enabled defaults OFF — indexing personal files is opt-in (privacy); the
    folders list is prefilled so the user only has to flip the switch."""
    enabled: bool
    folders: tuple[str, ...]
    exclusions: tuple[str, ...]
    interval_minutes: int


def default_file_index_config() -> FileIndexConfig:
    return FileIndexConfig(
        enabled=False,
        folders=tuple(_default_index_folders()),
        exclusions=(),
        interval_minutes=360,  # every 6 hours
    )


def _clean_paths(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    out: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return tuple(out)


def _coerce_file_index(raw: Any) -> FileIndexConfig:
    default = default_file_index_config()
    if not isinstance(raw, dict):
        return default
    try:
        interval = int(raw.get("interval_minutes", default.interval_minutes))
    except (TypeError, ValueError):
        interval = default.interval_minutes
    interval = max(FILE_INDEX_MIN_INTERVAL, min(interval, FILE_INDEX_MAX_INTERVAL))
    folders = _clean_paths(raw.get("folders"))
    return FileIndexConfig(
        enabled=bool(raw.get("enabled", default.enabled)),
        folders=folders if folders else default.folders,
        exclusions=_clean_paths(raw.get("exclusions")),
        interval_minutes=interval,
    )


async def get_file_index_config(db: AsyncSession) -> FileIndexConfig:
    raw = await get_setting(db, FILE_INDEX_CONFIG_KEY, default=None)
    if raw is None:
        return default_file_index_config()
    return _coerce_file_index(raw)


async def set_file_index_config(db: AsyncSession, config: FileIndexConfig) -> None:
    await set_setting(db, FILE_INDEX_CONFIG_KEY, {
        "enabled": config.enabled,
        "folders": list(config.folders),
        "exclusions": list(config.exclusions),
        "interval_minutes": config.interval_minutes,
    })


async def get_file_index_job_id(db: AsyncSession) -> Optional[str]:
    value = await get_setting(db, FILE_INDEX_JOB_ID_KEY, default=None)
    return value if isinstance(value, str) and value else None


async def set_file_index_job_id(db: AsyncSession, job_id: Optional[str]) -> None:
    await set_setting(db, FILE_INDEX_JOB_ID_KEY, job_id)


# ---------------------------------------------------- voice config (Phase 7)

#: Whitelisted faster-whisper model ids. Anything else coerces to the default —
#: a hand-edited row can never point the loader at an arbitrary HuggingFace repo
#: id. Includes multilingual sizes AND English-optimized variants (the `.en` /
#: distil models are ~2x faster and more accurate on English; distil-large-v3
#: rivals large-v3 quality at small-model speed). faster-whisper downloads each
#: on first use.
VOICE_STT_MODELS = (
    "tiny",
    "base",
    "base.en",
    "small",
    "small.en",
    "distil-small.en",
    "medium",
    "distil-large-v3",
    "large-v3",
)

#: Where an engine runs. "auto" resolves to CUDA when a GPU is present, else CPU
#: (see gpu_bootstrap.cuda_available); "cpu"/"cuda" force it, with a CPU fallback
#: in code if a forced CUDA load fails — voice must never wedge on a bad device.
VOICE_DEVICES = ("auto", "cpu", "cuda")

#: faster-whisper compute types. "auto" derives from the resolved device
#: (CUDA → int8_float16, the VRAM-safe near-float16 default; CPU → int8).
VOICE_STT_COMPUTE_TYPES = ("auto", "float16", "int8_float16", "int8")

#: Curated Kokoro preset voices (id → display label). Kokoro ships ~54 voices
#: across 8 languages; we expose a focused English set. `voice` is validated
#: against these ids — a hand-edited row can never name a voice the pack does
#: not contain. Ids follow Kokoro's `[lang][gender]_[name]` scheme (a=American
#: English, b=British English).
VOICE_TTS_VOICES = (
    ("af_heart", "Heart — US, female"),
    ("af_bella", "Bella — US, female"),
    ("af_sarah", "Sarah — US, female"),
    ("af_nicole", "Nicole — US, female"),
    ("am_michael", "Michael — US, male"),
    ("am_adam", "Adam — US, male"),
    ("am_fenrir", "Fenrir — US, male"),
    ("bf_emma", "Emma — UK, female"),
    ("bf_isabella", "Isabella — UK, female"),
    ("bm_george", "George — UK, male"),
    ("bm_lewis", "Lewis — UK, male"),
)

#: The ids alone, for validation.
VOICE_TTS_VOICE_IDS = tuple(v for v, _ in VOICE_TTS_VOICES)

#: Default speaking voice (must be in VOICE_TTS_VOICE_IDS).
DEFAULT_VOICE_ID = "af_heart"

#: Speaking-speed bounds (Kokoro `speed`; 1.0 = natural).
VOICE_TTS_MIN_SPEED = 0.5
VOICE_TTS_MAX_SPEED = 2.0


@dataclass(frozen=True)
class VoiceConfig:
    """Phase 7 voice settings. `enabled` is the master switch for voice
    (push-to-talk in, speech out) and defaults OFF — enabling triggers a
    large local model download, so it is strictly opt-in like the file index.
    Part 3 output fields: `output_enabled` defaults ON but is always gated on
    the master `enabled` at the API layer, so nothing speaks until voice
    itself is opted in; `speak_all_responses` (Part 4) covers typed turns;
    `speak_proactive` (Part 5) covers server-initiated pushes;
    `listen_on_summon` (Part 5) starts a hands-free recording when the global
    hotkey summons the window — strictly opt-in, like `enabled` itself.

    `voice` is a Kokoro preset voice id (validated against VOICE_TTS_VOICE_IDS,
    default af_heart) — Kokoro has fixed preset voices, no cloning. `tts_speed`
    is the speaking speed (VOICE_TTS_MIN_SPEED..MAX_SPEED, 1.0 = natural).

    `stt_device`/`tts_device` select CPU vs GPU per engine (default "auto" =
    GPU-when-present, else CPU); `stt_compute_type` tunes whisper's precision
    ("auto" derives it from the device). All coerce to their default when a
    stored row carries an out-of-whitelist value, so a pre-GPU-round config
    deserializes cleanly.

    Phase 12 ambient fields, both default OFF and gated under the master
    `enabled`: `continuous_conversation` (Part 12.1) re-opens a short hands-free
    window after a spoken reply so the user can talk back without re-triggering;
    `wake_word` (Part 12.2) enables always-on on-device "Hey Jarvis" detection
    in the renderer (raw audio never leaves the machine)."""
    enabled: bool
    stt_model: str
    review_before_send: bool
    output_enabled: bool
    voice: str
    speak_proactive: bool
    speak_all_responses: bool
    listen_on_summon: bool
    tts_speed: float
    stt_device: str
    tts_device: str
    stt_compute_type: str
    continuous_conversation: bool
    wake_word: bool


def default_voice_config() -> VoiceConfig:
    return VoiceConfig(
        enabled=False,
        stt_model="small",
        review_before_send=False,
        output_enabled=True,
        voice=DEFAULT_VOICE_ID,
        speak_proactive=False,
        speak_all_responses=False,
        listen_on_summon=False,
        tts_speed=1.0,
        stt_device="auto",
        tts_device="auto",
        stt_compute_type="auto",
        continuous_conversation=False,
        wake_word=False,
    )


def _clamp_speed(value: Any, fallback: float) -> float:
    try:
        return max(VOICE_TTS_MIN_SPEED, min(VOICE_TTS_MAX_SPEED, float(value)))
    except (TypeError, ValueError):
        return fallback


def _coerce_voice(raw: Any) -> VoiceConfig:
    """A stored dict → VoiceConfig. Unknown keys are ignored, so an existing
    row still carrying the removed Chatterbox fields (tts_exaggeration /
    tts_cfg_weight / tts_device / tts_fast, or a clone-id `voice`) deserializes
    cleanly on upgrade — an out-of-whitelist `voice` simply falls back to the
    default preset."""
    default = default_voice_config()
    if not isinstance(raw, dict):
        return default
    stt_model = raw.get("stt_model", default.stt_model)
    if stt_model not in VOICE_STT_MODELS:
        stt_model = default.stt_model
    voice = raw.get("voice", default.voice)
    if voice not in VOICE_TTS_VOICE_IDS:
        voice = default.voice
    stt_device = raw.get("stt_device", default.stt_device)
    if stt_device not in VOICE_DEVICES:
        stt_device = default.stt_device
    tts_device = raw.get("tts_device", default.tts_device)
    if tts_device not in VOICE_DEVICES:
        tts_device = default.tts_device
    stt_compute_type = raw.get("stt_compute_type", default.stt_compute_type)
    if stt_compute_type not in VOICE_STT_COMPUTE_TYPES:
        stt_compute_type = default.stt_compute_type
    return VoiceConfig(
        enabled=bool(raw.get("enabled", default.enabled)),
        stt_model=stt_model,
        review_before_send=bool(raw.get("review_before_send", default.review_before_send)),
        output_enabled=bool(raw.get("output_enabled", default.output_enabled)),
        voice=voice,
        speak_proactive=bool(raw.get("speak_proactive", default.speak_proactive)),
        speak_all_responses=bool(raw.get("speak_all_responses", default.speak_all_responses)),
        listen_on_summon=bool(raw.get("listen_on_summon", default.listen_on_summon)),
        tts_speed=_clamp_speed(raw.get("tts_speed", default.tts_speed), default.tts_speed),
        stt_device=stt_device,
        tts_device=tts_device,
        stt_compute_type=stt_compute_type,
        continuous_conversation=bool(
            raw.get("continuous_conversation", default.continuous_conversation)
        ),
        wake_word=bool(raw.get("wake_word", default.wake_word)),
    )


async def get_voice_config(db: AsyncSession) -> VoiceConfig:
    raw = await get_setting(db, VOICE_CONFIG_KEY, default=None)
    if raw is None:
        return default_voice_config()
    return _coerce_voice(raw)


async def set_voice_config(db: AsyncSession, config: VoiceConfig) -> None:
    await set_setting(db, VOICE_CONFIG_KEY, {
        "enabled": config.enabled,
        "stt_model": config.stt_model,
        "review_before_send": config.review_before_send,
        "output_enabled": config.output_enabled,
        "voice": config.voice,
        "speak_proactive": config.speak_proactive,
        "speak_all_responses": config.speak_all_responses,
        "listen_on_summon": config.listen_on_summon,
        "tts_speed": config.tts_speed,
        "stt_device": config.stt_device,
        "tts_device": config.tts_device,
        "stt_compute_type": config.stt_compute_type,
        "continuous_conversation": config.continuous_conversation,
        "wake_word": config.wake_word,
    })


# ---------------------------------------------------- context config (Phase 8)

# Bounds for the sensing cadences — validated here so a hand-edited row can
# never arm an absurdly tight capture loop or a nonsensical idle threshold.
CONTEXT_MIN_OCR_INTERVAL = 5            # seconds — a floor on screen-capture cadence
CONTEXT_MAX_OCR_INTERVAL = 3600
CONTEXT_MIN_IDLE_THRESHOLD = 30         # seconds — below this "idle" is meaningless
CONTEXT_MAX_IDLE_THRESHOLD = 3600


@dataclass(frozen=True)
class ContextConfig:
    """Phase 8 — the Context Layer's privacy-first settings.

    `enabled` is the MASTER kill switch and defaults OFF: no sensing of any
    kind happens until the user opts in, and flipping it off stops every
    backend write path AND (via the Electron settings poll) the native sensing
    loops. `device_sensing` (active app/window title + idle time) defaults ON
    but is only effective under the master switch. `screen_ocr` is the OCR
    CAPABILITY and defaults OFF — even with it on, the actual screen capture is
    additionally armed per-session in Electron (never silently persisted on).

    `affective_sensing` (Phase 13) is its OWN opt-in master, effective only under
    the context master and defaulting OFF: it derives a COARSE load bucket
    (calm/steady/busy/stressed) from typing cadence, voice energy, and activity
    intensity — an arousal/effort proxy, never an emotion read. It is the highest-
    uncertainty signal, so it is separately gated and separately indicated.

    Retention is structurally NONE: nothing sensed is stored to SQLite (only
    this config is); the world model and the rolling OCR summary live in memory
    and are staleness-gated. There is deliberately no retention field to set.

    `affective_sensing` carries a trailing default so a pre-Phase-13 stored row
    (and any external ContextConfig constructor) deserializes cleanly; the coercer
    and default still set it explicitly, the VoiceConfig discipline."""
    enabled: bool
    device_sensing: bool
    screen_ocr: bool
    ocr_interval_seconds: int
    idle_threshold_seconds: int
    affective_sensing: bool = False


def default_context_config() -> ContextConfig:
    return ContextConfig(
        enabled=False,          # master OFF — sensing is strictly opt-in
        device_sensing=True,    # the lightest signal; only effective under `enabled`
        screen_ocr=False,       # OCR is the most sensitive — opt-in on top of `enabled`
        ocr_interval_seconds=30,
        idle_threshold_seconds=300,
        affective_sensing=False,  # Phase 13 — separate opt-in, highest uncertainty
    )


def _clamp_int(value: Any, lo: int, hi: int, fallback: int) -> int:
    try:
        return max(lo, min(int(value), hi))
    except (TypeError, ValueError):
        return fallback


def _coerce_context(raw: Any) -> ContextConfig:
    """A stored dict → ContextConfig, defaulting any missing/invalid part
    (never a crash from a hand-edited row) — the FileIndexConfig discipline."""
    default = default_context_config()
    if not isinstance(raw, dict):
        return default
    return ContextConfig(
        enabled=bool(raw.get("enabled", default.enabled)),
        device_sensing=bool(raw.get("device_sensing", default.device_sensing)),
        screen_ocr=bool(raw.get("screen_ocr", default.screen_ocr)),
        ocr_interval_seconds=_clamp_int(
            raw.get("ocr_interval_seconds"),
            CONTEXT_MIN_OCR_INTERVAL, CONTEXT_MAX_OCR_INTERVAL,
            default.ocr_interval_seconds,
        ),
        idle_threshold_seconds=_clamp_int(
            raw.get("idle_threshold_seconds"),
            CONTEXT_MIN_IDLE_THRESHOLD, CONTEXT_MAX_IDLE_THRESHOLD,
            default.idle_threshold_seconds,
        ),
        affective_sensing=bool(raw.get("affective_sensing", default.affective_sensing)),
    )


async def get_context_config(db: AsyncSession) -> ContextConfig:
    raw = await get_setting(db, CONTEXT_CONFIG_KEY, default=None)
    if raw is None:
        return default_context_config()
    return _coerce_context(raw)


async def set_context_config(db: AsyncSession, config: ContextConfig) -> None:
    await set_setting(db, CONTEXT_CONFIG_KEY, {
        "enabled": config.enabled,
        "device_sensing": config.device_sensing,
        "screen_ocr": config.screen_ocr,
        "ocr_interval_seconds": config.ocr_interval_seconds,
        "idle_threshold_seconds": config.idle_threshold_seconds,
        "affective_sensing": config.affective_sensing,
    })


# ------------------------------------------------- initiative config (Phase 9)

#: The autonomy CEILING the policy caps every candidate at. Ordered least → most
#: capable so a candidate's proposed autonomy can be clamped by index:
#: - "off"     — the engine surfaces nothing (belt; the job also won't run when
#:               the master `enabled` is false).
#: - "suggest" — only informational nudges; nothing ever starts a plan.
#: - "ask"     — nudges may carry a goal; ACCEPTING starts an approval-gated Task.
#: - "act"     — Jarvis may auto-start the Task without waiting for Accept. Even
#:               then every write inside still pauses at the approval gate — a
#:               hand-edited row can never grant silent write authority.
INITIATIVE_AUTONOMY_LEVELS = ("off", "suggest", "ask", "act")

# Bounds validated here so a hand-edited row can never arm an absurd cadence or
# an unbounded suggestion firehose.
INITIATIVE_MIN_INTERVAL = 15          # minutes — a floor on the heartbeat cadence
INITIATIVE_MAX_INTERVAL = 24 * 60
INITIATIVE_MIN_BUDGET = 0             # 0 = generate but never surface (a soft mute)
INITIATIVE_MAX_BUDGET = 50
INITIATIVE_MIN_GAP = 5                # minutes — rate limiter floor between surfaced items
INITIATIVE_MAX_GAP = 12 * 60


@dataclass(frozen=True)
class InitiativeConfig:
    """Phase 9 — the Initiative Engine's settings, privacy-and-quota-first.

    `enabled` is the master switch and defaults OFF: Jarvis never volunteers a
    thing until the user opts in (the sensing/index/voice convention). `autonomy`
    is the CEILING the code-owned policy caps every candidate at — default
    "ask", so accepting a suggestion is always required before any plan starts;
    "act" (auto-start) is opt-in only and STILL routes every write through the
    approval gate. The governor fields make the throttled DeepSeek pass safe:
    `daily_budget` caps how many suggestions surface per local day,
    `quiet_start_hour`/`quiet_end_hour` blackout a window (the pass skips
    entirely — no generation, no push), and `min_gap_minutes` rate-limits how
    close two surfaced items can be. `interval_minutes` is the heartbeat cadence
    (a pure interval, like the reindex job). hours are LOCAL wall clock."""
    enabled: bool
    autonomy: str
    interval_minutes: int
    daily_budget: int
    quiet_start_hour: int
    quiet_end_hour: int
    min_gap_minutes: int


def default_initiative_config() -> InitiativeConfig:
    return InitiativeConfig(
        enabled=False,          # master OFF — proactivity is strictly opt-in
        autonomy="ask",         # accept-to-start by default; "act" is opt-in
        interval_minutes=45,
        daily_budget=5,
        quiet_start_hour=22,    # 22:00 → 08:00 quiet by default
        quiet_end_hour=8,
        min_gap_minutes=30,
    )


def _coerce_initiative(raw: Any) -> InitiativeConfig:
    """A stored dict → InitiativeConfig, defaulting any missing/invalid part
    (never a crash from a hand-edited row) — the ContextConfig discipline."""
    default = default_initiative_config()
    if not isinstance(raw, dict):
        return default
    autonomy = raw.get("autonomy", default.autonomy)
    if autonomy not in INITIATIVE_AUTONOMY_LEVELS:
        autonomy = default.autonomy
    return InitiativeConfig(
        enabled=bool(raw.get("enabled", default.enabled)),
        autonomy=autonomy,
        interval_minutes=_clamp_int(
            raw.get("interval_minutes"),
            INITIATIVE_MIN_INTERVAL, INITIATIVE_MAX_INTERVAL,
            default.interval_minutes,
        ),
        daily_budget=_clamp_int(
            raw.get("daily_budget"),
            INITIATIVE_MIN_BUDGET, INITIATIVE_MAX_BUDGET,
            default.daily_budget,
        ),
        quiet_start_hour=_clamp_int(
            raw.get("quiet_start_hour"), 0, 23, default.quiet_start_hour
        ),
        quiet_end_hour=_clamp_int(
            raw.get("quiet_end_hour"), 0, 23, default.quiet_end_hour
        ),
        min_gap_minutes=_clamp_int(
            raw.get("min_gap_minutes"),
            INITIATIVE_MIN_GAP, INITIATIVE_MAX_GAP,
            default.min_gap_minutes,
        ),
    )


async def get_initiative_config(db: AsyncSession) -> InitiativeConfig:
    raw = await get_setting(db, INITIATIVE_CONFIG_KEY, default=None)
    if raw is None:
        return default_initiative_config()
    return _coerce_initiative(raw)


async def set_initiative_config(db: AsyncSession, config: InitiativeConfig) -> None:
    await set_setting(db, INITIATIVE_CONFIG_KEY, {
        "enabled": config.enabled,
        "autonomy": config.autonomy,
        "interval_minutes": config.interval_minutes,
        "daily_budget": config.daily_budget,
        "quiet_start_hour": config.quiet_start_hour,
        "quiet_end_hour": config.quiet_end_hour,
        "min_gap_minutes": config.min_gap_minutes,
    })


async def get_initiative_job_id(db: AsyncSession) -> Optional[str]:
    value = await get_setting(db, INITIATIVE_JOB_ID_KEY, default=None)
    return value if isinstance(value, str) and value else None


async def set_initiative_job_id(db: AsyncSession, job_id: Optional[str]) -> None:
    await set_setting(db, INITIATIVE_JOB_ID_KEY, job_id)
