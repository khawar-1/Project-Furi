"""Memory conflict capture (2026-08-04, Tier 2 item 7).

`supersede_is_covered` refuses a replacement that does not contain every word of
the fact it replaces — correctly, and that rule is untouched here. What was
wrong is what happened next: the extractor's judgement that two facts collide
went into a `logger.info` and nowhere else, so "moved to Lahore" and "lives in
Karachi" both lived forever with nothing able to read the disagreement back.

The tests are mostly about restraint: capture is a QUESTION for the user, not a
verdict. Nothing here resolves itself, and only a human click deletes anything.
"""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.models import Contact, ContactInteraction, MemoryConflict, SemanticMemory, utc_now
from app.memory.budget import MAX_FACTS_PER_CONTACT
from app.memory.conflicts import (
    CONFLICT_RETENTION_DAYS,
    dismiss_conflict,
    list_open_conflicts,
    purge_settled_conflicts,
    record_conflict,
    resolve_conflict,
)
from app.memory.engine import MemoryEngine


async def _fact(db, content: str, **kw) -> SemanticMemory:
    row = SemanticMemory(content=content, subject=kw.pop("subject", "user"), **kw)
    db.add(row)
    await db.commit()
    return row


async def _conflicts(db) -> list[MemoryConflict]:
    return list((await db.execute(select(MemoryConflict))).scalars().all())


# ------------------------------------------------------------- the capture


@pytest.mark.asyncio
async def test_the_incident_a_refused_supersede_is_queued_not_logged_away(db_session):
    """THE INCIDENT, frozen. Before this, both facts survived and the
    disagreement was a log line nobody could query."""
    await _fact(db_session, "Lives in Karachi")

    engine = MemoryEngine(db=db_session, qdrant=None)
    await engine.apply_supersede_candidates(
        {"_supersede_candidates": ["Lives in Karachi"]}, "Moved to Lahore"
    )

    rows = await _conflicts(db_session)
    assert len(rows) == 1
    assert rows[0].old_content == "Lives in Karachi"
    assert rows[0].new_content == "Moved to Lahore"
    assert rows[0].status == "open"


@pytest.mark.asyncio
async def test_a_covered_supersede_still_supersedes_and_queues_nothing(db_session):
    """The supersede RULE is unchanged. A replacement that covers its target
    still soft-deletes it, with no question for the user."""
    old = await _fact(db_session, "Works at Systems")

    engine = MemoryEngine(db=db_session, qdrant=None)
    await engine.apply_supersede_candidates(
        {"_supersede_candidates": ["Works at Systems"]},
        "Works at Systems Limited in Lahore",
    )

    await db_session.refresh(old)
    assert old.is_active is False
    assert await _conflicts(db_session) == []


@pytest.mark.asyncio
async def test_nothing_is_queued_when_the_older_fact_is_already_gone(db_session):
    """A row pointing at a fact that no longer exists is a question with no
    answer — it would render as a conflict the user cannot act on."""
    await record_conflict(
        db_session, old_content="Never existed", new_content="Something new"
    )
    assert await _conflicts(db_session) == []


@pytest.mark.asyncio
async def test_the_same_pair_is_never_queued_twice(db_session):
    """Re-extracting the same conversation is normal and must not ask the user
    the same question repeatedly."""
    await _fact(db_session, "Lives in Karachi")
    for _ in range(3):
        await record_conflict(
            db_session, old_content="Lives in Karachi", new_content="Moved to Lahore"
        )
    assert len(await _conflicts(db_session)) == 1


@pytest.mark.asyncio
async def test_a_different_replacement_is_its_own_question(db_session):
    await _fact(db_session, "Lives in Karachi")
    await record_conflict(
        db_session, old_content="Lives in Karachi", new_content="Moved to Lahore"
    )
    await record_conflict(
        db_session, old_content="Lives in Karachi", new_content="Moved to Islamabad"
    )
    assert len(await _conflicts(db_session)) == 2


@pytest.mark.asyncio
async def test_capture_never_raises_into_the_extraction_path(db_session, monkeypatch):
    """It is called while writing the user's facts. A bookkeeping failure must
    not cost them the fact that was just extracted."""
    await _fact(db_session, "Lives in Karachi")

    import app.memory.conflicts as conflicts

    def _boom(*a, **kw):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(conflicts, "select", _boom)
    assert await record_conflict(
        db_session, old_content="Lives in Karachi", new_content="Moved to Lahore"
    ) is None


# ----------------------------------------------------------- the resolution


