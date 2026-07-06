"""
Live-transcript regressions from the fishing conversation ("me, ali and jamil
are going to fishing on the 1st of next month" → "i meant hamil and ali raza"):

1. A shared fact with SEVERAL ambiguous names parks them all behind ONE
   question and one reply can resolve them all — never one name slammed over
   every placeholder ("Went to fishing with Ali Raza and Ali Raza").
2. Contact-side text is DERIVED from the user perspective, never trusted from
   the LLM's flip ("Khawar went to fishing with Khawar and Khawar").
3. Future-dated facts are stored as plans, not past events.
"""
from datetime import date

import pytest

from app.api.chat import _build_system_prompt
from app.memory.conversation_state import get_session
from app.memory.engine import (
    normalize_future_phrasing,
    resolve_confirmation_multi,
)
from sqlalchemy import select

from app.db.models import ContactInteraction, SemanticMemory


FISHING_FACT = {
    "fact_user_perspective": "Went to fishing with {CONTACT:ali} and {CONTACT:jamil} on 2026-08-01",
    "fact_contact_perspective": "Went to fishing with {USER} and {USER} on 2026-08-01",  # LLM misfill, must be ignored
    "subject": "shared",
    "related_contacts": ["ali", "jamil"],
    "event_date": "2026-08-01",
    "category": "personal",
}


async def _seed_roster(engine):
    contacts = {}
    for name in ("ali khan", "Ali Raza", "hamil", "jamil", "Jamil Ali", "jamil ali khan"):
        contacts[name] = await engine.create_contact_manual(name)
    return contacts


async def _semantic_contents(db_session):
    result = await db_session.execute(
        select(SemanticMemory).where(SemanticMemory.is_active == True)
    )
    return [m.content for m in result.scalars().all()]


async def _log_for(db_session, contact_id):
    result = await db_session.execute(
        select(ContactInteraction).where(ContactInteraction.contact_id == contact_id)
    )
    return [i.description for i in result.scalars().all()]


# ============================================================
# Future-dated facts are plans, not history
# ============================================================

def test_future_fact_rewritten_as_plan():
    future = date(2026, 8, 1)
    today = date(2026, 7, 6)
    assert normalize_future_phrasing(
        "Went to fishing with hamil and Ali Raza on 2026-08-01", future, today=today
    ) == "Planning to go to fishing with hamil and Ali Raza on 2026-08-01"
    assert normalize_future_phrasing(
        "Played chess with Sam", date(2026, 9, 1), today=today
    ) == "Planning to play chess with Sam"
    # Past and undated facts are untouched
    assert normalize_future_phrasing("Went to coffee", date(2026, 7, 1), today=today) == "Went to coffee"
    assert normalize_future_phrasing("Went to coffee", None, today=today) == "Went to coffee"
    # Already-plan phrasing untouched even for future dates
    assert normalize_future_phrasing("Planning to go fishing", future, today=today) == "Planning to go fishing"


# ============================================================
# All ambiguous names park together, one reply resolves them all
# ============================================================

@pytest.mark.asyncio
async def test_all_ambiguous_names_park_behind_one_question(engine, db_session, session_id):
    await engine.store_user_profile({"name": "Khawar"})
    await _seed_roster(engine)

    saved = await engine.store_shared_fact(dict(FISHING_FACT), session_id=session_id)

    assert saved is None  # parked, nothing written
    assert await _semantic_contents(db_session) == []
    pr = get_session(session_id).pending_resolution
    assert pr is not None
    mentions = pr.mentions()
    assert [m["name"] for m in mentions] == ["ali", "jamil"]
    # "ali" may mean any contact carrying that name — including "Jamil Ali"
    assert {"ali khan", "Ali Raza"} <= {c["name"] for c in mentions[0]["candidates"]}
    assert {"jamil", "Jamil Ali", "jamil ali khan"} <= {c["name"] for c in mentions[1]["candidates"]}


