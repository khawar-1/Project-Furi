"""Plan traces — the audit trail for WHY a plan gave up (2026-08-03).

The premise this round corrected: "every failed plan is already in ActivityLog"
is half false. ActivityLog records the failure SYMPTOM (one row per failed tool
call, with no plan_id); the DIAGNOSIS — which structural guard refused a draft,
how many replan rounds burned, which of the eleven FAILED sites finally fired —
was handed to the LLM as retry feedback and discarded.

These tests drive the REAL planner graph (the test_agent_planner harness) rather
than calling the stamps directly, because the load-bearing question is whether a
stamp fired from inside a LangGraph node reaches the trace the entry point holds
— i.e. whether the ContextVar rule actually holds here.
"""
import json

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.tools  # noqa: F401 — registers the real file/terminal tools
from app.agents.planner import AgentPlanner, MAX_REPLANS
from app.agents.schemas import PlanStatus
from app.core import plan_trace
from app.db.database import Base
from app.db.models import PlanTrace

from tests.test_agent_planner import FakeProvider, plan_json, step


async def _traces(db_session) -> list[PlanTrace]:
    result = await db_session.execute(select(PlanTrace).order_by(PlanTrace.created_at))
    return list(result.scalars().all())


# --------------------------------------------------------------- the unit rules


def test_goal_is_clipped_but_its_true_size_survives():
    trace = plan_trace.begin(session_id="s", goal="  " + "x" * 900 + "  ")
    assert len(trace.goal) == plan_trace.PLAN_GOAL_MAX_CHARS
    assert trace.goal_chars == 900
    plan_trace.reset()


def test_execution_is_derived_from_task_id_not_guessed():
    inline = plan_trace.begin(session_id="s", goal="g")
    assert inline.execution == "inline"
    plan_trace.reset()
    background = plan_trace.begin(session_id="s", goal="g", task_id="t1")
    assert background.execution == "background"
    plan_trace.reset()


def test_an_unknown_fail_class_is_recorded_as_unclassified_never_as_a_new_state():
    """The closed-set rule. A twelfth FAILED site that forgets its constant must
    be VISIBLE in the data, not silently inventing a category."""
    trace = plan_trace.begin(session_id="s", goal="g")
    plan_trace.note_failed("something_nobody_declared")
    assert trace.fail_class == plan_trace.FAIL_UNCLASSIFIED
    plan_trace.reset()


def test_rejections_are_bounded_but_the_true_count_is_not():
    trace = plan_trace.begin(session_id="s", goal="g")
    for _ in range(plan_trace.MAX_RECORDED_REJECTIONS + 7):
        plan_trace.note_rejected(plan_trace.GUARD_SCOPE, "x" * 900)
    assert len(trace.rejections) == plan_trace.MAX_RECORDED_REJECTIONS
    assert trace.rejection_count == plan_trace.MAX_RECORDED_REJECTIONS + 7
    assert len(trace.rejections[0]["feedback"]) == plan_trace.REJECTION_FEEDBACK_MAX_CHARS
    plan_trace.reset()


def test_stamping_with_no_trace_current_is_a_no_op():
    """Every planner internal must stay callable from tests, scripts and benches
    with no fixture — the routing_trace rule."""
    plan_trace.reset()
    plan_trace.note_rejected(plan_trace.GUARD_SCOPE, "x")
    plan_trace.note_replan()
    plan_trace.note_step_failed("delete_file", "sig", "boom")
    plan_trace.note_failed(plan_trace.FAIL_REPLAN_CAP)
    plan_trace.note_plan(None)
    assert plan_trace.current() is None


def test_every_fail_class_constant_is_in_the_closed_set():
    declared = {
        value for name, value in vars(plan_trace).items()
        if name.startswith("FAIL_") and isinstance(value, str)
    }
    assert declared == set(plan_trace.FAIL_CLASSES)


# ------------------------------------------------------- the coverage invariant


