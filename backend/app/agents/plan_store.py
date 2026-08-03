"""
Jarvis OS — Pending Plan Store (Phase 3, Part 4; persisted in Phase 3.5)

Holding area for plans awaiting user approval or a clarifying-question
answer. Two layers:

- In-memory dict — the hot cache (foreground owns state, as before).
- SQLite `parked_plans` — the truth. Every parked plan is written through,
  so a backend restart or the cache TTL never destroys a plan the user has
  not answered yet. Rows expire after PLAN_DB_TTL_SECONDS and are deleted
  the moment the plan is consumed (pop_plan) — one answer per plan, still.

The persisted payload includes the planner INPUTS that are excluded from API
serialization (conversation, memory_context, user_answers, questions_asked):
without them a plan resumed after a restart would replan amnesiac.

Persistence failures are logged and never break parking — the plan still
lives in memory for the caller (graceful degradation to Phase 3 behavior).
"""
import json
import time
from datetime import timedelta
from typing import Optional

from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.schemas import AgentPlan, PlanStatus
from app.db.models import ParkedPlan, utc_now

PLAN_TTL_SECONDS = 600  # in-memory hot-cache TTL (a cache miss falls back to SQLite)
PLAN_DB_TTL_SECONDS = 24 * 3600  # how long a parked plan survives unanswered

# plan_id → (plan, expiry epoch)
_PENDING_PLANS: dict[str, tuple[AgentPlan, float]] = {}

# AgentPlan fields excluded from API serialization but required to resume a
# plan faithfully after a restart — persisted alongside the public fields.
_PLANNER_INPUT_FIELDS = ("conversation", "memory_context", "user_answers", "questions_asked")


def _evict_expired() -> None:
    now = time.time()
    for plan_id in [pid for pid, (_, exp) in _PENDING_PLANS.items() if now > exp]:
        logger.info(f"Pending plan evicted from the memory cache: {plan_id}")
        del _PENDING_PLANS[plan_id]


# ============================================================ serialization

def _serialize_plan(plan: AgentPlan) -> str:
    data = plan.model_dump(mode="json")
    for field in _PLANNER_INPUT_FIELDS:
        data[field] = getattr(plan, field)
    return json.dumps(data, default=str)


def _deserialize_plan(payload: str) -> Optional[AgentPlan]:
    try:
        return AgentPlan.model_validate(json.loads(payload))
    except Exception as e:
        logger.warning(f"Parked plan payload could not be restored: {e}")
        return None


# ================================================================== the API

async def put_plan(db: AsyncSession, plan: AgentPlan) -> None:
    """Park a plan awaiting approval/answer: memory cache + SQLite row."""
    _evict_expired()
    _PENDING_PLANS[plan.id] = (plan, time.time() + PLAN_TTL_SECONDS)
    try:
        row = await db.get(ParkedPlan, plan.id)
        expires_at = utc_now() + timedelta(seconds=PLAN_DB_TTL_SECONDS)
        if row is None:
            db.add(ParkedPlan(
                id=plan.id,
                session_id=plan.session_id,
                status=plan.status.value,
                payload=_serialize_plan(plan),
                expires_at=expires_at,
            ))
        else:  # re-park after a replan/follow-up question: same id, new state
            row.status = plan.status.value
            row.payload = _serialize_plan(plan)
            row.expires_at = expires_at
        await db.commit()
    except Exception as e:
        logger.warning(f"Persisting parked plan {plan.id} failed (memory copy kept): {e}")


def get_plan(plan_id: str) -> Optional[AgentPlan]:
    """Peek the in-memory cache without consuming. None on cache miss — use
    pop_plan for the authoritative (SQLite-backed) lookup."""
    _evict_expired()
    entry = _PENDING_PLANS.get(plan_id)
    return entry[0] if entry else None


