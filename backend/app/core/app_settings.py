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
