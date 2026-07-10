"""
Phase 4, Part 6 — Live plan narration + cooperative mid-plan cancel.

Narration: the planner pushes one "plan_step" event per step transition
(running → completed/failed) — best-effort, and a narration failure never
breaks execution.

Cancel: cooperative, checked BETWEEN steps — a step that already started
always finishes (never killed mid-write); everything not yet run is SKIPPED,
the plan settles CANCELLED, and the cancellation is audited in ActivityLog.
A cancel beats an approval pause (never ask the user to approve work they
just cancelled), including the settle-time race where the cancel lands while
the final planning round is in flight.
"""
import asyncio
import json
from typing import AsyncIterator, List, Optional

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.tools  # noqa: F401 — registers the real file/terminal tools
from app.agents import cancellation, narration, plan_store, task_runner
from app.agents.planner import AgentPlanner
from app.agents.schemas import AgentPlan, PlanStatus, PlanStep, StepStatus
from app.agents.task_runner import request_task_cancel, start_task, wait_for_task
from app.core.base_tool import PermissionLevel
from app.core.dependencies import get_db
from app.db.database import Base
from app.db.models import ActivityLog, Message, ParkedPlan, Task
from app.providers.base import (
    EmbeddingResponse,
    LLMMessage,
    LLMProvider,
    LLMResponse,
)
from main import app


class FakeProvider(LLMProvider):
    """chat() pops scripted responses; fails loudly if over-called."""

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


def write_step(id_suffix: str = "w") -> PlanStep:
    return PlanStep(
        description=f"Create file {id_suffix}",
        tool="create_file",
        parameters={"path": f"C:/tmp/{id_suffix}.txt", "content": "x"},
        permission_level=PermissionLevel.WRITE,
        requires_approval=True,
    )


# ================================================================== fixtures

