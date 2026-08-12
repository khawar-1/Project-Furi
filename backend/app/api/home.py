"""
Furi OS — Home & IoT API (Feature 1)

Connection settings and a read-only device view for the Settings panel. The
router only orchestrates (the reminders-router rule: routers orchestrate,
modules own their domain) — `app/core/app_settings.py` owns the config and
`app/integrations/home_assistant.py` owns everything that speaks to the hub.

The token is WRITE-ONLY over this API: a PUT accepts one, a GET never returns
one (only whether one is stored). That mirrors `/api/autofill`, where a SECRET
value is never returned on read.
"""
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel

from app.core.app_settings import HomeConfig, get_home_config, set_home_config
from app.core.dependencies import get_db
from app.integrations.home_assistant import (
    HomeApiError,
    HomeNotConnectedError,
    get_home_client,
    has_token,
    reset_home_client,
    validate_base_url,
    write_token,
)

router = APIRouter()


class HomeUpdate(BaseModel):
    enabled: bool
    base_url: str = ""
    # None = leave the stored token alone (the common case: the user is
    # toggling `enabled` and the UI has no token to send back, because GET
    # never returned one). "" = explicitly clear it.
    token: str | None = None


async def _payload(db) -> dict:
    config = await get_home_config(db)
    return {
        "enabled": config.enabled,
        "base_url": config.base_url,
        "has_token": has_token(),
        # What the UI needs to explain WHY nothing works, without a round trip
        # to the hub: an enabled integration with no address or no credential is
        # a configuration problem, not a connection problem.
        "configured": bool(config.base_url) and has_token(),
    }


@router.get("/settings")
async def get_settings(db=Depends(get_db)) -> dict:
    return await _payload(db)


@router.put("/settings")
async def put_settings(update: HomeUpdate, db=Depends(get_db)) -> dict:
    """Save the connection settings.

    Validation happens HERE with a 400 rather than a silent drop: this is a
    human editing a field, and a silently-ignored address reads as "Furi is
    broken" (the contact-validation rule — a silent drop is right for the LLM,
    wrong for a person)."""
    base_url = ""
    if update.base_url.strip():
        try:
            base_url = validate_base_url(update.base_url)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    if update.enabled and not base_url:
        raise HTTPException(
            status_code=400,
            detail="Add your hub's address before turning Home & devices on.",
        )

    if update.token is not None:
        write_token(update.token)

    await set_home_config(
        db,
        HomeConfig(enabled=update.enabled, base_url=base_url),
    )
    # The client caches its base URL and token for the life of the process, so a
    # changed address would otherwise keep talking to the old hub until restart.
    reset_home_client()
    return await _payload(db)


@router.post("/test-connection")
async def test_connection(db=Depends(get_db)) -> dict:
    """Ask the hub who it is. The honest answer to 'did my settings work?' —
    every failure mode reports its own message, so the user is never left
    guessing between a wrong address, a wrong token and a hub that is off."""
    config = await get_home_config(db)
    try:
        client = await get_home_client(config.base_url)
        version = await client.ping()
    except HomeNotConnectedError as e:
        return {"connected": False, "detail": str(e)}
    except HomeApiError as e:
        return {"connected": False, "detail": str(e)}
    except Exception as e:  # noqa: BLE001 — a probe must never 500
        logger.warning(f"Home Assistant test-connection failed: {type(e).__name__}")
        return {"connected": False, "detail": f"Unexpected error: {type(e).__name__}"}
    return {
        "connected": True,
        "version": version,
        "detail": f"Connected to Home Assistant {version}".strip(),
    }


@router.get("/devices")
async def list_devices(db=Depends(get_db)) -> dict:
    """Every device the hub exposes, for the Settings audit list — "this is what
    Furi can see and control". Read-only; the agent path goes through the
    tools and their approval gate, never here."""
    config = await get_home_config(db)
    try:
        client = await get_home_client(config.base_url)
        devices = await client.states()
    except HomeNotConnectedError as e:
        return {"devices": [], "count": 0, "connected": False, "detail": str(e)}
    except HomeApiError as e:
        return {"devices": [], "count": 0, "connected": False, "detail": str(e)}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Home Assistant device list failed: {type(e).__name__}")
        return {
            "devices": [], "count": 0, "connected": False,
            "detail": f"Unexpected error: {type(e).__name__}",
        }

    rows = [
        {
            "entity_id": d.entity_id,
            "name": d.name,
            "domain": d.domain,
            "state": d.state,
            "area": d.area,
        }
        for d in devices
    ]
    return {
        "devices": rows,
        "count": len(rows),
        "connected": True,
        "areas": sorted({d.area for d in devices if d.area}),
    }
