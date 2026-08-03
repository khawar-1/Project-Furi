"""
Jarvis OS — Cooperative Plan Pause & Steer (2026-08-03)

"When Jarvis is performing a task and it got something wrong, I want to pause
it and tell it what to do."

Until now the only mid-run lever was CANCEL, which is TERMINAL: every pending
step is marked SKIPPED and the plan is gone, so the completed work can only be
recovered by re-asking from scratch. This module is cancel's sibling — the same
cooperative machinery, one behavioural difference that is the entire point:

    apply_cancellation  →  pending steps become SKIPPED (they will never run)
    apply_pause         →  pending steps stay PENDING   (they are still to run)

That single difference is what lets a paused plan be CONTINUED or REPLANNED.
Everything else is deliberately identical to app/agents/cancellation.py:

- Cooperative: the flag is checked by the planner BETWEEN steps (and by
  _settle before parking a pause). A step already executing always finishes —
  a tool call is never killed mid-write. That is why the API answers "pause
  requested", not "paused".
- In-memory only, and that is correct: it targets a run alive in THIS process.
  A restart kills the run itself and fail_interrupted_tasks settles the row.
  (A plan that ALREADY paused is a different thing — it is parked in SQLite by
  the normal plan_store path and does survive a restart.)
- Every applied pause and every steer is AUDITED in ActivityLog. An audit-write
  failure is logged, never raised — registry discipline.

The steer
---------
A pause may carry an instruction ("stop, use the D drive one"). It rides in the
same registry entry, is consumed ONCE (take_steer), and the task runner hands it
to planner.answer() — which already appends it to plan.user_answers, re-enters
the graph at `revise`, keeps every completed step's results, and forces FRESH
approval on anything new it produces. There is no new resume machinery here and
deliberately so: a paused plan's correction IS an answer to the implicit
question "what should I do differently?".

CURRENT_TASK_ID
---------------
A long-running tool (a `browse` step can run for minutes) needs to know which
run it belongs to so it can stop between its OWN actions. A ContextVar, not a
module global: several domain agents run concurrently on one loop, and a global
would let one agent's pause stop another's browse. The task runner sets it once
per run; anything awaited inside that run inherits it.
"""
import json
from contextvars import ContextVar
from typing import Callable, Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.schemas import AgentPlan, PlanStatus, StepStatus
from app.db.models import ActivityLog

# task_id → the steer that came with the pause ("" for a bare pause). Set by
# the API/router, consulted by the planner's between-steps check, consumed by
# the runner, cleared when the run's asyncio task finishes.
_PAUSE_REQUESTED: dict[str, str] = {}

# The run the current coroutine belongs to. Read by tools that loop internally
# (browse) so they can stop between their own actions rather than only at the
# step boundary — see stop_check_for_current().
CURRENT_TASK_ID: ContextVar[Optional[str]] = ContextVar(
    "jarvis_current_task_id", default=None
)

# Marker a tool puts in its ToolResult.output when it stopped because the USER
# asked, rather than because anything went wrong. Code-owned, so apply_pause
# never has to match on prose.
STOPPED_BY_USER = "stopped_by_user"

# How many times one run may be steered before it must settle. A steer is
# consumed once, so this only bounds a pathological loop (steer → pause → steer);
# a user correcting the same run repeatedly still works, one round per message.
MAX_STEER_ROUNDS = 8


# ============================================================== the registry

def request_pause(task_id: str, steer: str = "") -> None:
    """Ask a live run to stop between steps, optionally carrying the
    instruction to apply when it does."""
    _PAUSE_REQUESTED[task_id] = (steer or "").strip()
    logger.info(
        f"Pause requested for background task {task_id}"
        + (f" with a steer: '{(steer or '')[:80]}'" if steer else "")
    )


def pause_requested(task_id: str) -> bool:
    return task_id in _PAUSE_REQUESTED


