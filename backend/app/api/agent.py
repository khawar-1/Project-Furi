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
    get_plan,
    planner_memory_context,
    pop_plan,
    put_plan,
    resume_task_in_background,
    settle_cancelled_task,
)
from app.agents.rendering import serialize_plan_for_api
from app.agents.summary import completed_plan_text
from app.core.dependencies import get_db, get_llm_provider
from app.db.persist import persist_message_best_effort
from app.providers.base import LLMProvider
from app.tools.registry import registry

router = APIRouter()


class ExecuteRequest(BaseModel):
    goal: str = Field(..., min_length=1, max_length=4000)
    session_id: Optional[str] = None


class SpokenApproval(BaseModel):
    """An approval given OFF the card — by voice, or from the phone surface.

    ⚠️ `contract_hash` IS THE WHOLE POINT. `task_router._is_typed_approval`
    deliberately REFUSES a typed "yes" because consent to a write is consent to
    a SIGNATURE SET, and a bare word is bound to nothing. This carries the same
    binding through a different channel: the client echoes the hash of the
    contract it was GIVEN, and the server re-derives it from the plan it just
    popped. It is not a password and it is not a secret — it is proof that the
    thing being approved is the thing that was presented.

    `utterance` is what the user actually said. THE BACKEND DECIDES whether it
    is consent, deliberately — putting that word set in the client would make
    the one consent rule in the system untestable and unfalsifiable, and would
    be a second copy of a list the moment anything else needed it."""

    contract_hash: str = Field(..., min_length=16, max_length=128)
    utterance: str = Field(..., min_length=1, max_length=500)


class ApproveRequest(BaseModel):
    plan_id: str
    approved: bool
    # Absent = approved on the DESKTOP card, which needs no echo: the card is
    # push-updated, so it IS the contract. Present = approved off-card and must
    # prove what it saw.
    spoken: Optional[SpokenApproval] = None
    # ⚠️ THE SAME BINDING WITHOUT THE VOICE (2026-08-04). The remote phone
    # surface renders a plan from `Task.plan_payload`, which is a POLLED
    # SNAPSHOT — it can lag behind the parked plan when a steer or a replan
    # re-parks it under the same id. So the phone echoes the hash of the
    # contract it drew, and the server refuses if the steps have moved since.
    #
    # Three places already promised this and the phone did not do it
    # (`remote_manifest`'s own docstring, `rendering.serialize_plan_for_api`,
    # CLAUDE.md) — the "BACKEND_HOST was decorative" shape: a documented claim
    # the code did not make. Optional, so the desktop card path is unchanged.
    contract_hash: Optional[str] = Field(default=None, min_length=16, max_length=128)

    def echoed_contract_hash(self) -> Optional[str]:
        """The hash this client claims it was shown, from EITHER channel.

        One accessor so the pre-pop check and the post-pop re-derivation cannot
        drift into disagreeing about what was echoed."""
        if self.spoken is not None:
            return self.spoken.contract_hash
        return self.contract_hash


class ChooseRequest(BaseModel):
    plan_id: str
    answer: str = Field(..., min_length=1, max_length=2000)


# Statuses a plan is PARKED in — it stopped without settling, so it must stay
# answerable. PAUSED (2026-08-03) is here for the same reason the other two
# are: a plan that is not parked cannot be resumed, and the user's remaining
# steps would be silently lost.
_PARKABLE = (
    PlanStatus.AWAITING_APPROVAL,
    PlanStatus.AWAITING_CHOICE,
    PlanStatus.PAUSED,
)


def _plan_response(plan: AgentPlan, outcome_text: Optional[str] = None) -> dict:
    # ⚠️ DELEGATES rather than re-implementing. This used to be its own copy of
    # serialize_plan_for_api's body — model_dump plus the requires_approval
    # flag — and the day the shared serializer gained the spoken contract and
    # its hash (2026-08-03), the /api/agent/* endpoints would silently have
    # been the ONE surface without them. A second copy of a serializer is the
    # same hole as a second copy of a list, and this codebase has recorded that
    # one five times.
    data = serialize_plan_for_api(plan)
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
    if plan.status in _PARKABLE:
        await put_plan(db, plan)
    return _plan_response(plan)


_CONTRACT_CHANGED = (
    "That approval was for a different set of steps — the plan has changed "
    "since it was read out. Nothing has run; please review it again."
)


