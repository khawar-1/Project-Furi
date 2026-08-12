"""
The approval card was DEAF, and abandoned rows never got swept (2026-08-03).

TWO defects, both reported by the user in the same breath as "what would happen
to a confirmation card if I tell Furi what it's doing isn't correct?".

DEFECT 1 — a typed correction never reached the plan holding an approval card.
AWAITING_APPROVAL was not in plan_store._OPEN_STATUSES, so the message fell
through to the task router (starting a SECOND agent on the correction) while
the original card stayed live and CLICKABLE for the parked plan's whole 24h
TTL. Approve it the next morning and the uncorrected plan ran.

DEFECT 2 — reconciliation was startup-only. On a backend that stays up for
days, a task abandoned past its plan's TTL kept a row saying "paused", counted
as an active worker, with a Continue button that could only ever error.

⚠️ THE TRAP IN FIXING DEFECT 2, and the test that guards it:
fail_interrupted_tasks() also sweeps `running` → `failed`, which is true ONLY
at startup. Put THAT on a timer and every live agent dies mid-flight. The half
that is safe to repeat was split out; test_reconcile_never_touches_a_running_task
is what stops it being merged back.
"""
import asyncio
import json
from datetime import timedelta
from typing import AsyncIterator, List, Optional

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.tools  # noqa: F401 — registers the real file/terminal tools
from app.agents import plan_store
from app.agents.plan_store import get_choice_plan_for_session, put_plan
from app.agents.planner import AgentPlanner, _is_bare_continue
from app.agents.schemas import AgentPlan, PlanStatus, PlanStep, StepStatus
from app.agents.task_runner import (
    fail_interrupted_tasks,
    reconcile_expired_task_plans,
)
from app.api.task_router import _is_typed_approval, maybe_handle_task
from app.core import housekeeping
from app.db.database import Base
from app.db.models import ParkedPlan, Task, utc_now
from app.db.schemas import ChatMessage, ChatRequest
from app.providers.base import (
    EmbeddingResponse,
    LLMMessage,
    LLMProvider,
    LLMResponse,
)


class FakeProvider(LLMProvider):
    """chat() pops scripted responses and RAISES when over-called, so "this
    costs no LLM call" is an assertion rather than a hope."""

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


def request(text: str) -> ChatRequest:
    return ChatRequest(messages=[ChatMessage(role="user", content=text)])


# ================================================================== fixtures

@pytest_asyncio.fixture
async def db(tmp_path_factory, monkeypatch):
    db_dir = tmp_path_factory.mktemp("approval-steer-db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_dir / 'a.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    # The housekeeping sweep opens its OWN session (it runs on a timer, with no
    # request to borrow one from) — point it at this database so the wiring can
    # be driven end to end rather than only its parts.
    import app.db.database as database
    monkeypatch.setattr(database, "AsyncSessionLocal", factory)
    plan_store._PENDING_PLANS.clear()
    async with factory() as session:
        yield session
    plan_store._PENDING_PLANS.clear()
    await engine.dispose()


async def approval_plan(db, tmp_path, *, session_id: str = "s-approve") -> AgentPlan:
    """A REAL plan stopped at a real approval card: one completed read, one
    pending destructive step. Built by running the planner rather than
    hand-assembled, so the card is the genuine article."""
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "doomed.txt").write_text("x")
    steps = [
        step("Read a", "read_file", path=str(tmp_path / "a.txt")),
        step("Delete it", "delete_file", path=str(tmp_path / "doomed.txt")),
    ]
    provider = FakeProvider([plan_json(steps), plan_json(steps)])
    planner = AgentPlanner(db, provider, session_id=session_id)
    plan = await planner.start("read a then delete the other one")
    assert plan.status == PlanStatus.AWAITING_APPROVAL, plan.status
    assert (tmp_path / "doomed.txt").exists()  # the gate held
    return plan


# ============================ the word sets: why they must stay separate

