"""
Jarvis OS — Background Tasks API (Phase 4, Parts 5-6)

GET  /api/tasks              — list background tasks (status filter, newest first).
GET  /api/tasks/{id}         — one task, including its serialized plan snapshot.
POST /api/tasks/{id}/cancel  — cooperative mid-plan cancel (Part 6): sets the
                               flag a RUNNING plan checks between steps. The
                               step currently executing always finishes; the
                               cancelled outcome then arrives by push, audited
                               in ActivityLog. Paused tasks are cancelled from
                               their approval card (/api/agent/approve,
                               approved=false) — the approval gates stay in
                               one place.

Tasks are created through chat (background intent) and answered through the
existing /api/agent/approve|choose endpoints.
"""
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.agent_registry import agent_for_key
from app.agents.task_runner import request_task_cancel
from app.core.dependencies import get_db
from app.db.models import Task, utc_iso

router = APIRouter()

_STATUSES = {"running", "awaiting_approval", "awaiting_choice", "completed", "failed", "cancelled"}


def _serialize(task: Task) -> dict:
    plan = None
    if task.plan_payload:
        try:
            plan = json.loads(task.plan_payload)
        except ValueError:
            plan = None
    return {
        "id": task.id,
        "session_id": task.session_id,
        "goal": task.goal,
        "status": task.status,
        # Which domain agent owns this task, plus its human name for the UI.
        "domain": task.domain,
        "agent": agent_for_key(task.domain).display_name,
        "plan_id": task.plan_id,
        "plan": plan,
        "message": task.message,
        "created_at": utc_iso(task.created_at),
        "updated_at": utc_iso(task.updated_at),
        "finished_at": utc_iso(task.finished_at),
    }


@router.get("", summary="List background tasks")
async def list_tasks(
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    if status is not None and status not in _STATUSES:
        raise HTTPException(status_code=422, detail=f"Unknown status '{status}'")
    query = select(Task).order_by(Task.created_at.desc()).limit(limit)
    if status is not None:
        query = query.where(Task.status == status)
    result = await db.execute(query)
    return [_serialize(t) for t in result.scalars().all()]


@router.get("/{task_id}", summary="Get one background task")
async def get_task(task_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="No such task")
    return _serialize(task)


@router.post("/{task_id}/cancel", summary="Cancel a running background task")
async def cancel_task(task_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Request a cooperative mid-plan cancel. `accepted` means the flag was
    set on a live run — the actual cancelled outcome arrives as a "task"
    push event once the current step finishes (never killed mid-write)."""
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="No such task")

    if task.status != "running":
        detail = (
            "Only a running task can be cancelled here — a paused task is "
            "cancelled from its approval card."
            if task.status in ("awaiting_approval", "awaiting_choice")
            else f"This task already settled ({task.status})."
        )
        return {"task_id": task.id, "status": task.status, "accepted": False, "detail": detail}

    accepted = request_task_cancel(task.id)
    detail = (
        "Cancellation requested — the step currently running will finish, "
        "then the task stops. The outcome arrives as a notification."
        if accepted
        else "No live run found for this task in this backend process — it "
             "may have just settled, or the backend restarted (startup marks "
             "interrupted tasks failed)."
    )
    return {"task_id": task.id, "status": task.status, "accepted": accepted, "detail": detail}
