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
- a reply that settles none of the parked names NEVER destroys them — the
  question stays parked until answered or TTL-expired;
- after extraction parks a question, replies that arrived during extraction
  are replayed against it (apply_pending_resolution_reply);
- a name the user confirmed once is remembered for the whole session
  (ConversationSession.confirmed_names) and never re-asked;
- the create-contact question is never suppressed by a resolved note from
  the same turn.
"""
import json

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.api.chat import _build_system_prompt, apply_pending_resolution_reply
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

@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["yes", "its also happening", "what do u know about hamil?"])
async def test_unmatched_reply_keeps_fact_parked(engine, session_id, roster, reply):
    """No reply that fails to name a contact may destroy the parked fact
    (live regressions: 'yes', then 'its also happening')."""
    parked = await engine.store_shared_fact(dict(GYM_FACT), session_id=session_id)
    assert parked is None  # "jamil" is ambiguous → parked
    assert get_session(session_id).pending_resolution is not None

    saved = await apply_pending_resolution_reply(engine, session_id, reply)
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


# ------------------------------------------------------------------
# 4. Session-confirmed names must never re-ask (live regression:
#    every fact about "jamil" re-parked behind "which jamil?")
# ------------------------------------------------------------------

BOXING_FACT = {
    "fact_user_perspective": (
        "Planning to join a boxing gym with {CONTACT:jamil} next month"
    ),
    "fact_contact_perspective": (
        "Planning to join a boxing gym with {USER} next month"
    ),
    "subject": "shared",
    "related_contacts": ["jamil"],
    "event_date": None,
    "category": "personal",
}


@pytest.mark.asyncio
async def test_confirmed_name_is_not_reasked(engine, db_session, session_id, roster):
    """After the user answers 'which jamil?' once, a later fact saying
    'jamil' must save directly instead of parking again."""
    await engine.store_shared_fact(dict(GYM_FACT), session_id=session_id)
    assert await apply_pending_resolution_reply(engine, session_id, "jamil")

    saved = await engine.store_shared_fact(dict(BOXING_FACT), session_id=session_id)
    assert saved is not None, "session-confirmed 'jamil' must not park again"
    assert get_session(session_id).pending_resolution is None
    log = (
        await db_session.execute(
            select(ContactInteraction).where(
                ContactInteraction.contact_id == roster["jamil"].id
            )
        )
    ).scalars().all()
    assert any("boxing" in i.description.lower() for i in log)


@pytest.mark.asyncio
async def test_confirmed_name_plus_unknown_parks_creation(engine, session_id, roster):
    """The live daud flow: 'jamil' is already confirmed, 'daud' is unknown —
    the fact must park behind the CREATE question, not a re-ask of jamil."""
    await engine.store_shared_fact(dict(GYM_FACT), session_id=session_id)
    assert await apply_pending_resolution_reply(engine, session_id, "jamil")

    fact = dict(BOXING_FACT)
    fact["fact_user_perspective"] = (
        "Planning to join a boxing gym with {CONTACT:jamil} and {CONTACT:daud} next month"
    )
    fact["related_contacts"] = ["jamil", "daud"]
    saved = await engine.store_shared_fact(fact, session_id=session_id)
    assert saved is None
    sess = get_session(session_id)
    assert sess.pending_resolution is None, "'jamil' must not re-park"
    assert sess.pending_creation is not None
    assert sess.pending_creation.name == "daud"


def test_creation_question_not_suppressed_by_resolved_note():
    """Resolving one question can park a create-contact question in the same
    turn — the prompt must carry BOTH (live regression: 'add daud?' was
    silently dropped and the parked fact expired unasked)."""
    prompt = _build_system_prompt(
        disambiguation_resolved_note="DISAMBIGUATION RESOLVED: The user meant: jamil.",
        pending_creation={"name": "daud"},
    )
    assert "DISAMBIGUATION RESOLVED" in prompt
    assert "daud isn't in your contacts — want me to add them?" in prompt
    # A still-open disambiguation defers it (one clarifying question at a time)
    prompt = _build_system_prompt(
        pending_resolution={"original_name": "jamil", "candidates": [{"id": "1", "name": "jamil"}]},
        pending_creation={"name": "daud"},
    )
    assert "PENDING CONTACT CREATION" not in prompt
