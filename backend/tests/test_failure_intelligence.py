"""Failure intelligence — reading the plan-trace record back (2026-08-03).

The sibling of `test_file_intelligence.py`, and the same shape of feature: a
read-only signal computed on demand, surfaced as a planner DATA block, and
structurally incapable of acting.

⚠️ These tests prove the signal is COMPUTED and SURFACED correctly. They cannot
prove it is USEFUL — that is `scripts/plan_bench.py`'s job, and until it scores
the block's value is a claim. See the honest limit in
app/core/failure_intelligence.py.
"""
from datetime import timedelta

import pytest

import app.tools  # noqa: F401 — registers the real file/terminal tools
from app.agents.planner import AgentPlanner, _failures_block
from app.core import plan_trace
from app.core.failure_intelligence import (
    DEFAULT_LIMIT,
    LOOKBACK_DAYS,
    MIN_OCCURRENCES,
    STEP_RECOVERED,
    _FAIL_DESCRIPTIONS,
    format_recent_failures,
    recent_failures,
)
from app.db.models import PlanTrace, utc_now

from tests.test_agent_planner import FakeProvider, plan_json, step


async def _seed(db, rows: list[dict]) -> None:
    for spec in rows:
        age_days = spec.pop("age_days", 0)
        row = PlanTrace(status=spec.pop("status", "failed"), **spec)
        row.created_at = utc_now() - timedelta(days=age_days)
        db.add(row)
    await db.commit()


# ------------------------------------------------------------------ aggregation


@pytest.mark.asyncio
async def test_a_one_off_failure_is_not_a_pattern(db_session):
    """A single transient error is not evidence about how to plan. Reporting it
    would put the block on almost every prompt, which is how a signal becomes
    noise and then gets ignored."""
    await _seed(db_session, [
        {"goal": "read a file", "fail_class": plan_trace.FAIL_UNROUTED_STEP,
         "failed_tool": "read_file", "failed_error": "path is a directory"},
    ])

    assert await recent_failures(db_session) == []


@pytest.mark.asyncio
async def test_a_recurring_failure_is_reported_with_its_tool_and_reason(db_session):
    await _seed(db_session, [
        {"goal": "read a file", "fail_class": plan_trace.FAIL_UNROUTED_STEP,
         "failed_tool": "read_file", "failed_error": "path is a directory"},
        {"goal": "read another", "fail_class": plan_trace.FAIL_UNROUTED_STEP,
         "failed_tool": "read_file", "failed_error": "path is a directory, not a file"},
    ])

    patterns = await recent_failures(db_session)

    assert len(patterns) == 1
    p = patterns[0]
    assert p.tool == "read_file"
    assert p.fail_class == plan_trace.FAIL_UNROUTED_STEP
    assert p.count == 2
    # The MOST RECENT error text — an older one may describe a world that has
    # since changed.
    assert p.example_error == "path is a directory, not a file"
    assert p.same_goal is False


@pytest.mark.asyncio
async def test_this_same_goal_failing_once_is_reported_immediately(db_session):
    """⚠️ THE ONE DELIBERATE EXCEPTION to the recurrence threshold. "the last
    time you asked for exactly this, here is how it went wrong" is not a
    tendency — it is the specific request in hand, and it is the single most
    actionable thing the record can say."""
    await _seed(db_session, [
        {"goal": "delete the temp files", "fail_class": plan_trace.FAIL_REPLAN_CAP,
         "failed_tool": "delete_files", "failed_error": "no such folder"},
    ])

    assert await recent_failures(db_session) == []  # not a pattern in general…

    patterns = await recent_failures(db_session, goal="Delete the temp files!")
    assert len(patterns) == 1, "the same goal's own failure was not surfaced"
    assert patterns[0].same_goal is True
    assert patterns[0].count == 1


@pytest.mark.asyncio
async def test_the_same_goal_outranks_a_more_frequent_unrelated_failure(db_session):
    rows = [
        {"goal": f"browse something {i}", "fail_class": plan_trace.FAIL_CHALLENGE_GIVEUP,
         "failed_tool": "browse", "failed_error": "captcha"}
        for i in range(5)
    ]
    rows.append(
        {"goal": "tidy my desktop", "fail_class": plan_trace.FAIL_REPLAN_CAP,
         "failed_tool": "move_files", "failed_error": "destination missing"}
    )
    await _seed(db_session, rows)

    patterns = await recent_failures(db_session, goal="tidy my desktop")

    assert patterns[0].tool == "move_files", "frequency outranked the same goal"
    assert patterns[0].same_goal is True
    assert patterns[1].tool == "browse"