def test_every_failed_site_in_the_planner_stamps_a_fail_class():
    """⚠️ THE GUARD THAT KEEPS THIS SHUT.

    `fail_class` is only a closed set if every `plan.status = PlanStatus.FAILED`
    site names one. A twelfth site added later would otherwise write a row with
    a blank class — indistinguishable from a plan that did not fail — and
    nothing would ever say so. Same discipline as
    `test_every_path_param_is_covered_or_exempt`, which found a real hole on its
    first run.

    Counting is deliberately crude: the point is to FAIL LOUDLY when the two
    diverge, not to be clever about which line pairs with which."""
    from pathlib import Path

    source = Path(
        AgentPlanner.__module__.replace(".", "/") + ".py"
    )
    text = (Path(__file__).resolve().parents[1] / source).read_text(encoding="utf-8")
    failed_sites = text.count("plan.status = PlanStatus.FAILED")
    stamps = text.count("plan_trace.note_failed(")
    assert failed_sites == stamps, (
        f"{failed_sites} FAILED site(s) in planner.py but {stamps} note_failed() "
        "call(s). A new failure path must name its fail_class — add a constant "
        "in app/core/plan_trace.py and stamp it, or the row records a blank "
        "class that reads as 'did not fail'."
    )


# ------------------------------------------------- through the REAL planner graph


@pytest.mark.asyncio
async def test_a_completed_plan_writes_one_trace_row(db_session, tmp_path, session_id):
    """The baseline: a read-only run-through records its shape."""
    (tmp_path / "a.txt").write_text("a")
    provider = FakeProvider([
        plan_json([step("List it", "list_directory", path=str(tmp_path))]),
        plan_json([step("List it", "list_directory", path=str(tmp_path))]),
    ])
    planner = AgentPlanner(db_session, provider, session_id=session_id)
    plan = await planner.start(f"list every file in {tmp_path}")

    assert plan.status == PlanStatus.COMPLETED
    rows = await _traces(db_session)
    assert len(rows) == 1
    row = rows[0]
    assert row.plan_id == plan.id
    assert row.status == "completed"
    assert row.fail_class == ""          # nothing failed — no class
    assert row.entry == plan_trace.ENTRY_START
    assert row.execution == "inline"
    assert row.steps_completed == 1
    assert row.duration_ms is not None


@pytest.mark.asyncio
async def test_a_plan_that_gives_up_records_its_class_its_rounds_and_the_failing_step(
    db_session, tmp_path, session_id
):
    """THE INCIDENT SHAPE. A plan that burns its replan budget used to leave
    exactly one clue: a `plan.message` string. Now the class, the round count,
    the tool and its error are all queryable.

    `read_file` on a DIRECTORY is used deliberately: it is one of the
    NON-RECOVERABLE failure classes, so the ask-not-fail `_fallback_question`
    does not intercept and the plan reaches a real FAILED site. (A missing FILE
    would pause AWAITING_CHOICE instead — correct behaviour, wrong test.)"""
    read_dir = plan_json([step("Read it", "read_file", path=str(tmp_path))])
    provider = FakeProvider([read_dir] * (2 * (MAX_REPLANS + 3)))
    planner = AgentPlanner(db_session, provider, session_id=session_id)
    plan = await planner.start(f"read the file at {tmp_path}")

    assert plan.status == PlanStatus.FAILED
    rows = await _traces(db_session)
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "failed"
    assert row.fail_class in plan_trace.FAIL_CLASSES
    assert row.fail_class not in ("", plan_trace.FAIL_UNCLASSIFIED)
    # The step that failed — the join ActivityLog cannot make, because its rows
    # carry no plan_id.
    assert row.failed_tool == "read_file"
    assert row.failed_error
    assert row.steps_failed >= 1
    assert row.replan_count >= 1


@pytest.mark.asyncio
async def test_a_structural_rejection_survives_the_run(db_session, tmp_path, session_id):
    """⚠️ THE POINT OF THE WHOLE MODULE, and the strongest ContextVar check.

    `_repeated_failure` fires deep inside `_generate_steps`, which runs inside a
    LangGraph node — a different task from the entry point that began the trace.
    Its retry feedback is handed to the LLM and dropped. If the ContextVar rule
    were wrong (a `.set()` in a child task, say), this list would be empty and
    every other test here would still pass."""
    ghost = str(tmp_path / "ghost.txt")
    read_ghost = plan_json([step("Read it", "read_file", path=ghost)])
    provider = FakeProvider([read_ghost] * (2 * (MAX_REPLANS + 2)))
    planner = AgentPlanner(db_session, provider, session_id=session_id)
    await planner.start(f"read {ghost}")

    row = (await _traces(db_session))[0]
    rejections = json.loads(row.rejections)
    assert row.rejection_count >= 1, "the reject chain fired but recorded nothing"
    assert rejections, "rejections were recorded as a count but not as a diagnosis"
    assert rejections[0]["guard"] == plan_trace.GUARD_REPEATED_FAILURE
    assert "failed" in rejections[0]["feedback"].lower()
    # And the replan rounds, which live only in the LangGraph state dict.
    assert row.replan_count >= 1


