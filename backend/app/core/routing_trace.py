"""Furi OS — per-turn routing trace (2026-08-03)

One record per chat turn saying which router took it and, when none did, WHY.

WHY THIS EXISTS
---------------
``ActivityLog`` records every tool call, so every path where Furi *acts*
leaves evidence. Routing is the mirror image: it fails OPEN by design — five
routers each ``return None`` silently, and ``maybe_handle_task`` has two exits
that produce an ordinary chat reply and no row anywhere (the gate did not fire;
the classifier said CHAT). ``logger.info("Chat message routed to …")`` fires
*after* the CHAT return, so only successes were ever logged.

That is backwards for a system whose dominant failure mode is "it didn't do the
thing": the not-doing was the one unaudited event. On 2026-07-17 a live routing
miss genuinely could not be root-caused from data, and the round that fixed it
had to reason from the code instead.

RELATIONSHIP TO TurnTimer
-------------------------
This is ``app/core/timing.py``'s sibling and deliberately shares its shape —
one object per turn, stamped as the turn progresses, best-effort throughout, no
global state. The difference is that TurnTimer LOGS once and forgets, while this
PERSISTS, because the whole point is to query it weeks later.

⚠️ THE CONTEXTVAR RULE: set it ONCE at the top of the turn, then only ever
MUTATE the object it points at. ``_classify_message`` runs inside an
``asyncio.gather`` (task_router.py), and a child task gets a *copy* of the
context — a ``.set()`` there would not propagate back out, while mutating the
shared object does. A ContextVar rather than a module global for the same reason
``interruption.CURRENT_TASK_ID`` is one: concurrent turns must not stamp each
other's records.

Stamping is a NO-OP when no trace is current, so every router stays callable
from tests, scripts and the non-streaming route with no fixture and no change.

Writing follows ``app/db/persist.py`` exactly: log, ROLL BACK, return False —
never raise. A poisoned session is the 2026-07-12 incident (a failed "non-
critical" INSERT left the session in a failed-transaction state and killed
``start_task`` on the same session). This row is observability; it must never
be able to cost a turn.
"""
from __future__ import annotations

import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from loguru import logger
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RoutingDecision, utc_now

# The full message already lives in `messages`; this copy only has to be
# recognizable when you are scanning for the turn that went wrong.
ROUTING_MESSAGE_MAX_CHARS = 300
# Swept by the housekeeping pass. Matches the scheduled_jobs settled-row policy.
ROUTING_RETENTION_DAYS = 30

# Outcomes. Anything not in this list is a bug in a caller, not a new state.
OUTCOME_REMINDER = "reminder"
OUTCOME_ROUTINE = "routine"
OUTCOME_CONTINUATION = "continuation"
OUTCOME_INTERRUPT = "interrupt"
OUTCOME_PLAN_ANSWER = "plan_answer"
OUTCOME_APPROVAL_REFUSED = "approval_refused"
OUTCOME_TASK_INLINE = "task_inline"
OUTCOME_TASK_BACKGROUND = "task_background"
OUTCOME_CHAT = "chat"
OUTCOME_CHAT_RESCUED = "chat_rescued"
OUTCOME_ERROR = "error"

# Why a turn fell open to chat. These need three different fixes and were
# indistinguishable from outside until this table existed.
FAIL_NO_GOAL = "no_goal"
FAIL_GATE_CLOSED = "gate_closed"
FAIL_PARKED_QUESTION = "parked_question"
FAIL_CLASSIFIER_CHAT = "classifier_chat"
FAIL_CLASSIFIER_ERROR = "classifier_error"