@pytest_asyncio.fixture
async def task_db(tmp_path_factory, monkeypatch):
    """File-backed DB shared by the test session AND the runner's own
    sessions (SESSION_FACTORY) — an in-memory DB would give each connection
    its own empty database."""
    db_dir = tmp_path_factory.mktemp("narration-db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_dir / 'narr.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(task_runner, "SESSION_FACTORY", factory)
    plan_store._PENDING_PLANS.clear()
    async with factory() as session:
        yield session
    plan_store._PENDING_PLANS.clear()
    await engine.dispose()


@pytest.fixture(autouse=True)
def clean_cancel_flags():
    cancellation._CANCEL_REQUESTED.clear()
    yield
    cancellation._CANCEL_REQUESTED.clear()


@pytest.fixture
def narrated(monkeypatch):
    """Captures every "plan_step" event the planner narrates, in order."""
    events: list[dict] = []

    async def _push(event_type: str, payload: Optional[dict] = None) -> int:
        assert event_type == narration.PLAN_STEP_EVENT
        events.append(payload or {})
        return 1

    monkeypatch.setattr(narration, "push", _push)
    return events


@pytest.fixture
def pushed(monkeypatch):
    """Captures every "task" event the runner emits, in order."""
    events: list[tuple[str, dict]] = []

    async def _push(event_type: str, payload: Optional[dict] = None) -> int:
        events.append((event_type, payload or {}))
        return 1

    monkeypatch.setattr(task_runner, "push", _push)
    return events


async def _cancel_audit_rows(db) -> list[ActivityLog]:
    result = await db.execute(
        select(ActivityLog).where(ActivityLog.tool_name == "cancel_plan")
    )
    return list(result.scalars().all())


# ================================================================= narration

async def test_step_events_tick_running_then_completed(task_db, narrated, tmp_path):
    (tmp_path / "a.txt").write_text("x")
    steps = [
        step("List the folder", "list_directory", path=str(tmp_path)),
        step("Read a.txt", "read_file", path=str(tmp_path / "a.txt")),
    ]
    provider = FakeProvider([plan_json(steps), plan_json(steps)])  # draft, reflect
    planner = AgentPlanner(task_db, provider, session_id="s-narr")

    plan = await planner.start("list then read")
    assert plan.status == PlanStatus.COMPLETED

    # running → completed for each step, in execution order
    assert [(e["step_index"], e["status"]) for e in narrated] == [
        (0, "running"), (0, "completed"), (1, "running"), (1, "completed"),
    ]
    first = narrated[0]
    assert first["plan_id"] == plan.id
    assert first["session_id"] == "s-narr"
    assert first["tool"] == "list_directory"
    assert first["step_id"] == plan.steps[0].id
    assert first["step_count"] == 2
    # RUNNING is transient — the finished plan never carries it
    assert all(s.status == StepStatus.COMPLETED for s in plan.steps)


async def test_failed_step_narrates_the_error(task_db, narrated, tmp_path):
    steps = [step("Read a missing file", "read_file", path=str(tmp_path / "nope.txt"))]
    provider = FakeProvider([
        plan_json(steps), plan_json(steps),  # draft, reflect
        json.dumps({"steps": [], "unachievable_reason": "file is gone",
                    "question": None}),  # replan gives up…
    ])
    planner = AgentPlanner(task_db, provider, session_id="s-narr-fail")

    plan = await planner.start("read nope.txt")
    # …but a not-found target now ASKS instead of failing (ask-not-fail,
    # 2026-07-10) — the narration contract under test is unchanged: the
    # failed step narrated its real error before the plan paused.
    assert plan.status == PlanStatus.AWAITING_CHOICE
    assert "couldn't find 'nope.txt'" in plan.question.text

    assert [e["status"] for e in narrated] == ["running", "failed"]
    assert narrated[1]["error"]  # the tool's real error text rides the event


async def test_narration_failure_never_breaks_execution(task_db, monkeypatch, tmp_path):
    async def _explode(event_type: str, payload: Optional[dict] = None) -> int:
        raise RuntimeError("push channel down")

    monkeypatch.setattr(narration, "push", _explode)
    steps = [step("List the folder", "list_directory", path=str(tmp_path))]
    provider = FakeProvider([plan_json(steps), plan_json(steps)])
    planner = AgentPlanner(task_db, provider, session_id="s-narr-crash")

    plan = await planner.start("list my folder")
    assert plan.status == PlanStatus.COMPLETED  # narration is best-effort only


# ===================================================== cooperative cancel

async def test_cancel_between_steps_finishes_running_step(task_db, narrated, tmp_path):
    """The cancel lands after step 1 started: step 1 FINISHES (never killed
    mid-write), step 2 is skipped, the plan is CANCELLED and audited."""
    (tmp_path / "a.txt").write_text("x")
    steps = [
        step("List the folder", "list_directory", path=str(tmp_path)),
        step("Read a.txt", "read_file", path=str(tmp_path / "a.txt")),
    ]
    provider = FakeProvider([plan_json(steps), plan_json(steps)])

    checks = {"n": 0}

    def cancel_check() -> bool:
        checks["n"] += 1
        return checks["n"] > 1  # False before step 1, True before step 2

    planner = AgentPlanner(task_db, provider, session_id="s-cancel", cancel_check=cancel_check)
    plan = await planner.start("list then read")

    assert plan.status == PlanStatus.CANCELLED
    assert plan.steps[0].status == StepStatus.COMPLETED  # already running → finished
    assert plan.steps[1].status == StepStatus.SKIPPED    # never ran
    assert "1 step(s) had already finished" in plan.message

    # Step 2 never narrated anything — it never started
    assert [(e["step_index"], e["status"]) for e in narrated] == [
        (0, "running"), (0, "completed"),
    ]

    # Audited in ActivityLog
    rows = await _cancel_audit_rows(task_db)
    assert len(rows) == 1
    assert rows[0].session_id == "s-cancel"
    assert rows[0].success is True
    assert "1 step(s) had completed" in rows[0].result_summary
    assert "1 pending step(s) were skipped" in rows[0].result_summary


async def test_cancel_beats_the_approval_pause(task_db, narrated, tmp_path):
    """A cancel that is already pending when a write step comes up wins:
    the plan is CANCELLED, never parked for an approval the user no longer
    wants to give."""
    steps = [step("Create out.txt", "create_file", path=str(tmp_path / "out.txt"), content="hi")]
    provider = FakeProvider([plan_json(steps), plan_json(steps)])
    planner = AgentPlanner(
        task_db, provider, session_id="s-cancel-first", cancel_check=lambda: True
    )

    plan = await planner.start("create out.txt")
    assert plan.status == PlanStatus.CANCELLED
    assert plan.steps[0].status == StepStatus.SKIPPED
    assert not (tmp_path / "out.txt").exists()
    assert narrated == []  # nothing ran, nothing ticked
    assert plan.message == "Cancelled by the user — nothing further was executed."
    assert len(await _cancel_audit_rows(task_db)) == 1


async def test_runner_cancel_end_to_end(task_db, pushed, narrated, tmp_path):
    """Endpoint-shaped flow through the runner: request_task_cancel on a live
    run → the plan cancels between steps, the Task row settles cancelled,
    the outcome is pushed AND persisted as a chat message."""
    steps = [step("List the folder", "list_directory", path=str(tmp_path))]
    provider = FakeProvider([plan_json(steps), plan_json(steps)])

    task = await start_task(task_db, "list my files", "s-bg-cancel", provider=provider)
    # The spawned run has not had a slice yet — the flag is set before its
    # first between-steps check, so nothing executes at all.
    assert request_task_cancel(task.id) is True
    await wait_for_task(task.id)
    await task_db.refresh(task)

    assert task.status == "cancelled"
    assert task.finished_at is not None
    assert "Cancelled by the user" in task.message

    assert [e[0] for e in pushed] == ["task"]
    payload = pushed[0][1]
    assert payload["status"] == "cancelled"
    assert payload["title"] == "Task cancelled"
    assert payload["plan"]["status"] == "cancelled"

    result = await task_db.execute(
        select(Message).where(Message.session_id == "s-bg-cancel")
    )
    contents = [m.content for m in result.scalars().all()]
    assert any("Cancelled by the user" in c for c in contents)

    assert narrated == []  # no step ever started
    assert len(await _cancel_audit_rows(task_db)) == 1
    # The consumed flag never leaks into a later run of the same task id
    assert cancellation.cancel_requested(task.id) is False


async def test_settle_converts_pause_to_cancel(task_db, pushed):
    """A cancel that lands while the final planning round is in flight would
    otherwise park an approval request — _settle's last check converts the
    pause to CANCELLED and nothing is parked."""
    task = Task(goal="write stuff", session_id="s-settle", status="running")
    task_db.add(task)
    await task_db.commit()
    await task_db.refresh(task)

    plan = AgentPlan(
        goal="write stuff", session_id="s-settle",
        status=PlanStatus.AWAITING_APPROVAL, steps=[write_step()],
    )
    cancellation.request_cancel(task.id)
    await task_runner._settle(task_db, task, plan)

    assert task.status == "cancelled"
    assert plan.status == PlanStatus.CANCELLED
    assert plan.steps[0].status == StepStatus.SKIPPED
    assert plan_store.get_plan(plan.id) is None
    assert await task_db.get(ParkedPlan, plan.id) is None
    assert pushed[0][1]["status"] == "cancelled"
    assert len(await _cancel_audit_rows(task_db)) == 1


# ============================================================= cancel API

async def test_cancel_endpoint(task_db):
    async def _override_db():
        yield task_db

    app.dependency_overrides[get_db] = _override_db
    transport = httpx.ASGITransport(app=app)
    dummy_handle = None
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Unknown task → 404
            resp = await client.post("/api/tasks/nope/cancel")
            assert resp.status_code == 404

            # Settled task → not accepted, honest reason
            done = Task(goal="done already", status="completed")
            running = Task(goal="still going", status="running")
            paused = Task(goal="waiting on you", status="awaiting_approval")
            task_db.add_all([done, running, paused])
            await task_db.commit()

            resp = await client.post(f"/api/tasks/{done.id}/cancel")
            assert resp.status_code == 200
            assert resp.json()["accepted"] is False
            assert "already settled" in resp.json()["detail"]

            # Paused task → pointed at the approval card, flag never set
            resp = await client.post(f"/api/tasks/{paused.id}/cancel")
            assert resp.json()["accepted"] is False
            assert "approval card" in resp.json()["detail"]
            assert cancellation.cancel_requested(paused.id) is False

            # Running task with NO live run in this process → not accepted
            resp = await client.post(f"/api/tasks/{running.id}/cancel")
            assert resp.json()["accepted"] is False
            assert cancellation.cancel_requested(running.id) is False

            # Running task WITH a live run → accepted, flag set
            dummy_handle = asyncio.get_running_loop().create_task(asyncio.sleep(60))
            task_runner._RUNNING[running.id] = dummy_handle
            resp = await client.post(f"/api/tasks/{running.id}/cancel")
            assert resp.json()["accepted"] is True
            assert "currently running will finish" in resp.json()["detail"]
            assert cancellation.cancel_requested(running.id) is True
            task_runner._RUNNING.pop(running.id, None)
    finally:
        app.dependency_overrides.pop(get_db, None)
        if dummy_handle is not None:
            dummy_handle.cancel()
