"""
Phase 3.5 — Pending session-state persistence.
A parked memory question ("which jamil?" / "add daud?") must survive a
backend restart and the 30-minute session TTL: save_pending_state snapshots
it to SQLite, restore_pending_state resurrects COLD sessions only — a live
in-memory session is never overwritten.
"""
import time

from sqlalchemy import select

from app.db.models import PendingSessionState
from app.memory.conversation_state import (
    CONVERSATION_SESSIONS,
    ConversationSession,
    PendingCreation,
    PendingResolution,
)
from app.memory import session_persistence
from app.memory.session_persistence import (
    restore_pending_state,
    save_pending_state,
    purge_expired_pending_state,
)


def _park_resolution(session_id: str) -> PendingResolution:
    pr = PendingResolution(
        original_name="jamil",
        pending_update={"new_facts": [{"fact": "{USER} plays tekken with {CONTACT:jamil}"}]},
        candidates=[{"id": "c1", "name": "Jamil Ali"}, {"id": "c2", "name": "Jamil Khan"}],
        pending_shared_facts=[{"fact_user_perspective": "{USER} went gym with {CONTACT:jamil}"}],
        unresolved_mentions=[{"name": "jamil", "candidates": [
            {"id": "c1", "name": "Jamil Ali"}, {"id": "c2", "name": "Jamil Khan"},
        ]}],
        resolved_so_far={"ali": "c9"},
    )
    sess = ConversationSession()
    sess.pending_resolution = pr
    sess.confirmed_names = {"ali": "c9"}
    CONVERSATION_SESSIONS[session_id] = sess
    return pr


async def test_resolution_round_trip_after_restart(db_session, session_id):
    _park_resolution(session_id)
    await save_pending_state(db_session, session_id)

    CONVERSATION_SESSIONS.pop(session_id, None)  # simulate a backend restart
    await restore_pending_state(db_session, session_id)

    sess = CONVERSATION_SESSIONS.get(session_id)
    assert sess is not None
    pr = sess.pending_resolution
    assert pr is not None
    assert pr.original_name == "jamil"
    assert [c["name"] for c in pr.candidates] == ["Jamil Ali", "Jamil Khan"]
    assert pr.pending_shared_facts[0]["fact_user_perspective"].endswith("{CONTACT:jamil}")
    assert pr.unresolved_mentions[0]["name"] == "jamil"
    assert pr.resolved_so_far == {"ali": "c9"}
    assert pr.expires > time.time()  # fresh answer window — the question re-asks
    assert sess.confirmed_names == {"ali": "c9"}  # confirmed picks survive too


async def test_creation_round_trip_after_restart(db_session, session_id):
    sess = ConversationSession()
    sess.pending_creation = PendingCreation(
        name="daud",
        pending_update={"new_facts": [{"fact": "met {USER} at the gym"}]},
        pending_shared_facts=[{"fact_user_perspective": "{USER} met {CONTACT:daud}"}],
    )
    CONVERSATION_SESSIONS[session_id] = sess
    await save_pending_state(db_session, session_id)

    CONVERSATION_SESSIONS.pop(session_id, None)
    await restore_pending_state(db_session, session_id)

    pc = CONVERSATION_SESSIONS[session_id].pending_creation
    assert pc is not None
    assert pc.name == "daud"
    assert pc.pending_shared_facts[0]["fact_user_perspective"] == "{USER} met {CONTACT:daud}"


async def test_live_session_is_never_overwritten_by_restore(db_session, session_id):
    _park_resolution(session_id)
    await save_pending_state(db_session, session_id)

    # The live session moves on: the question gets answered and cleared
    CONVERSATION_SESSIONS[session_id].pending_resolution = None
    await restore_pending_state(db_session, session_id)

    # Memory is authoritative while alive — the stale row must not resurrect
    assert CONVERSATION_SESSIONS[session_id].pending_resolution is None


async def test_ttl_evicted_session_is_restored(db_session, session_id):
    """The 30-minute session TTL used to destroy the parked question; with
    persistence the cold session resurrects it instead."""
    _park_resolution(session_id)
    await save_pending_state(db_session, session_id)

    sess = CONVERSATION_SESSIONS[session_id]
    sess.last_updated = time.time() - sess.ttl - 1  # lunch break
    await restore_pending_state(db_session, session_id)

    restored = CONVERSATION_SESSIONS[session_id]
    assert restored.pending_resolution is not None
    assert restored.pending_resolution.original_name == "jamil"
    assert time.time() - restored.last_updated <= restored.ttl  # alive again


async def test_settled_state_deletes_the_row(db_session, session_id):
    _park_resolution(session_id)
    await save_pending_state(db_session, session_id)

    # Everything settles: question answered, nothing confirmed to remember
    CONVERSATION_SESSIONS[session_id] = ConversationSession()
    await save_pending_state(db_session, session_id)

    rows = (await db_session.execute(select(PendingSessionState))).scalars().all()
    assert rows == []
    CONVERSATION_SESSIONS.pop(session_id, None)
    await restore_pending_state(db_session, session_id)
    assert session_id not in CONVERSATION_SESSIONS  # nothing to resurrect


async def test_expired_row_is_not_restored(db_session, session_id, monkeypatch):
    monkeypatch.setattr(session_persistence, "PENDING_DB_TTL_SECONDS", -1)
    _park_resolution(session_id)
    await save_pending_state(db_session, session_id)

    CONVERSATION_SESSIONS.pop(session_id, None)
    await restore_pending_state(db_session, session_id)
    assert session_id not in CONVERSATION_SESSIONS
    rows = (await db_session.execute(select(PendingSessionState))).scalars().all()
    assert rows == []  # the dead row was cleaned up


async def test_purge_removes_only_expired_rows(db_session, session_id, monkeypatch):
    _park_resolution(session_id)
    await save_pending_state(db_session, session_id)

    other = f"{session_id}-expired"
    monkeypatch.setattr(session_persistence, "PENDING_DB_TTL_SECONDS", -1)
    _park_resolution(other)
    await save_pending_state(db_session, other)
    CONVERSATION_SESSIONS.pop(other, None)

    await purge_expired_pending_state(db_session)
    rows = (await db_session.execute(select(PendingSessionState))).scalars().all()
    assert [r.session_id for r in rows] == [session_id]
