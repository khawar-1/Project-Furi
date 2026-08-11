"""Jarvis OS — per-run plan trace (2026-08-03)

One record per planner invocation saying how it ended and, when it failed, WHY.

WHY THIS EXISTS
---------------
The suggestion this was built from says "every failed plan, every rejected
revision, every dead end is already in ActivityLog and nothing reads it back."
Half of that is false, and the false half is the expensive half.

``ActivityLog`` records the failure SYMPTOM: one row per failed tool call, with
the tool's own error text. It carries no ``plan_id``, no ``task_id``, no step
index — the only join key to a plan is ``session_id``.

The failure DIAGNOSIS is not recorded anywhere at all:

  * Every structural rejection in ``_generate_steps``' reject chain —
    ``_repeated_failure``, ``_scope_violation``, ``_recipient_violation``,
    ``_event_id_violation``, ``_browse_origin_violation``,
    ``_upload_path_violation``, ``_fill_violation``,
    ``_browse_downgrade_violation`` — produces a feedback string that is handed
    to the LLM as retry text and then DISCARDED.
  * ``failed_signatures`` and ``replan_count`` live in the LangGraph state dict
    for the duration of one run.
  * Of the twelve places that set ``PlanStatus.FAILED``, only two log anything.
  * ``Task.plan_payload`` is a JSON snapshot that ``models.py`` itself calls
    "a display/audit snapshot, never resumed from" — never queried, never
    parsed by any consumer — and an INLINE plan writes no Task row at all.

So "why did that plan give up?" was archaeology, exactly as routing was before
``routing_decisions``. This is that fix one layer down: the same shape, the same
rules, applied to planning instead of routing.

RELATIONSHIP TO routing_trace
-----------------------------
``app/core/routing_trace.py`` is the direct sibling and this deliberately copies
it: a mutable dataclass with its id assigned up front, a ContextVar, explicitly
named stamps, and a ``flush`` that follows ``app/db/persist.py``. Read that
module first; everything structural here is justified there.

Granularity is ONE ROW PER PLANNER INVOCATION, not per plan. A plan that pauses
for approval and is then resumed is two planning episodes with two different
sets of rejections, and collapsing them would lose the thing worth knowing.
``plan_id`` joins them back together and ``entry`` says which episode was which.

⚠️ THE CONTEXTVAR RULE (inherited, and load-bearing here too): set it ONCE at
the top of the invocation, then only ever MUTATE the object it points at. The
stamps fire from inside LangGraph nodes, and a child task gets a *copy* of the
context — a ``.set()`` there would not propagate back out, while mutating the
shared object does. ``flush`` is handed its trace by CLOSURE rather than reading
the ContextVar, for the same reason ``routing_trace.note_stream_outcome`` is.

Stamping is a NO-OP when no trace is current, so every planner internal stays
callable from tests, scripts and benches with no fixture and no change.

Writing follows ``app/db/persist.py``: log, ROLL BACK, return False — never
raise. This row is observability; it must never be able to cost a plan.
"""
from __future__ import annotations

import json
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Optional

from loguru import logger
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PlanTrace as PlanTraceRow, utc_now

# The goal is recorded so a row is recognizable on its own; the full text lives
# on the Task/plan payload where one exists.
PLAN_GOAL_MAX_CHARS = 300
PLAN_MESSAGE_MAX_CHARS = 600
# One rejection's retry-feedback text. Enough to tell the guards apart.
REJECTION_FEEDBACK_MAX_CHARS = 300
# A pathological run could reject on every attempt of every replan round; the
# first few are the diagnosis, the rest are noise in a JSON column.
MAX_RECORDED_REJECTIONS = 12
# Swept by the housekeeping pass. Matches routing_decisions.
PLAN_RETENTION_DAYS = 30

# Which entry point ran. A plan's story is start → (resume | answer)*.
ENTRY_START = "start"
ENTRY_RESUME = "resume"
ENTRY_ANSWER = "answer"