def test_the_carry_on_word_set_would_have_flipped_a_delete_on():
    """⚠️ THE MEASUREMENT behind two predicates instead of one reused.

    The obvious implementation is to borrow planner._is_bare_continue — it
    already means "a whole-message affirmation with no correction in it". But
    it answers a DIFFERENT question. At a PAUSE, "never mind" means "forget I
    interrupted you, carry on". At an APPROVAL CARD the same words mean
    "forget it, DON'T". One word set, opposite meanings: reusing it would have
    read a request to DROP a delete as consent to RUN it."""
    for opposite in ("never mind", "nvm", "never mind then"):
        assert _is_bare_continue(opposite) is True, opposite
        assert _is_typed_approval(opposite) is False, opposite


@pytest.mark.parametrize("message", [
    "yes", "yeah", "Yes.", "yep", "sure", "ok", "okay", "fine",
    "go ahead", "go ahead please", "do it", "send it", "proceed",
    "approve", "approved", "confirm", "yes please", "ok do it",
    "go for it", "yes go ahead sir",
])
def test_these_read_as_consent_and_are_refused(message):
    assert _is_typed_approval(message) is True


@pytest.mark.parametrize("message", [
    # Corrections — the whole point. A word left over means a STEER.
    "yes but use the D drive one",
    "ok but not that file",
    "sure, delete the other one instead",
    "actually use downloads on D",
    "no, use the D drive one",
    "that's not right",
    "use the other folder",
    # Declines, which cancel rather than approve.
    "no", "cancel", "stop", "forget it",
    # Opposites that the carry-on set wrongly accepts.
    "never mind", "nvm",
])
def test_these_are_not_consent(message):
    """Erring toward STEER is the safe direction: a misread steer replans and
    pauses again, a misread consent runs something irreversible."""
    assert _is_typed_approval(message) is False


# ================================== the plan store: an approval card is open

async def test_an_approval_card_owns_the_next_message(db, tmp_path):
    """THE INCIDENT, at its root. Before the fix get_choice_plan_for_session
    returned None here, which is why every router walked past a live card."""
    plan = await approval_plan(db, tmp_path)
    await put_plan(db, plan)

    found = await get_choice_plan_for_session(db, "s-approve")
    assert found is not None
    assert found.id == plan.id
    assert found.status == PlanStatus.AWAITING_APPROVAL


async def test_an_approval_card_is_found_after_a_restart(db, tmp_path):
    """SQLite is the truth — the memory cache is only a hot copy, and the card
    a user leaves overnight must still own their next message."""
    plan = await approval_plan(db, tmp_path)
    await put_plan(db, plan)
    plan_store._PENDING_PLANS.clear()  # the restart

    found = await get_choice_plan_for_session(db, "s-approve")
    assert found is not None and found.id == plan.id


# ============================================ the router: what typing means

async def test_the_incident_a_typed_correction_reaches_the_approval_plan(
    db, tmp_path
):
    """"That's not right — use the D drive one" while a delete card is up.
    Before the fix this started a SECOND agent and left the card armed."""
    plan = await approval_plan(db, tmp_path)
    await put_plan(db, plan)

    response = await maybe_handle_task(
        request("that's not right, use the D drive one"),
        "s-approve", db, FakeProvider(),
    )

    assert response is not None, "the correction did not reach the plan"
    # CONSUMED: the card can no longer be clicked into running the old plan.
    assert await get_choice_plan_for_session(db, "s-approve") is None


async def test_a_typed_approval_never_consumes_the_card(db, tmp_path):
    """"yes" must NOT approve — and must not destroy the card either, because
    the card is exactly where the user is being sent to confirm."""
    plan = await approval_plan(db, tmp_path)
    await put_plan(db, plan)

    response = await maybe_handle_task(
        request("yes go ahead"), "s-approve", db, FakeProvider(),
    )

    assert response is not None  # handled here, never fell through to chat
    still = await get_choice_plan_for_session(db, "s-approve")
    assert still is not None and still.id == plan.id
    assert still.status == PlanStatus.AWAITING_APPROVAL
    assert (tmp_path / "doomed.txt").exists()


async def test_stop_at_an_approval_card_reaches_the_plan(db, tmp_path):
    """Before the fix, "stop" here did NOTHING: the interrupt router needs a
    task in status `running` and an approval-paused one is `awaiting_approval`,
    so it fell to plain chat, which cannot stop anything. It now reaches the
    plan (where planner.answer reads it as a decline). The intercept sits
    AHEAD of looks_like_task, so a word with no domain noun still lands."""
    plan = await approval_plan(db, tmp_path)
    await put_plan(db, plan)

    response = await maybe_handle_task(request("stop"), "s-approve", db, FakeProvider())

    assert response is not None
    assert await get_choice_plan_for_session(db, "s-approve") is None