@pytest.mark.asyncio
async def test_resolving_hard_deletes_only_the_older_fact(db_session):
    old = await _fact(db_session, "Lives in Karachi")
    new = await _fact(db_session, "Moved to Lahore")
    conflict = await record_conflict(
        db_session, old_content="Lives in Karachi", new_content="Moved to Lahore"
    )

    engine = MemoryEngine(db=db_session, qdrant=None)
    assert await resolve_conflict(db_session, conflict.id, engine) is True

    remaining = (await db_session.execute(select(SemanticMemory))).scalars().all()
    assert [m.id for m in remaining] == [new.id]
    assert await list_open_conflicts(db_session) == []


@pytest.mark.asyncio
async def test_dismissing_keeps_both_facts(db_session):
    old = await _fact(db_session, "Lives in Karachi")
    new = await _fact(db_session, "Moved to Lahore")
    conflict = await record_conflict(
        db_session, old_content="Lives in Karachi", new_content="Moved to Lahore"
    )

    assert await dismiss_conflict(db_session, conflict.id) is True

    remaining = (await db_session.execute(select(SemanticMemory))).scalars().all()
    assert {m.id for m in remaining} == {old.id, new.id}
    assert await list_open_conflicts(db_session) == []


@pytest.mark.asyncio
async def test_a_settled_conflict_cannot_be_answered_twice(db_session):
    await _fact(db_session, "Lives in Karachi")
    conflict = await record_conflict(
        db_session, old_content="Lives in Karachi", new_content="Moved to Lahore"
    )
    assert await dismiss_conflict(db_session, conflict.id) is True
    assert await dismiss_conflict(db_session, conflict.id) is False

    engine = MemoryEngine(db=db_session, qdrant=None)
    assert await resolve_conflict(db_session, conflict.id, engine) is False


# -------------------------------------------------------------- retention


@pytest.mark.asyncio
async def test_an_open_conflict_is_never_swept(db_session):
    """⚠️ THE ONE RETENTION RULE THAT MATTERS. An open row is a question waiting
    on the user; ageing it out would silently drop the thing this feature
    exists to surface."""
    await _fact(db_session, "Lives in Karachi")
    conflict = await record_conflict(
        db_session, old_content="Lives in Karachi", new_content="Moved to Lahore"
    )
    conflict.detected_at = utc_now() - timedelta(days=CONFLICT_RETENTION_DAYS * 5)
    await db_session.commit()

    assert await purge_settled_conflicts(db_session) == 0
    assert len(await list_open_conflicts(db_session)) == 1


@pytest.mark.asyncio
async def test_an_old_settled_conflict_is_swept(db_session):
    await _fact(db_session, "Lives in Karachi")
    conflict = await record_conflict(
        db_session, old_content="Lives in Karachi", new_content="Moved to Lahore"
    )
    await dismiss_conflict(db_session, conflict.id)
    row = (await db_session.execute(select(MemoryConflict))).scalar_one()
    row.resolved_at = utc_now() - timedelta(days=CONFLICT_RETENTION_DAYS + 1)
    await db_session.commit()

    assert await purge_settled_conflicts(db_session) == 1
    assert await _conflicts(db_session) == []


@pytest.mark.asyncio
async def test_a_recently_settled_conflict_survives(db_session):
    await _fact(db_session, "Lives in Karachi")
    conflict = await record_conflict(
        db_session, old_content="Lives in Karachi", new_content="Moved to Lahore"
    )
    await dismiss_conflict(db_session, conflict.id)
    assert await purge_settled_conflicts(db_session) == 0


# ------------------------------------------------------------ route order


def test_conflict_routes_are_not_shadowed_by_the_id_route():
    """⚠️ FastAPI matches in DECLARATION ORDER. A literal path registered after
    a same-shaped parameterised one is read as an id — which does not 500, it
    answers as though there were simply no conflicts, i.e. the feature silently
    does not exist. CLAUDE.md records that exact trap twice
    (`/api/activity/routing`, `/api/activity/plans`); `/memory/archived` is the
    local precedent this follows."""
    from app.api.memory import router

    paths = [r.path for r in router.routes]

    def first(path: str) -> int:
        return paths.index(path)

    assert first("/conflicts") < first("/{memory_id}")
    assert first("/conflicts/{conflict_id}/resolve") < first("/{memory_id}/restore")
    assert first("/conflicts/{conflict_id}/dismiss") < first("/{memory_id}/restore")


# ------------------------------------------- the digest reaches the prompt