def take_steer(task_id: str) -> Optional[str]:
    """Consume the steer that came with the pause. Returns None when there is
    no pause pending or it carried no instruction — one steer per pause, so a
    resumed run can never re-apply the correction it already applied."""
    steer = _PAUSE_REQUESTED.pop(task_id, None)
    if steer:
        logger.info(f"Steer consumed for task {task_id}: '{steer[:80]}'")
        return steer
    return None


def clear_pause(task_id: str) -> None:
    _PAUSE_REQUESTED.pop(task_id, None)


def stop_check_for_current() -> Optional[Callable[[], bool]]:
    """A stop predicate for the run this coroutine belongs to, or None when it
    does not belong to a background task (an inline plan, a direct API call, a
    test). Built HERE, on the caller's loop, where the ContextVar is visible —
    the returned closure only reads a module-level dict, so it is safe to call
    from the browser thread."""
    task_id = CURRENT_TASK_ID.get()
    if not task_id:
        return None
    return lambda: task_id in _PAUSE_REQUESTED


# ================================================================== applying

def _stopped_by_user(step) -> bool:
    """Did this step fail because the user stopped it, rather than because
    something went wrong? Read from a code-owned marker in the tool's own
    output — never from the error prose."""
    result = getattr(step, "result", None)
    output = getattr(result, "output", None) if result is not None else None
    return isinstance(output, dict) and bool(output.get(STOPPED_BY_USER))


def apply_pause(plan: AgentPlan) -> None:
    """Hold a plan between steps: pending steps STAY PENDING (the difference
    from apply_cancellation), any open question is closed, and the message
    states exactly how far it got and what the user can do next.

    A step that FAILED because the user stopped it mid-action (a browse that
    stopped between its own actions) is reset to PENDING too, so a plain
    "carry on" re-runs it from the top rather than stepping over a hole. Safe:
    `browse` is READ, and any world-acting gesture or form submit inside it
    pauses for its own approval on the re-run."""
    for step in plan.steps:
        if step.status == StepStatus.FAILED and _stopped_by_user(step):
            step.status = StepStatus.PENDING
            step.result = None
    plan.question = None
    plan.status = PlanStatus.PAUSED
    completed = len(plan.completed_steps())
    remaining = len(plan.pending_steps())
    done = (
        f"{completed} step(s) had already finished"
        if completed
        else "nothing had run yet"
    )
    plan.message = (
        f"Paused — {done} and {remaining} step(s) are still to run; nothing "
        f"further was executed. Tell me what to change and I'll pick it up "
        f"from here, or say carry on to continue as planned."
    )
    logger.info(
        f"Plan {plan.id} paused cooperatively "
        f"({completed} completed, {remaining} still pending)"
    )


async def log_pause(db: AsyncSession, plan: AgentPlan) -> None:
    """Audit the applied pause in ActivityLog. Never raises."""
    await _audit(
        db,
        plan,
        tool_name="pause_plan",
        summary=(
            f"Paused by the user mid-plan — {len(plan.completed_steps())} step(s) "
            f"had completed, {len(plan.pending_steps())} pending step(s) are "
            f"held and were NOT skipped."
        ),
    )


async def log_steer(db: AsyncSession, plan: AgentPlan, steer: str) -> None:
    """Audit the correction the user steered a paused plan with. Never raises."""
    await _audit(
        db,
        plan,
        tool_name="steer_plan",
        summary=f"Paused plan steered by the user: '{(steer or '')[:300]}'",
    )


async def _audit(
    db: AsyncSession, plan: AgentPlan, *, tool_name: str, summary: str
) -> None:
    try:
        db.add(ActivityLog(
            session_id=plan.session_id,
            tool_name=tool_name,
            action=f"{tool_name}(plan_id='{plan.id}')"[:256],
            parameters=json.dumps({
                "plan_id": plan.id,
                "task_id": plan.task_id,
                "goal": plan.goal[:300],
            }),
            result_summary=summary,
            success=True,
            permission_level="read",
        ))
        await db.commit()
    except Exception as e:
        logger.warning(f"ActivityLog write failed for {tool_name} (non-critical): {e}")
