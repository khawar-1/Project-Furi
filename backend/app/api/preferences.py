"""
Jarvis OS — Preferences API (Phase 2)
Read and delete auto-extracted user preferences.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc

from app.core.dependencies import get_db
from app.db.models import Preference

router = APIRouter()


def _pref_to_dict(p: Preference) -> dict:
    return {
        "id": p.id,
        "key": p.key,
        "value": p.value,
        "description": p.description,
        "source": p.source,
        "confidence": p.confidence,
        "occurrence_count": p.occurrence_count,
        "created_at": p.created_at.isoformat(),
        "updated_at": p.updated_at.isoformat(),
    }


@router.get("", summary="List all preferences")
async def list_preferences(db: AsyncSession = Depends(get_db)) -> list:
    result = await db.execute(
        select(Preference).order_by(desc(Preference.confidence))
    )
    return [_pref_to_dict(p) for p in result.scalars().all()]


@router.delete("/{pref_id}", summary="Delete a preference")
async def delete_preference(
    pref_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(select(Preference).where(Preference.id == pref_id))
    pref = result.scalar_one_or_none()
    if not pref:
        raise HTTPException(status_code=404, detail="Preference not found")
    await db.delete(pref)
    await db.commit()
    return {"deleted": pref_id}