@pytest.mark.asyncio
async def test_an_empty_goal_records_its_own_class(db_session, session_id):
    """The one FAILED site that returns before the graph ever runs — proof the
    trace is begun at the entry point, not inside the graph."""
    provider = FakeProvider([])
    planner = AgentPlanner(db_session, provider, session_id=session_id)
    plan = await planner.start("   ")

    assert plan.status == PlanStatus.FAILED
    row = (await _traces(db_session))[0]
    assert row.fail_class == plan_trace.FAIL_EMPTY_GOAL
    assert row.status == "failed"


@pytest.mark.asyncio
async def test_a_raising_invocation_still_writes_its_row(db_session, session_id):
    """The `finally` earns its keep: an exception is the case where the record
    matters most, and a per-return flush would miss it.

    The graph itself is made to raise — a provider failure would not do, since
    `_generate_steps` catches those and turns them into a FAILED plan (which is
    an ordinary exit, not the one being tested here)."""
    provider = FakeProvider([])
    planner = AgentPlanner(db_session, provider, session_id=session_id)

    class ExplodingGraph:
        async def ainvoke(self, *_a, **_k):
            raise RuntimeError("graph exploded")

    planner._graph = ExplodingGraph()  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="graph exploded"):
        await planner.start("do something")

    rows = await _traces(db_session)
    assert len(rows) == 1
    assert rows[0].goal == "do something"


@pytest.mark.asyncio
async def test_each_invocation_of_one_plan_is_its_own_row_joined_by_plan_id(
    db_session, tmp_path, session_id
):
    """Granularity is per INVOCATION, not per plan: a plan that pauses and is
    resumed is two planning episodes with two different sets of rejections, and
    collapsing them would lose the thing worth knowing. `plan_id` joins them,
    `entry` says which was which."""
    target = tmp_path / "new.txt"
    create = plan_json([
        step("Create it", "create_file", path=str(target), content="hi")
    ])
    provider = FakeProvider([create, create, create, create])
    planner = AgentPlanner(db_session, provider, session_id=session_id)
    plan = await planner.start(f"create {target}")
    assert plan.status == PlanStatus.AWAITING_APPROVAL

    resumed = await planner.resume(plan, approved=True)
    assert resumed.status == PlanStatus.COMPLETED

    rows = await _traces(db_session)
    assert len(rows) == 2
    assert [r.entry for r in rows] == [plan_trace.ENTRY_START, plan_trace.ENTRY_RESUME]
    assert {r.plan_id for r in rows} == {plan.id}
    assert rows[0].status == "awaiting_approval"
    assert rows[1].status == "completed"


