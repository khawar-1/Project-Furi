"""
Furi OS — Routines API (Phase 6, Part 5 — teachable procedural memory)

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
from app.core.scheduled_routines import next_routine_run_at, set_routine_schedule
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
        # Schedule (Phase 10.2). schedule_job_id is internal plumbing — never
        # serialized. next_run_at is computed, so the UI shows when it will fire.
        "schedule_type": r.schedule_type,
        "schedule_minute": r.schedule_minute,
        "schedule_hour": r.schedule_hour,
        "schedule_weekday": r.schedule_weekday,
        "schedule_interval_minutes": r.schedule_interval_minutes,
        "next_run_at": utc_iso(next_routine_run_at(r)) if r.schedule_type else None,
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


class ScheduleRoutineRequest(BaseModel):
    """A schedule to set, or schedule_type=None/omitted to clear it. Ranges are
    re-validated + clamped in normalize_schedule_spec (the app_settings rule —
    the store is the final authority, not the request model)."""
    schedule_type: Optional[str] = None  # None/"" = clear | interval | daily | weekly
    schedule_hour: int = Field(9, ge=0, le=23)
    schedule_minute: int = Field(0, ge=0, le=59)
    schedule_weekday: Optional[int] = Field(None, ge=0, le=6)
    schedule_interval_minutes: Optional[int] = Field(None, ge=1, le=100000)


@router.put("/{routine_id}/schedule", summary="Set or clear a routine's schedule")
async def put_routine_schedule(
    routine_id: str, request: ScheduleRoutineRequest, db=Depends(get_db)
) -> dict:
    """Set/clear the time trigger and (re)arm its job in-request (the settings.py
    rule — takes effect immediately). 404 if the routine is gone, 400 on an
    invalid spec."""
    try:
        routine = await set_routine_schedule(db, routine_id, request.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if routine is None:
        raise HTTPException(status_code=404, detail="No such routine")
    return _serialize(routine)


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
