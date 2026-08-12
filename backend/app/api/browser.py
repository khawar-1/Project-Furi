"""
Furi OS — Browser media + account control API (Phase 14, Part 2)

The stop-control surface for a `browse` window left playing (keep_open) plus the
one-time sign-in flow. The media/login sessions are in-memory registries in
app/core/browser_session.py (a live Chromium page is not serializable —
memory-only BY DESIGN); this router only opens/reads/clears them. Behind
AuthMiddleware like every route.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel

from app.core import browser_runtime, browser_session
from app.core.app_settings import (
    BrowserVisionConfig,
    get_browser_vision_config,
    set_browser_vision_config,
)
from app.core.config import settings
from app.core.dependencies import get_db
from app.core.push import push

router = APIRouter()


@router.get("/media", summary="What the browser is currently playing or showing")
async def get_media() -> dict:
    """{playing, title, url} for a media window, PLUS `tabs` — one row per open
    agent tab (2026-08-01) — PLUS the original {window_open, window_title,
    window_url} single-window fields, kept so a client that has not moved to the
    tab list still reads correctly. Cheap and I/O-free: the StatusBar polls it to
    recover its indicators after a reload (the context_status precedent)."""
    active = browser_session.active_media()
    tabs = browser_session.active_browse_tabs()
    window = browser_session.active_result_window() or (tabs[0] if tabs else None)
    return {
        "playing": active is not None,
        "title": active.get("title", "") if active else "",
        "url": active.get("url", "") if active else "",
        "tabs": tabs,
        "window_open": window is not None,
        "window_title": window.get("title", "") if window else "",
        "window_url": window.get("url", "") if window else "",
    }


class CloseWindowRequest(BaseModel):
    # Which tab to close, by site key (the value `tabs[].site` carries). Omitted
    # or empty = close them all, which is what the original single-window Close
    # button meant and still means.
    site: str = ""


@router.post("/close-window", summary="Close a kept-open browser window or tab")
async def close_window(req: Optional[CloseWindowRequest] = None) -> dict:
    """Close the browser window left open for the user — one agent tab by site,
    or every tab plus a commit result page when no site is given. Idempotent —
    closing nothing is fine. Pushes a cleared state so any open StatusBar drops
    the indicator live."""
    site = (req.site if req else "") or ""
    # Everything here lives on the dedicated browser loop — close it there. With
    # no site this closes EVERY tab and every held session (a submitted-form
    # result window included), so there is nothing left to sweep separately.
    closed = await browser_runtime.run_browser(browser_session.close_browse_window(site))
    if closed:
        remaining = browser_session.active_browse_tabs()
        await push("browser_window", {"open": bool(remaining), "tabs": remaining})
    return {"closed": closed, "tabs": browser_session.active_browse_tabs()}


@router.post("/stop-media", summary="Stop and close a playing browser window")
async def stop_media() -> dict:
    """Close the current media session. Idempotent — stopping nothing is fine.
    Pushes a cleared state so any open StatusBar drops the indicator live."""
    # The media session was created on the dedicated browser loop — stop it there
    # (see app/core/browser_runtime.py).
    stopped = await browser_runtime.run_browser(browser_session.stop_media())
    if stopped:
        await push("browser_media", {"playing": False})
    return {"stopped": stopped}


class LoginRequest(BaseModel):
    # Where to send the sign-in window. Defaults to Google's account page; a
    # caller may pass e.g. https://www.youtube.com to sign in there directly.
    url: str = browser_session.DEFAULT_LOGIN_URL


@router.get("/account", summary="Whether a sign-in window is currently open")
async def account_status() -> dict:
    """{login_open}. The Settings card polls this so the button reflects an
    already-open window. I/O-free (the active_media precedent)."""
    return {"login_open": browser_session.login_window_open()}


@router.post("/login", summary="Open a one-time sign-in window in the Furi browser")
async def open_login(req: LoginRequest) -> dict:
    """Open the Furi browser profile as a normal, user-driven window so the
    user can sign into their account BY HAND. Furi never sees the credentials;
    the persistent profile keeps the session for later playback. 503 when no
    browser can launch (Playwright/Chromium missing) — a normal state, said
    plainly, not a 500."""
    try:
        # Launches Chromium (a subprocess) → must run on the browser loop.
        await browser_runtime.run_browser(
            browser_session.open_login_window(req.url or browser_session.DEFAULT_LOGIN_URL)
        )
    except browser_session.BrowserUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        logger.warning(f"open_login failed: {type(exc).__name__}: {exc}")
        raise HTTPException(
            status_code=500,
            detail=f"Could not open the sign-in window: {type(exc).__name__}",
        )
    return {"login_open": True}


@router.post("/close-login", summary="Close the sign-in window")
async def close_login() -> dict:
    """Close the sign-in window if open. Idempotent."""
    # The login window lives on the browser loop; close it there.
    closed = await browser_runtime.run_browser(browser_session.close_login_window())
    return {"closed": closed}


# ----------------------------------------------------- vision fallback (15.3)
class VisionUpdate(BaseModel):
    enabled: bool
    # Optional so a pre-posture client's {"enabled": true} body stays valid (the
    # VoiceConfig PUT convention). An unrecognised value coerces to the default in
    # app_settings rather than 400-ing — one field of a toggle card is not worth
    # failing a save over.
    posture: Optional[str] = None


def _vision_configured() -> bool:
    """Whether a vision credential is present in .env — so the toggle can only
    do something. The key lives in .env (the OAuth/API-key convention); only the
    on/off flag is a runtime setting. Enabling with no key configured stays
    DOM-only, so the card shows this to explain why."""
    provider = (settings.VISION_PROVIDER or "gemini").lower().strip()
    if provider == "gemini":
        return bool(settings.VISION_API_KEY or settings.GEMINI_API_KEY)
    return bool(settings.VISION_API_KEY)


def _vision_state(cfg: BrowserVisionConfig) -> dict:
    return {
        "enabled": cfg.enabled,
        "posture": cfg.posture,
        "configured": _vision_configured(),
        "provider": settings.VISION_PROVIDER,
        "model": settings.VISION_MODEL,
    }


@router.get("/vision", summary="The DOM-first vision fallback toggle + whether a key is configured")
async def get_vision(db=Depends(get_db)) -> dict:
    """{enabled, configured, provider, model}. `configured` reflects .env (a key
    present); `enabled` is the runtime toggle. The Settings card reads both to
    disable the switch with a hint when no vision key is configured."""
    return _vision_state(await get_browser_vision_config(db))


@router.put("/vision", summary="Enable/disable browser vision, and set its posture")
async def put_vision(update: VisionUpdate, db=Depends(get_db)) -> dict:
    """Flip vision on/off and choose its posture ("dom_first" — the default — or
    "vision_first"). Even ON it only runs when a vision key is configured in .env;
    under dom_first it is consulted only where the DOM cannot help."""
    current = await get_browser_vision_config(db)
    await set_browser_vision_config(
        db,
        BrowserVisionConfig(
            enabled=bool(update.enabled),
            posture=(update.posture or current.posture),
        ),
    )
    return _vision_state(await get_browser_vision_config(db))