@pytest.mark.asyncio
async def test_a_write_failure_never_costs_the_plan(
    db_session, tmp_path, session_id, monkeypatch
):
    """Observability must never be able to take a plan down — the persist.py
    rule. A flush that raises is logged and swallowed."""
    (tmp_path / "a.txt").write_text("a")

    async def boom(*_a, **_k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(plan_trace, "flush", boom)
    provider = FakeProvider([
        plan_json([step("List it", "list_directory", path=str(tmp_path))]),
        plan_json([step("List it", "list_directory", path=str(tmp_path))]),
    ])
    planner = AgentPlanner(db_session, provider, session_id=session_id)
    with pytest.raises(RuntimeError):
        # The monkeypatched flush raises from the `finally`; the REAL flush
        # swallows everything, which the next assertion pins.
        await planner.start(f"list {tmp_path}")

    # The real one: a broken DB write returns False rather than raising.
    monkeypatch.undo()

    class Boom:
        def add(self, *_a, **_k):
            raise RuntimeError("nope")

        async def commit(self):  # pragma: no cover — add() raises first
            pass

        async def rollback(self):
            pass

    trace = plan_trace.begin(session_id="s", goal="g")
    assert await plan_trace.flush(Boom(), trace) is False
    plan_trace.reset()


@pytest.mark.asyncio
async def test_a_typed_carry_on_writes_one_row_not_two(db_session, tmp_path, session_id):
    """⚠️ THE NESTED-TRACE BUG, frozen.

    `_answer` continues a paused plan by calling resume. Routing that through
    the PUBLIC `resume` would begin a second trace, and the inner `finally`
    would `reset()` the ContextVar — so every stamp after it in the OUTER
    invocation becomes a silent no-op, and two rows describe one action. It
    calls `_resume` instead, which is the same invocation.

    This is the shape the whole module is vulnerable to, so it is pinned rather
    than trusted: the caller asked for ONE planner invocation and must get ONE
    record of it."""
    from app.agents.interruption import apply_pause

    target = tmp_path / "new.txt"
    create = plan_json([
        step("Create it", "create_file", path=str(target), content="hi")
    ])
    provider = FakeProvider([create, create, create, create])
    planner = AgentPlanner(db_session, provider, session_id=session_id)
    plan = await planner.start(f"create {target}")
    apply_pause(plan)
    assert plan.status == PlanStatus.PAUSED

    before = len(await _traces(db_session))
    await planner.answer(plan, "carry on")
    rows = await _traces(db_session)

    assert len(rows) - before == 1, (
        "a typed 'carry on' wrote more than one trace row — the answer path is "
        "opening a nested trace, which also blinds the outer one's stamps"
    )
    assert rows[-1].entry == plan_trace.ENTRY_ANSWER


@pytest.mark.asyncio
async def test_retention_drops_only_rows_past_the_window(db_session):
    from datetime import timedelta

    from app.db.models import utc_now

    old = PlanTrace(goal="old", status="failed")
    old.created_at = utc_now() - timedelta(days=plan_trace.PLAN_RETENTION_DAYS + 1)
    fresh = PlanTrace(goal="fresh", status="failed")
    db_session.add_all([old, fresh])
    await db_session.commit()

    deleted = await plan_trace.purge_old_plan_traces(db_session)
    assert deleted == 1
    assert [r.goal for r in await _traces(db_session)] == ["fresh"]


# --------------------------------------------------------------- the sweep wiring


@pytest_asyncio.fixture
async def swept_db(tmp_path_factory, monkeypatch):
    """A database the housekeeping sweep can reach. The sweep opens its OWN
    session (it runs on a timer, with no request to borrow one from), so a test
    that only calls the purge functions directly proves the FUNCTIONS work and
    says nothing about whether the timer calls them — the 2026-08-03 lesson,
    where exactly that gap made a falsification come back green."""
    db_dir = tmp_path_factory.mktemp("plan-trace-sweep")
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_dir / 'a.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    import app.db.database as database

    monkeypatch.setattr(database, "AsyncSessionLocal", factory)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_the_periodic_sweep_ages_out_both_new_audit_trails(swept_db):
    """⚠️ THE WIRING TEST. Both purges must be ON the housekeeping tuple, not
    merely importable."""
    from datetime import timedelta

    from app.core import housekeeping
    from app.db.models import ActivityLog, utc_now
    from app.tools.registry import ACTIVITY_RETENTION_DAYS

    stale_trace = PlanTrace(goal="stale", status="failed")
    stale_trace.created_at = utc_now() - timedelta(days=plan_trace.PLAN_RETENTION_DAYS + 1)
    fresh_trace = PlanTrace(goal="fresh", status="failed")

    stale_row = ActivityLog(tool_name="delete_file", action="old", success=True)
    stale_row.created_at = utc_now() - timedelta(days=ACTIVITY_RETENTION_DAYS + 1)
    fresh_row = ActivityLog(tool_name="delete_file", action="recent", success=True)

    swept_db.add_all([stale_trace, fresh_trace, stale_row, fresh_row])
    await swept_db.commit()

    await housekeeping.run_housekeeping_pass()

    traces = await _traces(swept_db)
    assert [t.goal for t in traces] == ["fresh"], "plan traces are not on the sweep"
    rows = (await swept_db.execute(select(ActivityLog))).scalars().all()
    assert [r.action for r in rows] == ["recent"], "activity log is not on the sweep"


def test_activity_retention_stays_far_above_the_folder_habit_horizon():
    """⚠️ A COUPLING, PINNED. `file_intelligence.frequent_folders` ranks the
    user's save/move habits by counting successful file-tool rows over ALL time,
    with no date filter. `activity_log` was unbounded until this round, and
    bounding it too tightly would silently shrink a signal the planner uses
    (rule 18) — action at a distance, with nothing to say it happened.

    A year is the deliberate choice: it bounds the table, which was the actual
    defect, while leaving the ranking materially intact. Shortening it to the
    30 days the two decision trails get would be a behaviour change wearing a
    tidy-up's clothes."""
    from app.tools.registry import ACTIVITY_RETENTION_DAYS

    assert ACTIVITY_RETENTION_DAYS >= 180, (
        "activity_log retention drives file_intelligence's folder ranking — see "
        "the note on ACTIVITY_RETENTION_DAYS before shortening it"
    )
    assert ACTIVITY_RETENTION_DAYS > plan_trace.PLAN_RETENTION_DAYS