@dataclass
class RouteTrace:
    """Mutable per-turn record. Built by :func:`begin`, stamped by the routers,
    written once by :func:`flush`."""

    session_id: Optional[str] = None
    message: str = ""
    message_chars: int = 0
    has_conversation: bool = False

    gate_fired: Optional[bool] = None
    gate_reason: str = ""

    # The routing label this turn got, whoever decided it. classifier_ms IS
    # NULL is the precise test for "no LLM call was made" — see the model.
    label: Optional[str] = None
    mode: Optional[str] = None
    classifier_ms: Optional[int] = None
    classifier_error: Optional[str] = None
    classifier_model: Optional[str] = None

    bare_navigation: bool = False
    background_intent: bool = False

    agent: Optional[str] = None
    execution: Optional[str] = None
    outcome: str = OUTCOME_CHAT
    fail_open_reason: str = ""

    rescue_fired: bool = False
    rescue_ok: Optional[bool] = None
    impersonation_cut: bool = False

    task_id: Optional[str] = None
    plan_id: Optional[str] = None

    # Assigned up front so the streaming generator can UPDATE this exact row
    # later without re-reading it.
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    _started: float = field(default_factory=time.perf_counter)

    def route_ms(self) -> int:
        return int((time.perf_counter() - self._started) * 1000)


CURRENT: ContextVar[Optional[RouteTrace]] = ContextVar("routing_trace", default=None)


def begin(
    session_id: Optional[str], message: str, *, has_conversation: bool = False
) -> RouteTrace:
    """Start a turn's trace and make it current. Call ONCE, at the top of the
    turn — see the ContextVar rule in the module docstring."""
    text = (message or "").strip()
    trace = RouteTrace(
        session_id=session_id,
        message=text[:ROUTING_MESSAGE_MAX_CHARS],
        message_chars=len(text),
        has_conversation=has_conversation,
    )
    CURRENT.set(trace)
    return trace


def current() -> Optional[RouteTrace]:
    return CURRENT.get()


def reset() -> None:
    """Drop the current trace. Hygiene for tests and for the non-streaming
    route; a request task's context dies with it either way."""
    CURRENT.set(None)


# ------------------------------------------------------------------ stamping
# Explicit named stamps rather than a note(**fields) helper ON PURPOSE: a
# mistyped keyword in a kwargs version would silently record nothing, which is
# the "no-op that reports success" failure this codebase has already been bitten
# by (the 2026-07-27 unroute that lifted nothing and logged that it had).
# These are attribute writes — a typo is an AttributeError at author time.


def note_gate(fired: bool, reason: str = "") -> None:
    trace = CURRENT.get()
    if trace is not None:
        trace.gate_fired = fired
        trace.gate_reason = reason


def note_label(label: Optional[str], mode: Optional[str] = None) -> None:
    """Record a label decided in CODE (the bare-navigation shortcut). Leaves the
    classifier_* fields untouched, so a NULL classifier_ms still means "no LLM
    call was made on this turn"."""
    trace = CURRENT.get()
    if trace is not None:
        trace.label = label
        trace.mode = mode


def note_classified(
    label: Optional[str],
    mode: Optional[str] = None,
    *,
    ms: Optional[int] = None,
    error: Optional[str] = None,
    model: Optional[str] = None,
) -> None:
    """Record a label the CLASSIFIER produced, with its cost and any failure."""
    trace = CURRENT.get()
    if trace is None:
        return
    trace.label = label
    trace.mode = mode
    if ms is not None:
        trace.classifier_ms = ms
    if error is not None:
        trace.classifier_error = error[:256]
    if model is not None:
        trace.classifier_model = model[:64]


def note_bare_navigation() -> None:
    trace = CURRENT.get()
    if trace is not None:
        trace.bare_navigation = True


def note_background() -> None:
    trace = CURRENT.get()
    if trace is not None:
        trace.background_intent = True


def note_outcome(
    outcome: str,
    *,
    agent: Optional[str] = None,
    execution: Optional[str] = None,
    task_id: Optional[str] = None,
    plan_id: Optional[str] = None,
) -> None:
    trace = CURRENT.get()
    if trace is None:
        return
    trace.outcome = outcome
    if agent is not None:
        trace.agent = agent
    if execution is not None:
        trace.execution = execution
    if task_id is not None:
        trace.task_id = task_id
    if plan_id is not None:
        trace.plan_id = plan_id


