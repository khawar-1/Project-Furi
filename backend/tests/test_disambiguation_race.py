"""
Regression tests for the lost Jamil gym fact (live transcript, 2026-07-06).

Three defects conspired:
1. The extractor emitted meta-conversation junk ("Mentioned Ali Raza" /
   "Mentioned Khawar") from a bare disambiguation reply — and because the
   junk landed seconds AFTER the real parked fact was applied, it also
   pushed the real fact down the newest-first fact lists.
2. The disambiguation question is asked in the same turn, but the fact only
   parks when background extraction finishes — a fast reply ("jamil", 3s
   later) resolved nothing, and the fact parked AFTER its answer.
3. The next unrelated reply ("yes") matched no contact, and the failure path
   destroyed the parked fact permanently.

Rules now:
- meta-conversation facts are never stored (deterministic filter + prompt);
- replies that don't attempt to name anyone leave the question parked;
- after extraction parks a question, replies that arrived during extraction
  are replayed against it (apply_pending_resolution_reply).
"""
import json

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.api.chat import _looks_like_name_answer, apply_pending_resolution_reply
from app.db.models import ContactInteraction, SemanticMemory
from app.memory.conversation_state import get_session
from app.memory.extractor import is_meta_conversation_fact, run_extraction_pipeline

GYM_FACT = {
    "fact_user_perspective": (
        "Planning to start gym with {CONTACT:jamil}, {CONTACT:hamil} "
        "and {CONTACT:Ali Raza} on 2026-07-08"
    ),
    "fact_contact_perspective": (
        "Planning to start gym with {USER}, {CONTACT:hamil} "
        "and {CONTACT:Ali Raza} on 2026-07-08"
    ),
    "subject": "shared",
    "related_contacts": ["jamil", "hamil", "Ali Raza"],
    "event_date": "2026-07-08",
    "category": "personal",
}


@pytest_asyncio.fixture
async def roster(engine):
    """The live contact roster that makes 'jamil' ambiguous."""
    contacts = {}
    for name in ("jamil", "Jamil Ali", "jamil ali khan", "hamil", "Ali Raza"):
        contacts[name] = await engine.create_contact_manual(name)
    await engine.store_user_profile({"name": "Khawar"})
    return contacts


class _StubProvider:
    """LLM stub returning a fixed extraction JSON."""

    provider_name = "stub"
    model_name = "stub"

    def __init__(self, payload: dict):
        self._payload = json.dumps(payload)

    async def chat(self, messages, **kwargs):
        return type("R", (), {"content": self._payload})()


# ------------------------------------------------------------------
# 1. Meta-conversation junk facts
# ------------------------------------------------------------------

def test_meta_fact_detector():
    assert is_meta_conversation_fact("Mentioned ali raza")
    assert is_meta_conversation_fact("Mentioned Khawar")
    assert is_meta_conversation_fact("Inquired about hamil")
    assert is_meta_conversation_fact(
        "Inquired about whether Hamil's birthday is passed or is coming"
    )
    assert is_meta_conversation_fact("The user inquired about the Tekken match")
    assert is_meta_conversation_fact("Asked about the weather")
    assert is_meta_conversation_fact("Wants to know Hamil's birthday")
    # Real facts must never be filtered
    assert not is_meta_conversation_fact("Planning to start gym with hamil on 2026-07-08")
    assert not is_meta_conversation_fact("Went to coffee with Jamil Ali on 2026-07-06")
    assert not is_meta_conversation_fact("Discussed the trip with Ali over dinner")
    assert not is_meta_conversation_fact("Got a new job at Google")


@pytest.mark.asyncio
async def test_pipeline_drops_meta_facts(engine, db_session, session_id, roster):
    """A junk 'Mentioned X' shared fact must not be written to either side."""
    payload = {
        "facts_about_user": [
            {
                "fact_user_perspective": "Mentioned {CONTACT:Ali Raza}",
                "fact_contact_perspective": "Mentioned {USER}",
                "subject": "shared",
                "related_contacts": ["Ali Raza"],
                "event_date": None,
            }
        ],
    }
    await run_extraction_pipeline(
        user_message="ali raza",
        assistant_message="Ali Raza. Got it.",
        session_id=session_id,
        db=db_session,
        engine=engine,
        provider=_StubProvider(payload),
    )
    memories = (await db_session.execute(select(SemanticMemory))).scalars().all()
    assert not any("mentioned" in m.content.lower() for m in memories)
    interactions = (
        await db_session.execute(
            select(ContactInteraction).where(
                ContactInteraction.contact_id == roster["Ali Raza"].id
            )
        )
    ).scalars().all()
    assert not any("mentioned" in i.description.lower() for i in interactions)


