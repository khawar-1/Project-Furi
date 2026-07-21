"""
Jarvis OS — Autofill profile API (Phase 15.2)

CRUD over the curated autofill profile the browser fills forms from. The user
owns this data; a commit-mode browse may only fill a form with a value that
traces to it or to the user's own words (the grounding lock — see
app/agents/browser_grounding.fill_value_is_grounded).

SECRET fields are write-through and DISPLAY-MASKED: a GET never returns a secret
value (only that one is set), and the value never reaches any LLM prompt or chat
history — code substitutes it at fill time (the password-never-read rule). All
domain logic lives in app/core/autofill.py (the reminders rule).
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.autofill import (
    KINDS,
    delete_field,
    list_fields,
    normalize_key,
    upsert_field,
)
from app.core.dependencies import get_db
from app.db.models import AutofillField, utc_iso

router = APIRouter()


def _serialize(row: AutofillField) -> dict:
    is_secret = row.kind == "secret"
    return {
        "id": row.id,
        "key": row.key,
        "label": row.label,
        "kind": row.kind,
        # A secret value is never returned — the UI shows a masked placeholder
        # and can only replace it. `has_value` tells the UI one is set.
        "value": None if is_secret else row.value,
        "is_secret": is_secret,
        "has_value": bool(row.value),
        "created_at": utc_iso(row.created_at),
        "updated_at": utc_iso(row.updated_at),
    }


@router.get("", summary="List autofill profile fields")
async def get_fields(db=Depends(get_db)) -> list[dict]:
    return [_serialize(r) for r in await list_fields(db)]


class UpsertFieldRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=120)
    value: str = Field(..., min_length=1)
    kind: str = Field("text")
    key: Optional[str] = Field(None, max_length=64)


@router.post("", summary="Create or update an autofill field")
async def post_field(request: UpsertFieldRequest, db=Depends(get_db)) -> dict:
    if request.kind not in KINDS:
        raise HTTPException(
            status_code=400, detail=f"kind must be one of {', '.join(KINDS)}"
        )
    key = normalize_key(request.key or request.label)
    if not key:
        raise HTTPException(status_code=400, detail="A key or label is required")
    try:
        row = await upsert_field(
            db, key=key, label=request.label, value=request.value, kind=request.kind
        )
    except ValueError as exc:
        # Bad kind / empty / an unsafe document path (the file-tools path safety).
        raise HTTPException(status_code=400, detail=str(exc))
    return _serialize(row)


@router.delete("/{key}", summary="Delete an autofill field")
async def remove_field(key: str, db=Depends(get_db)) -> dict:
    deleted = await delete_field(db, key)
    if not deleted:
        raise HTTPException(status_code=404, detail="No such field")
    return {"deleted": True}
