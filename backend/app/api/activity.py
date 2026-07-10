"""
Jarvis OS — Activity API (Phase 3, Part 5)
Read-only access to the ActivityLog audit trail (written by execute_tool for
every tool invocation — including blocked approval-gate attempts).

GET /api/activity                — newest first, across all sessions
GET /api/activity/{session_id}   — newest first, one session
"""
import json

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_db
from app.db.models import ActivityLog, utc_iso

router = APIRouter()


def _row_to_dict(row: ActivityLog) -> dict:
    parameters = None
    if row.parameters:
        try:
            parameters = json.loads(row.parameters)
        except (ValueError, TypeError):
            parameters = row.parameters  # stored as opaque text — return as-is
    return {
        "id": row.id,
        "session_id": row.session_id,
        "tool_name": row.tool_name,
        "action": row.action,
        "parameters": parameters,
        "result_summary": row.result_summary,
        "success": row.success,
        "permission_level": row.permission_level,
        "duration_ms": row.duration_ms,
        "created_at": utc_iso(row.created_at),
    }


@router.get("", summary="List recent activity (newest first)")
async def list_activity(
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list:
    result = await db.execute(
        select(ActivityLog).order_by(desc(ActivityLog.created_at)).limit(limit)
    )
    return [_row_to_dict(row) for row in result.scalars().all()]


@router.get("/{session_id}", summary="Activity for one session (newest first)")
async def session_activity(
    session_id: str,
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list:
    result = await db.execute(
        select(ActivityLog)
        .where(ActivityLog.session_id == session_id)
        .order_by(desc(ActivityLog.created_at))
        .limit(limit)
    )
    return [_row_to_dict(row) for row in result.scalars().all()]
