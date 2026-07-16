"""
Phase 9 — the suggestions domain + feedback signal (app/core/suggestions.py).

Uses the conftest db_session (in-memory AsyncSession). Covers CRUD, the
accept-starts-an-approval-gated-task invariant (start_task is stubbed — the
point is that accept routes THROUGH it, re-deriving the plan so the gate
re-applies), dismiss, expiry, dedupe, the bipolar affinity signal + clamp, and
the chat-leak fix (initiative_affinity:* keys never render into MEMORY CONTEXT).
"""
from datetime import timedelta

import pytest

from app.core import suggestions as sug
from app.db.models import Preference, Suggestion, utc_now
from app.memory.engine import MemoryEngine


# ------------------------------------------------------------------ dedupe key

def test_dedupe_key_normalizes():
    a = sug.make_dedupe_key("email_followup", "Reply to Bob", "Draft a reply to Bob!")
    b = sug.make_dedupe_key("email_followup", "reply  to   bob", "draft a reply to bob")
    assert a == b
    assert a.startswith("email_followup:")


def test_dedupe_key_uses_goal_over_title():
    with_goal = sug.make_dedupe_key("general", "Title A", "the goal")
    title_only = sug.make_dedupe_key("general", "Title A", None)
    assert with_goal != title_only


# ------------------------------------------------------------------ CRUD

async def test_create_and_list(db_session):
    await sug.create_suggestion(db_session, category="general", title="A", body="do a")
    await sug.create_suggestion(db_session, category="general", title="B", body="do b")
    rows = await sug.list_suggestions(db_session)
    assert {r.title for r in rows} == {"A", "B"}
    # newest first
    assert rows[0].title == "B"


async def test_list_filters_by_status(db_session):
    await sug.create_suggestion(db_session, category="general", title="P", body="x")
    d = await sug.create_suggestion(db_session, category="general", title="D", body="x")
    await sug.dismiss_suggestion(db_session, d.id)
    pending = await sug.list_suggestions(db_session, status="pending")
    assert [r.title for r in pending] == ["P"]


async def test_create_sets_ttl_and_dedupe(db_session):
    row = await sug.create_suggestion(db_session, category="general", title="A", body="x")
    assert row.expires_at is not None and row.expires_at > utc_now()
    assert row.dedupe_key  # auto-derived


async def test_has_recent_duplicate(db_session):
    row = await sug.create_suggestion(db_session, category="general", title="A", body="x")
    assert await sug.has_recent_duplicate(db_session, row.dedupe_key) is True
    assert await sug.has_recent_duplicate(db_session, "nope:xyz") is False


# ------------------------------------------------------------------ accept

async def test_accept_without_goal_marks_accepted_and_tunes_positive(db_session):
    row = await sug.create_suggestion(db_session, category="calendar_prep", title="A", body="x")
    updated = await sug.accept_suggestion(db_session, row.id)
    assert updated.status == "accepted"
    assert updated.task_id is None  # no goal → nothing started
    affinities = await sug.get_affinities(db_session)
    assert affinities["calendar_prep"] == 1


async def test_accept_with_goal_starts_approval_gated_task(db_session, monkeypatch):
    """A suggestion with a goal routes THROUGH start_task on accept — the plan
    is re-derived, so the approval gate re-applies (we assert the routing, not
    the planner). start_task is stubbed to avoid a real LLM/planner run."""
    started = {}

    class _FakeTask:
        id = "task-123"

    async def _fake_start(db, goal, session_id, conversation="", memory="", provider=None):
        started["goal"] = goal
        started["session_id"] = session_id
        return _FakeTask()

    async def _fake_mem(db, goal):
        return ""

    import app.agents as agents
    monkeypatch.setattr(agents, "start_task", _fake_start)
    monkeypatch.setattr(agents, "planner_memory_context", _fake_mem)

    row = await sug.create_suggestion(
        db_session, category="file_cleanup", title="Tidy", body="tidy up",
        goal="Organize my desktop screenshots", session_id="s1",
    )
    updated = await sug.accept_suggestion(db_session, row.id)
    assert started["goal"] == "Organize my desktop screenshots"
    assert started["session_id"] == "s1"
    assert updated.status == "accepted" and updated.task_id == "task-123"


