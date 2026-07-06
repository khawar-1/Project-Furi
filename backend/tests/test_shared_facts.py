"""
Dual-perspective shared facts — the "coffee with Ali" feature.

store_shared_fact must:
  RESOLVED   → write user perspective to About Me AND contact perspective
               (with the user's real name) to the contact's fact log
  AMBIGUOUS  → park the whole fact in the session's PendingResolution
  NOT_FOUND  → park it in the session's PendingCreation
"""
from datetime import date

import pytest
from sqlalchemy import select

from app.db.models import ContactInteraction, SemanticMemory
from app.memory.conversation_state import get_session
from app.memory.engine import substitute_placeholders, parse_event_date
from app.memory.extraction_schema import ExtractionResult, SharedFact


COFFEE_FACT = {
    "fact_user_perspective": "Went to coffee with {CONTACT:ali} on 2026-07-06",
    "fact_contact_perspective": "Went to coffee with {USER} on 2026-07-06",
    "subject": "shared",
    "related_contacts": ["ali"],
    "event_date": "2026-07-06",
    "category": "personal",
}


async def _semantic_contents(db):
    result = await db.execute(select(SemanticMemory).where(SemanticMemory.is_active == True))
    return [m for m in result.scalars().all()]


async def _interactions_for(db, contact_id):
    result = await db.execute(
        select(ContactInteraction).where(ContactInteraction.contact_id == contact_id)
    )
    return list(result.scalars().all())


# ============================================================
# Placeholder substitution
# ============================================================

def test_substitute_placeholders_user_and_contact():
    out = substitute_placeholders(
        "Went to coffee with {CONTACT:Ali} on 2026-07-06",
        user_name="Khawar",
        contact_mapping={"ali": "Jamil Ali"},
    )
    assert out == "Went to coffee with Jamil Ali on 2026-07-06"

    out = substitute_placeholders(
        "Went to coffee with {USER} on 2026-07-06", user_name="Khawar"
    )
    assert out == "Went to coffee with Khawar on 2026-07-06"


def test_substitute_placeholders_fallbacks():
    # Unknown user → "the user"; unmapped contact → name as said
    out = substitute_placeholders("Met {USER} and {CONTACT:Zara}")
    assert out == "Met the user and Zara"
    # default_contact_name fills unmapped placeholders (post-disambiguation)
    out = substitute_placeholders("Coffee with {CONTACT:ali}", default_contact_name="Jamil Ali")
    assert out == "Coffee with Jamil Ali"


def test_parse_event_date():
    assert parse_event_date("2026-07-06") == date(2026, 7, 6)
    assert parse_event_date("tomorrow") is None
    assert parse_event_date(None) is None


# ============================================================
# store_shared_fact routing
# ============================================================

@pytest.mark.asyncio
async def test_resolved_contact_gets_both_perspectives(engine, db_session, session_id):
    await engine.store_user_profile({"name": "Khawar"})
    contact = await engine.create_contact_manual("Jamil Ali")

    await engine.store_shared_fact(dict(COFFEE_FACT), session_id=session_id)

    # User side: About Me carries the resolved FULL contact name + date
    memories = await _semantic_contents(db_session)
    assert len(memories) == 1
    assert memories[0].content == "Went to coffee with Jamil Ali on 2026-07-06"
    assert memories[0].subject == "shared"
    assert memories[0].event_date == date(2026, 7, 6)

    # Contact side: fact log carries the USER's name + date
    interactions = await _interactions_for(db_session, contact.id)
    assert len(interactions) == 1
    assert interactions[0].description == "Went to coffee with Khawar on 2026-07-06"
    assert interactions[0].event_date == date(2026, 7, 6)


@pytest.mark.asyncio
async def test_resolved_write_is_idempotent(engine, db_session, session_id):
    await engine.store_user_profile({"name": "Khawar"})
    contact = await engine.create_contact_manual("Jamil Ali")

    await engine.store_shared_fact(dict(COFFEE_FACT), session_id=session_id)
    await engine.store_shared_fact(dict(COFFEE_FACT), session_id=session_id)

    assert len(await _semantic_contents(db_session)) == 1
    assert len(await _interactions_for(db_session, contact.id)) == 1


@pytest.mark.asyncio
async def test_ambiguous_name_parks_fact_and_writes_nothing(engine, db_session, session_id):
    await engine.create_contact_manual("Jamil Ali")
    await engine.create_contact_manual("Jamil Khan")

    fact = dict(COFFEE_FACT)
    fact["fact_user_perspective"] = "Went to coffee with {CONTACT:jamil} on 2026-07-06"
    fact["related_contacts"] = ["jamil"]

    await engine.store_shared_fact(fact, session_id=session_id)

    assert await _semantic_contents(db_session) == []
    sess = get_session(session_id)
    assert sess.pending_resolution is not None
    assert sess.pending_resolution.original_name == "jamil"
    assert len(sess.pending_resolution.pending_shared_facts) == 1
    candidate_names = {c["name"] for c in sess.pending_resolution.candidates}
    assert candidate_names == {"Jamil Ali", "Jamil Khan"}


@pytest.mark.asyncio
async def test_unknown_name_parks_creation_and_writes_nothing(engine, db_session, session_id):
    fact = dict(COFFEE_FACT)
    fact["fact_user_perspective"] = "Had lunch with {CONTACT:Zara} on 2026-07-05"
    fact["fact_contact_perspective"] = "Had lunch with {USER} on 2026-07-05"
    fact["related_contacts"] = ["Zara"]

    await engine.store_shared_fact(fact, session_id=session_id)

    assert await _semantic_contents(db_session) == []
    sess = get_session(session_id)
    assert sess.pending_creation is not None
    assert sess.pending_creation.name == "Zara"
    assert len(sess.pending_creation.pending_shared_facts) == 1


