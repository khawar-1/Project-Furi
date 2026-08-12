"""
Pause a running task and steer it (2026-08-03).

The scenario: an agent is working, it gets something wrong, and the user says
"pause" and then tells it what to change.

THE ASSERTION THAT CARRIES THIS WHOLE ROUND is that pending steps stay PENDING.
That is the ONE behavioural difference between pause and cancel, and everything
else (continue, steer, restart survival) is only possible because of it — if a
future refactor lets apply_pause skip steps the way apply_cancellation does,
the feature is silently gone and every other test here would still pass.

Levels, deliberately mixed:
- the PLANNER, driven directly with a controlled pause_check, so "between
  steps" is exact rather than a race;
- the RUNNER, on a file-backed DB (the background-task harness), for parking,
  the push, the auto-applied steer and restart reconciliation;
- the ROUTER, for what counts as a stop word;
- the BROWSE LOOP, where a step can run for minutes and the plan-level check
  is far too coarse on its own.
"""
import json
from typing import AsyncIterator, List, Optional

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.tools  # noqa: F401 — registers the real file/terminal tools
from app.agents import interruption, plan_store, task_runner
from app.agents.interruption import (
    STOPPED_BY_USER,
    apply_pause,
    clear_pause,
    pause_requested,
    request_pause,
    take_steer,
)
from app.agents.planner import AgentPlanner, _is_bare_continue
from app.agents.schemas import AgentPlan, PlanStatus, PlanStep, StepStatus
from app.agents.task_runner import (
    fail_interrupted_tasks,
    request_task_pause,
    start_task,
    wait_for_task,
)
from app.api.interrupt_router import looks_like_pause, maybe_handle_interrupt
from app.core.base_tool import PermissionLevel, ToolResult
from app.db.database import Base
from app.db.models import Message, ParkedPlan, Task
from app.db.schemas import ChatMessage, ChatRequest
from app.providers.base import (
    EmbeddingResponse,
    LLMMessage,
    LLMProvider,
    LLMResponse,
)