async def pop_plan(db: AsyncSession, plan_id: str) -> Optional[AgentPlan]:
    """Consume a pending plan (on approve/cancel/answer). Falls back to the
    SQLite row when the memory cache misses (restart / cache TTL). The row is
    deleted either way — one answer per plan; the delete's rowcount settles
    races, so two concurrent answers can never both win."""
    _evict_expired()
    entry = _PENDING_PLANS.pop(plan_id, None)
    plan = entry[0] if entry else None

    row_payload: Optional[str] = None
    try:
        if plan is None:
            row = await db.get(ParkedPlan, plan_id)
            if row is not None and row.expires_at > utc_now():
                row_payload = row.payload
        result = await db.execute(delete(ParkedPlan).where(ParkedPlan.id == plan_id))
        await db.commit()
        if plan is None and row_payload is not None:
            if result.rowcount == 0:  # someone else consumed it first
                return None
            plan = _deserialize_plan(row_payload)
            if plan is not None:
                logger.info(f"Parked plan {plan_id} restored from SQLite (cache miss)")
    except Exception as e:
        logger.warning(f"Parked-plan row cleanup/restore failed for {plan_id}: {e}")
    return plan


# The statuses in which a parked plan OWNS the session's next chat message.
# AWAITING_CHOICE: it asked a question and is waiting for the answer.
# PAUSED (2026-08-03): the user stopped it mid-run and is about to say what to
# change — the same relationship, with the question left implicit. Widening
# this ONE set is what routes a typed steer into planner.answer() *and* makes
# the reminder / routine / continuation routers defer to a paused plan, since
# they all already call this to mean "an open plan owns the next message".
# The states in which a parked plan OWNS the session's next chat message.
#
# AWAITING_APPROVAL joined the set on 2026-08-03, closing a hole that predates
# the pause round: an approval card was DEAF. Telling Jarvis "that's not right"
# while it asked to delete five files never reached the plan — the message fell
# through to the task router (starting a SECOND agent on the correction) while
# the original card stayed live and CLICKABLE for the parked plan's whole 24h
# TTL. Approve it the next morning and the uncorrected plan ran.
#
# A question card and an approval card are the same thing from the user's
# chair: Jarvis stopped and is waiting. They now behave the same. What a typed
# message MEANS at an approval card is decided in code by the task router —
# and a typed word can never GRANT approval (see _is_typed_approval there).
_OPEN_STATUSES = (
    PlanStatus.AWAITING_CHOICE,
    PlanStatus.PAUSED,
    PlanStatus.AWAITING_APPROVAL,
)


async def get_choice_plan_for_session(
    db: AsyncSession, session_id: str
) -> Optional[AgentPlan]:
    """The session's open plan — one awaiting a clarifying answer, or one the
    user paused — WITHOUT consuming it. The chat router peeks here so a typed
    reply reaches the plan it belongs to. Checks the memory cache first, then
    SQLite (restart survival). Newest first if several exist (shouldn't happen,
    but be deterministic)."""
    _evict_expired()
    candidates = [
        plan for plan, _ in _PENDING_PLANS.values()
        if plan.status in _OPEN_STATUSES and plan.session_id == session_id
    ]
    if candidates:
        return max(candidates, key=lambda p: p.created_at)

    try:
        result = await db.execute(
            select(ParkedPlan)
            .where(
                ParkedPlan.session_id == session_id,
                ParkedPlan.status.in_([s.value for s in _OPEN_STATUSES]),
                ParkedPlan.expires_at > utc_now(),
            )
            .order_by(ParkedPlan.created_at.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        if row is not None:
            return _deserialize_plan(row.payload)
    except Exception as e:
        logger.warning(f"Choice-plan lookup in SQLite failed for session {session_id}: {e}")
    return None


async def purge_expired_plans(db: AsyncSession) -> None:
    """Delete expired parked_plans rows (called at startup)."""
    result = await db.execute(delete(ParkedPlan).where(ParkedPlan.expires_at <= utc_now()))
    await db.commit()
    if result.rowcount:
        logger.info(f"Purged {result.rowcount} expired parked plan(s)")