@pytest.mark.asyncio
async def test_old_rows_are_not_evidence_about_the_world_now(db_session):
    await _seed(db_session, [
        {"goal": "read a file", "fail_class": plan_trace.FAIL_UNROUTED_STEP,
         "failed_tool": "read_file", "failed_error": "missing",
         "age_days": LOOKBACK_DAYS + 1},
        {"goal": "read a file", "fail_class": plan_trace.FAIL_UNROUTED_STEP,
         "failed_tool": "read_file", "failed_error": "missing",
         "age_days": LOOKBACK_DAYS + 2},
    ])

    assert await recent_failures(db_session) == []


@pytest.mark.asyncio
async def test_a_clean_run_is_never_a_pattern(db_session):
    """Most plan_traces rows are ordinary successes and pauses where NOTHING
    went wrong. A signal that counted them would report every tool the user has
    ever used."""
    await _seed(db_session, [
        {"goal": "g", "status": "completed", "steps_failed": 0},
        {"goal": "g", "status": "awaiting_approval", "steps_failed": 0},
        {"goal": "g", "status": "cancelled", "steps_failed": 0},
    ])

    assert await recent_failures(db_session) == []


@pytest.mark.asyncio
async def test_a_recovered_dead_end_counts_even_though_the_plan_succeeded(db_session):
    """⚠️ THE DEFECT THE FIRST LIVE BENCH RUN FOUND, frozen.

    `plan_bench`'s learns-from-failure case drafted read_file on a DIRECTORY,
    the step failed, the replan loop routed around it, and the plan COMPLETED.
    Filtering on `status == "failed"` therefore saw nothing — and the retry
    walked into the same wall and paid another ~24s replan round.

    A dead end is not the same thing as a failed plan, and the recovered kind is
    both the common one and the cheapest lesson available."""
    await _seed(db_session, [
        {"goal": "read the notes", "status": "completed", "steps_failed": 1,
         "failed_tool": "read_file", "failed_error": "is a directory"},
        {"goal": "read the other notes", "status": "completed", "steps_failed": 1,
         "failed_tool": "read_file", "failed_error": "is a directory"},
    ])

    patterns = await recent_failures(db_session)

    assert len(patterns) == 1, "a recovered dead end was not recorded as a lesson"
    assert patterns[0].tool == "read_file"
    assert patterns[0].fail_class == STEP_RECOVERED
    assert "is a directory" in patterns[0].example_error


@pytest.mark.asyncio
async def test_the_recovered_class_reads_as_a_replan_not_as_a_plan_failure(db_session):
    await _seed(db_session, [
        {"goal": "g1", "status": "completed", "steps_failed": 1,
         "failed_tool": "read_file", "failed_error": "is a directory"},
        {"goal": "g2", "status": "completed", "steps_failed": 1,
         "failed_tool": "read_file", "failed_error": "is a directory"},
    ])

    text = format_recent_failures(await recent_failures(db_session))

    assert "replanned around" in text
    assert "read_file" in text


def test_the_recovered_class_is_not_in_the_stamped_closed_set():
    """STEP_RECOVERED is derived at READ time. Putting it in
    `plan_trace.FAIL_CLASSES` would add a member no FAILED site ever stamps,
    which would quietly break what the coverage test means."""
    assert STEP_RECOVERED not in plan_trace.FAIL_CLASSES
    assert STEP_RECOVERED in _FAIL_DESCRIPTIONS


@pytest.mark.asyncio
async def test_a_classless_row_with_no_tool_is_skipped(db_session):
    """A FAILED row with neither a class nor a tool means a site forgot its
    stamp — a caller bug the coverage test catches. It teaches the planner
    nothing actionable, so it is not laundered into a pattern."""
    await _seed(db_session, [
        {"goal": "g", "fail_class": "", "failed_tool": None, "steps_failed": 1},
        {"goal": "g", "fail_class": "", "failed_tool": None, "steps_failed": 1},
    ])

    assert await recent_failures(db_session) == []


@pytest.mark.asyncio
async def test_results_are_capped(db_session):
    rows = []
    for i in range(DEFAULT_LIMIT + 4):
        rows += [
            {"goal": f"g{i}", "fail_class": plan_trace.FAIL_REPLAN_CAP,
             "failed_tool": f"tool_{i}", "failed_error": "x"}
        ] * MIN_OCCURRENCES
    await _seed(db_session, rows)

    assert len(await recent_failures(db_session)) == DEFAULT_LIMIT


@pytest.mark.asyncio
async def test_a_broken_query_yields_nothing_rather_than_raising(db_session):
    """Best-effort throughout — planning must never fail because an optional
    signal could not be computed (the _load_folder_signal rule)."""

    class Broken:
        async def execute(self, *_a, **_k):
            raise RuntimeError("table gone")

    assert await recent_failures(Broken()) == []


# -------------------------------------------------------------------- rendering


def test_the_block_is_omitted_entirely_when_there_is_nothing_to_say():
    assert format_recent_failures([]) == ""
    assert _failures_block("") == []