async def test_the_nudge_says_nothing_ran_and_points_at_the_card():
    """Deterministic, never LLM-paraphrased — the rule every consent text in
    this codebase follows. It has to state the fact and the remedy."""
    from app.api.task_router import _typed_approval_nudge

    text = await _typed_approval_nudge()
    assert "Nothing has run" in text
    assert "Approve" in text and "Cancel" in text


async def test_sibling_routers_defer_to_an_approval_card(db, tmp_path):
    """Every router calls get_choice_plan_for_session to mean "an open plan
    owns this message". Widening that set is what makes them ALL defer — so a
    "remind me at 6" typed at a card is not swallowed by the reminder router
    before the plan ever sees it."""
    from app.api.continuation_router import maybe_handle_continuation
    from app.api.interrupt_router import maybe_handle_interrupt
    from app.api.reminder_router import maybe_handle_reminder
    from app.api.routine_router import maybe_handle_routine

    plan = await approval_plan(db, tmp_path)
    await put_plan(db, plan)

    provider = FakeProvider()
    assert await maybe_handle_reminder(
        request("remind me at 6 to call mum"), "s-approve", db
    ) is None
    assert await maybe_handle_routine(
        request("run my cleanup routine"), "s-approve", db, provider
    ) is None
    assert await maybe_handle_continuation(
        request("look again"), "s-approve", db, provider
    ) is None
    assert await maybe_handle_interrupt(
        request("stop"), "s-approve", db
    ) is None


# ================================= the planner: steering an approval plan

async def test_a_typed_decline_cancels_the_approval_plan(db, tmp_path):
    """The typed twin of the Cancel button. Safe in a way its mirror image is
    not: declining can only ever do LESS than the card asked for. Costs no LLM
    call — the FakeProvider raises if one is made."""
    plan = await approval_plan(db, tmp_path)

    quiet = FakeProvider([])
    dropped = await AgentPlanner(db, quiet).answer(plan, "no")

    assert dropped.status == PlanStatus.CANCELLED
    assert quiet.chat_calls == 0
    assert (tmp_path / "doomed.txt").exists()


async def test_a_correction_keeps_the_completed_work_and_re_approves(db, tmp_path):
    """The point of the whole fix: the read that already succeeded is KEPT, the
    write is replanned, and what comes back faces the gate again with a fresh
    signature. A steer can never smuggle an approved write past it."""
    plan = await approval_plan(db, tmp_path)
    doomed_signature = plan.pending_steps()[0].signature()

    revision = [step("Delete the right one", "delete_file", path=str(tmp_path / "a.txt"))]
    steered = await AgentPlanner(db, FakeProvider([plan_json(revision)])).answer(
        plan, "not that one — delete a.txt instead"
    )

    assert steered.status == PlanStatus.AWAITING_APPROVAL
    assert len(steered.completed_steps()) == 1  # the read survived
    pending = steered.pending_steps()
    assert [s.tool for s in pending] == ["delete_file"]
    assert pending[0].signature() != doomed_signature  # FRESH approval
    assert (tmp_path / "a.txt").exists()  # and it has not run
    assert "not that one — delete a.txt instead" in steered.user_answers


# ====================== the background path, which is where writes live

