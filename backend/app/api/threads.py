"""
Jarvis OS — Goal threads API (Phase 11, Part 3 — ongoing-concern tracking)

Read + resolve/dismiss surface over goal threads for the Threads panel. Threads
are primarily CAPTURED from conversation by the extractor; this router lets the
user see them, mark one resolved ("it landed") or dismissed, and add one
manually. Everything goes through app/core/goal_threads.py (the reminders rule).
"""
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.dependencies import get_db
from app.core.goal_threads import (
    drop_thread,
    list_threads,
    resolve_thread,
    upsert_thread,
)
from app.db.models import GoalThread, utc_iso

router = APIRouter()


def _serialize(t: GoalThread) -> dict:
    return {
        "id": t.id,
        "title": t.title,
        "description": t.description,
        "status": t.status,
        "contact_id": t.contact_id,
        # event_date is a calendar date — bare isoformat (the timestamp convention).
        "event_date": t.event_date.isoformat() if t.event_date else None,
        "next_check_at": utc_iso(t.next_check_at),
        "last_nudged_at": utc_iso(t.last_nudged_at),
        "source": t.source,
        "created_at": utc_iso(t.created_at),
        "updated_at": utc_iso(t.updated_at),
    }


@router.get("", summary="List goal threads")
async def get_threads(
    status: Optional[str] = None, db=Depends(get_db)
) -> list[dict]:
    rows = await list_threads(db, status=status)
    return [_serialize(t) for t in rows]


class CreateThreadRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=512)
    description: Optional[str] = Field(None, max_length=4000)
    event_date: Optional[str] = None  # YYYY-MM-DD


@router.post("", summary="Create a goal thread (manual)")
async def post_thread(request: CreateThreadRequest, db=Depends(get_db)) -> dict:
    event_date: Optional[date] = None
    if request.event_date:
        try:
            event_date = datetime.strptime(request.event_date.strip(), "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="event_date must be YYYY-MM-DD")
    thread = await upsert_thread(
        db, request.title.strip(), description=request.description,
        event_date=event_date, source="manual",
    )
    if thread is None:
        raise HTTPException(status_code=400, detail="Title is required")
    return _serialize(thread)


@router.post("/{thread_id}/resolve", summary="Mark a thread resolved (it landed)")
async def resolve(thread_id: str, db=Depends(get_db)) -> dict:
    thread = await resolve_thread(db, thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="No such thread")
    return _serialize(thread)


@router.post("/{thread_id}/dismiss", summary="Dismiss a thread (stop nudging)")
async def dismiss(thread_id: str, db=Depends(get_db)) -> dict:
    thread = await drop_thread(db, thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="No such thread")
    return _serialize(thread)