@pytest.mark.asyncio
async def test_reply_answers_both_names_at_once(engine, session_id):
    contacts = await _seed_roster(engine)
    mentions = [
        {"name": "ali", "candidates": [
            {"id": contacts["ali khan"].id, "name": "ali khan"},
            {"id": contacts["Ali Raza"].id, "name": "Ali Raza"},
        ]},
        {"name": "jamil", "candidates": [
            {"id": contacts["jamil"].id, "name": "jamil"},
            {"id": contacts["Jamil Ali"].id, "name": "Jamil Ali"},
            {"id": contacts["jamil ali khan"].id, "name": "jamil ali khan"},
        ]},
    ]
    all_contacts = await engine.get_all_contacts()

    assignment = resolve_confirmation_multi("i meant hamil and ali raza", mentions, all_contacts)

    # "ali raza" answers "ali" (candidate membership); "hamil" answers "jamil"
    # (name correction — fuzzy pairing with the leftover mention)
    assert assignment == {
        "ali": contacts["Ali Raza"].id,
        "jamil": contacts["hamil"].id,
    }


@pytest.mark.asyncio
async def test_fishing_flow_writes_every_perspective_correctly(engine, db_session, session_id):
    """End-to-end engine replay of the fishing transcript."""
    await engine.store_user_profile({"name": "Khawar"})
    contacts = await _seed_roster(engine)

    # Turn 1: extraction parks the fact behind the two ambiguous names
    await engine.store_shared_fact(dict(FISHING_FACT), session_id=session_id)
    sess = get_session(session_id)
    pr = sess.pending_resolution
    assert pr is not None

    # Turn 2: "i meant hamil and ali raza" — what chat.py does deterministically
    all_contacts = await engine.get_all_contacts()
    by_id = {c.id: c for c in all_contacts}
    assignment = resolve_confirmation_multi("i meant hamil and ali raza", pr.mentions(), all_contacts)
    preresolved = {name.lower(): by_id[cid] for name, cid in assignment.items()}
    parked_facts = pr.pending_shared_facts
    sess.pending_resolution = None

    saved_texts = []
    for fact in parked_facts:
        text = await engine.store_shared_fact(fact, session_id=session_id, preresolved=preresolved)
        if text:
            saved_texts.append(text)

    # User's About Me: future event phrased as a plan, both real names, once
    assert saved_texts == ["Planning to go to fishing with Ali Raza and hamil on 2026-08-01"]
    assert await _semantic_contents(db_session) == saved_texts

    # Each contact's log: the OTHER participants from their point of view —
    # never their own name, never the user's name more than once
    assert await _log_for(db_session, contacts["hamil"].id) == [
        "Planning to go to fishing with Ali Raza and Khawar on 2026-08-01"
    ]
    assert await _log_for(db_session, contacts["Ali Raza"].id) == [
        "Planning to go to fishing with Khawar and hamil on 2026-08-01"
    ]
    # Nothing pending, nothing leaked to the wrong jamils
    assert get_session(session_id).pending_resolution is None
    for other in ("jamil", "Jamil Ali", "jamil ali khan", "ali khan"):
        assert await _log_for(db_session, contacts[other].id) == []


