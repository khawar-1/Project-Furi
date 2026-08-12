"""
Furi OS — Reminders API (Phase 4, Part 4)

List/cancel access to reminders, plus a manual-create endpoint (the primary
creation path is chat — see app/core/reminder_parser.py + the hook in
app/api/chat.py — but a UI surface needing to add one directly, or a test,
can go straight through this router). Everything goes through
app/core/reminders.py; this router never touches the tables directly.
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.dependencies import get_db
from app.core.reminders import cancel_reminder, create_reminder, list_reminders
from app.db.models import utc_iso

router = APIRouter()


def _serialize(r) -> dict:
    return {
        "id": r.id,
        "text": r.text,
        "session_id": r.session_id,
        "due_at": utc_iso(r.due_at),
        "status": r.status,
        "job_id": r.job_id,
        "created_at": utc_iso(r.created_at),
        "fired_at": utc_iso(r.fired_at),
    }


@router.get("", summary="List reminders (soonest due first)")
async def get_reminders(
    status: Optional[str] = Query(None, pattern="^(pending|fired|cancelled)$"),
    limit: int = Query(50, ge=1, le=500),
    db=Depends(get_db),
) -> list[dict]:
    rows = await list_reminders(db, status=status, limit=limit)
    return [_serialize(r) for r in rows]


@router.delete("/{reminder_id}", summary="Cancel a pending reminder")
async def delete_reminder(reminder_id: str, db=Depends(get_db)) -> dict:
    cancelled = await cancel_reminder(db, reminder_id)
    return {"cancelled": cancelled}


class CreateReminderRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)
    due_at: datetime
    session_id: Optional[str] = None


@router.post("", summary="Create a reminder directly (manual UI path)")
async def post_reminder(request: CreateReminderRequest, db=Depends(get_db)) -> dict:
    # A naive datetime.astimezone() attaches the SERVER's local tz, treating
    # the naive value as already being local wall-clock time — matching the
    # convention reminder_parser.py uses for chat-derived due times.
    due_at = request.due_at if request.due_at.tzinfo else request.due_at.astimezone()
    if due_at <= datetime.now(due_at.tzinfo):
        raise HTTPException(status_code=400, detail="due_at must be in the future")
    reminder = await create_reminder(db, request.text.strip(), due_at, request.session_id)
    return _serialize(reminder)