@pytest_asyncio.fixture
async def task_db(tmp_path_factory, monkeypatch):
    """File-backed and shared with the runner's OWN sessions — an in-memory DB
    gives every connection its own empty database (the background-task rule)."""
    from app.agents import task_runner

    db_dir = tmp_path_factory.mktemp("approval-steer-task-db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_dir / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(task_runner, "SESSION_FACTORY", factory)

    async def _push(event_type, payload=None):
        return 1

    monkeypatch.setattr(task_runner, "push", _push)
    plan_store._PENDING_PLANS.clear()
    async with factory() as session:
        yield session
    plan_store._PENDING_PLANS.clear()
    await engine.dispose()


async def test_a_correction_steers_a_BACKGROUND_approval_card(task_db, tmp_path):
    """The common case, and the one worth proving rather than inferring: the
    router DELEGATEs anything that writes, so a real approval card belongs to a
    background Task. Its typed correction continues in the background too —
    the plan must be accepted by the continuation runner in AWAITING_APPROVAL,
    and the write it replans must still face the gate."""
    from app.agents.task_runner import (
        answer_task_in_background,
        start_task,
        wait_for_task,
    )

    (tmp_path / "keep.txt").write_text("x")
    (tmp_path / "doomed.txt").write_text("x")
    first = [step("Delete it", "delete_file", path=str(tmp_path / "doomed.txt"))]
    revision = [step("Delete the right one", "delete_file", path=str(tmp_path / "keep.txt"))]
    provider = FakeProvider([plan_json(first), plan_json(first), plan_json(revision)])

    task = await start_task(task_db, "delete the file", "s-bg", provider=provider)
    await wait_for_task(task.id)
    await task_db.refresh(task)
    assert task.status == "awaiting_approval"

    parked = await get_choice_plan_for_session(task_db, "s-bg")
    assert parked is not None, "a background approval card must own the next message"

    popped = await plan_store.pop_plan(task_db, parked.id)
    assert await answer_task_in_background(
        task_db, popped, "not that one, delete keep.txt", provider
    ) is not None
    await wait_for_task(task.id)
    await task_db.refresh(task)

    assert task.status == "awaiting_approval"
    # ⚠️ THE ASSERTION THAT MAKES THIS A REGRESSION TEST. A status check alone
    # passes on the BROKEN code too: an answer() that refuses AWAITING_APPROVAL
    # logs a warning and returns the plan UNCHANGED, which settles to exactly
    # the same "awaiting_approval" row with exactly the same files on disk.
    # (Measured — the first cut of this test passed under that revert.) What
    # separates fixed from broken is WHICH file the card now names.
    steered = await get_choice_plan_for_session(task_db, "s-bg")
    assert steered is not None
    assert [s.parameters["path"] for s in steered.pending_steps()] == [
        str(tmp_path / "keep.txt")
    ], "the correction never reached the plan — the card still names the old file"
    # And it is stopped at the gate again: nothing was deleted either way.
    assert (tmp_path / "doomed.txt").exists()
    assert (tmp_path / "keep.txt").exists()


# ============================================= the sweep (defect 2)

async def make_task(db, status: str, *, plan_id: Optional[str] = None) -> Task:
    task = Task(
        goal="tidy my desktop", session_id="s-sweep", status=status, plan_id=plan_id,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


async def park_row(db, plan_id: str, *, expired: bool) -> None:
    db.add(ParkedPlan(
        id=plan_id,
        session_id="s-sweep",
        status="paused",
        payload=json.dumps({}),
        expires_at=utc_now() + timedelta(hours=-1 if expired else 24),
    ))
    await db.commit()


async def test_reconcile_never_touches_a_running_task(db):
    """⚠️ THE TRAP. fail_interrupted_tasks also sweeps `running` → `failed`,
    which is true ONLY at startup because no run can be live then. On a timer
    that would kill every working agent mid-flight. The periodic half must be
    structurally incapable of it."""
    live = await make_task(db, "running")

    settled = await reconcile_expired_task_plans(db)

    await db.refresh(live)
    assert settled == 0
    assert live.status == "running"


async def test_the_periodic_pass_never_kills_a_live_agent(db):
    """⚠️ THE SAME TRAP, ONE LEVEL UP — and the test that actually guards it.

    The direct-call test above still passes if housekeeping is re-pointed at
    fail_interrupted_tasks, because it never touches the wiring: it proves the
    SAFE function is safe, not that the sweep calls it. (Measured: reverting
    the wiring left it green.) This drives the real pass, so "which reconcile
    does the timer call?" is the thing under test."""
    live = await make_task(db, "running")

    await housekeeping.run_housekeeping_pass()

    await db.refresh(live)
    assert live.status == "running", (
        "the periodic sweep killed a live agent -- it is calling the "
        "STARTUP reconcile, whose running->failed half is true only at boot"
    )


async def test_reconcile_settles_a_task_whose_parked_plan_expired(db):
    """The abandoned row: un-continuable, yet still counted as a live worker
    until the next restart."""
    task = await make_task(db, "paused", plan_id="p-gone")
    await park_row(db, "p-gone", expired=True)

    assert await reconcile_expired_task_plans(db) == 1

    await db.refresh(task)
    assert task.status == "failed"
    assert task.finished_at is not None
    assert "expired while waiting for your answer" in task.message


async def test_reconcile_settles_a_task_with_no_parked_row_at_all(db):
    task = await make_task(db, "awaiting_approval", plan_id="p-missing")

    assert await reconcile_expired_task_plans(db) == 1

    await db.refresh(task)
    assert task.status == "failed"


async def test_reconcile_keeps_a_task_whose_parked_plan_is_live(db):
    """A plan parked seconds ago carries a 24h TTL, so a sweep running the
    instant after _settle can never be in scope."""
    task = await make_task(db, "paused", plan_id="p-live")
    await park_row(db, "p-live", expired=False)

    assert await reconcile_expired_task_plans(db) == 0

    await db.refresh(task)
    assert task.status == "paused"


async def test_all_three_waiting_statuses_are_swept(db):
    """One tuple, both readers — `paused` was nearly missed when it was added,
    and a hand-kept second copy of a list is how that happens."""
    for i, status in enumerate(("awaiting_approval", "awaiting_choice", "paused")):
        await make_task(db, status, plan_id=f"p-{i}")

    assert await reconcile_expired_task_plans(db) == 3


async def test_startup_reconcile_still_does_both_halves(db):
    """Splitting the function must not have narrowed what startup does."""
    live = await make_task(db, "running")
    waiting = await make_task(db, "paused", plan_id="p-none")

    await fail_interrupted_tasks(db)

    await db.refresh(live)
    await db.refresh(waiting)
    assert live.status == "failed"
    assert "interrupted by a backend restart" in live.message
    assert waiting.status == "failed"
    assert "expired while waiting" in waiting.message


async def test_a_failing_sweep_step_does_not_skip_the_others(db, monkeypatch):
    """Independently best-effort, the gather_briefing_sections rule: one bad
    source must not cost the pass."""
    called: list[str] = []

    async def boom(_db):
        called.append("plans")
        raise RuntimeError("purge exploded")

    async def ok_pending(_db):
        called.append("pending")

    async def ok_tasks(_db):
        called.append("tasks")
        return 0

    monkeypatch.setattr("app.agents.purge_expired_plans", boom)
    monkeypatch.setattr(
        "app.memory.session_persistence.purge_expired_pending_state", ok_pending
    )
    monkeypatch.setattr(
        "app.agents.task_runner.reconcile_expired_task_plans", ok_tasks
    )

    await housekeeping.run_housekeeping_pass()  # must not raise

    assert called == ["plans", "pending", "tasks"]


async def test_start_housekeeping_is_idempotent_and_stop_awaits_it():
    """A re-entered lifespan must never leave two sweepers running, and
    shutdown must not leave a task pending on a loop about to close."""
    monkey_interval = housekeeping.SWEEP_INTERVAL_SECONDS
    try:
        housekeeping.start_housekeeping()
        first = housekeeping._sweeper
        housekeeping.start_housekeeping()
        assert housekeeping._sweeper is first  # no second sweeper

        await housekeeping.stop_housekeeping()
        assert housekeeping._sweeper is None
        assert first.done()
        await housekeeping.stop_housekeeping()  # idempotent
    finally:
        housekeeping.SWEEP_INTERVAL_SECONDS = monkey_interval
        await housekeeping.stop_housekeeping()


async def test_the_sweep_does_not_fire_immediately_on_arming(monkeypatch):
    """It sleeps FIRST — startup has just done this work more thoroughly, and
    a pass racing the lifespan's own purge buys nothing."""
    ran: list[int] = []

    async def spy():
        ran.append(1)

    monkeypatch.setattr(housekeeping, "run_housekeeping_pass", spy)
    try:
        housekeeping.start_housekeeping()
        await asyncio.sleep(0.05)
        assert ran == []
    finally:
        await housekeeping.stop_housekeeping()