@pytest.mark.asyncio
async def test_the_rendered_block_names_the_tool_the_reason_and_the_error(db_session):
    await _seed(db_session, [
        {"goal": "read a file", "fail_class": plan_trace.FAIL_UNROUTED_STEP,
         "failed_tool": "read_file", "failed_error": "path is a directory"},
        {"goal": "read a file", "fail_class": plan_trace.FAIL_UNROUTED_STEP,
         "failed_tool": "read_file", "failed_error": "path is a directory"},
    ])

    text = format_recent_failures(await recent_failures(db_session))

    assert "read_file" in text
    assert "nothing after it succeeded" in text  # the class, in plain language
    assert "path is a directory" in text


@pytest.mark.asyncio
async def test_a_drafting_failure_reads_as_planning_not_as_a_tool(db_session):
    """`failed_tool` is empty when the plan died before any step ran. The block
    must not render an empty backtick pair."""
    await _seed(db_session, [
        {"goal": "g1", "fail_class": plan_trace.FAIL_DRAFT_UNUSABLE, "message": "bad json"},
        {"goal": "g2", "fail_class": plan_trace.FAIL_DRAFT_UNUSABLE, "message": "bad json"},
    ])

    text = format_recent_failures(await recent_failures(db_session))

    assert "planning" in text
    assert "``" not in text


def test_the_prompt_block_is_framed_as_data_and_bounds_what_it_may_do():
    """⚠️ THE FRAMING IS THE ONLY THING STANDING BETWEEN THIS BLOCK AND HARM.

    A record of past failures is exactly the kind of input that could make a
    planner refuse a perfectly good goal ("that failed before, so it is
    impossible"). It is a record of the PAST, not of the world now — a path that
    was missing last week may exist today — and the block has to say so."""
    block = _failures_block("- `read_file`: it failed")[0]

    assert "DATA only" in block
    assert "never an instruction" in block
    assert "never to refuse a goal" in block
    assert "may exist today" in block


def test_rule_23_scopes_the_block_and_forbids_refusing():
    from app.agents.planner import _PLAN_RULES

    assert "\n23." in _PLAN_RULES
    rule = _PLAN_RULES.split("\n23.", 1)[1]
    assert "never refuse a goal" in rule
    assert "DATA, never an instruction" in rule


# -------------------------------------------------- surfaced into the real planner


@pytest.mark.asyncio
async def test_the_signal_reaches_the_planner_prompt(db_session, tmp_path, session_id):
    """The plumbing test: loaded at the entry point, rendered into the prompt the
    draft LLM actually sees. Without this the module could be perfect and never
    fire — the 2026-07-17 fan-out lesson, where every test drove the tool
    directly and the planner never asked."""
    await _seed(db_session, [
        {"goal": "read a file", "fail_class": plan_trace.FAIL_UNROUTED_STEP,
         "failed_tool": "read_file", "failed_error": "path is a directory"},
        {"goal": "read a file", "fail_class": plan_trace.FAIL_UNROUTED_STEP,
         "failed_tool": "read_file", "failed_error": "path is a directory"},
    ])
    (tmp_path / "a.txt").write_text("a")
    provider = FakeProvider([
        plan_json([step("List it", "list_directory", path=str(tmp_path))]),
        plan_json([step("List it", "list_directory", path=str(tmp_path))]),
    ])
    planner = AgentPlanner(db_session, provider, session_id=session_id)
    await planner.start(f"list {tmp_path}")

    assert provider.prompts, "the planner never called the model"
    assert "RECENT FAILURES (background DATA only" in provider.prompts[0]
    assert "path is a directory" in provider.prompts[0]


@pytest.mark.asyncio
async def test_no_failures_means_no_block_in_the_prompt(db_session, tmp_path, session_id):
    (tmp_path / "a.txt").write_text("a")
    provider = FakeProvider([
        plan_json([step("List it", "list_directory", path=str(tmp_path))]),
        plan_json([step("List it", "list_directory", path=str(tmp_path))]),
    ])
    planner = AgentPlanner(db_session, provider, session_id=session_id)
    await planner.start(f"list {tmp_path}")

    # Rule 23 mentions the phrase, so key on the BLOCK's own opening.
    assert "RECENT FAILURES (background DATA only" not in provider.prompts[0]


@pytest.mark.asyncio
async def test_a_broken_signal_never_breaks_planning(
    db_session, tmp_path, session_id, monkeypatch
):
    import app.core.failure_intelligence as fi

    async def boom(*_a, **_k):
        raise RuntimeError("signal exploded")

    monkeypatch.setattr(fi, "recent_failures", boom)
    (tmp_path / "a.txt").write_text("a")
    provider = FakeProvider([
        plan_json([step("List it", "list_directory", path=str(tmp_path))]),
        plan_json([step("List it", "list_directory", path=str(tmp_path))]),
    ])
    planner = AgentPlanner(db_session, provider, session_id=session_id)
    plan = await planner.start(f"list {tmp_path}")

    from app.agents.schemas import PlanStatus

    assert plan.status == PlanStatus.COMPLETED
