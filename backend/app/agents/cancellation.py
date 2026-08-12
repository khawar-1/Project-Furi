"""
Furi OS — Cooperative Plan Cancellation (Phase 4, Part 6)

Cancel a background task MID-PLAN, between steps. The rules, all in code:

- Cancellation is COOPERATIVE: the flag is checked by the planner between
  steps (and by _settle before parking a pause). A step that is already
  running always finishes — a tool call is never killed mid-write. That is
  why the API answer is "cancellation requested", not "cancelled".
- The flag is in-memory only, and that is correct: it targets a run that is
  alive in THIS process. A backend restart kills the run itself, and startup
  reconciliation (fail_interrupted_tasks) already settles the row honestly —
  there is nothing for a persisted flag to cancel.
- Every applied cancellation is AUDITED in ActivityLog (the same trail tool
  runs use): which plan, how many steps had completed, how many were skipped.
  An audit-write failure is logged, never raised — registry discipline.
"""
import json

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.schemas import AgentPlan, PlanStatus, StepStatus
from app.db.models import ActivityLog

# task_id → cancel requested. Set by the API, consulted by the planner's
# between-steps check, cleared when the run's asyncio task finishes.
_CANCEL_REQUESTED: set[str] = set()


def request_cancel(task_id: str) -> None:
    _CANCEL_REQUESTED.add(task_id)
    logger.info(f"Cancellation requested for background task {task_id}")


def cancel_requested(task_id: str) -> bool:
    return task_id in _CANCEL_REQUESTED


def clear_cancel(task_id: str) -> None:
    _CANCEL_REQUESTED.discard(task_id)


def apply_cancellation(plan: AgentPlan) -> None:
    """Flip a plan to CANCELLED between steps: every not-yet-run step is
    SKIPPED (it will never execute), any open question is closed, and the
    message states exactly how far the plan got. Deterministic — the same
    words the approval-gate cancel path uses, extended with the mid-run
    step count."""
    skipped = 0
    for step in plan.pending_steps():
        step.status = StepStatus.SKIPPED
        skipped += 1
    plan.question = None
    plan.status = PlanStatus.CANCELLED
    completed = len(plan.completed_steps())
    if completed:
        plan.message = (
            f"Cancelled by you mid-run — {completed} step(s) had already "
            f"finished (see the Activity timeline); {skipped} remaining "
            f"step(s) were skipped and nothing further was executed."
        )
    else:
        plan.message = "Cancelled by the user — nothing further was executed."
    logger.info(
        f"Plan {plan.id} cancelled cooperatively "
        f"({completed} completed, {skipped} skipped)"
    )


async def log_cancellation(db: AsyncSession, plan: AgentPlan) -> None:
    """Audit the applied cancellation in ActivityLog. Never raises."""
    try:
        completed = len(plan.completed_steps())
        skipped = sum(1 for s in plan.steps if s.status == StepStatus.SKIPPED)
        db.add(ActivityLog(
            session_id=plan.session_id,
            tool_name="cancel_plan",
            action=f"cancel_plan(plan_id='{plan.id}')"[:256],
            parameters=json.dumps({
                "plan_id": plan.id,
                "task_id": plan.task_id,
                "goal": plan.goal[:300],
            }),
            result_summary=(
                f"Cancelled by the user mid-plan — {completed} step(s) had "
                f"completed, {skipped} pending step(s) were skipped and never ran."
            ),
            success=True,
            permission_level="read",
        ))
        await db.commit()
    except Exception as e:
        logger.warning(f"ActivityLog write failed for plan cancel (non-critical): {e}")
