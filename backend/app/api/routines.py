"""
Jarvis OS — Routines API (Phase 6, Part 5 — teachable procedural memory)

List/create/delete access to saved routines, plus a direct run endpoint. The
PRIMARY creation path is chat ("save this as a routine called X" — see
app/api/routine_router.py); this router is the management surface for the
Routines panel (and tests). Everything goes through app/core/routines.py; the
router never touches the table directly (the reminders rule: modules own
their domain).
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.agents import planner_memory_context, start_task
from app.core.dependencies import get_db, get_llm_provider
from app.core.routines import (
    create_routine,
    delete_routine,
    list_routines,
)
from app.db.models import Routine, utc_iso
from app.providers.base import LLMProvider

router = APIRouter()


def _serialize(r: Routine) -> dict:
    return {
        "id": r.id,
        "name": r.name,
        "normalized_name": r.normalized_name,
        "goal_template": r.goal_template,
        "is_active": r.is_active,
        "created_at": utc_iso(r.created_at),
        "updated_at": utc_iso(r.updated_at),
    }


@router.get("", summary="List saved routines")
async def get_routines(db=Depends(get_db)) -> list[dict]:
    rows = await list_routines(db)
    return [_serialize(r) for r in rows]


class CreateRoutineRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=256)
    goal_template: str = Field(..., min_length=1, max_length=8000)


@router.post("", summary="Create/re-teach a routine (manual UI path)")
async def post_routine(request: CreateRoutineRequest, db=Depends(get_db)) -> dict:
    routine = await create_routine(db, request.name.strip(), request.goal_template.strip())
    return _serialize(routine)


@router.delete("/{routine_id}", summary="Delete a routine")
async def remove_routine(routine_id: str, db=Depends(get_db)) -> dict:
    deleted = await delete_routine(db, routine_id)
    return {"deleted": deleted}


class RunRoutineRequest(BaseModel):
    session_id: Optional[str] = None


@router.post("/{routine_id}/run", summary="Run a routine now (background task)")
async def run_routine(
    routine_id: str,
    request: RunRoutineRequest,
    db=Depends(get_db),
    provider: LLMProvider = Depends(get_llm_provider),
) -> dict:
    """Start the routine's goal_template as a background Task. The plan is
    re-derived from the stored goal STRING, so the approval gate and path
    guards re-apply on the fresh plan; the outcome arrives via push/toast."""
    routine = await db.get(Routine, routine_id)
    if routine is None or not routine.is_active:
        raise HTTPException(status_code=404, detail="No such routine")

    try:
        memory = await planner_memory_context(db, routine.goal_template)
    except Exception:
        memory = ""
    task = await start_task(
        db, routine.goal_template, request.session_id, memory=memory, provider=provider
    )
    return {"task_id": task.id, "status": task.status}
