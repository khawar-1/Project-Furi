"""
Furi OS — Integrations API (Phase 5, Part 1)

Connect/disconnect/status for the Google account. Everything goes through
app/integrations/google_auth.py; this router never touches token files or
Google endpoints directly (the reminders-router rule: routers orchestrate,
modules own their domain).

The connect flow is asynchronous by nature — the user finishes it in their
browser — so POST /connect returns immediately ("pending") and the UI polls
GET /status (purely local, no network) until connected or the flow times
out. Like the whole API, this is loopback-only and relies on the
BACKEND_HOST=127.0.0.1 rule.
"""
from fastapi import APIRouter, HTTPException

from app.integrations.google_auth import auth_manager

router = APIRouter()


@router.get("/google/status", summary="Google account connection status")
async def google_status() -> dict:
    return auth_manager().status()


@router.post("/google/connect", summary="Start the Google OAuth consent flow")
async def google_connect() -> dict:
    manager = auth_manager()
    if not manager.is_configured:
        raise HTTPException(
            status_code=400,
            detail=(
                "Google OAuth is not configured — set GOOGLE_CLIENT_ID and "
                "GOOGLE_CLIENT_SECRET in .env, then restart the backend."
            ),
        )
    status = await manager.start_connect()
    return {"status": status}


@router.post("/google/disconnect", summary="Revoke and forget the Google account")
async def google_disconnect() -> dict:
    return await auth_manager().disconnect()
