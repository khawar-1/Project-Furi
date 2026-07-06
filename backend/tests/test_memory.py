"""
Jarvis OS — Memory engine tests (SQLite-only mode, no Qdrant).
Run with: venv\\Scripts\\python -m pytest tests/ -v
"""
import json

import pytest
from unittest.mock import AsyncMock, MagicMock


# ============================================================
# 1. Store and retrieve semantic memory
# ============================================================

@pytest.mark.asyncio
async def test_store_and_retrieve_semantic_memory(engine):
    memory = await engine.store_semantic_memory(
        content="User prefers Python over JavaScript",
        category="preference",
        source="explicit",
    )
    assert memory.id is not None
    assert memory.content == "User prefers Python over JavaScript"
    assert memory.category == "preference"

    # Retrieve via fallback (no Qdrant)
    results = await engine.search_semantic_memory("Python preference", limit=5)
    assert len(results) >= 1
    assert any(m.content == "User prefers Python over JavaScript" for m in results)


# ============================================================
# 2. Deduplication — same content not stored twice
# ============================================================

@pytest.mark.asyncio
async def test_semantic_memory_deduplication(engine):
    content = "User works on AI systems"
    await engine.store_semantic_memory(content)
    await engine.store_semantic_memory(content)  # duplicate

    results = await engine.search_semantic_memory("AI systems")
    assert sum(1 for m in results if m.content == content) == 1


# ============================================================
# 3. Manual contact creation + fuzzy find
# ============================================================

@pytest.mark.asyncio
async def test_create_and_find_contact(engine):
    contact = await engine.create_contact_manual(
        "Ahmed Khan",
        {"relationship_type": "colleague", "organization": "TechCorp"},
    )
    assert contact.id is not None
    assert contact.name == "Ahmed Khan"
    assert contact.relationship_type == "colleague"

    # Fuzzy find (SQLite fallback — contains match)
    matches = await engine.find_contact("Ahmed")
    assert len(matches) >= 1
    assert matches[0].name == "Ahmed Khan"


@pytest.mark.asyncio
async def test_create_contact_manual_rejects_exact_duplicate(engine):
    await engine.create_contact_manual("Sara Ali")
    with pytest.raises(ValueError):
        await engine.create_contact_manual("sara ali")


# ============================================================
# 4. store_contact merges into an existing contact
# ============================================================

@pytest.mark.asyncio
async def test_store_contact_updates_existing(engine, session_id):
    await engine.create_contact_manual("Sara Ali", {"relationship_type": "friend"})
    updated = await engine.store_contact(
        "Sara Ali",
        {"email": "sara@example.com",
         "new_facts": [{"fact": "Moved to Lahore in 2026", "category": "personal"}]},
        session_id=session_id,
    )
    assert updated is not None
    assert updated.email == "sara@example.com"

    contacts = await engine.get_all_contacts()
    assert len([c for c in contacts if c.name == "Sara Ali"]) == 1


# ============================================================
# 5. Contact skills merge without duplicates
# ============================================================

@pytest.mark.asyncio
async def test_update_contact_merges_skills(engine, session_id):
    contact = await engine.create_contact_manual("Ali Raza", {"skills": ["Python"]})
    updated = await engine.update_contact(contact.id, {"skills": ["python", "React"]})
    assert json.loads(updated.skills) == ["Python", "React"]


# ============================================================
# 6. Store and retrieve an episode
# ============================================================

@pytest.mark.asyncio
async def test_store_and_retrieve_episode(engine):
    ep = await engine.store_episode(
        title="Completed Phase 1",
        summary="User finished the Electron + FastAPI setup",
        importance=0.9,
    )
    assert ep.id is not None
    assert ep.title == "Completed Phase 1"

    episodes = await engine.search_episodes("Phase 1")
    assert len(episodes) >= 1


# ============================================================
# 7. Upsert preference — confidence increases on repeat
# ============================================================