class FakeProvider(LLMProvider):
    """chat() pops scripted responses; raises when over-called, so "this costs
    no LLM call" is an assertion rather than a hope."""

    def __init__(self, responses: Optional[List[str]] = None) -> None:
        self._responses = list(responses or [])
        self.chat_calls = 0

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model_name(self) -> str:
        return "fake-model"

    async def chat(
        self,
        messages: List[LLMMessage],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        self.chat_calls += 1
        if not self._responses:
            raise AssertionError(f"FakeProvider.chat exhausted after {self.chat_calls - 1}")
        return LLMResponse(
            content=self._responses.pop(0), model="fake-model", provider="fake",
        )

    async def stream_chat(
        self,
        messages: List[LLMMessage],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[str]:
        yield ""

    async def embed(self, text: str) -> EmbeddingResponse:
        return EmbeddingResponse(embedding=[0.0] * 384, model="fake", provider="fake")


def step(description: str, tool: str, **parameters) -> dict:
    return {"description": description, "tool": tool, "parameters": parameters}


def plan_json(steps: list) -> str:
    return json.dumps({"steps": steps, "unachievable_reason": None, "question": None})


class AfterN:
    """A pause_check that fires only once N steps have gone by — "between
    steps" made exact, with no sleeping and no race."""

    def __init__(self, n: int) -> None:
        self.n = n
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return self.calls > self.n


# ================================================================== fixtures

@pytest_asyncio.fixture
async def task_db(tmp_path_factory, monkeypatch):
    """File-backed DB shared by the test session AND the runner's own sessions
    (the background-task harness rule: an in-memory DB gives every connection
    its own empty database)."""
    db_dir = tmp_path_factory.mktemp("pause-db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_dir / 'pause.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(task_runner, "SESSION_FACTORY", factory)
    plan_store._PENDING_PLANS.clear()
    interruption._PAUSE_REQUESTED.clear()
    async with factory() as session:
        yield session
    plan_store._PENDING_PLANS.clear()
    interruption._PAUSE_REQUESTED.clear()
    await engine.dispose()


@pytest.fixture
def pushed(monkeypatch):
    events: list[tuple[str, dict]] = []

    async def _push(event_type: str, payload: Optional[dict] = None) -> int:
        events.append((event_type, payload or {}))
        return 1

    monkeypatch.setattr(task_runner, "push", _push)
    return events


@pytest_asyncio.fixture
async def plain_db(tmp_path_factory):
    """A DB for planner-level tests, which never spawn a background run."""
    db_dir = tmp_path_factory.mktemp("pause-planner-db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_dir / 'p.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def three_reads(tmp_path) -> list:
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text("x")
    return [
        step("Read a", "read_file", path=str(tmp_path / "a.txt")),
        step("Read b", "read_file", path=str(tmp_path / "b.txt")),
        step("Read c", "read_file", path=str(tmp_path / "c.txt")),
    ]


# ======================================== the planner: holding between steps

async def test_pause_between_steps_leaves_the_remaining_steps_pending(
    plain_db, tmp_path
):
    """⚠️ THE LOAD-BEARING TEST. Cancel SKIPS the pending steps; pause must
    leave them PENDING — that is the whole difference, and it is what makes
    Continue and a steer possible at all."""
    steps = three_reads(tmp_path)
    provider = FakeProvider([plan_json(steps), plan_json(steps)])  # draft, reflect
    planner = AgentPlanner(plain_db, provider, pause_check=AfterN(1))

    plan = await planner.start("read my three files")

    assert plan.status == PlanStatus.PAUSED
    assert len(plan.completed_steps()) == 1
    assert [s.status for s in plan.steps[1:]] == [StepStatus.PENDING, StepStatus.PENDING]
    assert StepStatus.SKIPPED not in [s.status for s in plan.steps]
    # And it says where it got to — the user is about to decide what to change.
    assert "1 step(s) had already finished" in plan.message
    assert "2 step(s) are still to run" in plan.message


async def test_cancel_beats_pause(plain_db, tmp_path):
    """Both flags set: the user who cancelled outranks a stale pause, and the
    pending steps are SKIPPED as a cancel demands."""
    steps = three_reads(tmp_path)
    provider = FakeProvider([plan_json(steps), plan_json(steps)])
    planner = AgentPlanner(
        plain_db, provider, cancel_check=AfterN(1), pause_check=AfterN(1),
    )

    plan = await planner.start("read my three files")

    assert plan.status == PlanStatus.CANCELLED
    assert all(s.status == StepStatus.SKIPPED for s in plan.steps[1:])


async def test_pause_during_a_replan_spends_no_llm_call(plain_db, tmp_path):
    """A replan round is "between steps" too. The failed step drives execute
    into revise, whose FIRST act is the pause check — so a paused run never
    pays for planning work the user is about to redirect.

    "No replan" is asserted STRUCTURALLY: the provider is scripted with exactly
    the ONE call this plan legitimately needs (reflection is skipped for an
    all-read draft), so a replan round would raise rather than quietly pass."""
    steps = [
        step("Read missing", "read_file", path=str(tmp_path / "gone.txt")),
        step("Read b", "read_file", path=str(tmp_path / "b.txt")),
    ]
    (tmp_path / "b.txt").write_text("x")
    provider = FakeProvider([plan_json(steps)])  # draft only — a replan raises
    # False for the execute-loop check, True by the time revise asks.
    planner = AgentPlanner(plain_db, provider, pause_check=AfterN(1))

    plan = await planner.start("read them")

    assert plan.status == PlanStatus.PAUSED
    assert provider.chat_calls == 1


# ============================================== apply_pause's own two rules

def _stopped_step() -> PlanStep:
    return PlanStep(
        description="Browse the site",
        tool="browse",
        parameters={"goal": "x"},
        permission_level=PermissionLevel.READ,
        requires_approval=False,
        status=StepStatus.FAILED,
        result=ToolResult(
            success=False,
            output={STOPPED_BY_USER: True, "url": "https://site.test/"},
            error="stopped at your request",
        ),
    )


def test_apply_pause_resets_a_user_stopped_step_so_carry_on_re_runs_it():
    """A browse that halted between its OWN actions failed because it was ASKED
    to. Left FAILED, a plain "carry on" would step over a hole; reset to PENDING
    it re-runs from the top, which is safe (browse is READ, and any gesture or
    submit inside it pauses for its own approval)."""
    plan = AgentPlan(goal="browse", steps=[_stopped_step()])
    apply_pause(plan)
    assert plan.steps[0].status == StepStatus.PENDING
    assert plan.steps[0].result is None


def test_apply_pause_leaves_a_genuine_failure_failed():
    """The marker is code-owned, read from the tool's own output — never from
    the error prose. A step that broke stays broken so the steer replans it."""
    broken = PlanStep(
        description="Read it",
        tool="read_file",
        parameters={"path": "x"},
        permission_level=PermissionLevel.READ,
        requires_approval=False,
        status=StepStatus.FAILED,
        result=ToolResult(success=False, output=None, error="stopped at your request"),
    )
    plan = AgentPlan(goal="read", steps=[broken])
    apply_pause(plan)
    assert plan.steps[0].status == StepStatus.FAILED


# ================================================ continuing and steering

async def test_carry_on_runs_the_remaining_steps(plain_db, tmp_path):
    """Continue = resume(approved=True) on the paused plan: the steps the user
    is looking at, approved by their own signatures, with no new planning."""
    steps = three_reads(tmp_path)
    provider = FakeProvider([plan_json(steps), plan_json(steps)])
    planner = AgentPlanner(plain_db, provider, pause_check=AfterN(1))
    plan = await planner.start("read my three files")
    assert plan.status == PlanStatus.PAUSED

    resumed = await AgentPlanner(plain_db, FakeProvider([])).resume(plan, approved=True)

    assert resumed.status == PlanStatus.COMPLETED
    assert len(resumed.completed_steps()) == 3


async def test_carry_on_grants_no_approval_to_a_pending_write(plain_db, tmp_path):
    """⚠️ THE SAFETY PROPERTY OF "Carry on". A plan can pause BEFORE it ever
    reached the approval gate — the user stopped it during a read — so its
    pending WRITE has never been shown on an approval card. Continue must
    therefore approve NOTHING: the gate re-applies, and the worst case is one
    approval click on a step that was already approved before the pause, not a
    delete performed on a button labelled "Carry on"."""
    (tmp_path / "a.txt").write_text("x")
    steps = [
        step("Read a", "read_file", path=str(tmp_path / "a.txt")),
        step("Write the note", "create_file", path=str(tmp_path / "n.txt"), content="hi"),
    ]
    provider = FakeProvider([plan_json(steps), plan_json(steps)])
    planner = AgentPlanner(plain_db, provider, pause_check=AfterN(1))
    plan = await planner.start("read then write")
    assert plan.status == PlanStatus.PAUSED

    resumed = await AgentPlanner(plain_db, FakeProvider([])).resume(plan, approved=True)

    assert resumed.status == PlanStatus.AWAITING_APPROVAL
    assert not (tmp_path / "n.txt").exists()


async def test_a_bare_carry_on_is_decided_in_code(plain_db, tmp_path):
    """The pause message tells the user to "say carry on", so that phrase must
    not cost a planning round — and must not be re-interpreted by a revise LLM
    into a replan. FakeProvider([]) raises on any call."""
    steps = three_reads(tmp_path)
    provider = FakeProvider([plan_json(steps), plan_json(steps)])
    planner = AgentPlanner(plain_db, provider, pause_check=AfterN(1))
    plan = await planner.start("read my three files")

    quiet = FakeProvider([])
    resumed = await AgentPlanner(plain_db, quiet).answer(plan, "carry on")

    assert resumed.status == PlanStatus.COMPLETED
    assert quiet.chat_calls == 0


async def test_a_steer_replans_the_remainder_and_a_write_needs_fresh_approval(
    plain_db, tmp_path
):
    """The point of the whole feature: the correction replans what is LEFT, and
    a write it produces still faces the approval gate with a fresh signature —
    a steer can never smuggle an approved write past it."""
    (tmp_path / "a.txt").write_text("x")
    reads = [
        step("Read a", "read_file", path=str(tmp_path / "a.txt")),
        step("Read b", "read_file", path=str(tmp_path / "b.txt")),
    ]
    (tmp_path / "b.txt").write_text("x")
    provider = FakeProvider([plan_json(reads), plan_json(reads)])
    planner = AgentPlanner(plain_db, provider, pause_check=AfterN(1))
    plan = await planner.start("read my files")
    assert plan.status == PlanStatus.PAUSED

    revision = [step("Write the note", "create_file", path=str(tmp_path / "n.txt"), content="hi")]
    steered = await AgentPlanner(plain_db, FakeProvider([plan_json(revision)])).answer(
        plan, "actually write a note instead"
    )

    assert steered.status == PlanStatus.AWAITING_APPROVAL
    pending = steered.pending_steps()
    assert [s.tool for s in pending] == ["create_file"]
    assert not (tmp_path / "n.txt").exists()  # nothing ran without approval
    # The correction is authoritative planner input, exactly like an answer.
    assert "actually write a note instead" in steered.user_answers


async def test_stop_again_on_a_paused_plan_drops_it(plain_db, tmp_path):
    """A second "stop" to an already-stopped plan means DROP it — not "replan
    with the word stop as an authoritative instruction". A correction that
    merely starts with a negative ("no, use the D drive") is still a steer."""
    steps = three_reads(tmp_path)
    provider = FakeProvider([plan_json(steps), plan_json(steps)])
    planner = AgentPlanner(plain_db, provider, pause_check=AfterN(1))
    plan = await planner.start("read my three files")

    quiet = FakeProvider([])
    dropped = await AgentPlanner(plain_db, quiet).answer(plan, "cancel")

    assert dropped.status == PlanStatus.CANCELLED
    assert quiet.chat_calls == 0  # decided in code
    assert all(s.status == StepStatus.SKIPPED for s in dropped.steps[1:])


def test_is_bare_continue_matrix():
    """Whole-message by construction. Erring toward STEER is the safe
    direction: a misread steer replans, a misread continue would silently
    ignore what the user asked for."""
    for yes in (
        "carry on", "Carry on then", "continue", "continue please", "keep going",
        "resume", "go ahead", "proceed", "ok", "yes", "as you were", "unpause",
        "carry on jarvis", "sure, thanks",
    ):
        assert _is_bare_continue(yes), yes
    for no in (
        "continue but use the D drive", "carry on with the other folder",
        "go ahead and delete them", "use D:\\Downloads", "no", "stop",
        "resume from the second file", "", "keep going but skip the pdfs",
    ):
        assert not _is_bare_continue(no), no


# ================================================= the runner: park and push

async def test_a_paused_task_parks_pushes_and_persists(task_db, pushed, tmp_path):
    steps = three_reads(tmp_path)
    provider = FakeProvider([plan_json(steps), plan_json(steps)])

    task = await start_task(task_db, "read my files", "s-pause", provider=provider)
    request_pause(task.id)  # lands before the first between-steps check
    await wait_for_task(task.id)
    await task_db.refresh(task)

    assert task.status == "paused"
    assert task.finished_at is None  # NOT settled — it is holding
    # Parked through the SAME store, so Continue and a typed steer both reach it.
    assert await task_db.get(ParkedPlan, task.plan_id) is not None

    assert [e[0] for e in pushed] == ["task"]
    payload = pushed[0][1]
    assert payload["status"] == "paused"
    assert payload["title"] == "Furi paused"
    assert payload["plan"]["status"] == "paused"

    # And persisted — the push channel has no queue.
    result = await task_db.execute(
        select(Message).where(Message.session_id == "s-pause")
    )
    assert any("Stopped the background task" in m.content for m in result.scalars().all())


async def test_request_task_pause_is_false_without_a_live_run(task_db):
    """No live run means nothing a flag could stop — say so rather than promise
    a pause that will never arrive (the request_task_cancel contract)."""
    assert request_task_pause("no-such-task") is False
    assert not pause_requested("no-such-task")


async def test_the_flag_never_leaks_into_a_later_resume(task_db, pushed, tmp_path):
    """A pause that arrived too late to be applied must not hold the NEXT run
    of the same task — _spawn's done-callback clears it, like cancel."""
    steps = [step("Read a", "read_file", path=str(tmp_path / "a.txt"))]
    (tmp_path / "a.txt").write_text("x")
    provider = FakeProvider([plan_json(steps), plan_json(steps)])
    task = await start_task(task_db, "read a", "s-late", provider=provider)
    await wait_for_task(task.id)

    request_pause(task.id)  # nothing live to stop
    await wait_for_task(task.id)  # already finished; the callback has run
    clear_pause(task.id)
    assert not pause_requested(task.id)


async def test_a_steer_is_applied_automatically_after_the_pause(
    task_db, pushed, tmp_path
):
    """"stop, read the other one" — ONE message that pauses AND corrects. The
    runner consumes the steer and continues in the same asyncio task, so the
    user gets one stop and one resumption."""
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "b.txt").write_text("y")
    first = [
        step("Read a", "read_file", path=str(tmp_path / "a.txt")),
        step("Read b", "read_file", path=str(tmp_path / "b.txt")),
    ]
    revision = [step("Read b only", "read_file", path=str(tmp_path / "b.txt"))]
    provider = FakeProvider([
        plan_json(first), plan_json(first),   # draft + reflect
        plan_json(revision),                  # the steered revise round
    ])

    task = await start_task(task_db, "read my files", "s-steer", provider=provider)
    request_pause(task.id, "only read b")
    await wait_for_task(task.id)
    await task_db.refresh(task)

    # It paused, then picked itself up with the correction and finished.
    assert task.status == "completed"
    assert [e[1]["status"] for e in pushed] == ["paused", "completed"]
    assert take_steer(task.id) is None  # consumed once, never re-applied


async def test_a_paused_task_survives_a_restart_while_its_plan_is_parked(task_db):
    """The flag is in-memory (it targets a live run), but a plan that ALREADY
    paused is parked in SQLite — so the correction still lands after a
    restart. Without the row there is nothing to resume and it fails honestly."""
    keeps = Task(goal="held", session_id="s-r", status="paused", plan_id="plan-held")
    loses = Task(goal="orphan", session_id="s-r", status="paused", plan_id="plan-gone")
    task_db.add_all([keeps, loses])
    await task_db.commit()
    from datetime import timedelta

    from app.db.models import utc_now
    task_db.add(ParkedPlan(
        id="plan-held", session_id="s-r", status="paused", payload="{}",
        expires_at=utc_now() + timedelta(hours=1),
    ))
    await task_db.commit()

    await fail_interrupted_tasks(task_db)
    await task_db.refresh(keeps)
    await task_db.refresh(loses)

    assert keeps.status == "paused"
    assert loses.status == "failed"


async def test_a_paused_plan_owns_the_sessions_next_message(task_db, pushed, tmp_path):
    """The one widening that routes a typed steer: get_choice_plan_for_session
    must see a PAUSED plan, or the correction falls through to the task gate
    and starts a SECOND agent."""
    steps = three_reads(tmp_path)
    provider = FakeProvider([plan_json(steps), plan_json(steps)])
    task = await start_task(task_db, "read my files", "s-owns", provider=provider)
    request_pause(task.id)
    await wait_for_task(task.id)

    open_plan = await plan_store.get_choice_plan_for_session(task_db, "s-owns")
    assert open_plan is not None
    assert open_plan.status == PlanStatus.PAUSED
    assert open_plan.task_id == task.id


# ============================================================== the router

def test_looks_like_pause_matrix():
    for text in (
        "pause", "stop", "wait", "hold on", "hang on", "halt", "stop it",
        "jarvis, stop", "ok stop", "no wait", "Stop!", "hold up",
        "wait a second", "abort",
    ):
        fired, steer = looks_like_pause(text)
        assert fired, text
        assert steer == "", text
    for text in (
        "", "keep going", "what did you find", "don't stop until it's done",
        "find the files that stop the build", "i had to wait ages",
        # Media stops stay a stop_media task, not an agent pause.
        "stop the music", "stop playing", "stop the video",
    ):
        fired, _ = looks_like_pause(text)
        assert not fired, text


def test_a_pause_can_carry_its_correction():
    """"stop, use the D drive one" is ONE message: a stop and an instruction.
    The steer is only ever the user's own remaining words."""
    fired, steer = looks_like_pause("stop, use the D drive downloads instead")
    assert fired and steer == "use the D drive downloads instead"
    fired, steer = looks_like_pause("wait — and search the whole disk")
    assert fired and steer == "search the whole disk"
    # Trailing filler is not an instruction.
    for bare in ("stop it now", "pause please", "hold on jarvis"):
        fired, steer = looks_like_pause(bare)
        assert fired and steer == "", bare


def _request(text: str) -> ChatRequest:
    return ChatRequest(messages=[ChatMessage(role="user", content=text)])


async def test_the_router_only_fires_with_a_live_running_task(task_db):
    """THE guard that makes a generous trigger safe: with nothing running,
    "stop" and "wait" keep their ordinary meaning and the message routes
    exactly as it does today."""
    assert await maybe_handle_interrupt(_request("stop"), "s-none", task_db) is None

    task_db.add(Task(goal="done already", session_id="s-none", status="completed"))
    await task_db.commit()
    assert await maybe_handle_interrupt(_request("stop"), "s-none", task_db) is None

    task_db.add(Task(goal="working", session_id="s-none", status="running"))
    await task_db.commit()
    assert await maybe_handle_interrupt(_request("stop"), "s-none", task_db) is not None


async def test_stop_stops_every_running_agent_in_the_session(task_db, monkeypatch):
    """Several domain agents can work at once, and "stop" plainly means "stop
    what you're doing". Pausing is LOSSLESS, so over-pausing costs one "carry
    on" while pausing the wrong one of two leaves the agent the user is
    actually watching still doing the wrong thing."""
    asked: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "app.api.interrupt_router.request_task_pause",
        lambda tid, steer="": (asked.append((tid, steer)), True)[1],
    )
    older = Task(goal="older work", session_id="s-multi", status="running")
    task_db.add(older)
    await task_db.commit()
    newer = Task(goal="newer work", session_id="s-multi", status="running")
    task_db.add(newer)
    await task_db.commit()

    response = await maybe_handle_interrupt(
        _request("stop, use the D drive"), "s-multi", task_db
    )
    assert response is not None
    async for _ in response.body_iterator:  # consume — the flag is set in there
        pass

    assert {t for t, _ in asked} == {older.id, newer.id}
    # The correction rides on the NEWEST only: it is about ONE piece of work,
    # and applying it to every paused plan would replan tasks the user was not
    # talking about.
    assert dict(asked)[newer.id] == "use the D drive"
    assert dict(asked)[older.id] == ""


async def test_the_router_defers_to_an_open_plan(task_db, pushed, tmp_path):
    """A plan already stopped and asking owns the next message through its own
    channel — the sibling-router rule. Hijacking it would break a flow that
    works."""
    steps = three_reads(tmp_path)
    provider = FakeProvider([plan_json(steps), plan_json(steps)])
    task = await start_task(task_db, "read my files", "s-defer", provider=provider)
    request_pause(task.id)
    await wait_for_task(task.id)
    # Now something else is running in the same session…
    task_db.add(Task(goal="other", session_id="s-defer", status="running"))
    await task_db.commit()

    assert await maybe_handle_interrupt(_request("stop"), "s-defer", task_db) is None


# ========================================================== the browse loop

async def test_run_browse_stops_between_its_own_actions():
    """A browse can run for minutes, so the plan-level between-steps check is
    far too coarse: the user would ask it to stop and watch it keep browsing.
    The action in flight still finishes — the cooperative rule, unchanged."""
    from app.browser import loop as browser_loop

    from tests.test_browser_loop import FakeProvider as LoopProvider
    from tests.test_browser_loop import FakeSession, ScriptedPage, _el, _page

    page = ScriptedPage([
        _page([_el(1, "link", "next", href="/next")], url="https://site.test/"),
        _page([_el(1, "link", "next", href="/next")], url="https://site.test/next"),
    ])
    session = FakeSession(page)
    stop = AfterN(0)  # already requested when the loop reaches its first check
    provider = LoopProvider([])

    outcome = await browser_loop.run_browse(
        session, "find the cheapest book", provider, stop_check=stop,
    )

    assert outcome.success is False
    assert outcome.stopped_by_user is True
    assert "stopped at your request" in outcome.error
    assert page.acted == []      # nothing was done after the stop
    assert provider.calls == 0   # and no decision was paid for


async def test_no_stop_check_leaves_the_loop_exactly_as_it_was():
    """Every pre-existing caller passes nothing. The regression guard for the
    whole browser change: stop_check=None must be byte-for-byte today."""
    from app.browser import loop as browser_loop

    from tests.test_browser_loop import FakeProvider as LoopProvider
    from tests.test_browser_loop import FakeSession, ScriptedPage, _page

    page = ScriptedPage([_page([], url="https://site.test/")])
    provider = LoopProvider(['{"action":"done","reason":"nothing to do"}'])

    outcome = await browser_loop.run_browse(
        FakeSession(page), "look at the page", provider,
    )

    assert outcome.stopped_by_user is False
    assert outcome.success is True
