"""
Furi OS — Schedule API (Phase 4, Part 2)

Read/cancel access to the scheduler's job table, plus a dev utility that
schedules a delayed push event (the Part 2 analogue of POST /ws/test).
Everything goes through the JarvisScheduler methods — the scheduler owns
its persistence rules; this router never touches the table directly.
"""
from datetime import timedelta
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.scheduler import scheduler
from app.db.models import utc_iso, utc_now

router = APIRouter()


@router.get("", summary="List scheduled jobs (soonest first)")
async def list_jobs(
    status: Optional[str] = Query(None, pattern="^(pending|fired|failed|cancelled)$"),
    limit: int = Query(50, ge=1, le=500),
) -> list:
    return await scheduler.list_jobs(status=status, limit=limit)


@router.delete("/{job_id}", summary="Cancel a pending job")
async def cancel_job(job_id: str) -> dict:
    cancelled = await scheduler.cancel(job_id)
    return {"cancelled": cancelled}


class ScheduleTestRequest(BaseModel):
    """Dev utility input — schedule a push event delay_seconds from now."""
    delay_seconds: float = Field(5.0, ge=0, le=3600)
    event_type: str = Field("scheduled_test", min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)


@router.post("/test", summary="Schedule a test push event (dev utility)")
async def schedule_test(request: ScheduleTestRequest = Body(default=ScheduleTestRequest())) -> dict:
    run_at = utc_now() + timedelta(seconds=request.delay_seconds)
    try:
        job_id = await scheduler.schedule_at(
            run_at,
            "push",
            {"event_type": request.event_type, "payload": request.payload},
        )
    except ValueError as e:  # unregistered kind — impossible for "push", but honest
        raise HTTPException(status_code=400, detail=str(e))
    return {"job_id": job_id, "run_at": utc_iso(run_at), "kind": "push"}
