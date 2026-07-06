"""
Extraction pipeline guards: the user must never become their own contact,
even when the LLM lists them in people_mentioned.
"""
import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.memory.conversation_state import get_session
from app.memory.extractor import run_extraction_pipeline


def _provider_returning(payload: dict) -> AsyncMock:
    provider = AsyncMock()
    provider.chat.return_value = MagicMock(content=json.dumps(payload))
    return provider


@pytest.mark.asyncio
async def test_user_named_in_same_turn_is_not_parked_as_contact(engine, db_session, session_id):
    """'My name is Khawar' — the enrichment carries the name; people_mentioned
    listing 'Khawar' must be dropped, not parked for creation."""
    provider = _provider_returning({
        "people_mentioned": [{"name": "Khawar"}],
        "user_profile_enrichment": {"name": "Khawar"},
    })

    await run_extraction_pipeline(
        user_message="Hi, my name is Khawar.",
        assistant_message="Nice to meet you, Khawar.",
        session_id=session_id,
        db=db_session,
        engine=engine,
        provider=provider,
    )

    profile = await engine.get_user_profile()
    assert profile.name == "Khawar"
    assert await engine.get_all_contacts() == []
    assert get_session(session_id).pending_creation is None


@pytest.mark.asyncio
async def test_known_user_name_is_not_parked_as_contact(engine, db_session, session_id):
    await engine.store_user_profile({"name": "Khawar"})
    provider = _provider_returning({
        "people_mentioned": [{"name": "khawar", "relationship": "other",
                              "new_facts": [{"fact": "Some fact", "category": "other"}]}],
    })

    await run_extraction_pipeline(
        user_message="Note that Khawar likes chess.",
        assistant_message="Got it.",
        session_id=session_id,
        db=db_session,
        engine=engine,
        provider=provider,
    )

    assert await engine.get_all_contacts() == []
    assert get_session(session_id).pending_creation is None


@pytest.mark.asyncio
async def test_unknown_person_with_facts_is_parked(engine, db_session, session_id):
    provider = _provider_returning({
        "people_mentioned": [{"name": "Zara", "relationship": "friend"}],
        "facts_about_user": [{
            "fact_user_perspective": "Had lunch with {CONTACT:Zara} on 2026-07-05",
            "fact_contact_perspective": "Had lunch with {USER} on 2026-07-05",
            "subject": "shared",
            "related_contacts": ["Zara"],
            "event_date": "2026-07-05",
        }],
    })

    await run_extraction_pipeline(
        user_message="I had lunch with Zara yesterday.",
        assistant_message="Sounds nice.",
        session_id=session_id,
        db=db_session,
        engine=engine,
        provider=provider,
    )

    sess = get_session(session_id)
    assert sess.pending_creation is not None
    assert sess.pending_creation.name == "Zara"
    assert len(sess.pending_creation.pending_shared_facts) == 1
    # relationship detail parked too
    assert sess.pending_creation.pending_update.get("relationship_type") == "friend"
    # nothing written yet
    assert await engine.get_all_contacts() == []
