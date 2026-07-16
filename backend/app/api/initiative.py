"""
Jarvis OS — Initiative API (Phase 9)

The suggestion feed + the Initiative Engine settings. Everything goes through
app/core/suggestions.py and app/core/initiative.py; this router only
orchestrates (the reminders-router rule: routers orchestrate, modules own their
domain). The settings PUT re-syncs the scheduler job in the same request, so a
toggle / interval / autonomy change takes effect immediately — no restart.
run-now is the manual trigger (and the deterministic hook for the verify skill).
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.app_settings import (
    INITIATIVE_AUTONOMY_LEVELS,
    InitiativeConfig,
    get_initiative_config,
    get_initiative_job_id,
    set_initiative_config,
)
from app.core.dependencies import get_db, get_llm_provider
from app.core.initiative import run_initiative_now, sync_initiative_job
from app.core.scheduler import scheduler
from app.core.suggestions import (
    accept_suggestion,
    dismiss_suggestion,
    list_suggestions,
)
from app.db.models import utc_iso

router = APIRouter()


def _serialize(s) -> dict:
    return {
        "id": s.id,
        "session_id": s.session_id,
        "category": s.category,
        "title": s.title,
        "body": s.body,
        "rationale": s.rationale,
        "autonomy": s.autonomy,
        "priority": s.priority,
        "goal": s.goal,
        "status": s.status,
        "task_id": s.task_id,
        "created_at": utc_iso(s.created_at),
        "updated_at": utc_iso(s.updated_at),
        "expires_at": utc_iso(s.expires_at),
    }


# --------------------------------------------------------------- suggestions

@router.get("/suggestions", summary="List suggestions (newest first)")
async def get_suggestions(
    status: Optional[str] = Query(
        None, pattern="^(pending|accepted|dismissed|acted|expired)$"
    ),
    limit: int = Query(50, ge=1, le=200),
    db=Depends(get_db),
) -> list[dict]:
    rows = await list_suggestions(db, status=status, limit=limit)
    return [_serialize(r) for r in rows]


@router.post("/suggestions/{suggestion_id}/accept", summary="Accept a suggestion")
async def post_accept(
    suggestion_id: str, db=Depends(get_db), provider=Depends(get_llm_provider)
) -> dict:
    row = await accept_suggestion(db, suggestion_id, provider=provider)
    if row is None:
        raise HTTPException(
            status_code=404, detail="Suggestion not found or no longer pending"
        )
    return _serialize(row)


@router.post("/suggestions/{suggestion_id}/dismiss", summary="Dismiss a suggestion")
async def post_dismiss(suggestion_id: str, db=Depends(get_db)) -> dict:
    row = await dismiss_suggestion(db, suggestion_id)
    if row is None:
        raise HTTPException(
            status_code=404, detail="Suggestion not found or no longer pending"
        )
    return _serialize(row)


# ------------------------------------------------------------------ settings

class InitiativeUpdate(BaseModel):
    enabled: bool
    autonomy: str = "ask"
    interval_minutes: int = Field(45, ge=1, le=100_000)
    daily_budget: int = Field(5, ge=0, le=1000)
    quiet_start_hour: int = Field(22, ge=0, le=23)
    quiet_end_hour: int = Field(8, ge=0, le=23)
    min_gap_minutes: int = Field(30, ge=0, le=100_000)


async def _settings_payload(db) -> dict:
    """Config + the next scheduled run (from the live pending job, so the UI
    shows the real timer) + the autonomy vocabulary for the picker."""
    config = await get_initiative_config(db)
    next_run_at = None
    job_id = await get_initiative_job_id(db)
    if job_id:
        jobs = await scheduler.list_jobs(status="pending", limit=10_000)
        row = next((j for j in jobs if j["id"] == job_id), None)
        if row is not None:
            next_run_at = row["run_at"]  # already utc_iso from list_jobs
    return {
        "enabled": config.enabled,
        "autonomy": config.autonomy,
        "interval_minutes": config.interval_minutes,
        "daily_budget": config.daily_budget,
        "quiet_start_hour": config.quiet_start_hour,
        "quiet_end_hour": config.quiet_end_hour,
        "min_gap_minutes": config.min_gap_minutes,
        "autonomy_levels": list(INITIATIVE_AUTONOMY_LEVELS),
        "next_run_at": next_run_at,
    }


@router.get("/settings", summary="Initiative Engine settings")
async def get_settings(db=Depends(get_db)) -> dict:
    return await _settings_payload(db)


@router.put("/settings", summary="Update Initiative Engine settings")
async def put_settings(update: InitiativeUpdate, db=Depends(get_db)) -> dict:
    if update.autonomy not in INITIATIVE_AUTONOMY_LEVELS:
        raise HTTPException(
            status_code=400,
            detail=f"autonomy must be one of: {', '.join(INITIATIVE_AUTONOMY_LEVELS)}",
        )
    # set_initiative_config clamps every numeric field via _coerce_initiative,
    # so an out-of-range value is bounded, never rejected — the bounds live in
    # one place (app_settings).
    await set_initiative_config(db, InitiativeConfig(
        enabled=update.enabled,
        autonomy=update.autonomy,
        interval_minutes=update.interval_minutes,
        daily_budget=update.daily_budget,
        quiet_start_hour=update.quiet_start_hour,
        quiet_end_hour=update.quiet_end_hour,
        min_gap_minutes=update.min_gap_minutes,
    ))
    # Re-sync the scheduler job in the same request — the change takes effect now.
    await sync_initiative_job(db)
    return await _settings_payload(db)


@router.post("/run-now", summary="Run one initiative pass immediately")
async def post_run_now(db=Depends(get_db)) -> dict:
    surfaced = await run_initiative_now(db)
    return {"surfaced": surfaced}