@pytest.mark.asyncio
async def test_user_only_fact_single_write(engine, db_session, session_id):
    fact = {
        "fact_user_perspective": "Started learning Rust on 2026-07-01",
        "fact_contact_perspective": "",
        "subject": "user",
        "related_contacts": [],
        "event_date": "2026-07-01",
    }
    await engine.store_shared_fact(fact, session_id=session_id)

    memories = await _semantic_contents(db_session)
    assert len(memories) == 1
    assert memories[0].subject == "user"


# ============================================================
# Applying parked facts after the user answers
# ============================================================

@pytest.mark.asyncio
async def test_apply_parked_fact_after_disambiguation(engine, db_session, session_id):
    await engine.store_user_profile({"name": "Khawar"})
    jamil_ali = await engine.create_contact_manual("Jamil Ali")
    await engine.create_contact_manual("Jamil Khan")

    fact = dict(COFFEE_FACT)
    fact["fact_user_perspective"] = "Went to coffee with {CONTACT:jamil} on 2026-07-06"
    fact["related_contacts"] = ["jamil"]
    await engine.store_shared_fact(fact, session_id=session_id)

    parked = get_session(session_id).pending_resolution.pending_shared_facts[0]
    await engine.apply_shared_fact_to_contact(parked, jamil_ali)

    memories = await _semantic_contents(db_session)
    assert [m.content for m in memories] == ["Went to coffee with Jamil Ali on 2026-07-06"]
    interactions = await _interactions_for(db_session, jamil_ali.id)
    assert [i.description for i in interactions] == ["Went to coffee with Khawar on 2026-07-06"]


@pytest.mark.asyncio
async def test_apply_user_only_after_declined_creation(engine, db_session, session_id):
    await engine.store_user_profile({"name": "Khawar"})
    fact = {
        "fact_user_perspective": "Had lunch with {CONTACT:Zara} on 2026-07-05",
        "fact_contact_perspective": "Had lunch with {USER} on 2026-07-05",
        "subject": "shared",
        "related_contacts": ["Zara"],
        "event_date": "2026-07-05",
    }
    await engine.apply_shared_fact_user_only(fact, as_said_name="Zara")

    memories = await _semantic_contents(db_session)
    assert [m.content for m in memories] == ["Had lunch with Zara on 2026-07-05"]
    # No contact was created, no interactions anywhere
    assert await engine.get_all_contacts() == []


# ============================================================
# Guards against extractor misfills (live-testing regressions)
# ============================================================

@pytest.mark.asyncio
async def test_unflipped_contact_perspective_is_overridden(engine, db_session, session_id):
    """If the LLM misfills the contact perspective, the contact side is DERIVED
    from the user perspective instead — the LLM flip is never trusted when the
    template allows a deterministic flip."""
    await engine.store_user_profile({"name": "Khawar"})
    contact = await engine.create_contact_manual("Jamil Ali")

    fact = dict(COFFEE_FACT)
    fact["related_contacts"] = ["Jamil Ali"]
    fact["fact_user_perspective"] = "Went to coffee with {CONTACT:Jamil Ali} on 2026-07-06"
    # Misfill: contact perspective names the contact instead of {USER}
    fact["fact_contact_perspective"] = "Went to coffee with Jamil Ali on 2026-07-06"

    await engine.store_shared_fact(fact, session_id=session_id)

    memories = await _semantic_contents(db_session)
    assert len(memories) == 1  # user side still written
    interactions = await _interactions_for(db_session, contact.id)
    assert [i.description for i in interactions] == ["Went to coffee with Khawar on 2026-07-06"]


@pytest.mark.asyncio
async def test_store_contact_empty_details_does_not_park_creation(engine, session_id):
    """A bare mention with no facts must not trigger 'want me to add them?'."""
    result = await engine.store_contact(
        "Khawar", {"email": None, "phone": None, "skills": [], "new_facts": []},
        session_id=session_id,
    )
    assert result is None
    assert get_session(session_id).pending_creation is None


# ============================================================
# Extraction schema validation
# ============================================================

def test_extraction_result_accepts_legacy_string_facts():
    result = ExtractionResult.model_validate(
        {"facts_about_user": ["User is a Python developer"]}
    )
    assert result.facts_about_user[0].fact_user_perspective == "User is a Python developer"
    assert result.facts_about_user[0].subject == "user"


def test_extraction_result_defaults_and_clamping():
    result = ExtractionResult.model_validate(
        {
            "people_mentioned": [{"name": "  Ahmed  ", "skills": None}],
            "relationships": [
                {"source_name": "A", "edge_type": "friend_of", "target_name": "B", "confidence": 5}
            ],
            "important_events": [{"title": "X", "importance": "not-a-number"}],
            "unknown_key": {"ignored": True},
        }
    )
    assert result.people_mentioned[0].name == "Ahmed"
    assert result.people_mentioned[0].skills == []
    assert result.relationships[0].confidence == 1.0  # clamped
    assert result.important_events[0].importance == 0.7  # default on garbage
    assert result.facts_about_user == []


def test_shared_fact_normalizes_subject_and_dates():
    fact = SharedFact.model_validate(
        {"fact": "Old-shape fact text here", "subject": "banana", "event_date": "null"}
    )
    assert fact.fact_user_perspective == "Old-shape fact text here"
    assert fact.subject == "user"
    assert fact.event_date is None
