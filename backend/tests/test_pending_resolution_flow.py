"""
Deterministic pending-resolution flow — regression tests for the bugs where
(1) store_contact called resolve_confirmation with a missing argument and
(2) resolved disambiguations never applied the parked writes.
"""
import pytest

from app.memory.conversation_state import PendingResolution, get_session
from app.memory.engine import MemoryEngine  # noqa: F401 (fixture type)


# ============================================================
# store_contact with an active PendingResolution
# ============================================================

@pytest.mark.asyncio
async def test_store_contact_resolves_against_pending_candidates(engine, session_id):
    """Regression: this path used to raise TypeError (2-arg resolve_confirmation)."""
    jamil_ali = await engine.create_contact_manual("Jamil Ali")
    await engine.create_contact_manual("Jamil Khan")

    sess = get_session(session_id)
    sess.pending_resolution = PendingResolution(
        original_name="jamil",
        pending_update={"phone": "0300-1234567"},
        candidates=[
            {"id": jamil_ali.id, "name": "Jamil Ali"},
        ] + [{"id": c.id, "name": c.name} for c in await engine.get_all_contacts() if c.name == "Jamil Khan"],
    )

    # The extractor re-extracted the clarified full name on the next turn
    updated = await engine.store_contact(
        "Jamil Ali", {"email": "jamil@example.com"}, session_id=session_id
    )

    assert updated is not None
    assert updated.id == jamil_ali.id
    # Parked update merged with the new details
    assert updated.phone == "0300-1234567"
    assert updated.email == "jamil@example.com"
    # Pending state consumed
    assert get_session(session_id).pending_resolution is None


@pytest.mark.asyncio
async def test_store_contact_unknown_name_parks_creation(engine, session_id):
    """NOT_FOUND no longer silently drops the fact — it waits for a yes/no."""
    result = await engine.store_contact(
        "Zara", {"new_facts": [{"fact": "Loves photography", "category": "personal"}]},
        session_id=session_id,
    )
    assert result is None
    sess = get_session(session_id)
    assert sess.pending_creation is not None
    assert sess.pending_creation.name == "Zara"
    assert sess.pending_creation.pending_update["new_facts"][0]["fact"] == "Loves photography"


@pytest.mark.asyncio
async def test_store_contact_ambiguous_parks_resolution(engine, session_id):
    await engine.create_contact_manual("Jamil Ali")
    await engine.create_contact_manual("Jamil Khan")

    result = await engine.store_contact("jamil", {"phone": "111"}, session_id=session_id)

    assert result is None
    sess = get_session(session_id)
    assert sess.pending_resolution is not None
    assert sess.pending_resolution.pending_update == {"phone": "111"}


# ============================================================
# Yes/no interpretation for pending contact creation
# ============================================================

def test_interpret_yes_no():
    from app.api.chat import _interpret_yes_no

    assert _interpret_yes_no("yes") is True
    assert _interpret_yes_no("Yeah, add her") is True
    assert _interpret_yes_no("sure go ahead") is True
    assert _interpret_yes_no("no") is False
    assert _interpret_yes_no("No, don't add them") is False
    assert _interpret_yes_no("not now") is False
    assert _interpret_yes_no("tell me a joke instead") is None
    # negation wins over an embedded affirmative
    assert _interpret_yes_no("no thanks, it's ok") is False


# ============================================================
# System prompt wiring
# ============================================================

def test_system_prompt_includes_pending_creation_question():
    from app.api.chat import _build_system_prompt

    prompt = _build_system_prompt(pending_creation={"name": "Zara"})
    assert "Zara isn't in your contacts" in prompt
    assert "PENDING CONTACT CREATION" in prompt


def test_system_prompt_resolved_note_takes_precedence():
    from app.api.chat import _build_system_prompt

    prompt = _build_system_prompt(
        pending_resolution={"original_name": "jamil", "candidates": [{"name": "Jamil Ali"}]},
        disambiguation_resolved_note="WAS SAVED to Jamil Ali",
        pending_creation={"name": "Zara"},
    )
    assert "BACKEND RESOLVED" in prompt
    assert "PENDING DISAMBIGUATION" not in prompt
    assert "PENDING CONTACT CREATION" not in prompt
