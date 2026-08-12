"""
Furi OS — Remote device tokens (2026-08-03)

*(Tier 2, item 5 — "one machine, one room")*

The local token (`~/.jarvis/auth_token`) is a machine secret for a client on the
same machine, and it grants EVERYTHING. A phone needs something different: its
own credential, with a name so it can be recognised, an expiry so an abandoned
device stops working on its own, and a revoke that takes effect immediately.

⚠️ STORED HASHED, and that is not ceremony. The local token has to be readable
(Electron reads the file and hands it to the renderer), so it is stored in the
clear by necessity. A device token never needs to be read back — the only
operation is "does the one being presented match?" — so storing sha256 makes a
leaked `jarvis.db`, backup or sync copy useless for getting in. When you can
verify without reading, store the hash.

No migration: the list lives in the existing `app_settings` k/v table, the
`VoiceConfig`/`FileIndexConfig` pattern.

⚠️ THIS FILE GRANTS NO CAPABILITY BY ITSELF. Which routes a remote token can
reach is decided by `remote_manifest.py` and enforced by what
`create_remote_app` mounts. A valid token on the remote listener still cannot
reach `/chat/stream`, because that route is not there.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.app_settings import get_setting, set_setting
from app.db.models import utc_iso, utc_now

REMOTE_TOKENS_KEY = "remote.devices"

# A paired phone stops working after this unless re-paired. Long enough to be
# useful, short enough that a lost device is not a permanent key.
DEFAULT_TTL_DAYS = 30
# Plenty for a household; a cap stops an unbounded k/v row.
MAX_DEVICES = 10
TOKEN_BYTES = 32


@dataclass
class RemoteDevice:
    """One paired device. `token_hash` is sha256 of the token — the token
    itself exists exactly once, in the response to the pairing call."""

    id: str
    name: str
    token_hash: str
    created_at: str
    expires_at: str
    last_seen_at: Optional[str] = None
    revoked: bool = False

    def is_live(self, now: Optional[datetime] = None) -> bool:
        if self.revoked:
            return False
        try:
            expiry = datetime.fromisoformat(self.expires_at)
        except (TypeError, ValueError):
            return False  # unparseable = not live; never fail open
        now = now or utc_now()
        if expiry.tzinfo is not None:
            expiry = expiry.replace(tzinfo=None)
        return expiry > now


def hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


async def _load(db: AsyncSession) -> list[RemoteDevice]:
    raw = await get_setting(db, REMOTE_TOKENS_KEY, default=None)
    if not isinstance(raw, list):
        return []
    devices: list[RemoteDevice] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            devices.append(RemoteDevice(
                id=str(item["id"]),
                name=str(item.get("name") or "device"),
                token_hash=str(item["token_hash"]),
                created_at=str(item.get("created_at") or ""),
                expires_at=str(item.get("expires_at") or ""),
                last_seen_at=item.get("last_seen_at"),
                revoked=bool(item.get("revoked", False)),
            ))
        except (KeyError, TypeError):
            # A malformed row is dropped, never treated as a valid credential.
            logger.warning("Dropping a malformed remote-device row")
    return devices


async def _save(db: AsyncSession, devices: list[RemoteDevice]) -> None:
    await set_setting(db, REMOTE_TOKENS_KEY, [asdict(d) for d in devices])


async def list_devices(db: AsyncSession) -> list[RemoteDevice]:
    """Every paired device, newest first. Callers must never surface
    `token_hash` — it is not a secret, but it is not the user's business
    either, and showing it invites someone to think it is the token."""
    return sorted(await _load(db), key=lambda d: d.created_at, reverse=True)


async def pair_device(
    db: AsyncSession, name: str, *, ttl_days: int = DEFAULT_TTL_DAYS
) -> tuple[RemoteDevice, str]:
    """Create a device and return it WITH its one-time token.

    ⚠️ The token is returned exactly once and never stored — only its hash is.
    If the user loses it they pair again; there is no recovery, by design."""
    devices = [d for d in await _load(db) if d.is_live()]
    if len(devices) >= MAX_DEVICES:
        raise ValueError(
            f"There are already {MAX_DEVICES} paired devices — revoke one first."
        )
    token = secrets.token_urlsafe(TOKEN_BYTES)
    now = utc_now()
    device = RemoteDevice(
        id=secrets.token_hex(8),
        name=(name or "device").strip()[:64] or "device",
        token_hash=hash_token(token),
        created_at=utc_iso(now) or "",
        expires_at=utc_iso(now + timedelta(days=max(1, ttl_days))) or "",
    )
    await _save(db, [*await _load(db), device])
    logger.info(f"🔗 Paired remote device '{device.name}' (expires {device.expires_at})")
    return device, token


async def revoke_device(db: AsyncSession, device_id: str) -> bool:
    devices = await _load(db)
    found = False
    for d in devices:
        if d.id == device_id and not d.revoked:
            d.revoked = True
            found = True
    if found:
        await _save(db, devices)
        logger.info(f"🔗 Revoked remote device {device_id}")
    return found


async def verify_token(db: AsyncSession, token: str) -> Optional[RemoteDevice]:
    """The live device this token belongs to, or None.

    Constant-time compare against each candidate hash — the same discipline
    `auth._token_ok` uses. Fails closed on anything unparseable or expired."""
    if not token:
        return None
    presented = hash_token(token)
    for device in await _load(db):
        if not device.is_live():
            continue
        if secrets.compare_digest(presented, device.token_hash):
            return device
    return None


async def touch_device(db: AsyncSession, device_id: str) -> None:
    """Record that a device was used. Best-effort — this is a convenience for
    the pairing UI ("last seen"), and a write failure must never cost a
    request."""
    try:
        devices = await _load(db)
        for d in devices:
            if d.id == device_id:
                d.last_seen_at = utc_iso(utc_now())
                await _save(db, devices)
                return
    except Exception as e:  # pragma: no cover — defensive
        logger.debug(f"remote device touch failed (non-critical): {e}")