@pytest.mark.asyncio
async def test_pipeline_drops_meta_facts_from_contact_new_facts(
    engine, db_session, session_id, roster
):
    """Junk in people_mentioned.new_facts is filtered; real facts survive."""
    payload = {
        "people_mentioned": [
            {
                "name": "Ali Raza",
                "relationship": "friend",
                "new_facts": [
                    {"fact": "Mentioned {USER}", "category": "other"},
                    {"fact": "Got a new job at Google", "category": "work"},
                ],
            }
        ],
    }
    await run_extraction_pipeline(
        user_message="ali raza got a job at google",
        assistant_message="Nice.",
        session_id=session_id,
        db=db_session,
        engine=engine,
        provider=_StubProvider(payload),
    )
    interactions = (
        await db_session.execute(
            select(ContactInteraction).where(
                ContactInteraction.contact_id == roster["Ali Raza"].id
            )
        )
    ).scalars().all()
    descriptions = [i.description.lower() for i in interactions]
    assert any("google" in d for d in descriptions)
    assert not any("mentioned" in d for d in descriptions)


# ------------------------------------------------------------------
# 2. Non-name replies must not destroy a parked fact
# ------------------------------------------------------------------

def test_looks_like_name_answer():
    assert _looks_like_name_answer("jamil")
    assert _looks_like_name_answer("ali raza")
    assert _looks_like_name_answer("i meant jamil ali khan")
    # Affirmations, questions and long tangents are not name attempts
    assert not _looks_like_name_answer("yes")
    assert not _looks_like_name_answer("no")
    assert not _looks_like_name_answer("ok sure")
    assert not _looks_like_name_answer("what do u know about hamil?")
    assert not _looks_like_name_answer(
        "by the way did I tell you about the new project we started at work last month"
    )


@pytest.mark.asyncio
async def test_yes_reply_keeps_fact_parked(engine, session_id, roster):
    parked = await engine.store_shared_fact(dict(GYM_FACT), session_id=session_id)
    assert parked is None  # "jamil" is ambiguous → parked
    assert get_session(session_id).pending_resolution is not None

    saved = await apply_pending_resolution_reply(engine, session_id, "yes")
    assert saved == []
    assert get_session(session_id).pending_resolution is not None, (
        "a reply that names nobody must leave the question parked"
    )


# ------------------------------------------------------------------
# 3. Late-reply resolution (the park-after-answer race)
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_late_reply_resolves_parked_fact(engine, db_session, session_id, roster):
    """The exact live sequence: fact parks AFTER the user already answered
    'jamil'; replaying the reply must write both perspectives."""
    parked = await engine.store_shared_fact(dict(GYM_FACT), session_id=session_id)
    assert parked is None
    pr = get_session(session_id).pending_resolution
    assert pr is not None
    assert [m["name"] for m in pr.mentions()] == ["jamil"]
    # hamil and Ali Raza resolved at park time and must not be re-asked
    assert set(pr.resolved_so_far) == {"hamil", "ali raza"}

    saved = await apply_pending_resolution_reply(engine, session_id, "jamil")
    assert len(saved) == 1
    assert get_session(session_id).pending_resolution is None

    # User side: About Me got the fully substituted fact
    memory = (
        await db_session.execute(
            select(SemanticMemory).where(SemanticMemory.content == saved[0])
        )
    ).scalar_one()
    assert memory.subject == "shared"
    assert "jamil" in memory.content.lower()
    assert "hamil" in memory.content.lower()
    assert "Ali Raza" in memory.content

    # Contact side: jamil's log names the user, never jamil himself
    jamil_log = (
        await db_session.execute(
            select(ContactInteraction).where(
                ContactInteraction.contact_id == roster["jamil"].id
            )
        )
    ).scalars().all()
    assert len(jamil_log) == 1
    assert "Khawar" in jamil_log[0].description
    assert "gym" in jamil_log[0].description.lower()


@pytest.mark.asyncio
async def test_late_reply_with_full_name_resolves(engine, db_session, session_id, roster):
    """Answering with a longer variant ('jamil ali khan') picks that contact."""
    await engine.store_shared_fact(dict(GYM_FACT), session_id=session_id)
    saved = await apply_pending_resolution_reply(engine, session_id, "jamil ali khan")
    assert len(saved) == 1
    assert get_session(session_id).pending_resolution is None
    log = (
        await db_session.execute(
            select(ContactInteraction).where(
                ContactInteraction.contact_id == roster["jamil ali khan"].id
            )
        )
    ).scalars().all()
    assert len(log) == 1
    assert "Khawar" in log[0].description