# ---------------------------------------------------------------- fail classes
# A CLOSED SET, one constant per `plan.status = PlanStatus.FAILED` site in
# planner.py. The point of naming them is that they need different fixes and
# were indistinguishable from outside: a plan that died because the draft LLM
# returned junk and a plan that died because a real step failed and no replan
# routed around it look identical in every existing record.
#
# ⚠️ A new FAILED site MUST add a constant and stamp it. `test_plan_trace.py`
# counts the FAILED assignments in planner.py against the `note_failed(` calls
# and fails loudly when they diverge — the coverage-test discipline that found
# `read_file.path` on its first run. FAIL_UNCLASSIFIED exists so that a site
# which somehow slips through is VISIBLE in the data rather than silent.
FAIL_EMPTY_GOAL = "empty_goal"                  # the goal was blank
FAIL_CHALLENGE_GIVEUP = "challenge_giveup"      # CAPTCHA/challenge budget spent
FAIL_DRAFT_UNUSABLE = "draft_unusable"          # draft LLM output invalid/rejected
FAIL_UNACHIEVABLE = "unachievable"              # draft declared the goal impossible
FAIL_UNCONFIRMED_MUTATION = "unconfirmed_mutation"  # fired submit, unconfirmed — plan ENDS
FAIL_NOTHING_EXECUTED = "nothing_executed"      # no step ever ran
FAIL_UNROUTED_STEP = "unrouted_step"            # a failed step nothing routed around
FAIL_REPLAN_CAP = "replan_cap"                  # MAX_REPLANS spent
FAIL_QUESTION_CAP = "question_cap"              # MAX_QUESTIONS spent
FAIL_REVISION_UNUSABLE = "revision_unusable"    # replan LLM output unusable
FAIL_REVISION_IMPOSSIBLE = "revision_impossible"  # replanner declared the rest impossible
FAIL_UNCLASSIFIED = "unclassified"              # a FAILED site with no constant — a bug

FAIL_CLASSES = frozenset({
    FAIL_EMPTY_GOAL,
    FAIL_CHALLENGE_GIVEUP,
    FAIL_DRAFT_UNUSABLE,
    FAIL_UNACHIEVABLE,
    FAIL_UNCONFIRMED_MUTATION,
    FAIL_NOTHING_EXECUTED,
    FAIL_UNROUTED_STEP,
    FAIL_REPLAN_CAP,
    FAIL_QUESTION_CAP,
    FAIL_REVISION_UNUSABLE,
    FAIL_REVISION_IMPOSSIBLE,
    FAIL_UNCLASSIFIED,
})

# ----------------------------------------------------------------- guard names
# The reject-chain members, in the order `_generate_steps` evaluates them. Used
# as the `guard` key on a recorded rejection.
GUARD_REPEATED_FAILURE = "repeated_failure"
GUARD_SCOPE = "scope"
GUARD_RECIPIENT = "recipient"
GUARD_EVENT_ID = "event_id"
GUARD_ENTITY_ID = "entity_id"
GUARD_WINDOW_HANDLE = "window_handle"
GUARD_BROWSE_ORIGIN = "browse_origin"
GUARD_UPLOAD_PATH = "upload_path"
GUARD_FILL = "fill"
GUARD_BROWSE_DOWNGRADE = "browse_downgrade"
GUARD_BROWSE_SUBSTITUTION = "browse_substitution"
GUARD_COMPLETED_DUPLICATE = "completed_duplicate"


@dataclass
class PlanTrace:
    """Mutable per-invocation record. Built by :func:`begin`, stamped as the
    planner runs, written once by :func:`flush`."""

    session_id: Optional[str] = None
    plan_id: Optional[str] = None
    task_id: Optional[str] = None
    goal: str = ""
    goal_chars: int = 0
    agent_key: str = "general"
    entry: str = ENTRY_START

    execution: str = "inline"          # inline | background (derived from task_id)
    status: str = ""                   # the plan's settled status
    fail_class: str = ""               # "" unless status == failed
    message: str = ""

    steps_total: int = 0
    steps_completed: int = 0
    steps_failed: int = 0
    steps_skipped: int = 0

    replan_count: int = 0
    questions_asked: int = 0

    # [{"guard": ..., "feedback": ...}] — the diagnosis that is discarded today.
    rejections: list[dict[str, str]] = field(default_factory=list)
    rejection_count: int = 0           # true total, even past MAX_RECORDED_REJECTIONS

    failed_tool: Optional[str] = None
    failed_signature: Optional[str] = None
    failed_error: Optional[str] = None

    # Assigned up front for symmetry with RouteTrace (and so a future amend can
    # find this exact row without re-reading it).
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    _started: float = field(default_factory=time.perf_counter)

    def duration_ms(self) -> int:
        return int((time.perf_counter() - self._started) * 1000)


