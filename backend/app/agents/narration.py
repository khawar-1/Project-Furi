"""
Jarvis OS — Live Plan Narration (Phase 4, Part 6)

Per-step status events published over the Part 1 push channel while a plan
executes, so a live PlanCard can tick its rows (running → completed/failed)
in real time. Best-effort by definition, exactly like the channel itself:
no listener, no problem — the plan's own state (and, for background tasks,
the Task row + persisted chat message) stays the truth. Narration failing
must NEVER break execution, so narrate_step swallows everything.

Every payload field is code-derived from the step the planner is actually
executing — the LLM cannot influence what the ticker shows beyond the step
description the user already saw on the card.
"""
from loguru import logger

from app.agents.schemas import AgentPlan, PlanStep
from app.core.push import push

PLAN_STEP_EVENT = "plan_step"


async def narrate_step(plan: AgentPlan, step: PlanStep, index: int) -> None:
    """Push one step-status event (called with step.status already set to
    RUNNING / COMPLETED / FAILED). Never raises."""
    try:
        await push(PLAN_STEP_EVENT, {
            "plan_id": plan.id,
            "task_id": plan.task_id,
            "session_id": plan.session_id,
            "step_id": step.id,
            "step_index": index,
            "step_count": len(plan.steps),
            "status": step.status.value,
            "description": step.description,
            "tool": step.tool,
            "permission_level": step.permission_level.value,
            "error": (
                step.result.error
                if step.result is not None and not step.result.success
                else None
            ),
        })
    except Exception as e:  # push never raises, but narration must never break a plan
        logger.warning(f"Step narration failed for plan {plan.id} (non-critical): {e}")
