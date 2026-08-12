"""
Furi OS — Contacts API (Phase 2)
Full CRUD for relationship memory including identity resolution.
"""
from typing import Optional
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from sqlalchemy.orm import selectinload

from app.core.dependencies import get_db, get_qdrant
from app.db.models import Contact, ContactInteraction
from app.memory.contact_validation import normalize_birthday, normalize_email
from app.memory.engine import MemoryEngine

router = APIRouter()


def _validate_contact_payload(payload: dict) -> dict:
    """
    Deterministic email/birthday validation for the manual paths. The engine
    silently skips values that fail normalization — right for the LLM
    extractor, wrong for a human edit, which deserves an explicit 400 instead
    of a save that quietly didn't happen. Valid values are replaced by their
    canonical forms; empty strings pass through (PUT clear semantics).
    """
    payload = dict(payload)
    email = payload.get("email")
    if email:
        canonical = normalize_email(email)
        if canonical is None:
            raise HTTPException(
                status_code=400, detail=f"Invalid email address: '{email}'"
            )
        payload["email"] = canonical
    birthday = payload.get("birthday")
    if birthday:
        canonical = normalize_birthday(birthday)
        if canonical is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Invalid birthday: '{birthday}' — use YYYY-MM-DD, "
                    "or MM-DD when the year is unknown"
                ),
            )
        payload["birthday"] = canonical
    return payload


def _contact_to_dict(c: Contact, include_interactions: bool = False) -> dict:
    data = {
        "id": c.id,
        "name": c.name,
        "email": c.email,
        "phone": c.phone,
        "organization": c.organization,
        "relationship_type": c.relationship_type,
        "skills": json.loads(c.skills) if c.skills else [],
        "birthday": c.birthday,
        "important_dates": json.loads(c.important_dates) if c.important_dates else {},
        "interaction_count": c.interaction_count,
        "last_interaction": c.last_interaction.isoformat() if c.last_interaction else None,
        "created_at": c.created_at.isoformat(),
        "updated_at": c.updated_at.isoformat(),
    }
    if include_interactions and hasattr(c, "interactions"):
        data["interactions"] = [
            {
                "id": i.id,
                "description": i.description,
                "category": i.category,
                "interaction_date": i.interaction_date.isoformat(),
            }
            for i in c.interactions
        ]
    return data


@router.get("", summary="List all contacts")
async def list_contacts(
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
) -> list:
    result = await db.execute(
        select(Contact)
        .where(Contact.is_active == True)
        .order_by(desc(Contact.updated_at))
        .limit(limit)
    )
    contacts = result.scalars().all()
    return [_contact_to_dict(c) for c in contacts]


@router.post("", summary="Create a contact manually")
async def create_contact(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    qdrant=Depends(get_qdrant),
) -> dict:
    name = payload.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    payload = _validate_contact_payload(payload)

    engine = MemoryEngine(db=db, qdrant=qdrant)
    try:
        contact = await engine.create_contact_manual(name, payload)
        return _contact_to_dict(contact)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/resolve/{name}", summary="Fuzzy identity resolution by name")
async def resolve_contact(
    name: str,
    db: AsyncSession = Depends(get_db),
    qdrant=Depends(get_qdrant),
) -> dict:
    """Find contacts matching a name using vector similarity."""
    engine = MemoryEngine(db=db, qdrant=qdrant)
    matches = await engine.find_contact(name)

    if not matches:
        return {"status": "not_found", "matches": []}
    if len(matches) == 1:
        return {"status": "resolved", "contact": _contact_to_dict(matches[0])}
    return {
        "status": "ambiguous",
        "matches": [_contact_to_dict(c) for c in matches],
    }


@router.get("/{contact_id}", summary="Get contact details with interaction history")
async def get_contact(
    contact_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(Contact)
        .where(Contact.id == contact_id, Contact.is_active == True)
        .options(selectinload(Contact.interactions))
    )
    contact = result.scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    return _contact_to_dict(contact, include_interactions=True)


@router.put("/{contact_id}", summary="Update a contact")
async def update_contact(
    contact_id: str,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    qdrant=Depends(get_qdrant),
) -> dict:
    payload = _validate_contact_payload(payload)
    engine = MemoryEngine(db=db, qdrant=qdrant)
    try:
        contact = await engine.update_contact(
            contact_id, payload, clear_empty=True, touch_interaction=False
        )
    except ValueError:
        raise HTTPException(status_code=404, detail="Contact not found")
    return _contact_to_dict(contact)


@router.delete("/{contact_id}", summary="Delete a contact")
async def delete_contact(
    contact_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(select(Contact).where(Contact.id == contact_id))
    contact = result.scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    contact.is_active = False
    await db.commit()
    # Phase 5 Part 4: cancel the pending birthday reminder — sync no-ops the
    # schedule for an inactive contact, leaving only the cancel.
    from app.core.birthdays import sync_contact_birthday_job
    await sync_contact_birthday_job(db, contact)
    return {"deleted": contact_id}


@router.delete(
    "/{contact_id}/interactions/{interaction_id}",
    summary="Delete a fact from a contact's fact log",
)
async def delete_interaction(
    contact_id: str,
    interaction_id: str,
    db: AsyncSession = Depends(get_db),
    qdrant=Depends(get_qdrant),
) -> dict:
    """Permanently delete one fact-log entry (must belong to this contact)."""
    engine = MemoryEngine(db=db, qdrant=qdrant)
    deleted = await engine.delete_contact_fact(contact_id, interaction_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Fact not found for this contact")
    return {"deleted": interaction_id}


@router.post("/{contact_id}/interactions", summary="Add interaction to contact")
async def add_interaction(
    contact_id: str,
    payload: dict,
    db: AsyncSession = Depends(get_db),
) -> dict:
    description = payload.get("description", "").strip()
    if not description:
        raise HTTPException(status_code=422, detail="description is required")

    interaction = ContactInteraction(
        contact_id=contact_id,
        description=description,
    )
    db.add(interaction)

    # Update contact stats
    result = await db.execute(select(Contact).where(Contact.id == contact_id))
    contact = result.scalar_one_or_none()
    if contact:
        contact.interaction_count += 1
        from datetime import datetime
        contact.last_interaction = datetime.utcnow()

    await db.commit()
    return {
        "id": interaction.id,
        "contact_id": contact_id,
        "description": description,
        "interaction_date": interaction.interaction_date.isoformat(),
    }