CURRENT: ContextVar[Optional[PlanTrace]] = ContextVar("plan_trace", default=None)


def begin(
    *,
    session_id: Optional[str],
    goal: str,
    agent_key: str = "general",
    entry: str = ENTRY_START,
    plan_id: Optional[str] = None,
    task_id: Optional[str] = None,
) -> PlanTrace:
    """Start an invocation's trace and make it current. Call ONCE, at the top of
    the entry point — see the ContextVar rule in the module docstring."""
    text = (goal or "").strip()
    trace = PlanTrace(
        session_id=session_id,
        plan_id=plan_id,
        task_id=task_id,
        goal=text[:PLAN_GOAL_MAX_CHARS],
        goal_chars=len(text),
        agent_key=agent_key or "general",
        entry=entry,
        execution="background" if task_id else "inline",
    )
    CURRENT.set(trace)
    return trace


def current() -> Optional[PlanTrace]:
    return CURRENT.get()


def reset() -> None:
    """Drop the current trace. Hygiene for tests and for the entry points'
    finally blocks; a request task's context dies with it either way."""
    CURRENT.set(None)


# ------------------------------------------------------------------ stamping
# Explicit named stamps rather than a note(**fields) helper, for the reason
# routing_trace.py:161-165 gives: a mistyped keyword in a kwargs version
# silently records nothing, which is the "no-op that reports success" failure
# this codebase has already been bitten by. These are attribute writes — a typo
# is an AttributeError at author time.


def note_rejected(guard: str, feedback: Optional[str]) -> None:
    """A drafted/revised step set was refused by a structural guard. THE reason
    this module exists: today this string is handed to the LLM and dropped."""
    trace = CURRENT.get()
    if trace is None:
        return
    trace.rejection_count += 1
    if len(trace.rejections) < MAX_RECORDED_REJECTIONS:
        trace.rejections.append({
            "guard": (guard or "")[:64],
            "feedback": (feedback or "")[:REJECTION_FEEDBACK_MAX_CHARS],
        })


def note_replan() -> None:
    """A revise round was entered. `replan_count` lives in the LangGraph state
    dict, so it is otherwise unobservable once the run ends."""
    trace = CURRENT.get()
    if trace is not None:
        trace.replan_count += 1


def note_step_failed(tool: str, signature: str, error: Optional[str]) -> None:
    """The step that failed. Last one wins — that is the one a replan was
    working on when the plan ran out of road."""
    trace = CURRENT.get()
    if trace is None:
        return
    trace.failed_tool = (tool or "")[:128] or None
    trace.failed_signature = (signature or "")[:512] or None
    trace.failed_error = ((error or "").strip() or None)
    if trace.failed_error:
        trace.failed_error = trace.failed_error[:PLAN_MESSAGE_MAX_CHARS]


def note_failed(fail_class: str) -> None:
    """Record WHICH of the FAILED sites fired. The column this table exists for.
    An unknown value is coerced to FAIL_UNCLASSIFIED and logged rather than
    stored as a new state — the closed-set rule."""
    trace = CURRENT.get()
    if trace is None:
        return
    if fail_class not in FAIL_CLASSES:
        logger.warning(
            f"plan_trace.note_failed got an unknown fail class {fail_class!r} — "
            "recording as unclassified; add a constant in app/core/plan_trace.py"
        )
        fail_class = FAIL_UNCLASSIFIED
    trace.fail_class = fail_class


