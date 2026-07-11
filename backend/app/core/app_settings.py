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
from typing import Any, Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AppSetting

# ------------------------------------------------------------------ keys
BRIEFING_CONFIG_KEY = "daily_briefing.config"
BRIEFING_JOB_ID_KEY = "daily_briefing.job_id"


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