def note_fail_open(reason: str) -> None:
    """Record WHY this turn is falling through to the chat path. The one column
    this table exists for."""
    trace = CURRENT.get()
    if trace is not None:
        trace.outcome = OUTCOME_CHAT
        trace.fail_open_reason = reason


# ------------------------------------------------------------------ writing


async def flush(db: AsyncSession, trace: Optional[RouteTrace]) -> bool:
    """Write the row. On ANY failure log, ROLL BACK, return False — the
    persist.py rule. Never raises."""
    if trace is None:
        return False
    try:
        db.add(RoutingDecision(
            id=trace.id,
            session_id=trace.session_id,
            message=trace.message,
            message_chars=trace.message_chars,
            has_conversation=trace.has_conversation,
            gate_fired=trace.gate_fired,
            gate_reason=trace.gate_reason,
            label=trace.label,
            mode=trace.mode,
            classifier_ms=trace.classifier_ms,
            classifier_error=trace.classifier_error,
            classifier_model=trace.classifier_model,
            bare_navigation=trace.bare_navigation,
            background_intent=trace.background_intent,
            agent=trace.agent,
            execution=trace.execution,
            outcome=trace.outcome,
            fail_open_reason=trace.fail_open_reason,
            rescue_fired=trace.rescue_fired,
            rescue_ok=trace.rescue_ok,
            impersonation_cut=trace.impersonation_cut,
            route_ms=trace.route_ms(),
            task_id=trace.task_id,
            plan_id=trace.plan_id,
        ))
        await db.commit()
        return True
    except Exception as e:
        logger.warning(f"Routing-decision write failed (non-critical): {e}")
        try:
            await db.rollback()  # un-poison the session — see the module docstring
        except Exception as rb:
            logger.warning(f"Rollback after failed routing write also failed: {rb}")
        return False


async def note_stream_outcome(
    db: AsyncSession,
    trace: Optional[RouteTrace],
    *,
    rescue_fired: bool = False,
    rescue_ok: Optional[bool] = None,
    impersonation_cut: bool = False,
) -> bool:
    """Amend a written row with what happened INSIDE the stream.

    The row is written when routing decides, which is before the chat LLM has
    said anything — but two of the most valuable signals only exist afterwards.
    A dead-end rescue means routing missed a web turn and the backstop caught
    it; an impersonation cut means the chat LLM fabricated a task lifecycle,
    which is what a routing miss looks like from the user's seat.

    Called from the streaming generator, which reaches its trace by CLOSURE —
    deliberately not through the ContextVar, whose context is not guaranteed to
    be the request's inside a StreamingResponse."""
    if trace is None:
        return False
    values: dict = {}
    if rescue_fired:
        trace.rescue_fired = True
        trace.rescue_ok = rescue_ok
        trace.outcome = OUTCOME_CHAT_RESCUED
        values.update(
            rescue_fired=True, rescue_ok=rescue_ok, outcome=OUTCOME_CHAT_RESCUED
        )
    if impersonation_cut:
        trace.impersonation_cut = True
        values["impersonation_cut"] = True
    if not values:
        return False
    try:
        await db.execute(
            update(RoutingDecision).where(RoutingDecision.id == trace.id).values(**values)
        )
        await db.commit()
        return True
    except Exception as e:
        logger.warning(f"Routing-decision update failed (non-critical): {e}")
        try:
            await db.rollback()
        except Exception as rb:
            logger.warning(f"Rollback after failed routing update also failed: {rb}")
        return False


async def purge_old_decisions(db: AsyncSession, days: int = ROUTING_RETENTION_DAYS) -> int:
    """Drop rows past the retention window. Called by the housekeeping sweep.
    Returns the number deleted; raises nothing the caller has to guard beyond
    its own best-effort wrapper."""
    cutoff = utc_now() - timedelta(days=days)
    result = await db.execute(
        delete(RoutingDecision).where(RoutingDecision.created_at < cutoff)
    )
    await db.commit()
    return int(result.rowcount or 0)
