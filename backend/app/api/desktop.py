"""
Furi OS — Desktop control API (Feature 2)

Settings and a read-only view of the application registry for the Settings
panel. The router only orchestrates (the reminders-router rule: routers
orchestrate, modules own their domain) — `app/core/app_settings.py` owns the
config and `app/core/desktop.py` owns everything that touches the OS.

`GET /apps` is the TRUST SURFACE. `launch_app` can start anything in this list
and nothing outside it, so "what exactly can Furi open?" has to be answerable
without running a plan and without reading the code.
"""
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel

from app.core.app_settings import DesktopConfig, get_desktop_config, set_desktop_config
from app.core.dependencies import get_db
from app.core.desktop import (
    DesktopError,
    UnsupportedDesktopController,
    discover_apps,
    get_controller,
)

router = APIRouter()


class DesktopUpdate(BaseModel):
    enabled: bool
    allow_launch: bool = True
    allow_close: bool = False
    allow_input: bool = True
    allow_clipboard: bool = False
    allow_screenshot: bool = False
    screenshot_retention_days: int = 7


def _supported() -> tuple[bool, str]:
    """Whether this machine can do desktop control at all, and why not.

    Reported rather than hidden: on a platform without an implementation every
    tool would fail at execution time with the same message, and a user is
    owed that fact in Settings instead of after a plan runs."""
    try:
        controller = get_controller()
    except Exception as e:  # noqa: BLE001
        return False, f"Desktop control is unavailable: {type(e).__name__}"
    if isinstance(controller, UnsupportedDesktopController):
        try:
            controller.list_windows()
        except DesktopError as e:
            return False, str(e)
        except Exception:  # noqa: BLE001
            return False, "Desktop control is not supported on this platform."
    return True, ""


async def _payload(db) -> dict:
    config = await get_desktop_config(db)
    supported, detail = _supported()
    return {
        "enabled": config.enabled,
        "allow_launch": config.allow_launch,
        "allow_close": config.allow_close,
        "allow_input": config.allow_input,
        "allow_clipboard": config.allow_clipboard,
        "allow_screenshot": config.allow_screenshot,
        "screenshot_retention_days": config.screenshot_retention_days,
        "supported": supported,
        "detail": detail,
    }


@router.get("/settings")
async def get_settings(db=Depends(get_db)) -> dict:
    return await _payload(db)


@router.put("/settings")
async def put_settings(update: DesktopUpdate, db=Depends(get_db)) -> dict:
    """Save the settings.

    Validation is a 400 rather than a silent clamp: this is a human editing a
    field, and a silently-adjusted value reads as "Furi ignored me" (the
    contact-validation rule — a silent drop is right for the LLM, wrong for a
    person). The coercer still clamps, as the defence against a hand-edited row."""
    if not (1 <= update.screenshot_retention_days <= 365):
        raise HTTPException(
            status_code=400,
            detail="Screenshot retention must be between 1 and 365 days.",
        )
    await set_desktop_config(
        db,
        DesktopConfig(
            enabled=update.enabled,
            allow_launch=update.allow_launch,
            allow_close=update.allow_close,
            allow_input=update.allow_input,
            allow_clipboard=update.allow_clipboard,
            allow_screenshot=update.allow_screenshot,
            screenshot_retention_days=update.screenshot_retention_days,
        ),
    )
    return await _payload(db)


@router.get("/apps")
async def list_apps(refresh: bool = False, db=Depends(get_db)) -> dict:
    """Every application `launch_app` can start — the audit list.

    This is the complete reachable surface of that tool, which is the property
    that keeps it out of DESTRUCTIVE: there is no path parameter and no command
    line, so what is listed here is what can be launched, and nothing else."""
    try:
        apps = discover_apps(force=refresh)
    except Exception as e:  # noqa: BLE001 — an audit view must never 500
        logger.warning(f"Desktop app discovery failed: {type(e).__name__}")
        return {"apps": [], "count": 0, "detail": f"Could not read the Start Menu: {type(e).__name__}"}
    # Names only. The .lnk paths are an implementation detail, and putting a
    # list of filesystem paths on a settings screen invites someone to think
    # they can be edited into the tool.
    return {"apps": [a.name for a in apps], "count": len(apps), "detail": ""}
