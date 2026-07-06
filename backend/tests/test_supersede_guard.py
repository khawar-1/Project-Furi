"""
Live-transcript regression (gym conversation): facts_to_supersede used to be
applied unconditionally BEFORE the replacement fact was written. When the
extractor "merged" the gym fact with a new participant whose name was
ambiguous, the old About Me fact was deleted while the replacement parked
behind the "which jamil?" question — silent data loss.

Rules now:
- supersede requests travel WITH each extracted fact and fire only AFTER the
  replacement's user-side text is actually written;
- the old fact is deleted only if the new text COVERS it (every word of the
  old text appears in the new one) — a lossy "merge" can't destroy data.
"""
import pytest
from sqlalchemy import select

from app.db.models import SemanticMemory
from app.memory.conversation_state import get_session
from app.memory.engine import resolve_confirmation_multi, supersede_is_covered


OLD_GYM_FACT = "Planning to start gym with Ali Raza and hamil on 2026-07-07"


async def _facts(db_session, active_only=True):
    stmt = select(SemanticMemory)
    if active_only:
        stmt = stmt.where(SemanticMemory.is_active == True)
    result = await db_session.execute(stmt)
    return {m.content: m.is_active for m in result.scalars().all()}


async def _seed(engine):
    await engine.store_user_profile({"name": "Khawar"})
    contacts = {}
    for name in ("Ali Raza", "hamil", "jamil", "Jamil Ali", "jamil ali khan"):
        contacts[name] = await engine.create_contact_manual(name)
    await engine.store_semantic_memory(
        OLD_GYM_FACT, category="personal", subject="shared"
    )
    return contacts


def test_supersede_is_covered():
    # merge that keeps everything → covered
    assert supersede_is_covered(
        OLD_GYM_FACT,
        "Planning to start gym with Ali Raza, hamil and jamil ali khan on 2026-07-07",
    )
    # rewording that keeps all words → covered
    assert supersede_is_covered(
        "Inquired about hamil's birthday",
        "Inquired about whether hamil's birthday is passed or is coming",
    )
    # "merge" that silently dropped participants → NOT covered
    assert not supersede_is_covered(
        OLD_GYM_FACT, "Planning to start gym with jamil on 2026-07-07"
    )
    # unrelated fact → NOT covered
    assert not supersede_is_covered(
        "Works at Acme as an engineer", "Planning to start gym on 2026-07-07"
    )
    assert not supersede_is_covered("", "anything")


@pytest.mark.asyncio
async def test_supersede_waits_for_parked_replacement(engine, db_session, session_id):
    """The gym transcript replay: old fact must survive while the merged
    replacement is parked, and be superseded only once it lands."""
    contacts = await _seed(engine)

    merged = {
        "fact_user_perspective": (
            "Planning to start gym with {CONTACT:ali raza}, {CONTACT:hamil} "
            "and {CONTACT:jamil} on 2026-07-07"
        ),
        "fact_contact_perspective": "Planning to start gym with {USER} on 2026-07-07",
        "subject": "shared",
        "related_contacts": ["ali raza", "hamil", "jamil"],
        "event_date": "2026-07-07",
        "category": "personal",
        "_supersede_candidates": [OLD_GYM_FACT],
    }
    saved = await engine.store_shared_fact(merged, session_id=session_id)

    # "jamil" is ambiguous → replacement parked, old fact UNTOUCHED
    assert saved is None
    assert (await _facts(db_session)).get(OLD_GYM_FACT) == True

    # User answers "i meant jamil ali khan" — the chat.py flow
    sess = get_session(session_id)
    pr = sess.pending_resolution
    assert pr is not None
    all_contacts = await engine.get_all_contacts()
    by_id = {c.id: c for c in all_contacts}
    assignment = resolve_confirmation_multi("i meant jamil ali khan", pr.mentions(), all_contacts)
    preresolved = {n.lower(): by_id[cid] for n, cid in pr.resolved_so_far.items()}
    preresolved.update({n.lower(): by_id[cid] for n, cid in assignment.items()})
    parked = pr.pending_shared_facts
    sess.pending_resolution = None
    saved_texts = [
        await engine.store_shared_fact(f, session_id=session_id, preresolved=preresolved)
        for f in parked
    ]

    # Replacement written, old fact superseded only NOW
    new_text = "Planning to start gym with Ali Raza, hamil and jamil ali khan on 2026-07-07"
    assert saved_texts == [new_text]
    facts = await _facts(db_session)
    assert facts.get(new_text) == True
    assert OLD_GYM_FACT not in facts  # soft-deleted


@pytest.mark.asyncio
async def test_lossy_replacement_never_deletes_broader_fact(engine, db_session, session_id):
    """The second data-loss shape from the transcript: the extractor claims to
    have merged, but the new text dropped Ali Raza and hamil."""
    await _seed(engine)

    lossy = {
        "fact_user_perspective": "Planning to start gym with {CONTACT:hamil} on 2026-07-07",
        "fact_contact_perspective": "Planning to start gym with {USER} on 2026-07-07",
        "subject": "shared",
        "related_contacts": ["hamil"],
        "event_date": "2026-07-07",
        "category": "personal",
        "_supersede_candidates": [OLD_GYM_FACT],
    }
    saved = await engine.store_shared_fact(lossy, session_id=session_id)

    assert saved == "Planning to start gym with hamil on 2026-07-07"
    facts = await _facts(db_session)
    # New fact saved AND the broader old fact preserved
    assert facts.get(saved) == True
    assert facts.get(OLD_GYM_FACT) == True


@pytest.mark.asyncio
async def test_covered_supersede_applies_on_direct_write(engine, db_session, session_id):
    await engine.store_user_profile({"name": "Khawar"})
    old = "Inquired about hamil's birthday"
    await engine.store_semantic_memory(old, subject="user")

    fact = {
        "fact_user_perspective": "Inquired about whether hamil's birthday is passed or is coming",
        "fact_contact_perspective": "",
        "subject": "user",
        "related_contacts": [],
        "event_date": None,
        "category": "personal",
        "_supersede_candidates": [old],
    }
    saved = await engine.store_shared_fact(fact, session_id=session_id)

    facts = await _facts(db_session)
    assert facts.get(saved) == True
    assert old not in facts  # legitimately superseded


@pytest.mark.asyncio
async def test_supersede_never_deletes_identical_text(engine, db_session, session_id):
    """The extractor sometimes lists the very fact it re-emits — that must not
    delete the (deduped) row."""
    await engine.store_user_profile({"name": "Khawar"})
    text = "Planning to start gym with hamil on 2026-07-07"
    await engine.create_contact_manual("hamil")
    await engine.store_semantic_memory(text, subject="shared")

    fact = {
        "fact_user_perspective": "Planning to start gym with {CONTACT:hamil} on 2026-07-07",
        "fact_contact_perspective": "Planning to start gym with {USER} on 2026-07-07",
        "subject": "shared",
        "related_contacts": ["hamil"],
        "event_date": "2026-07-07",
        "category": "personal",
        "_supersede_candidates": [text],
    }
    saved = await engine.store_shared_fact(fact, session_id=session_id)

    assert saved == text
    facts = await _facts(db_session)
    assert facts.get(text) == True
