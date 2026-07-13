"""
Jarvis OS — Background Task Runner (Phase 4, Part 5)

Plans escape the chat turn: a persisted Task row wraps an AgentPlan that
executes as a detached asyncio task. SQLite is the truth for task state —
the asyncio task is only the engine; a restart marks still-`running` rows
failed at startup (steps may have run; ActivityLog is the audit trail).

Every pause and outcome lands in _settle, which does three things in a
fixed order:
1. Park the plan (paused) through the SAME put_plan/pop_plan store inline
   plans use — signature approvals and consume-once semantics are untouched.
2. Persist an assistant chat Message into the task's session (the push
   channel has no queue — the same rule reminders follow).
3. push() a "task" event, best-effort. The payload carries title/body, so
   Part 3's native-toast wiring shows it with zero changes, plus the
   serialized plan so a live window renders the approval card in chat.

All texts are deterministic (rendering.py) — no LLM call ever happens in
the background runner; what the user approves or is told about an outcome
is never paraphrased.

Runner failures NEVER propagate (reminder-handler discipline): a crashed
run settles its Task as failed and pushes the failure.
"""
import asyncio
import json
from typing import Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.cancellation import (
    apply_cancellation,
    cancel_requested,
    clear_cancel,
    log_cancellation,
    request_cancel,
)
from app.agents.plan_store import put_plan
from app.agents.planner import AgentPlanner
from app.agents.rendering import deterministic_plan_text, serialize_plan_for_api
from app.agents.schemas import AgentPlan, PlanStatus
from app.core.push import push
from app.db.models import Message, ParkedPlan, Task, utc_now
from app.providers.base import LLMProvider

TASK_EVENT = "task"

# Indirection so tests can point the runner at a test database. Resolved at
# call time, never at import time (same pattern as memory_tools).
SESSION_FACTORY = None


def _session_factory():
    if SESSION_FACTORY is not None:
        return SESSION_FACTORY
    from app.db.database import AsyncSessionLocal
    return AsyncSessionLocal


# Live runner handles: keeps the asyncio tasks referenced (an unreferenced
# task can be garbage-collected mid-run) and lets tests await completion.
_RUNNING: dict[str, asyncio.Task] = {}

_PAUSED_STATUS = {
    PlanStatus.AWAITING_APPROVAL: "awaiting_approval",
    PlanStatus.AWAITING_CHOICE: "awaiting_choice",
}
_TERMINAL_STATUS = {
    PlanStatus.COMPLETED: "completed",
    PlanStatus.FAILED: "failed",
    PlanStatus.CANCELLED: "cancelled",
}

_TITLES = {
    "awaiting_approval": "Jarvis needs your approval",
    "awaiting_choice": "Jarvis has a question",
    "completed": "Task complete",
    "failed": "Task failed",
    "cancelled": "Task cancelled",
}


def _spawn(task_id: str, coro) -> None:
    handle = asyncio.get_running_loop().create_task(coro)
    _RUNNING[task_id] = handle

    def _done(fut: asyncio.Task) -> None:
        _RUNNING.pop(task_id, None)
        # A cancel flag that never got consumed (the run finished first, or
        # paused before the next between-steps check) must not leak into a
        # later resume of the same task.
        clear_cancel(task_id)
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc is not None:  # _run_* swallow their own errors — this is a backstop
            logger.error(f"Background task {task_id} runner crashed: {exc}")

    handle.add_done_callback(_done)


async def wait_for_task(task_id: str) -> None:
    """Await the background run, if one is in flight (tests / shutdown)."""
    handle = _RUNNING.get(task_id)
    if handle is not None:
        await handle


def _short_goal(goal: str, cap: int = 80) -> str:
    goal = " ".join(goal.split())
    return goal if len(goal) <= cap else goal[: cap - 1] + "…"


def _task_body(task: Task, plan: AgentPlan, status: str) -> str:
    """The one deterministic text for this transition — used verbatim as the
    push body (→ native toast) and as the persisted chat message."""
    text = deterministic_plan_text(plan)
    goal = _short_goal(task.goal)
    prefixes = {
        "awaiting_approval": f'Background task "{goal}" is ready and needs your approval.\n\n',
        "awaiting_choice": f'Background task "{goal}" has a question for you.\n\n',
        "completed": f'Finished the background task "{goal}". ',
        "failed": f'The background task "{goal}" failed. ',
        "cancelled": f'Background task "{goal}": ',
    }
    return prefixes.get(status, "") + text


# ============================================================== entry points

async def start_task(
    db: AsyncSession,
    goal: str,
    session_id: Optional[str],
    conversation: str = "",
    memory: str = "",
    provider: Optional[LLMProvider] = None,
) -> Task:
    """Persist the Task row FIRST (row-before-work, like create_reminder),
    then start planning/executing in the background. The caller's provider is
    reused by the detached run — providers are stateless clients."""
    task = Task(goal=goal, session_id=session_id, status="running")
    db.add(task)
    await db.commit()
    await db.refresh(task)
    logger.info(f"Background task {task.id} started: '{goal[:80]}'")
    _spawn(task.id, _run_new(task.id, goal, session_id, conversation, memory, provider))
    return task