@pytest.mark.asyncio
async def test_preference_upsert_increases_confidence(engine):
    await engine.upsert_preference("code_style", "prefers Python", confidence=0.7)
    pref = await engine.upsert_preference("code_style", "prefers Python", confidence=0.7)
    assert pref.occurrence_count == 2
    assert pref.confidence > 0.7  # confidence increased


# ============================================================
# 8. Context builder — retrieve_context + format_context
# ============================================================

@pytest.mark.asyncio
async def test_context_builder_returns_relevant_facts(engine, session_id):
    await engine.store_semantic_memory("User is a software engineer", category="work")
    await engine.upsert_preference("code_style", "User prefers concise code")

    bundle = await engine.retrieve_context("help me write some code", session_id=session_id)
    context = await engine.format_context(bundle)
    assert "WHAT I KNOW ABOUT YOU" in context or "YOUR PREFERENCES" in context
    assert len(context) > 50


@pytest.mark.asyncio
async def test_context_builder_empty_on_no_memories(engine, session_id):
    bundle = await engine.retrieve_context("hello there", session_id=session_id)
    context = await engine.format_context(bundle)
    assert context == ""


# ============================================================
# 9. User profile upsert merges arrays
# ============================================================

@pytest.mark.asyncio
async def test_user_profile_merge(engine):
    await engine.store_user_profile({"name": "Khawar", "skills": ["Python"]})
    profile = await engine.store_user_profile({"profession": "AI Engineer", "skills": ["python", "React"]})
    assert profile.name == "Khawar"
    assert profile.profession == "AI Engineer"
    assert json.loads(profile.skills) == ["Python", "React"]


# ============================================================
# 10. Entity extraction — JSON parsing + retry against the schema
# ============================================================

@pytest.mark.asyncio
async def test_entity_extraction_json_parsing():
    from app.memory.extractor import extract_entities

    mock_provider = AsyncMock()
    mock_provider.chat.return_value = MagicMock(
        content=json.dumps({
            "people_mentioned": [{"name": "Ahmed", "relationship": "colleague"}],
            "facts_about_user": [{
                "fact_user_perspective": "Worked with {CONTACT:Ahmed} on the backend on 2026-07-06",
                "fact_contact_perspective": "Worked with {USER} on the backend on 2026-07-06",
                "subject": "shared",
                "related_contacts": ["Ahmed"],
                "event_date": "2026-07-06",
            }],
        })
    )

    result = await extract_entities(
        "I was working with Ahmed on the backend",
        mock_provider,
        existing_contacts=["Ahmed (No Org)"],
        existing_facts=[],
        conversation_history=[],
        user_name="Khawar",
    )
    assert result is not None
    assert result.people_mentioned[0].name == "Ahmed"
    assert result.facts_about_user[0].related_contacts == ["Ahmed"]
    assert result.facts_about_user[0].event_date == "2026-07-06"


@pytest.mark.asyncio
async def test_entity_extraction_retries_once_on_invalid_json():
    from app.memory.extractor import extract_entities

    mock_provider = AsyncMock()
    mock_provider.chat.side_effect = [
        MagicMock(content="this is not json"),
        MagicMock(content='{"facts_about_user": []}'),
    ]

    result = await extract_entities(
        "hello", mock_provider,
        existing_contacts=[], existing_facts=[], conversation_history=[],
    )
    assert result is not None
    assert mock_provider.chat.call_count == 2


@pytest.mark.asyncio
async def test_entity_extraction_gives_up_after_two_failures():
    from app.memory.extractor import extract_entities

    mock_provider = AsyncMock()
    mock_provider.chat.return_value = MagicMock(content="still not json")

    result = await extract_entities(
        "hello", mock_provider,
        existing_contacts=[], existing_facts=[], conversation_history=[],
    )
    assert result is None
    assert mock_provider.chat.call_count == 2
