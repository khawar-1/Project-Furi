"""
Jarvis OS — Agent API (Phase 3, Part 5)

POST /api/agent/execute  — plan a user goal; returns the finished plan for
                           READ-only goals, or an awaiting_approval plan
                           (parked in the pending-plan store) otherwise.
POST /api/agent/approve  — approve or cancel a parked plan; executes the
                           approved steps and returns the final plan. If the
                           run pauses again (replanned steps carry NEW
                           signatures), the plan re-parks for fresh approval.
POST /api/agent/choose   — answer a plan's clarifying question (status
                           awaiting_choice). The answer feeds the next
                           planning round; it never executes anything itself.
GET  /api/agent/tools    — JSON schemas of every registered tool.

The response is always the serialized AgentPlan — the frontend decides what
to render from plan.status: "completed" | "awaiting_approval" | "failed" |
"cancelled". All enforcement (approval gate, blocklist, signatures) lives
below this layer; these endpoints only move plans in and out of the store.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

import app.tools  # noqa: F401 — importing the package registers every tool
from app.agents import (
    AgentPlan,
    AgentPlanner,
    PlanStatus,
    answer_task_in_background,
    deterministic_plan_text,
    planner_memory_context,
    pop_plan,
    put_plan,
    resume_task_in_background,
    settle_cancelled_task,
)
from app.agents.summary import completed_plan_text
from app.core.dependencies import get_db, get_llm_provider
from app.db.persist import persist_message_best_effort
from app.providers.base import LLMProvider
from app.tools.registry import registry

router = APIRouter()


class ExecuteRequest(BaseModel):
    goal: str = Field(..., min_length=1, max_length=4000)
    session_id: Optional[str] = None


class ApproveRequest(BaseModel):
    plan_id: str
    approved: bool


class ChooseRequest(BaseModel):
    plan_id: str
    answer: str = Field(..., min_length=1, max_length=2000)


def _plan_response(plan: AgentPlan, outcome_text: Optional[str] = None) -> dict:
    data = plan.model_dump(mode="json")
    # Convenience flag so the frontend never string-compares the status enum
    data["requires_approval"] = plan.status == PlanStatus.AWAITING_APPROVAL
    # The readable outcome of an INLINE plan that reached a terminal state
    # through this endpoint (clicked option / Approve button). The chat SSE
    # path streams this text; the endpoints must return it or the answer is
    # never delivered (live bug 2026-07-12: "find all PDF files … tell me how
    # many" → folder question → click → silence).
    data["outcome_text"] = outcome_text
    return data


async def _persist_plan_message(
    db: AsyncSession, session_id: str, role: str, content: str
) -> None:
    """Best-effort chat-history write: the plan's outcome must never be lost
    to a persistence hiccup — the HTTP response still carries it. Delegates
    to the shared helper, whose rollback keeps the session usable."""
    await persist_message_best_effort(
        db, session_id, role, content, what="plan outcome message",
    )


async def _finalize_inline_plan(
    db: AsyncSession,
    provider: LLMProvider,
    plan: AgentPlan,
    user_reply: Optional[str] = None,
) -> Optional[str]:
    """Deliver an INLINE plan's outcome after a resume/answer through these
    endpoints — the mirror of what _stream_plan_run does for typed chat
    answers, so clicked options and typed replies are equivalent END TO END
    (the stated invariant; before 2026-07-12 it held only for the pause, not
    the outcome). Task-owned plans never come here: their outcome arrives by
    push via _settle.

    Returns the text the frontend should append as an assistant message, or
    None when the live PlanCard already carries the state (pauses re-park and
    the card shows the question/approval; a cancel shows its banner).
    Everything is also persisted to chat history so a reload still has the
    answer — inline PlanCards don't survive reloads, the Message rows do.
    """
    session_id = plan.session_id
    if session_id and user_reply:
        # The clicked option is the user's answer — the typed path persists
        # it, so this path must too (history parity).
        await _persist_plan_message(db, session_id, "user", user_reply)

    if plan.status == PlanStatus.COMPLETED:
        text = await completed_plan_text(provider, plan)
        if session_id and text:
            await _persist_plan_message(db, session_id, "assistant", text)
        return text or None
    if plan.status == PlanStatus.FAILED:
        text = deterministic_plan_text(plan)  # failures are never paraphrased
        if session_id and text:
            await _persist_plan_message(db, session_id, "assistant", text)
        return text or None

    # Cancelled: the card's "Cancelled — nothing was changed" banner is the
    # live feedback; persist for history honesty but return nothing.
    # Re-paused (fresh approval / follow-up question): the card carries the
    # interaction live; persist the deterministic text so the pending ask
    # survives a reload, exactly as the streamed path persists pause texts.
    if session_id:
        text = deterministic_plan_text(plan)
        if text:
            await _persist_plan_message(db, session_id, "assistant", text)
    return None


def _executing_snapshot(plan: AgentPlan) -> dict:
    """Response for a plan that just went back to background execution
    (Phase 4, Part 5): a deep copy taken BEFORE the detached run can mutate
    the plan, with status forced to executing. The real outcome arrives by
    push, not in this response."""
    snapshot = plan.model_copy(deep=True)
    snapshot.status = PlanStatus.EXECUTING
    snapshot.question = None
    return _plan_response(snapshot)


@router.post("/execute", summary="Plan and run a goal (pauses for approval)")
async def execute_goal(
    request: ExecuteRequest,
    db: AsyncSession = Depends(get_db),
    provider: LLMProvider = Depends(get_llm_provider),
) -> dict:
    memory = await planner_memory_context(db, request.goal)
    planner = AgentPlanner(db, provider, session_id=request.session_id, memory=memory)
    try:
        plan = await planner.start(request.goal)
    except Exception as e:
        logger.error(f"Agent execute failed for goal '{request.goal[:80]}': {e}")
        raise HTTPException(
            status_code=500,
            detail="The agent could not process this goal. Please try again.",
        )
    if plan.status in (PlanStatus.AWAITING_APPROVAL, PlanStatus.AWAITING_CHOICE):
        await put_plan(db, plan)
    return _plan_response(plan)


@router.post("/approve", summary="Approve or cancel a pending plan")
async def approve_plan(
    request: ApproveRequest,
    db: AsyncSession = Depends(get_db),
    provider: LLMProvider = Depends(get_llm_provider),
) -> dict:
    plan = await pop_plan(db, request.plan_id)
    if plan is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "This plan is no longer pending — it may have expired "
                "(plans wait 24 hours for approval) or was already answered."
            ),
        )
    # Phase 4, Part 5: a task-owned plan the user just approved goes back to
    # BACKGROUND execution — this response is only a snapshot; the outcome
    # arrives by push. Cancels stay inline (no LLM, no tools). If the Task
    # row is somehow gone, fall through to the inline resume so an approval
    # is never lost.
    if (
        plan.task_id
        and request.approved
        and plan.status == PlanStatus.AWAITING_APPROVAL
    ):
        response = _executing_snapshot(plan)
        task = await resume_task_in_background(db, plan, provider)
        if task is not None:
            return response

    # Carry the parked plan's chat + memory context so a replan after
    # approval sees what the original planning round saw
    planner = AgentPlanner(
        db, provider, session_id=plan.session_id,
        conversation=plan.conversation, memory=plan.memory_context,
    )
    try:
        plan = await planner.resume(plan, approved=request.approved)
    except Exception as e:
        # Steps may have run before the crash — ActivityLog keeps the audit
        # trail; the consumed approval is NOT re-parked.
        logger.error(f"Agent resume failed for plan {plan.id}: {e}")
        raise HTTPException(
            status_code=500,
            detail="The plan could not be resumed. Check the Activity timeline "
            "for anything that already ran.",
        )
    if plan.status in (PlanStatus.AWAITING_APPROVAL, PlanStatus.AWAITING_CHOICE):
        # Replanned steps have new signatures → fresh approval. A choice plan
        # that resume() refused to touch (approve on a question) re-parks too,
        # so a stray approve click can never destroy an open question.
        await put_plan(db, plan)
    elif plan.task_id and plan.status == PlanStatus.CANCELLED:
        # A cancelled background task settles its Task row here (inline —
        # cancelling runs nothing). No push: the user cancelled from the UI,
        # this response is the feedback.
        await settle_cancelled_task(db, plan)
    outcome = (
        await _finalize_inline_plan(db, provider, plan)
        if plan.task_id is None else None
    )
    return _plan_response(plan, outcome)


@router.post("/choose", summary="Answer a plan's clarifying question")
async def choose_option(
    request: ChooseRequest,
    db: AsyncSession = Depends(get_db),
    provider: LLMProvider = Depends(get_llm_provider),
) -> dict:
    plan = await pop_plan(db, request.plan_id)
    if plan is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "This question is no longer pending — it may have expired "
                "(plans wait 24 hours) or was already answered."
            ),
        )
    # Phase 4, Part 5: a task-owned question continues in the background —
    # clicked options and typed chat replies behave identically.
    if plan.task_id:
        response = _executing_snapshot(plan)
        task = await answer_task_in_background(db, plan, request.answer, provider)
        if task is not None:
            return response

    planner = AgentPlanner(
        db, provider, session_id=plan.session_id,
        conversation=plan.conversation, memory=plan.memory_context,
    )
    try:
        plan = await planner.answer(plan, request.answer)
    except Exception as e:
        logger.error(f"Agent answer failed for plan {plan.id}: {e}")
        raise HTTPException(
            status_code=500,
            detail="The plan could not continue after your answer. Check the "
            "Activity timeline for anything that already ran.",
        )
    if plan.status in (PlanStatus.AWAITING_APPROVAL, PlanStatus.AWAITING_CHOICE):
        await put_plan(db, plan)  # paused again: fresh approval or a follow-up question
    outcome = (
        await _finalize_inline_plan(db, provider, plan, user_reply=request.answer)
        if plan.task_id is None else None
    )
    return _plan_response(plan, outcome)


@router.get("/tools", summary="List all registered tools")
async def list_tools() -> list:
    return [d.model_dump(mode="json") for d in registry.definitions()]
