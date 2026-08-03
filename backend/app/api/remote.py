"""
Jarvis OS — Remote pairing API (2026-08-03)

Pair a phone, see what is paired, revoke one. Served on the LOCAL app only —
these routes are deliberately absent from `remote_manifest.REMOTE_ROUTES`, so a
paired device cannot pair another or revoke its own revocation. Pairing happens
at the machine, which is the point.

GET    /api/remote            what is paired, and whether the listener is up
POST   /api/remote/pair       create a device; returns the token ONCE
DELETE /api/remote/{id}       revoke
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.dependencies import get_db
from app.core.remote_link import listen_address, pairing_link, qr_data_uri
from app.core.remote_tokens import (
    DEFAULT_TTL_DAYS,
    MAX_DEVICES,
    list_devices,
    pair_device,
    revoke_device,
)

router = APIRouter()


class PairRequest(BaseModel):
    name: str = Field("phone", min_length=1, max_length=64)
    ttl_days: int = Field(DEFAULT_TTL_DAYS, ge=1, le=365)


def _device_out(device) -> dict:
    """⚠️ NEVER serializes `token_hash`. It is not a secret, but showing it
    invites someone to think it is the token — and the token exists exactly
    once, in the pairing response."""
    return {
        "id": device.id,
        "name": device.name,
        "created_at": device.created_at,
        "expires_at": device.expires_at,
        "last_seen_at": device.last_seen_at,
        "revoked": device.revoked,
        "live": device.is_live(),
    }


@router.get("", summary="Paired devices + remote listener state")
async def remote_status(db: AsyncSession = Depends(get_db)) -> dict:
    devices = await list_devices(db)
    return {
        "enabled": settings.REMOTE_ENABLED,
        "host": settings.REMOTE_HOST,
        "port": settings.REMOTE_PORT,
        # Where a phone would actually reach it. `host` is the BIND setting and
        # is normally the 0.0.0.0 wildcard, which is not something anyone can
        # type — so the card shows this one.
        "address": listen_address(settings.REMOTE_HOST),
        "max_devices": MAX_DEVICES,
        "devices": [_device_out(d) for d in devices],
    }


@router.post("/pair", summary="Pair a device (returns its token once)")
async def pair(
    request: PairRequest, db: AsyncSession = Depends(get_db)
) -> dict:
    """The token is returned HERE AND NOWHERE ELSE — only its hash is stored.
    Lose it and you pair again; there is no recovery, deliberately."""
    try:
        device, token = await pair_device(db, request.name, ttl_days=request.ttl_days)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    # ⚠️ THE ONLY MOMENT THE TOKEN EXISTS IN THE CLEAR, so the link and its QR
    # are built HERE. There is no route that regenerates them later — only the
    # hash is stored, deliberately (`remote_tokens.pair_device`). Until
    # 2026-08-04 this returned the literal string "http://<this-machine>:8765"
    # and nothing in the repo resolved the machine's address, so the feature
    # could not be reached without the user going and finding their own IP.
    url = pairing_link(token, host=settings.REMOTE_HOST, port=settings.REMOTE_PORT)
    return {
        **_device_out(device),
        "token": token,
        "url": url,
        # Nullable: `segno` is optional, and a missing QR must never fail
        # pairing — the link alone is enough to finish the job.
        "qr": qr_data_uri(url),
        "note": (
            "This token is shown once. It grants read access plus approving, "
            "answering, pausing and cancelling work already in progress — it "
            "cannot start anything."
        ),
    }


@router.delete("/{device_id}", summary="Revoke a paired device")
async def revoke(device_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    if not await revoke_device(db, device_id):
        raise HTTPException(status_code=404, detail="No live device with that id")
    return {"revoked": True, "id": device_id}
