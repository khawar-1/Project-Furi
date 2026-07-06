"""
User-initiated fact deletion (About Me trash button / contact fact-log delete).

Rules:
- deleting an About Me fact is a HARD delete: the SQLite row is removed (not
  just is_active=False) and the Qdrant point is deleted with it;
- deleting a contact fact removes the ContactInteraction row and decrements
  the contact's interaction_count;
- the interaction must belong to the given contact (no cross-contact deletes);
- deleting one side of a shared fact never touches the other side;
- after deletion the exact-text dedup no longer blocks re-adding the fact.
"""
import pytest
from sqlalchemy import select

from app.db.models import Contact, ContactInteraction, SemanticMemory
from app.memory.engine import MemoryEngine


class _QdrantStub:
    """Records delete calls; enough of the AsyncQdrantClient surface for deletion."""

    def __init__(self):
        self.deleted = []  # (collection_name, point_ids)

    async def delete(self, collection_name, points_selector):
        self.deleted.append((collection_name, list(points_selector.points)))


@pytest.mark.asyncio
async def test_delete_semantic_memory_removes_row(engine, db_session):
    memory = await engine.store_semantic_memory("Prefers dark mode", subject="user")

    assert await engine.delete_semantic_memory(memory.id) is True

    # Row is GONE, not soft-deleted
    result = await db_session.execute(
        select(SemanticMemory).where(SemanticMemory.id == memory.id)
    )
    assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_delete_semantic_memory_removes_qdrant_point(engine, db_session):
    memory = await engine.store_semantic_memory("Learning Rust", subject="user")

    stub = _QdrantStub()
    engine_with_qdrant = MemoryEngine(db=db_session, qdrant=stub)
    assert await engine_with_qdrant.delete_semantic_memory(memory.id) is True

    assert stub.deleted == [("semantic_memory", [memory.id])]


@pytest.mark.asyncio
async def test_delete_missing_memory_returns_false(engine):
    assert await engine.delete_semantic_memory("no-such-id") is False


@pytest.mark.asyncio
async def test_deleted_fact_can_be_readded(engine, db_session):
    """Hard delete clears the exact-text dedup — re-adding must create a new row."""
    text = "Based in Karachi"
    memory = await engine.store_semantic_memory(text, subject="user")
    await engine.delete_semantic_memory(memory.id)

    readded = await engine.store_semantic_memory(text, subject="user")
    assert readded.id != memory.id
    assert readded.content == text
    assert readded.is_active is True


@pytest.mark.asyncio
async def test_delete_contact_fact_removes_row_and_decrements_count(engine, db_session):
    contact = await engine.create_contact_manual("hamil")
    assert await engine.add_contact_fact(contact, "Planning to go fishing with Khawar on 2026-08-01")
    contact.interaction_count += 1
    await db_session.commit()

    result = await db_session.execute(
        select(ContactInteraction).where(ContactInteraction.contact_id == contact.id)
    )
    interaction = result.scalar_one()

    assert await engine.delete_contact_fact(contact.id, interaction.id) is True

    result = await db_session.execute(
        select(ContactInteraction).where(ContactInteraction.id == interaction.id)
    )
    assert result.scalar_one_or_none() is None
    refreshed = await db_session.execute(select(Contact).where(Contact.id == contact.id))
    assert refreshed.scalar_one().interaction_count == 0


@pytest.mark.asyncio
async def test_delete_contact_fact_refuses_wrong_contact(engine, db_session):
    """A fact id under a different contact's URL must 404, not delete."""
    hamil = await engine.create_contact_manual("hamil")
    jamil = await engine.create_contact_manual("jamil")
    assert await engine.add_contact_fact(hamil, "Started gym with Khawar on 2026-07-07")
    await db_session.commit()

    result = await db_session.execute(
        select(ContactInteraction).where(ContactInteraction.contact_id == hamil.id)
    )
    interaction = result.scalar_one()

    assert await engine.delete_contact_fact(jamil.id, interaction.id) is False
    # Untouched
    result = await db_session.execute(
        select(ContactInteraction).where(ContactInteraction.id == interaction.id)
    )
    assert result.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_deleting_one_side_of_shared_fact_keeps_the_other(engine, db_session, session_id):
    """User deletes the About Me copy → the contact's fact-log copy survives,
    and vice versa. The two sides are independent."""
    await engine.store_user_profile({"name": "Khawar"})
    contact = await engine.create_contact_manual("hamil")

    fact = {
        "fact_user_perspective": "Planning to go fishing with {CONTACT:hamil} on 2026-08-01",
        "fact_contact_perspective": "Planning to go fishing with {USER} on 2026-08-01",
        "subject": "shared",
        "related_contacts": ["hamil"],
        "event_date": "2026-08-01",
        "category": "personal",
    }
    saved = await engine.store_shared_fact(fact, session_id=session_id)
    assert saved is not None

    user_side = (
        await db_session.execute(
            select(SemanticMemory).where(SemanticMemory.content == saved)
        )
    ).scalar_one()
    contact_side = (
        await db_session.execute(
            select(ContactInteraction).where(ContactInteraction.contact_id == contact.id)
        )
    ).scalar_one()

    # Delete the About Me side → contact log untouched
    assert await engine.delete_semantic_memory(user_side.id) is True
    still_there = await db_session.execute(
        select(ContactInteraction).where(ContactInteraction.id == contact_side.id)
    )
    assert still_there.scalar_one_or_none() is not None

    # Delete the contact side too → both gone now
    assert await engine.delete_contact_fact(contact.id, contact_side.id) is True
    gone = await db_session.execute(
        select(ContactInteraction).where(ContactInteraction.contact_id == contact.id)
    )
    assert gone.scalar_one_or_none() is None