async def resume_task_in_background(
    db: AsyncSession, plan: AgentPlan, provider: LLMProvider
) -> Optional[Task]:
    """The user approved a task-owned plan: flip the Task back to running NOW
    (so the caller's response reflects it) and execute the approved steps in
    the background. Returns None when the Task row is gone — the caller then
    falls back to the inline resume, so an approval is never lost."""
    task = await _mark_running(db, plan)
    if task is None:
        return None
    _spawn(task.id, _run_continuation(task.id, plan, provider, answer=None))
    return task


async def answer_task_in_background(
    db: AsyncSession, plan: AgentPlan, answer: str, provider: LLMProvider
) -> Optional[Task]:
    """The user answered a task-owned plan's clarifying question: continue
    planning in the background. Same None-fallback contract as resume."""
    task = await _mark_running(db, plan)
    if task is None:
        return None
    _spawn(task.id, _run_continuation(task.id, plan, provider, answer=answer))
    return task


def request_task_cancel(task_id: str) -> bool:
    """Ask a LIVE background run to stop between steps (Phase 4, Part 6).
    Cooperative: the step currently executing finishes — never killed
    mid-write — then the planner settles the plan as CANCELLED (audited in
    ActivityLog) and the outcome arrives by push like any other transition.
    Returns False when no run is in flight in this process — there is
    nothing a flag could stop (the row's state is settled elsewhere:
    approval-gate cancel for paused tasks, startup reconciliation after a
    restart)."""
    if task_id not in _RUNNING:
        return False
    request_cancel(task_id)
    return True


async def settle_cancelled_task(db: AsyncSession, plan: AgentPlan) -> None:
    """Mirror a user-cancelled plan onto its Task row. No push — the user
    cancelled from the UI, so the endpoint's response IS the feedback; the
    outcome message is still persisted so the session history stays coherent."""
    if not plan.task_id:
        return
    task = await db.get(Task, plan.task_id)
    if task is None:
        logger.warning(f"Cancelled plan {plan.id} has no Task row ({plan.task_id})")
        return
    await _settle(db, task, plan, notify=False)


async def _mark_running(db: AsyncSession, plan: AgentPlan) -> Optional[Task]:
    if not plan.task_id:
        return None
    task = await db.get(Task, plan.task_id)
    if task is None:
        logger.warning(f"Plan {plan.id} points at missing Task {plan.task_id}")
        return None
    task.status = "running"
    await db.commit()
    return task


# ================================================================== runners

async def _run_new(
    task_id: str,
    goal: str,
    session_id: Optional[str],
    conversation: str,
    memory: str,
    provider: Optional[LLMProvider],
) -> None:
    async with _session_factory()() as db:
        task = await db.get(Task, task_id)
        if task is None:
            logger.warning(f"Background run found no Task row {task_id} — dropped")
            return
        try:
            planner = AgentPlanner(
                db, provider, session_id=session_id,
                conversation=conversation, memory=memory,
                cancel_check=lambda: cancel_requested(task_id),
            )
            plan = await planner.start(goal)
            await _settle(db, task, plan)
        except Exception as e:
            logger.error(f"Background task {task_id} crashed while planning: {e}")
            await _fail_task(db, task_id, f"The planner crashed: {e}")


async def _run_continuation(
    task_id: str, plan: AgentPlan, provider: LLMProvider, answer: Optional[str]
) -> None:
    """Post-approval / post-answer execution. If this crashes, the plan is
    NEVER re-parked — steps may have run; ActivityLog is the audit trail.
    The Task settles as failed instead (same rule approve_plan documents)."""
    async with _session_factory()() as db:
        task = await db.get(Task, task_id)
        if task is None:
            logger.warning(f"Background continuation found no Task row {task_id} — dropped")
            return
        try:
            planner = AgentPlanner(
                db, provider, session_id=plan.session_id,
                conversation=plan.conversation, memory=plan.memory_context,
                cancel_check=lambda: cancel_requested(task_id),
            )
            if answer is None:
                plan = await planner.resume(plan, approved=True)
            else:
                plan = await planner.answer(plan, answer)
            await _settle(db, task, plan)
        except Exception as e:
            logger.error(f"Background task {task_id} crashed while resuming: {e}")
            await _fail_task(
                db, task_id,
                "The plan could not be resumed. Check the Activity timeline "
                "for anything that already ran.",
            )


# ================================================================== settling

