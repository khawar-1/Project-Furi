"""
Jarvis OS — Failure intelligence (2026-08-03)

The read half of the round that gave plans an audit trail. A read-only signal
derived ON DEMAND from `plan_traces` — no new table, no background job, and
(crucially) nothing that can act. Directly the `file_intelligence.frequent_folders`
precedent: frozen dataclass → one flat SELECT → Python aggregation → sort →
`format_*()` that returns "" when there is nothing to say.

WHY plan_traces AND NOT ActivityLog
-----------------------------------
`ActivityLog` records one row per failed TOOL CALL, with no plan_id and no
failure classification. A plan that burned two replan rounds and gave up leaves
three near-identical rows there and no way to tell they were one event, or which
of them was the one the plan actually died on. `plan_traces` records the
INVOCATION: which of the eleven failure sites fired, how many rounds it cost,
and which step was in hand when it ran out of road. That is the difference
between "read_file errored" and "this goal is unachievable the way it is being
planned". Same reasoning `core/routines.py:134` gives for reading Tasks rather
than ActivityLog: a recurring *goal*, not per-tool churn.

It reads FAILED PLANS AND RECOVERED DEAD ENDS ALIKE — see the ⚠️ note on
STEP_RECOVERED below, which the first live measurement of this module forced.

⚠️ HONEST LIMIT — READ THIS BEFORE STRENGTHENING ANYTHING HERE
---------------------------------------------------------------
What this module produces is a PROMPT BLOCK. This codebase has measured
prompt-only rules at ZERO three separate times (rule 16 twice, the fan-out
clause once) and the recorded lesson is blunt: "a rule with nothing to check it
is a suggestion". Everything real in the planner's safety story — the approval
gate, `_recipient_violation`, `_event_id_violation`, `_scope_violation` — is a
comparator computed independently of the model's output. This is not one of
those. It cannot be: "avoid the approach that failed last time" is a judgement
about a plan that does not exist yet.

It is worth shipping anyway, for one reason: the round that built `plan_traces`
makes it MEASURABLE for the first time. `scripts/plan_bench.py` can now run a
goal twice and score whether the second attempt avoided the recorded dead end.

MEASURED on first release, `plan_bench learns-from-failure`, 4 consecutive runs,
both numbers 4/4: run 1 of "read the file at <a DIRECTORY>" costs 3 steps, 1
failed step and 1 replan round; run 2 of the identical goal costs 1 step, 0
failed steps and 0 replans. Runtime-verified on the real lifespan too. So it is
not a zero today — but four runs of a nondeterministic model is four runs.

OBSERVED COST, recorded because nothing detects it: in 1 of those 4 runs the
retry OVER-CORRECTED — it avoided the dead end by dropping `read_file` entirely,
listed the folder and never read the file, i.e. it answered a narrower question
than was asked. The plan still COMPLETED, so no gate and no check fires on it.

So the instruction, deliberately recorded here rather than left to be
rediscovered: **if it regresses to zero, do not rewrite the wording — that road
is falsified three times over. Either find a comparator or delete it.**

WHAT IT CANNOT DO
-----------------
It only reads OUR OWN recorded outcomes, so nothing a web page, an email or a
memory says can plant an entry — the same containment argument
`file_intelligence` makes. And the block is DATA: a plan it influences still
faces the approval gate, the path guards and every grounding lock unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import plan_trace
from app.db.models import PlanTrace

# How many distinct failure modes to surface. Small on purpose: the block rides
# in every planner prompt, and a long list of things that once went wrong reads
# as noise rather than as a warning.
DEFAULT_LIMIT = 4
# Rows older than this are not evidence about the world as it is now — a file
# that was missing in June may exist today.
LOOKBACK_DAYS = 30
# A failure mode has to RECUR before it is worth telling the planner about. One
# transient error is not a pattern, and reporting it as one would make the block
# fire on almost every plan.
MIN_OCCURRENCES = 2
ERROR_MAX_CHARS = 200

# ⚠️ A DEAD END IS NOT THE SAME THING AS A FAILED PLAN, and the first live
# measurement of this module is what proved it.
#
# `plan_bench`'s `learns-from-failure` case drafted `read_file` on a DIRECTORY,
# the step failed, the replan loop routed around it with `list_directory`, and
# the plan COMPLETED. Filtering on `status == "failed"` therefore saw nothing —
# and the retry made the identical mistake and paid another ~24s replan round.
#
# The lesson worth not repeating ("read_file on that path is a directory") lived
# on a SUCCESSFUL plan the whole time. A recovered dead end is in fact the more
# valuable signal: it is cheap, certain, and common, where an outright plan
# failure is rare. So the query reads failed STEPS as well as failed plans.
#
# This class is derived at READ time and is deliberately NOT in
# plan_trace.FAIL_CLASSES: that set is the closed enum of `PlanStatus.FAILED`
# SITES, and adding a member no site ever stamps would break the coverage test's
# meaning.
STEP_RECOVERED = "step_recovered"

# Plain-language readings. The planner is told what went wrong in words, not in
# our internal enum.
_FAIL_DESCRIPTIONS: dict[str, str] = {
    STEP_RECOVERED: "a step failed and had to be replanned around",
    plan_trace.FAIL_EMPTY_GOAL: "the goal was empty",
    plan_trace.FAIL_CHALLENGE_GIVEUP: "the site kept re-issuing a human check",
    plan_trace.FAIL_DRAFT_UNUSABLE: "the plan could not be drafted",
    plan_trace.FAIL_UNACHIEVABLE: "the goal was judged impossible as stated",
    plan_trace.FAIL_UNCONFIRMED_MUTATION: "a submit fired but could not be confirmed",
    plan_trace.FAIL_NOTHING_EXECUTED: "no step ever ran",
    plan_trace.FAIL_UNROUTED_STEP: "a step failed and nothing after it succeeded",
    plan_trace.FAIL_REPLAN_CAP: "it ran out of replan attempts",
    plan_trace.FAIL_QUESTION_CAP: "it ran out of clarifying questions",
    plan_trace.FAIL_REVISION_UNUSABLE: "the replan could not be drafted",
    plan_trace.FAIL_REVISION_IMPOSSIBLE: "the replanner declared the rest impossible",
    plan_trace.FAIL_UNCLASSIFIED: "it failed",
}


@dataclass(frozen=True)
class FailurePattern:
    """A failure mode that has recurred. `tool` is "" when the plan died before
    any step ran (a drafting failure), which is itself worth knowing."""

    tool: str
    fail_class: str
    count: int
    last_seen: Optional[datetime]
    example_error: str
    example_goal: str
    same_goal: bool  # this exact goal has failed this way before


def _describe(fail_class: str) -> str:
    return _FAIL_DESCRIPTIONS.get(fail_class, "it failed")


def _normalized(goal: str) -> str:
    """The ONE goal normalizer, borrowed rather than re-implemented — a second
    copy would drift from the one `routines` and `pattern_mining` share."""
    try:
        from app.core.routines import normalize_goal

        return normalize_goal(goal or "")
    except Exception:  # pragma: no cover — defensive
        return (goal or "").strip().lower()


async def recent_failures(
    db: AsyncSession,
    *,
    goal: str = "",
    limit: int = DEFAULT_LIMIT,
    lookback_days: int = LOOKBACK_DAYS,
    min_occurrences: int = MIN_OCCURRENCES,
) -> list[FailurePattern]:
    """Failure modes that have recurred lately, most significant first.

    Ranking puts failures of THIS SAME goal ahead of everything else — "the last
    time you asked for exactly this, here is how it went wrong" is worth more
    than a general tendency — then falls back to frequency, with recency
    breaking ties. Goal matching reuses `routines.normalize_goal`, so it is the
    same notion of "the same goal" the routine-offer and cadence features use;
    there is no fuzzy matching here and deliberately so.

    Best-effort — a query or parse failure yields []."""
    from datetime import timedelta

    from sqlalchemy import or_

    from app.db.models import utc_now

    try:
        cutoff = utc_now() - timedelta(days=lookback_days)
        rows = (
            await db.execute(
                select(PlanTrace)
                # Failed PLANS and recovered DEAD ENDS both — see STEP_RECOVERED.
                .where(or_(PlanTrace.status == "failed", PlanTrace.steps_failed > 0))
                .where(PlanTrace.created_at >= cutoff)
                .order_by(PlanTrace.created_at.asc())
            )
        ).scalars().all()
    except Exception as e:  # pragma: no cover — defensive, never break planning
        logger.warning(f"recent_failures query failed (non-critical): {e}")
        return []

    wanted = _normalized(goal) if goal else ""
    # key -> mutable [count, last_seen, error, goal, same_goal]
    agg: dict[tuple[str, str], list] = {}
    for row in rows:
        tool = (row.failed_tool or "").strip()
        fail_class = (row.fail_class or "").strip()
        if not fail_class:
            # No class means the plan did NOT fail — it recovered from a failed
            # step. That is a dead end worth not repeating, but only if we know
            # which step it was; a classless row with no tool either is a caller
            # bug (see the coverage test) and teaches nothing actionable.
            if not tool:
                continue
            fail_class = STEP_RECOVERED
        key = (tool, fail_class)
        same = bool(wanted) and _normalized(row.goal or "") == wanted
        entry = agg.get(key)
        if entry is None:
            agg[key] = [
                1, row.created_at,
                (row.failed_error or row.message or "").strip()[:ERROR_MAX_CHARS],
                (row.goal or "").strip(), same,
            ]
            continue
        entry[0] += 1
        if row.created_at is not None and (entry[1] is None or row.created_at >= entry[1]):
            entry[1] = row.created_at
            # Keep the MOST RECENT example — an older error text may describe a
            # world that has since changed.
            error = (row.failed_error or row.message or "").strip()
            if error:
                entry[2] = error[:ERROR_MAX_CHARS]
            entry[3] = (row.goal or "").strip()
        entry[4] = entry[4] or same

    patterns = [
        FailurePattern(
            tool=tool, fail_class=fail_class, count=count, last_seen=seen,
            example_error=error, example_goal=example, same_goal=same,
        )
        for (tool, fail_class), (count, seen, error, example, same) in agg.items()
        # A same-goal failure is reported even once: it is not a "tendency", it
        # is the specific thing the user just asked for, going wrong.
        if count >= min_occurrences or same
    ]
    patterns.sort(
        key=lambda p: (p.same_goal, p.count, p.last_seen or datetime.min), reverse=True
    )
    return patterns[: max(0, limit)]


def format_recent_failures(patterns: list[FailurePattern]) -> str:
    """Render the ranked failures as plain text for the planner DATA block.
    "" when there is nothing to report (the block is then omitted entirely)."""
    if not patterns:
        return ""
    lines = []
    for p in patterns:
        who = f"`{p.tool}`" if p.tool else "planning"
        when = "this same request" if p.same_goal else f"{p.count}x"
        line = f"- {who} ({when}): {_describe(p.fail_class)}"
        if p.example_error:
            line += f' — "{p.example_error}"'
        lines.append(line)
    return (
        "What has gone wrong recently, from Jarvis's own record of its past "
        "plans (see rule 23):\n" + "\n".join(lines)
    )
