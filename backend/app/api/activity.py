"""
Jarvis OS — Activity API (Phase 3, Part 5)
Read-only access to the ActivityLog audit trail (written by execute_tool for
every tool invocation — including blocked approval-gate attempts) and, since
2026-08-03, to the two trails that record what Jarvis DIDN'T do: routing (one
row per chat turn, saying which router took it and — when none did — why) and
plan traces (one row per planner invocation, saying why a plan gave up).

GET /api/activity                — newest first, across all sessions
GET /api/activity/routing        — routing decisions, newest first (filterable)
GET /api/activity/plans          — plan traces, newest first (filterable)
GET /api/activity/{session_id}   — newest first, one session
"""
import json
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_db
from app.db.models import ActivityLog, PlanTrace, RoutingDecision, utc_iso

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


def _routing_row_to_dict(row: RoutingDecision) -> dict:
    return {
        "id": row.id,
        "session_id": row.session_id,
        "created_at": utc_iso(row.created_at),
        "message": row.message,
        "message_chars": row.message_chars,
        "has_conversation": row.has_conversation,
        "gate_fired": row.gate_fired,
        "gate_reason": row.gate_reason,
        "label": row.label,
        "mode": row.mode,
        "classifier_ms": row.classifier_ms,
        "classifier_error": row.classifier_error,
        "classifier_model": row.classifier_model,
        "bare_navigation": row.bare_navigation,
        "background_intent": row.background_intent,
        "agent": row.agent,
        "execution": row.execution,
        "outcome": row.outcome,
        "fail_open_reason": row.fail_open_reason,
        "rescue_fired": row.rescue_fired,
        "rescue_ok": row.rescue_ok,
        "impersonation_cut": row.impersonation_cut,
        "route_ms": row.route_ms,
        "task_id": row.task_id,
        "plan_id": row.plan_id,
    }


def _plan_trace_to_dict(row: PlanTrace) -> dict:
    rejections: list = []
    if row.rejections:
        try:
            parsed = json.loads(row.rejections)
            rejections = parsed if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            rejections = []  # stored as opaque text — never surface a broken blob
    return {
        "id": row.id,
        "session_id": row.session_id,
        "created_at": utc_iso(row.created_at),
        "plan_id": row.plan_id,
        "task_id": row.task_id,
        "goal": row.goal,
        "goal_chars": row.goal_chars,
        "agent_key": row.agent_key,
        "entry": row.entry,
        "execution": row.execution,
        "status": row.status,
        "fail_class": row.fail_class,
        "message": row.message,
        "steps_total": row.steps_total,
        "steps_completed": row.steps_completed,
        "steps_failed": row.steps_failed,
        "steps_skipped": row.steps_skipped,
        "replan_count": row.replan_count,
        "questions_asked": row.questions_asked,
        "rejections": rejections,
        "rejection_count": row.rejection_count,
        "failed_tool": row.failed_tool,
        "failed_signature": row.failed_signature,
        "failed_error": row.failed_error,
        "duration_ms": row.duration_ms,
    }


# ⚠️ DECLARED BEFORE /{session_id}. FastAPI matches routes in registration
# order, so a later declaration would be swallowed by the path parameter and
# "routing" would be looked up as a session id — returning an empty list that
# reads exactly like "nothing has been routed".
@router.get("/routing", summary="Routing decisions (newest first)")
async def list_routing_decisions(
    outcome: Optional[str] = Query(None, description="e.g. chat, task_background"),
    fail_open_reason: Optional[str] = Query(
        None, description="gate_closed | classifier_chat | classifier_error | …"
    ),
    label: Optional[str] = Query(None, description="TASK | EMAIL | WEB | BROWSE | …"),
    session_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list:
    """The audit trail for what Jarvis DIDN'T do.

    Filter by `fail_open_reason` to separate the three causes of a chat outcome
    that are otherwise identical from outside: the gate never fired, the model
    judged it conversation, or the model call failed."""
    query = select(RoutingDecision)
    if outcome:
        query = query.where(RoutingDecision.outcome == outcome)
    if fail_open_reason:
        query = query.where(RoutingDecision.fail_open_reason == fail_open_reason)
    if label:
        query = query.where(RoutingDecision.label == label.upper())
    if session_id:
        query = query.where(RoutingDecision.session_id == session_id)
    result = await db.execute(
        query.order_by(desc(RoutingDecision.created_at)).limit(limit)
    )
    return [_routing_row_to_dict(row) for row in result.scalars().all()]


# ⚠️ ALSO BEFORE /{session_id} — same trap as /routing above.
@router.get("/plans", summary="Plan traces (newest first)")
async def list_plan_traces(
    status: Optional[str] = Query(None, description="e.g. failed, completed, cancelled"),
    fail_class: Optional[str] = Query(
        None, description="empty_goal | replan_cap | unrouted_step | …"
    ),
    failed_tool: Optional[str] = Query(None),
    plan_id: Optional[str] = Query(None, description="one plan's whole story"),
    session_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list:
    """The audit trail for WHY a plan gave up.

    ActivityLog records the failure symptom — one row per failed tool call.
    This records the diagnosis: which structural guard refused a draft, how many
    replan rounds were burned, and which of the FAILED sites finally fired.
    Filter by `fail_class` to separate causes that are otherwise identical from
    outside; filter by `plan_id` to read one plan's whole story in order."""
    query = select(PlanTrace)
    if status:
        query = query.where(PlanTrace.status == status)
    if fail_class:
        query = query.where(PlanTrace.fail_class == fail_class)
    if failed_tool:
        query = query.where(PlanTrace.failed_tool == failed_tool)
    if plan_id:
        query = query.where(PlanTrace.plan_id == plan_id)
    if session_id:
        query = query.where(PlanTrace.session_id == session_id)
    result = await db.execute(query.order_by(desc(PlanTrace.created_at)).limit(limit))
    return [_plan_trace_to_dict(row) for row in result.scalars().all()]


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