async def test_accept_non_pending_returns_none(db_session):
    row = await sug.create_suggestion(db_session, category="general", title="A", body="x")
    await sug.dismiss_suggestion(db_session, row.id)
    assert await sug.accept_suggestion(db_session, row.id) is None  # no double-run


async def test_accept_missing_returns_none(db_session):
    assert await sug.accept_suggestion(db_session, "does-not-exist") is None


# ------------------------------------------------------------------ dismiss

async def test_dismiss_marks_and_tunes_negative(db_session):
    row = await sug.create_suggestion(db_session, category="file_cleanup", title="A", body="x")
    updated = await sug.dismiss_suggestion(db_session, row.id)
    assert updated.status == "dismissed"
    affinities = await sug.get_affinities(db_session)
    assert affinities["file_cleanup"] == -1


async def test_dismiss_non_pending_returns_none(db_session):
    row = await sug.create_suggestion(db_session, category="general", title="A", body="x")
    await sug.accept_suggestion(db_session, row.id)
    assert await sug.dismiss_suggestion(db_session, row.id) is None


# ------------------------------------------------------------------ expiry

async def test_expire_stale(db_session):
    fresh = await sug.create_suggestion(db_session, category="general", title="fresh", body="x")
    stale = Suggestion(
        title="stale", body="x", category="general", status="pending", autonomy="suggest",
        created_at=utc_now() - timedelta(days=3), expires_at=utc_now() - timedelta(days=2),
    )
    db_session.add(stale)
    await db_session.commit()

    n = await sug.expire_stale(db_session)
    assert n == 1
    await db_session.refresh(stale)
    await db_session.refresh(fresh)
    assert stale.status == "expired"
    assert fresh.status == "pending"  # not past expiry


# ------------------------------------------------------------------ affinity

async def test_affinity_accept_clamps_at_max(db_session):
    for _ in range(8):
        await sug.apply_initiative_feedback(db_session, "calendar_prep", accepted=True)
    assert (await sug.get_affinities(db_session))["calendar_prep"] == sug.AFFINITY_MAX


async def test_affinity_dismiss_clamps_at_min(db_session):
    for _ in range(8):
        await sug.apply_initiative_feedback(db_session, "file_cleanup", accepted=False)
    assert (await sug.get_affinities(db_session))["file_cleanup"] == sug.AFFINITY_MIN


async def test_affinity_accept_then_dismiss_nets(db_session):
    await sug.apply_initiative_feedback(db_session, "general", accepted=True)
    await sug.apply_initiative_feedback(db_session, "general", accepted=True)
    await sug.apply_initiative_feedback(db_session, "general", accepted=False)
    assert (await sug.get_affinities(db_session))["general"] == 1


# ------------------------------------------------- the chat-leak fix (privacy)

async def test_affinity_keys_are_hidden_from_chat_preferences(db_session):
    """The internal affinity rows must never render into the chat MEMORY
    CONTEXT — get_preferences() filters them by default; the initiative
    gatherer reads them with include_internal=True."""
    # A real user preference + an internal affinity row.
    db_session.add(Preference(key="email_style", value="concise and formal"))
    await db_session.commit()
    await sug.apply_initiative_feedback(db_session, "calendar_prep", accepted=True)

    mem = MemoryEngine(db=db_session, qdrant=None)
    public = await mem.get_preferences()
    keys = {p.key for p in public}
    assert "email_style" in keys
    assert not any(k.startswith("initiative_affinity:") for k in keys)

    internal = await mem.get_preferences(include_internal=True)
    assert any(k.key.startswith("initiative_affinity:") for k in internal)


async def test_format_context_excludes_affinity_rows(db_session):
    """Belt: the rendered context block never contains an affinity score."""
    await sug.apply_initiative_feedback(db_session, "file_cleanup", accepted=False)
    mem = MemoryEngine(db=db_session, qdrant=None)
    bundle = await mem.retrieve_context("what do you know", session_id=None)
    rendered = await mem.format_context(bundle)
    assert "initiative_affinity" not in rendered
