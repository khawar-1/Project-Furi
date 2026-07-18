"""
Jarvis OS — Browser media + account control API (Phase 14, Part 2)

The stop-control surface for a `browse` window left playing (keep_open) plus the
one-time sign-in flow. The media/login sessions are in-memory registries in
app/core/browser_session.py (a live Chromium page is not serializable —
memory-only BY DESIGN); this router only opens/reads/clears them. Behind
AuthMiddleware like every route.
"""
from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel

from app.core import browser_runtime, browser_session
from app.core.push import push

router = APIRouter()


@router.get("/media", summary="What the browser is currently playing or showing")
async def get_media() -> dict:
    """{playing, title, url} for a media window PLUS {window_open, window_title,
    window_url} for a kept-open commit result window. Cheap and I/O-free — the
    StatusBar polls it to recover both indicators after a reload (the
    context_status precedent)."""
    active = browser_session.active_media()
    result = browser_session.active_result_window()
    return {
        "playing": active is not None,
        "title": active.get("title", "") if active else "",
        "url": active.get("url", "") if active else "",
        "window_open": result is not None,
        "window_title": result.get("title", "") if result else "",
        "window_url": result.get("url", "") if result else "",
    }


@router.post("/close-window", summary="Close a kept-open commit result window")
async def close_window() -> dict:
    """Close the browser window left open after a form submit/upload so the user
    could see the response. Idempotent — closing nothing is fine. Pushes a cleared
    state so any open StatusBar drops the indicator live."""
    # The result window lives on the dedicated browser loop — close it there.
    closed = await browser_runtime.run_browser(browser_session.close_result_window())
    if closed:
        await push("browser_window", {"open": False})
    return {"closed": closed}


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


@router.post("/login", summary="Open a one-time sign-in window in the Jarvis browser")
async def open_login(req: LoginRequest) -> dict:
    """Open the Jarvis browser profile as a normal, user-driven window so the
    user can sign into their account BY HAND. Jarvis never sees the credentials;
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
