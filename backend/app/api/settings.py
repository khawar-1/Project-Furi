"""
Jarvis OS — Settings API (Phase 5, Part 6)

Runtime app settings the user toggles from the UI. Today that's the daily
briefing (on/off + time). Everything goes through app/core/app_settings.py and
app/core/daily_briefing.py; this router only orchestrates (the reminders-router
rule: routers orchestrate, modules own their domain).

The briefing PUT re-syncs the scheduler job in the same request, so turning it
on/off or changing the time takes effect immediately — no restart. run-now is
the manual "Send now" trigger (and the deterministic hook for the verify skill);
it is read-only end to end, so there is nothing to approve.
"""
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.app_settings import (
    BriefingConfig,
    get_briefing_config,
    get_briefing_job_id,
    set_briefing_config,
)
from app.core.daily_briefing import run_briefing_now, sync_briefing_job
from app.core.dependencies import get_db
from app.core.scheduler import scheduler

router = APIRouter()

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class BriefingUpdate(BaseModel):
    enabled: bool
    time: str  # "HH:MM" local


async def _briefing_payload(db) -> dict:
    """The GET/PUT response shape: config + the next scheduled run (from the
    live pending job, so the UI shows the real timer, not just the config)."""
    config = await get_briefing_config(db)
    next_run_at = None
    job_id = await get_briefing_job_id(db)
    if job_id:
        jobs = await scheduler.list_jobs(status="pending", limit=10_000)
        row = next((j for j in jobs if j["id"] == job_id), None)
        if row is not None:
            next_run_at = row["run_at"]  # already utc_iso from list_jobs
    return {
        "enabled": config.enabled,
        "time": config.time_str,
        "next_run_at": next_run_at,
    }


@router.get("/briefing", summary="Daily briefing settings")
async def get_briefing(db=Depends(get_db)) -> dict:
    return await _briefing_payload(db)


@router.put("/briefing", summary="Update daily briefing settings")
async def put_briefing(update: BriefingUpdate, db=Depends(get_db)) -> dict:
    match = _TIME_RE.match(update.time.strip())
    if not match:
        raise HTTPException(
            status_code=400,
            detail="time must be 24-hour 'HH:MM' (00:00–23:59), e.g. 08:00",
        )
    hour, minute = int(match.group(1)), int(match.group(2))
    await set_briefing_config(db, BriefingConfig(enabled=update.enabled, hour=hour, minute=minute))
    # Re-sync the scheduler job in the same request — the change takes effect now.
    await sync_briefing_job(db)
    return await _briefing_payload(db)


@router.post("/briefing/run-now", summary="Compose and deliver a briefing immediately")
async def post_briefing_run_now(db=Depends(get_db)) -> dict:
    body = await run_briefing_now(db)
    return {"delivered": True, "message": body}
