"""
Deterministic ambiguity detection in the foreground (live-transcript
regressions): the backend — not the LLM — decides when a name in the current
message is ambiguous, and the system prompt forces the "which one?" question.
"""
import pytest

from app.api.chat import _build_system_prompt


# ============================================================
# retrieve_context: ambiguous mentions detected, never resolved silently
# ============================================================

@pytest.mark.asyncio
async def test_ambiguous_mention_detected_not_resolved(engine, session_id):
    # The user's real roster shape: exact contact 'jamil' plus longer variants
    for name in ("jamil", "Jamil Ali", "jamil ali khan", "jami"):
        await engine.create_contact_manual(name)

    bundle = await engine.retrieve_context(
        "i went to drink cofee with jamil yesterdy",
        session_id=session_id,
        current_message="i went to drink cofee with jamil yesterdy",
    )

    assert len(bundle.ambiguous_mentions) == 1
    mention = bundle.ambiguous_mentions[0]
    assert mention["mention"] == "jamil"
    names = {c["name"] for c in mention["candidates"]}
    assert names == {"jamil", "Jamil Ali", "jamil ali khan"}
    # Ambiguous candidates must never be pinned as resolved/[ACTIVE]
    assert bundle.resolved_entities == []
    # ...but they ARE shown in context so the LLM can list the options
    shown = {c.name for c in bundle.contacts}
    assert {"jamil", "Jamil Ali", "jamil ali khan"} <= shown


@pytest.mark.asyncio
async def test_fully_qualified_mention_resolves_without_flagging(engine, session_id):
    for name in ("jamil", "Jamil Ali", "jamil ali khan"):
        await engine.create_contact_manual(name)

    bundle = await engine.retrieve_context(
        "i had coffee with jamil ali khan today",
        session_id=session_id,
        current_message="i had coffee with jamil ali khan today",
    )

    assert bundle.ambiguous_mentions == []
    assert [e.name for e in bundle.resolved_entities] == ["jamil ali khan"]


@pytest.mark.asyncio
async def test_stale_mentions_from_prior_turns_not_rescanned(engine, session_id):
    for name in ("jamil", "Jamil Ali"):
        await engine.create_contact_manual(name)

    # Joined retrieval text contains the old ambiguous mention, but the
    # CURRENT message does not — no ambiguity flag.
    bundle = await engine.retrieve_context(
        "coffee with jamil\nthanks, that is all",
        session_id=session_id,
        current_message="thanks, that is all",
    )
    assert bundle.ambiguous_mentions == []


# ============================================================
# System prompt directive
# ============================================================

def test_prompt_forces_question_for_ambiguous_mentions():
    prompt = _build_system_prompt(
        ambiguous_mentions=[{
            "mention": "jamil",
            "candidates": [
                {"id": "1", "name": "jamil"},
                {"id": "2", "name": "Jamil Ali"},
                {"id": "3", "name": "jamil ali khan"},
            ],
        }]
    )
    assert "AMBIGUOUS NAME(S) IN THE CURRENT MESSAGE" in prompt
    assert "jamil ali khan" in prompt
    assert "MUST ask" in prompt


def test_prompt_priority_note_beats_ambiguity_directive():
    prompt = _build_system_prompt(
        disambiguation_resolved_note="WAS SAVED to Jamil Ali Khan",
        ambiguous_mentions=[{"mention": "jamil", "candidates": [{"id": "1", "name": "jamil"}]}],
    )
    assert "BACKEND RESOLVED" in prompt
    assert "AMBIGUOUS NAME(S)" not in prompt


# ============================================================
# "Saved just now" timeline clause (live-transcript regression:
# "we had previously noted..." for a fact confirmed seconds earlier)
# ============================================================

def test_saved_just_now_clause_lists_facts():
    from app.api.chat import _saved_just_now_clause

    clause = _saved_just_now_clause(
        ["Playing a badminton match with jamil ali khan on 2026-08-03", ""]
    )
    assert "SAVED JUST NOW" in clause
    assert "badminton" in clause
    assert "FIRST time" in clause
    # empty strings are dropped; all-empty input yields no clause
    assert _saved_just_now_clause(["", None]) == ""


@pytest.mark.asyncio
async def test_apply_shared_fact_returns_saved_text(engine, session_id):
    await engine.store_user_profile({"name": "Khawar"})
    contact = await engine.create_contact_manual("jamil ali khan")
    text = await engine.apply_shared_fact_to_contact(
        {
            "fact_user_perspective": "Playing badminton with {CONTACT:jamil} on 2026-08-03",
            "fact_contact_perspective": "Playing badminton with {USER} on 2026-08-03",
            "subject": "shared",
            "related_contacts": ["jamil"],
            "event_date": "2026-08-03",
        },
        contact,
    )
    assert text == "Playing badminton with jamil ali khan on 2026-08-03"


# ============================================================
# {USER} substitution in contact fact logs
# ============================================================

@pytest.mark.asyncio
async def test_new_facts_substitute_user_placeholder(engine, db_session):
    from sqlalchemy import select
    from app.db.models import ContactInteraction

    await engine.store_user_profile({"name": "Khawar"})
    contact = await engine.create_contact_manual("Zara")
    await engine.update_contact(contact.id, {
        "new_facts": [{"fact": "Had lunch with {USER} on 2026-07-05", "category": "personal"}]
    })

    result = await db_session.execute(
        select(ContactInteraction).where(ContactInteraction.contact_id == contact.id)
    )
    descriptions = [i.description for i in result.scalars().all()]
    assert descriptions == ["Had lunch with Khawar on 2026-07-05"]