@pytest.mark.asyncio
async def test_partial_answer_reparks_only_remaining_name(engine, db_session, session_id):
    """Reply resolves 'ali' only — 'jamil' re-parks with the resolution
    remembered, and the next reply completes the writes."""
    await engine.store_user_profile({"name": "Khawar"})
    contacts = await _seed_roster(engine)

    await engine.store_shared_fact(dict(FISHING_FACT), session_id=session_id)
    sess = get_session(session_id)

    # Turn 2: only answers the "ali" half
    all_contacts = await engine.get_all_contacts()
    by_id = {c.id: c for c in all_contacts}
    pr = sess.pending_resolution
    assignment = resolve_confirmation_multi("ali raza", pr.mentions(), all_contacts)
    assert assignment == {"ali": contacts["Ali Raza"].id}
    preresolved = {n.lower(): by_id[cid] for n, cid in assignment.items()}
    parked = pr.pending_shared_facts
    sess.pending_resolution = None
    for fact in parked:
        assert await engine.store_shared_fact(fact, session_id=session_id, preresolved=preresolved) is None

    pr2 = get_session(session_id).pending_resolution
    assert pr2 is not None
    assert [m["name"] for m in pr2.mentions()] == ["jamil"]
    assert pr2.resolved_so_far == {"ali": contacts["Ali Raza"].id}
    assert await _semantic_contents(db_session) == []  # still nothing written

    # Turn 3: answers the remaining name; earlier resolution must survive
    assignment2 = resolve_confirmation_multi("i meant hamil", pr2.mentions(), all_contacts)
    preresolved2 = {n.lower(): by_id[cid] for n, cid in pr2.resolved_so_far.items()}
    preresolved2.update({n.lower(): by_id[cid] for n, cid in assignment2.items()})
    parked2 = pr2.pending_shared_facts
    get_session(session_id).pending_resolution = None
    saved = [
        await engine.store_shared_fact(f, session_id=session_id, preresolved=preresolved2)
        for f in parked2
    ]

    assert saved == ["Planning to go to fishing with Ali Raza and hamil on 2026-08-01"]
    assert await _log_for(db_session, contacts["hamil"].id) == [
        "Planning to go to fishing with Ali Raza and Khawar on 2026-08-01"
    ]
    assert await _log_for(db_session, contacts["Ali Raza"].id) == [
        "Planning to go to fishing with Khawar and hamil on 2026-08-01"
    ]


# ============================================================
# LLM perspective misfills can never reach a contact log
# ============================================================

@pytest.mark.asyncio
async def test_all_user_placeholder_misfill_is_blocked(engine, db_session, session_id):
    """When the derivation can't run (template names the user via {USER}) and
    the LLM filled every slot with {USER}, the garbage is dropped."""
    await engine.store_user_profile({"name": "Khawar"})
    contact = await engine.create_contact_manual("hamil")

    fact = {
        "fact_user_perspective": "{USER} hosted a dinner for {CONTACT:hamil} on 2026-07-04",
        "fact_contact_perspective": "{USER} had dinner with {USER} and {USER} on 2026-07-04",
        "subject": "shared",
        "related_contacts": ["hamil"],
        "event_date": "2026-07-04",
        "category": "personal",
    }
    await engine.store_shared_fact(fact, session_id=session_id)

    # User side saved; contact side blocked (user's name appears 3 times)
    assert await _semantic_contents(db_session) == [
        "Khawar hosted a dinner for hamil on 2026-07-04"
    ]
    assert await _log_for(db_session, contact.id) == []


# ============================================================
# The disambiguation question is grouped per name
# ============================================================

def test_prompt_groups_options_per_name():
    mentions = [
        {"name": "ali", "candidates": [
            {"id": "1", "name": "ali khan"}, {"id": "2", "name": "Ali Raza"},
        ]},
        {"name": "jamil", "candidates": [
            {"id": "3", "name": "jamil"}, {"id": "4", "name": "Jamil Ali"},
            {"id": "5", "name": "jamil ali khan"},
        ]},
    ]
    prompt = _build_system_prompt(
        ambiguous_mentions=[
            {"mention": m["name"], "candidates": m["candidates"]} for m in mentions
        ]
    )
    assert '- "ali" could be any of: ali khan, Ali Raza' in prompt
    assert '- "jamil" could be any of: jamil, Jamil Ali, jamil ali khan' in prompt
    assert "EACH ambiguous name SEPARATELY" in prompt
    assert "NEVER merge different names' options" in prompt

    # Same grouping when the question comes from a parked resolution
    prompt2 = _build_system_prompt(
        pending_resolution={
            "original_name": "ali",
            "candidates": mentions[0]["candidates"],
            "mentions": mentions,
        }
    )
    assert '- "ali" → possible matches: ali khan, Ali Raza' in prompt2
    assert '- "jamil" → possible matches: jamil, Jamil Ali, jamil ali khan' in prompt2
    assert "EACH unresolved name SEPARATELY" in prompt2
