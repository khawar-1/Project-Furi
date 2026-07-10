"""
Jarvis OS — Pending Session-State Persistence (Phase 3.5)

Snapshots a ConversationSession's PARKED QUESTIONS (pending_resolution /
pending_creation) and the session's confirmed names to the SQLite
`pending_resolutions` table, and restores them when the in-memory session is
cold (backend restart, or the 30-minute session TTL evicted it).

Boundary with conversation_state.py ("foreground owns state"):
- While a session is ALIVE in memory, the in-memory object stays the single
  authority — this module never mutates a live session.
- The SQLite row only resurrects COLD sessions, so an unanswered
  "which jamil?" survives a lunch break or a restart instead of silently
  dropping the parked fact.
- Restored pending questions get a FRESH answer window (`expires`): the user
  is back, the system prompt re-asks, and the reply can resolve it.

Every function is failure-tolerant: persistence must never break a chat turn.
"""
import json
import time
from datetime import timedelta
from typing import Optional

from loguru import logger
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PendingSessionState, utc_now
from app.memory.conversation_state import (
    CONVERSATION_SESSIONS,
    ConversationSession,
    PendingCreation,
    PendingResolution,
)

PENDING_DB_TTL_SECONDS = 24 * 3600  # how long a parked question survives unanswered


# ============================================================ serialization

def _resolution_to_dict(pr: PendingResolution) -> dict:
    return {
        "original_name": pr.original_name,
        "pending_update": pr.pending_update,
        "candidates": pr.candidates,
        "pending_shared_facts": pr.pending_shared_facts,
        "unresolved_mentions": pr.unresolved_mentions,
        "resolved_so_far": pr.resolved_so_far,
    }


def _resolution_from_dict(data: dict) -> PendingResolution:
    return PendingResolution(
        original_name=data.get("original_name") or "",
        pending_update=data.get("pending_update") or {},
        candidates=data.get("candidates") or [],
        pending_shared_facts=data.get("pending_shared_facts") or [],
        unresolved_mentions=data.get("unresolved_mentions") or [],
        resolved_so_far=data.get("resolved_so_far") or {},
        # fresh answer window — the restored question is re-asked
    )


def _creation_to_dict(pc: PendingCreation) -> dict:
    return {
        "name": pc.name,
        "pending_update": pc.pending_update,
        "pending_shared_facts": pc.pending_shared_facts,
        "resolved_so_far": pc.resolved_so_far,
    }


def _creation_from_dict(data: dict) -> PendingCreation:
    return PendingCreation(
        name=data.get("name") or "",
        pending_update=data.get("pending_update") or {},
        pending_shared_facts=data.get("pending_shared_facts") or [],
        resolved_so_far=data.get("resolved_so_far") or {},
    )


def _session_is_live(sess: ConversationSession) -> bool:
    return time.time() - sess.last_updated <= sess.ttl


# ================================================================== the API

async def save_pending_state(db: AsyncSession, session_id: str) -> None:
    """Write-through snapshot of the session's parked state. Called after the
    points where pending state settles (end of a chat turn's foreground
    resolution, end of background extraction). A session with nothing parked
    and nothing confirmed deletes its row."""
    try:
        sess = CONVERSATION_SESSIONS.get(session_id)
        now = time.time()
        pr = sess.pending_resolution if sess else None
        pc = sess.pending_creation if sess else None
        if pr is not None and now > pr.expires:
            pr = None
        if pc is not None and now > pc.expires:
            pc = None
        confirmed = dict(sess.confirmed_names) if sess else {}

        if pr is None and pc is None and not confirmed:
            await db.execute(
                delete(PendingSessionState).where(
                    PendingSessionState.session_id == session_id
                )
            )
            await db.commit()
            return

        row = await db.get(PendingSessionState, session_id)
        if row is None:
            row = PendingSessionState(session_id=session_id)
            db.add(row)
        row.resolution = json.dumps(_resolution_to_dict(pr)) if pr else None
        row.creation = json.dumps(_creation_to_dict(pc)) if pc else None
        row.confirmed_names = json.dumps(confirmed) if confirmed else None
        row.expires_at = utc_now() + timedelta(seconds=PENDING_DB_TTL_SECONDS)
        await db.commit()
    except Exception as e:
        logger.warning(f"Persisting pending session state failed (non-critical): {e}")


async def restore_pending_state(db: AsyncSession, session_id: str) -> None:
    """Resurrect a COLD session's parked questions from SQLite. No-op when the
    in-memory session is alive (memory is authoritative) or no row exists.
    Called at the top of a chat turn, BEFORE any get_session() use — so the
    task gate and the foreground resolution block both see the restored
    question exactly as if the backend had never restarted."""
    try:
        sess = CONVERSATION_SESSIONS.get(session_id)
        if sess is not None and _session_is_live(sess):
            return

        row = await db.get(PendingSessionState, session_id)
        if row is None:
            return
        if row.expires_at <= utc_now():
            await db.execute(
                delete(PendingSessionState).where(
                    PendingSessionState.session_id == session_id
                )
            )
            await db.commit()
            return

        restored = ConversationSession()
        if row.resolution:
            restored.pending_resolution = _resolution_from_dict(json.loads(row.resolution))
        if row.creation:
            restored.pending_creation = _creation_from_dict(json.loads(row.creation))
        if row.confirmed_names:
            restored.confirmed_names = json.loads(row.confirmed_names)
        CONVERSATION_SESSIONS[session_id] = restored
        logger.info(
            f"Restored parked session state for {session_id} from SQLite "
            f"(resolution={row.resolution is not None}, "
            f"creation={row.creation is not None})"
        )
    except Exception as e:
        logger.warning(f"Restoring pending session state failed (non-critical): {e}")


async def purge_expired_pending_state(db: AsyncSession) -> None:
    """Delete expired pending_resolutions rows (called at startup)."""
    result = await db.execute(
        delete(PendingSessionState).where(PendingSessionState.expires_at <= utc_now())
    )
    await db.commit()
    if result.rowcount:
        logger.info(f"Purged {result.rowcount} expired pending session row(s)")