async def _guard_spoken_approval(db: AsyncSession, request: "ApproveRequest") -> None:
    """The two conditions specific to a SPOKEN approval: are these words consent
    at all, and is voice permitted to approve this plan. Raises, never returns a
    verdict — so a caller cannot forget to check one.

    The third condition, the contract hash, lives in `_check_contract_hash`
    because the phone echoes one WITHOUT speaking (2026-08-04) and both callers
    must ask exactly the same question.

    Ordered cheapest-first, and every failure leaves the plan untouched."""
    from app.agents.spoken import (
        is_spoken_approval,
        plan_needs_screen,
        spoken_approval_level,
    )
    from app.core.app_settings import get_voice_config

    # 1. Is it consent at all? Fails CLOSED on anything unrecognised.
    if not is_spoken_approval(request.spoken.utterance):
        raise HTTPException(
            status_code=422,
            detail=(
                "That didn't sound like an approval, so nothing has run. Say "
                '"approve" to go ahead, "cancel" to drop it, or just tell me '
                "what to change."
            ),
        )

    plan = get_plan(request.plan_id)  # non-consuming peek
    if plan is None:
        return  # the pop below will 404 with the right message

    # 2. Is voice allowed to approve THIS plan? Checked server-side so a stale
    #    or hostile client cannot approve by voice while the setting is off —
    #    and `spoken_approval_level` folds in the voice MASTER switch, which
    #    this check read straight past until 2026-08-04.
    config = await get_voice_config(db)
    if plan_needs_screen(plan, spoken_approval_level(config)):
        raise HTTPException(
            status_code=403,
            detail=(
                "This one needs approving on the card, sir — nothing has run. "
                "Voice or spoken approval is turned off, or spoken approval is "
                "limited to non-destructive steps, in Settings."
            ),
        )


def _check_contract_hash(plan: Optional[AgentPlan], request: "ApproveRequest") -> None:
    """Is this consent to THESE steps? The binding that makes an off-card
    approval honest: a client can only hold this hash if it received the
    contract, and a plan whose pending steps changed produces a different one.

    A client that echoed nothing is not checked — that is the desktop card,
    which is push-updated and is itself the contract. `plan is None` skips too:
    the caller's own 404 says the right thing about a plan that is gone."""
    echoed = request.echoed_contract_hash()
    if plan is None or echoed is None or not request.approved:
        return
    if plan.contract_hash() != echoed:
        raise HTTPException(status_code=409, detail=_CONTRACT_CHANGED)


@router.post("/approve", summary="Approve or cancel a pending plan")
async def approve_plan(
    request: ApproveRequest,
    db: AsyncSession = Depends(get_db),
    provider: LLMProvider = Depends(get_llm_provider),
) -> dict:
    # ⚠️ EVERY OFF-CARD CHECK RUNS BEFORE THE POP, so a refusal leaves the plan
    # exactly where it was and the card stays clickable — the ordering
    # `_is_typed_approval`'s nudge already uses. Popping first would consume the
    # plan on a mis-heard word and turn it into a LOST approval.
    if request.spoken is not None and request.approved:
        await _guard_spoken_approval(db, request)
    # The hash check runs for BOTH off-card channels — spoken and the phone's
    # bare echo — against a non-consuming peek.
    _check_contract_hash(get_plan(request.plan_id), request)

    plan = await pop_plan(db, request.plan_id)
    if plan is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "This plan is no longer pending — it may have expired "
                "(plans wait 24 hours for approval) or was already answered."
            ),
        )
    # Re-derive from the plan we ACTUALLY popped, not the one we peeked at.
    # The peek is the courtesy that keeps the card alive on a mismatch; THIS is
    # the guarantee — the peek reads a hot cache and the pop reads the truth.
    try:
        _check_contract_hash(plan, request)
    except HTTPException:
        await put_plan(db, plan)  # un-consume: nothing was decided
        raise
    # Phase 4, Part 5: a task-owned plan the user just approved goes back to
    # BACKGROUND execution — this response is only a snapshot; the outcome
    # arrives by push. Cancels stay inline (no LLM, no tools). If the Task
    # row is somehow gone, fall through to the inline resume so an approval
    # is never lost.
    # PAUSED belongs here too (2026-08-03): "Continue" on a paused card is this
    # same call, and it MUST go back to the background runner — resuming it
    # inline would run the remaining steps inside this HTTP request, push
    # nothing, and leave the Task row stuck on "paused".
    if (
        plan.task_id
        and request.approved
        and plan.status in (PlanStatus.AWAITING_APPROVAL, PlanStatus.PAUSED)
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
    if plan.status in _PARKABLE:
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
    if plan.status in _PARKABLE:
        await put_plan(db, plan)  # paused again: fresh approval or a follow-up question
    outcome = (
        await _finalize_inline_plan(db, provider, plan, user_reply=request.answer)
        if plan.task_id is None else None
    )
    return _plan_response(plan, outcome)


@router.get("/tools", summary="List all registered tools")
async def list_tools() -> list:
    return [d.model_dump(mode="json") for d in registry.definitions()]