def note_plan(plan: Any, trace: Optional[PlanTrace] = None) -> None:
    """Read the settled facts off the plan. Called from the entry point's
    `finally`, so it must tolerate a None plan (the invocation raised) and any
    attribute being absent.

    `trace` is passed EXPLICITLY by the entry point, which holds it by closure —
    the same reason `flush` takes it rather than reading the ContextVar, and the
    same reason `routing_trace.note_stream_outcome` does. Falling back to
    CURRENT keeps it usable as an ordinary stamp."""
    trace = trace if trace is not None else CURRENT.get()
    if trace is None or plan is None:
        return
    _settle(trace, plan)


def _settle(trace: PlanTrace, plan: Any) -> None:
    """Copy the plan's terminal shape onto the trace. Defensive throughout —
    this runs in a `finally`, where the plan may be half-built.

    Idempotent by construction: the step counters are RESET before counting, so
    a second call cannot double them. (Nothing calls it twice today; a counter
    that silently doubles when someone later does is the kind of quiet wrongness
    this file exists to prevent, not create.)"""
    trace.steps_completed = 0
    trace.steps_failed = 0
    trace.steps_skipped = 0
    try:
        trace.plan_id = getattr(plan, "id", None) or trace.plan_id
        task_id = getattr(plan, "task_id", None)
        if task_id:
            trace.task_id = task_id
            trace.execution = "background"
        status = getattr(plan, "status", None)
        trace.status = getattr(status, "value", None) or str(status or "")
        trace.message = (getattr(plan, "message", None) or "")[:PLAN_MESSAGE_MAX_CHARS]
        trace.questions_asked = int(getattr(plan, "questions_asked", 0) or 0)
        if not trace.goal:
            goal = (getattr(plan, "goal", "") or "").strip()
            trace.goal = goal[:PLAN_GOAL_MAX_CHARS]
            trace.goal_chars = len(goal)

        steps = list(getattr(plan, "steps", []) or [])
        trace.steps_total = len(steps)
        for step in steps:
            value = getattr(getattr(step, "status", None), "value", "")
            if value == "completed":
                trace.steps_completed += 1
            elif value == "failed":
                trace.steps_failed += 1
            elif value == "skipped":
                trace.steps_skipped += 1
    except Exception as e:  # pragma: no cover — a trace must never cost a plan
        logger.debug(f"plan_trace settle skipped a field: {e}")


# ------------------------------------------------------------------- writing


async def flush(db: AsyncSession, trace: Optional[PlanTrace]) -> bool:
    """Write the row. On ANY failure log, ROLL BACK, return False — the
    persist.py rule. Never raises."""
    if trace is None:
        return False
    try:
        db.add(PlanTraceRow(
            id=trace.id,
            session_id=trace.session_id,
            plan_id=trace.plan_id,
            task_id=trace.task_id,
            goal=trace.goal,
            goal_chars=trace.goal_chars,
            agent_key=trace.agent_key,
            entry=trace.entry,
            execution=trace.execution,
            status=trace.status,
            fail_class=trace.fail_class,
            message=trace.message,
            steps_total=trace.steps_total,
            steps_completed=trace.steps_completed,
            steps_failed=trace.steps_failed,
            steps_skipped=trace.steps_skipped,
            replan_count=trace.replan_count,
            questions_asked=trace.questions_asked,
            rejections=json.dumps(trace.rejections, default=str),
            rejection_count=trace.rejection_count,
            failed_tool=trace.failed_tool,
            failed_signature=trace.failed_signature,
            failed_error=trace.failed_error,
            duration_ms=trace.duration_ms(),
        ))
        await db.commit()
        return True
    except Exception as e:
        logger.warning(f"Plan-trace write failed (non-critical): {e}")
        try:
            await db.rollback()  # un-poison the session — see the module docstring
        except Exception as rb:
            logger.warning(f"Rollback after failed plan-trace write also failed: {rb}")
        return False


async def purge_old_plan_traces(db: AsyncSession, days: int = PLAN_RETENTION_DAYS) -> int:
    """Drop rows past the retention window. Called by the housekeeping sweep."""
    cutoff = utc_now() - timedelta(days=days)
    result = await db.execute(
        delete(PlanTraceRow).where(PlanTraceRow.created_at < cutoff)
    )
    await db.commit()
    return int(result.rowcount or 0)