async def _settle(db: AsyncSession, task: Task, plan: AgentPlan, notify: bool = True) -> None:
    """Mirror the plan onto the Task row, park it if paused, persist the
    deterministic outcome/pause message, and push the event."""
    plan.task_id = task.id  # BEFORE parking, so approve/choose route back here

    # Cooperative cancel, last check (Part 6): a cancel that landed while the
    # final planning round was in flight would otherwise park the plan and
    # ask the user to approve work they just cancelled. Between "the run is
    # about to pause" and "the pause is parked" is still between steps.
    if plan.status in _PAUSED_STATUS and cancel_requested(task.id):
        apply_cancellation(plan)
        await log_cancellation(db, plan)

    status = _PAUSED_STATUS.get(plan.status) or _TERMINAL_STATUS.get(plan.status)
    if status is None:  # planner never returns EXECUTING — defensive only
        logger.error(f"Task {task.id} settled with unexpected plan status {plan.status}")
        status = "failed"
        plan.status = PlanStatus.FAILED
        plan.message = plan.message or "The plan ended in an unexpected state."

    if status in ("awaiting_approval", "awaiting_choice"):
        await put_plan(db, plan)  # unchanged store: same signatures, same pop-once

    task.status = status
    task.plan_id = plan.id
    task.plan_payload = json.dumps(serialize_plan_for_api(plan), default=str)
    body = _task_body(task, plan, status)
    if status in _TERMINAL_STATUS.values():
        task.message = body
        task.finished_at = utc_now()
    await db.commit()

    # Chat message first (the durable copy), push second (best-effort).
    if task.session_id:
        from app.db.persist import persist_message_best_effort
        await persist_message_best_effort(
            db, task.session_id, "assistant", body,
            what=f"task {task.id} message",
        )

    if notify:
        await push(TASK_EVENT, {
            "task_id": task.id,
            "status": status,
            "session_id": task.session_id,
            "goal": task.goal,
            "title": _TITLES.get(status, "Jarvis"),
            "body": body,
            "plan": serialize_plan_for_api(plan),
        })

    # Phase 6 Part 5: a goal that keeps completing earns an offer to be saved
    # as a reusable routine. Best-effort and completed-only — recurrence is
    # measured over completed Task rows (this one is already committed above),
    # and the offer must never affect settling.
    if status == "completed" and task.session_id:
        try:
            from app.core.routines import maybe_offer_routine
            await maybe_offer_routine(db, task.goal, task.session_id)
        except Exception as e:
            logger.warning(f"Routine offer-to-save check failed (non-critical): {e}")

    logger.info(f"Background task {task.id} → {status}")


async def _fail_task(db: AsyncSession, task_id: str, reason: str) -> None:
    """Settle a crashed run as failed. Never raises — this is the last line.
    Takes the id, not the instance: the rollback expires whatever the crashed
    transaction touched, so the row is re-fetched cleanly."""
    try:
        await db.rollback()  # the crash may have left the session dirty
        task = await db.get(Task, task_id)
        if task is None:
            return
        task.status = "failed"
        body = f'The background task "{_short_goal(task.goal)}" failed. {reason}'
        task.message = body
        task.finished_at = utc_now()
        await db.commit()
        if task.session_id:
            db.add(Message(session_id=task.session_id, role="assistant", content=body))
            await db.commit()
        await push(TASK_EVENT, {
            "task_id": task.id,
            "status": "failed",
            "session_id": task.session_id,
            "goal": task.goal,
            "title": _TITLES["failed"],
            "body": body,
        })
    except Exception as e:
        logger.error(f"Settling failed task {task.id} also failed: {e}")


# ================================================================== startup

async def fail_interrupted_tasks(db: AsyncSession) -> None:
    """Startup truth-keeping (called AFTER purge_expired_plans): a Task still
    `running` was killed by the restart — mark it failed honestly (steps may
    have run; ActivityLog has the audit). A paused Task whose parked_plans
    row is gone (expired unanswered, or consumed without settling) can never
    be resumed — failed too. Paused tasks WITH a live parked row survive:
    the plan restores from SQLite when the user answers."""
    interrupted = 0

    result = await db.execute(select(Task).where(Task.status == "running"))
    for task in result.scalars().all():
        task.status = "failed"
        body = (
            f'The background task "{_short_goal(task.goal)}" was interrupted '
            f"by a backend restart. Check the Activity timeline for anything "
            f"that already ran, and ask again if you still want it done."
        )
        task.message = body
        task.finished_at = utc_now()
        if task.session_id:
            db.add(Message(session_id=task.session_id, role="assistant", content=body))
        interrupted += 1

    result = await db.execute(
        select(Task).where(Task.status.in_(("awaiting_approval", "awaiting_choice")))
    )
    for task in result.scalars().all():
        row = await db.get(ParkedPlan, task.plan_id) if task.plan_id else None
        if row is not None and row.expires_at > utc_now():
            continue  # still answerable — the parked plan is the resume truth
        task.status = "failed"
        body = (
            f'The background task "{_short_goal(task.goal)}" expired while '
            f"waiting for your answer — nothing further was executed. Ask "
            f"again if you still want it done."
        )
        task.message = body
        task.finished_at = utc_now()
        if task.session_id:
            db.add(Message(session_id=task.session_id, role="assistant", content=body))
        interrupted += 1

    await db.commit()
    if interrupted:
        logger.info(f"Marked {interrupted} interrupted/expired background task(s) failed")