@pytest.mark.asyncio
async def test_the_clipped_tail_renders_as_knowledge_not_a_count(db_session):
    """⚠️ THE WHOLE POINT OF CONSOLIDATION, asserted at the surface that matters.
    Before this, everything past the recent window rendered as
    "… 12 older fact(s) not shown" and the model could not use any of it."""
    contact = Contact(name="Jamil Ali", relationship_type="friend")
    contact.history_digest = "Jamil is a cricket friend in Lahore, allergic to peanuts."
    db_session.add(contact)
    await db_session.commit()

    total = MAX_FACTS_PER_CONTACT + 12
    for i in range(total):
        row = ContactInteraction(
            contact_id=contact.id, description=f"note {i}", category="other"
        )
        row.interaction_date = utc_now() - timedelta(days=total - i)
        db_session.add(row)
    await db_session.commit()

    rows = (
        await db_session.execute(
            select(ContactInteraction)
            .where(ContactInteraction.contact_id == contact.id)
            .order_by(ContactInteraction.interaction_date.asc())
        )
    ).scalars().all()
    contact.history_digest_upto = rows[-MAX_FACTS_PER_CONTACT - 1].interaction_date
    await db_session.commit()

    engine = MemoryEngine(db=db_session, qdrant=None)
    from app.memory.engine import RetrievedContext

    rendered = await engine.format_context(RetrievedContext(
        resolved_entities=[], pending_resolution=None, user_profile=None,
        semantic_memories=[], preferences=[], episodes=[], contacts=[contact],
    ))

    assert "Earlier (summarised through" in rendered
    assert "allergic to peanuts" in rendered
    # Everything older is covered, so there is no leftover count line.
    assert "older fact(s) not shown" not in rendered


@pytest.mark.asyncio
async def test_facts_the_digest_does_not_reach_still_get_an_honest_count(db_session):
    """Claiming a digest covers facts it has never seen would be the "record
    lied" defect. Uncovered old facts keep their count line alongside it."""
    contact = Contact(name="Jamil Ali", relationship_type="friend")
    contact.history_digest = "Jamil is a cricket friend in Lahore."
    # Watermark far in the past: the digest covers none of the facts below.
    contact.history_digest_upto = utc_now() - timedelta(days=999)
    db_session.add(contact)
    await db_session.commit()

    total = MAX_FACTS_PER_CONTACT + 6
    for i in range(total):
        row = ContactInteraction(
            contact_id=contact.id, description=f"note {i}", category="other"
        )
        row.interaction_date = utc_now() - timedelta(days=total - i)
        db_session.add(row)
    await db_session.commit()

    engine = MemoryEngine(db=db_session, qdrant=None)
    from app.memory.engine import RetrievedContext

    rendered = await engine.format_context(RetrievedContext(
        resolved_entities=[], pending_resolution=None, user_profile=None,
        semantic_memories=[], preferences=[], episodes=[], contacts=[contact],
    ))

    assert "Earlier (summarised through" in rendered
    assert "6 older fact(s) not shown" in rendered


@pytest.mark.asyncio
async def test_a_contact_with_no_digest_renders_exactly_as_before(db_session):
    """The feature is additive: with nothing consolidated, the block is
    byte-for-byte what it was."""
    contact = Contact(name="Jamil Ali", relationship_type="friend")
    db_session.add(contact)
    await db_session.commit()

    total = MAX_FACTS_PER_CONTACT + 4
    for i in range(total):
        row = ContactInteraction(
            contact_id=contact.id, description=f"note {i}", category="other"
        )
        row.interaction_date = utc_now() - timedelta(days=total - i)
        db_session.add(row)
    await db_session.commit()

    engine = MemoryEngine(db=db_session, qdrant=None)
    from app.memory.engine import RetrievedContext

    rendered = await engine.format_context(RetrievedContext(
        resolved_entities=[], pending_resolution=None, user_profile=None,
        semantic_memories=[], preferences=[], episodes=[], contacts=[contact],
    ))

    assert "Earlier (summarised" not in rendered
    assert "4 older fact(s) not shown" in rendered


@pytest.mark.asyncio
async def test_a_question_whose_fact_left_by_another_route_is_not_asked(db_session):
    """⚠️ The two ends must agree. `record_conflict` refuses to queue a question
    about a fact that is not live; the queue must not go on asking one whose
    fact has since been superseded (soft) or deleted in About Me (hard)."""
    old = await _fact(db_session, "Lives in Karachi")
    conflict = await record_conflict(
        db_session, old_content="Lives in Karachi", new_content="Moved to Lahore"
    )
    assert len(await list_open_conflicts(db_session)) == 1

    # A later extraction supersedes it with a replacement that DOES cover it.
    old.is_active = False
    await db_session.commit()

    assert await list_open_conflicts(db_session) == []
    # The row itself survives for the retention sweep to settle — it is not
    # deleted out from under an audit.
    assert len(await _conflicts(db_session)) == 1
