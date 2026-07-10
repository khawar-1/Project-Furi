"""
Phase 4, Part 5 — Long-running background tasks: plans escape the chat turn.

Unit level: the task runner drives a scripted FakeProvider through real
registered tools against a file-backed DB (the runner opens its OWN sessions
via SESSION_FACTORY, like memory_tools). Push events are captured by
monkeypatching the runner's push — the channel itself is Part 1's problem.

HTTP level: the full loop over /chat/stream and /api/agent/approve — the
"organize my downloads folder and tell me when you're done" done-when.

Invariants under test: SQLite is the truth for task state; pauses park
through the SAME plan store (signature approvals, pop-once) and notify by
push with deterministic text; completion/failure is pushed unprompted AND
persisted as a chat message; a runner crash settles the task as failed and
never propagates.
"""
import json
from datetime import timedelta
from typing import AsyncIterator, List, Optional

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.tools  # noqa: F401 — registers the real file/terminal tools
from app.agents import plan_store, task_runner
from app.agents.plan_store import pop_plan
from app.agents.planner import AgentPlanner
from app.agents.task_runner import (
    answer_task_in_background,
    fail_interrupted_tasks,
    resume_task_in_background,
    settle_cancelled_task,
    start_task,
    wait_for_task,
)
from app.core.dependencies import get_db, get_llm_provider, get_qdrant
from app.db.database import Base
from app.db.models import Message, ParkedPlan, Task, utc_now
from app.providers.base import (
    EmbeddingResponse,
    LLMMessage,
    LLMProvider,
    LLMResponse,
)
from main import app


class FakeProvider(LLMProvider):
    """chat() pops scripted responses; fails loudly if over-called."""

    def __init__(
        self,
        responses: Optional[List[str]] = None,
        streams: Optional[List[str]] = None,
    ) -> None:
        self._responses = list(responses or [])
        self._streams = list(streams or [])
        self.chat_calls = 0
        self.prompts: List[str] = []

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
        self.prompts.append(messages[0].content)
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
        if not self._streams:
            raise AssertionError("FakeProvider.stream_chat exhausted")
        text = self._streams.pop(0)
        for word in text.split(" "):
            yield word + " "

    async def embed(self, text: str) -> EmbeddingResponse:
        return EmbeddingResponse(embedding=[0.0] * 384, model="fake", provider="fake")


def step(description: str, tool: str, **parameters) -> dict:
    return {"description": description, "tool": tool, "parameters": parameters}


def plan_json(
    steps: list, reason: Optional[str] = None, question: Optional[dict] = None
) -> str:
    return json.dumps(
        {"steps": steps, "unachievable_reason": reason, "question": question}
    )


# ================================================================== fixtures

@pytest_asyncio.fixture
async def task_db(tmp_path_factory, monkeypatch):
    """File-backed DB shared by the test session AND the runner's own
    sessions (SESSION_FACTORY) — an in-memory DB would give each connection
    its own empty database."""
    db_dir = tmp_path_factory.mktemp("bgtasks-db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_dir / 'tasks.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(task_runner, "SESSION_FACTORY", factory)
    plan_store._PENDING_PLANS.clear()
    async with factory() as session:
        yield session
    plan_store._PENDING_PLANS.clear()
    await engine.dispose()


@pytest.fixture
def pushed(monkeypatch):
    """Captures every push the runner emits, in order."""
    events: list[tuple[str, dict]] = []

    async def _push(event_type: str, payload: Optional[dict] = None) -> int:
        events.append((event_type, payload or {}))
        return 1

    monkeypatch.setattr(task_runner, "push", _push)
    return events


async def _messages(db, session_id: str) -> list[str]:
    result = await db.execute(
        select(Message).where(Message.session_id == session_id).order_by(Message.created_at)
    )
    return [m.content for m in result.scalars().all()]


# ========================================================= completion pushes

async def test_read_only_task_completes_and_pushes(task_db, pushed, tmp_path):
    (tmp_path / "a.txt").write_text("x")
    steps = [step("List the files", "list_directory", path=str(tmp_path))]
    provider = FakeProvider([plan_json(steps), plan_json(steps)])  # draft, reflect

    task = await start_task(task_db, "list my files", "s-bg-read", provider=provider)
    assert task.status == "running"
    await wait_for_task(task.id)
    await task_db.refresh(task)

    assert task.status == "completed"
    assert task.finished_at is not None
    assert 'Finished the background task "list my files"' in task.message
    assert json.loads(task.plan_payload)["status"] == "completed"

    # Pushed unprompted, with toast-ready title/body and the serialized plan
    assert [e[0] for e in pushed] == ["task"]
    payload = pushed[0][1]
    assert payload["status"] == "completed"
    assert payload["title"] == "Task complete"
    assert payload["task_id"] == task.id
    assert payload["plan"]["task_id"] == task.id

    # And persisted as a chat message — the push channel has no queue
    assert any("Finished the background task" in m for m in await _messages(task_db, "s-bg-read"))


async def test_planning_failure_settles_task_failed(task_db, pushed):
    provider = FakeProvider([])  # every LLM call raises → plan FAILED
    task = await start_task(task_db, "do something", "s-bg-fail", provider=provider)
    await wait_for_task(task.id)
    await task_db.refresh(task)

    assert task.status == "failed"
    assert pushed[0][1]["status"] == "failed"
    assert pushed[0][1]["title"] == "Task failed"
    assert any("failed" in m for m in await _messages(task_db, "s-bg-fail"))


async def test_runner_crash_settles_failed_and_never_propagates(task_db, pushed, monkeypatch):
    class ExplodingPlanner:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def start(self, goal: str):
            raise RuntimeError("boom")

    monkeypatch.setattr(task_runner, "AgentPlanner", ExplodingPlanner)
    task = await start_task(task_db, "explode", "s-bg-crash", provider=FakeProvider([]))
    await wait_for_task(task.id)  # must not raise — the runner swallows its own crash
    await task_db.refresh(task)

    assert task.status == "failed"
    assert "The planner crashed" in task.message
    assert pushed[0][1]["status"] == "failed"


# ==================================================== approval pause + resume

async def test_write_task_pauses_parks_and_resumes_on_approval(task_db, pushed, tmp_path):
    target = tmp_path / "out.txt"
    steps = [step("Create out.txt", "create_file", path=str(target), content="hi")]
    provider = FakeProvider([plan_json(steps), plan_json(steps)])

    task = await start_task(task_db, "create my file", "s-bg-write", provider=provider)
    await wait_for_task(task.id)
    await task_db.refresh(task)

    # Paused: Task row mirrors it, the plan is PARKED (same store as inline
    # plans), nothing executed, and the pause was pushed with the
    # deterministic approval text
    assert task.status == "awaiting_approval"
    assert not target.exists()
    parked = await task_db.get(ParkedPlan, task.plan_id)
    assert parked is not None
    pause = pushed[0][1]
    assert pause["status"] == "awaiting_approval"
    assert pause["title"] == "Jarvis needs your approval"
    assert "needs your approval" in pause["body"]
    assert "Create out.txt" in pause["body"]  # the exact step, never paraphrased
    assert pause["plan"]["requires_approval"] is True
    assert pause["plan"]["task_id"] == task.id

    # Approve: what /api/agent/approve does for a task-owned plan
    plan = await pop_plan(task_db, task.plan_id)
    assert plan is not None and plan.task_id == task.id
    resumed = await resume_task_in_background(task_db, plan, provider)
    assert resumed is not None and resumed.status == "running"
    await wait_for_task(task.id)
    await task_db.refresh(task)

    assert task.status == "completed"
    assert target.read_text(encoding="utf-8") == "hi"
    assert pushed[-1][1]["status"] == "completed"

    # Consume-once still holds: the parked plan is gone for good
    assert await pop_plan(task_db, plan.id) is None


async def test_cancel_settles_task_without_push(task_db, pushed, tmp_path):
    target = tmp_path / "never.txt"
    steps = [step("Create never.txt", "create_file", path=str(target), content="no")]
    provider = FakeProvider([plan_json(steps), plan_json(steps)])

    task = await start_task(task_db, "create a file", "s-bg-cancel", provider=provider)
    await wait_for_task(task.id)
    await task_db.refresh(task)  # the runner's own session updated the row
    pause_pushes = len(pushed)

    # Cancel: what /api/agent/approve does on approved=False (inline, no LLM)
    plan = await pop_plan(task_db, task.plan_id)
    assert plan is not None
    plan = await AgentPlanner(task_db, FakeProvider([])).resume(plan, approved=False)
    await settle_cancelled_task(task_db, plan)
    await task_db.refresh(task)

    assert task.status == "cancelled"
    assert not target.exists()
    assert len(pushed) == pause_pushes  # the user cancelled — no notification
    assert any("Cancelled by the user" in m for m in await _messages(task_db, "s-bg-cancel"))


async def test_question_pause_pushes_and_answer_continues(task_db, pushed, tmp_path):
    the_file = tmp_path / "a.txt"
    the_file.write_text("real")
    question = {"text": "Which file did you mean?", "options": [str(the_file)]}
    provider = FakeProvider([plan_json([], question=question)])  # draft asks

    task = await start_task(task_db, "read my file", "s-bg-ask", provider=provider)
    await wait_for_task(task.id)
    await task_db.refresh(task)

    assert task.status == "awaiting_choice"
    pause = pushed[0][1]
    assert pause["title"] == "Jarvis has a question"
    assert "Which file did you mean?" in pause["body"]

    # Answer: what /api/agent/choose does for a task-owned plan
    plan = await pop_plan(task_db, task.plan_id)
    read_step = [step("Read a.txt", "read_file", path=str(the_file))]
    continue_provider = FakeProvider([plan_json(read_step)])  # revise after answer
    resumed = await answer_task_in_background(task_db, plan, str(the_file), continue_provider)
    assert resumed is not None
    await wait_for_task(task.id)
    await task_db.refresh(task)

    assert task.status == "completed"
    assert pushed[-1][1]["status"] == "completed"


# ============================================================ startup truth

async def test_fail_interrupted_tasks_reconciles_at_startup(task_db):
    # Killed mid-run: running → failed, with a persisted explanation
    running = Task(goal="organize downloads", session_id="s-int", status="running")
    # Paused with a LIVE parked row: survives (the parked plan is the truth)
    alive = Task(goal="alive", session_id="s-int", status="awaiting_approval", plan_id="plan-alive")
    # Paused but its parked row is GONE (expired/purged): can never be answered
    orphan = Task(goal="orphan", session_id="s-int", status="awaiting_choice", plan_id="plan-gone")
    task_db.add_all([running, alive, orphan])
    task_db.add(ParkedPlan(
        id="plan-alive", session_id="s-int", status="awaiting_approval",
        payload="{}", expires_at=utc_now() + timedelta(hours=1),
    ))
    await task_db.commit()

    await fail_interrupted_tasks(task_db)
    await task_db.refresh(running)
    await task_db.refresh(alive)
    await task_db.refresh(orphan)

    assert running.status == "failed"
    assert "interrupted" in running.message
    assert alive.status == "awaiting_approval"  # untouched
    assert orphan.status == "failed"
    assert "expired" in orphan.message
    persisted = await _messages(task_db, "s-int")
    assert any("interrupted" in m for m in persisted)
    assert any("expired" in m for m in persisted)


# ======================================================== the HTTP done-when

def sse_events(body: str) -> list[dict]:
    return [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]


def plan_events(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("type") == "plan"]


def streamed_text(events: list[dict]) -> str:
    return "".join(e.get("delta", "") for e in events if e.get("type") != "plan")


@pytest_asyncio.fixture
async def client(tmp_path_factory, monkeypatch):
    """HTTP client with the runner's SESSION_FACTORY pointed at the same
    file-backed DB the app sees — the background run outlives the request."""
    db_dir = tmp_path_factory.mktemp("bgtasks-http-db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_dir / 'http.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _get_db():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def _no_extraction(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr("app.api.chat._run_extraction", _no_extraction)
    monkeypatch.setattr(task_runner, "SESSION_FACTORY", factory)
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_qdrant] = lambda: None
    plan_store._PENDING_PLANS.clear()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
    plan_store._PENDING_PLANS.clear()
    await engine.dispose()


def use_provider(
    responses: Optional[List[str]] = None, streams: Optional[List[str]] = None
) -> FakeProvider:
    provider = FakeProvider(responses, streams)
    app.dependency_overrides[get_llm_provider] = lambda: provider
    return provider


async def test_background_intent_end_to_end_over_http(client, pushed, tmp_path):
    """The Part 5 done-when, minus the toast: background goal → immediate ack
    → pause pushed → approve over HTTP → completion pushed."""
    target = tmp_path / "notes.txt"
    steps = [step("Create notes.txt", "create_file", path=str(target), content="hi")]
    provider = use_provider(responses=["TASK", plan_json(steps), plan_json(steps)])

    response = await client.post("/chat/stream", json={
        "messages": [{"role": "user", "content":
                      f"create a file {target} saying hi and tell me when you're done"}],
        "session_id": "s-http-bg",
    })
    assert response.status_code == 200
    events = sse_events(response.text)

    # The turn ends with a deterministic ack — no plan chunk, no waiting
    assert plan_events(events) == []
    assert "in the background" in streamed_text(events)
    assert events[-1]["done"] is True

    tasks = (await client.get("/api/tasks")).json()
    assert len(tasks) == 1
    task_id = tasks[0]["id"]
    await task_runner.wait_for_task(task_id)

    # The intent phrase never reached the planner (prompts[0] is the
    # classifier; the draft prompt exists only once the detached run ran)
    assert "tell me when you're done" not in provider.prompts[1]

    paused = (await client.get(f"/api/tasks/{task_id}")).json()
    assert paused["status"] == "awaiting_approval"
    assert paused["goal"] == f"create a file {target} saying hi"  # phrase stripped
    assert not target.exists()
    assert pushed[-1][1]["status"] == "awaiting_approval"
    plan_id = paused["plan"]["id"]

    # Approve from the (pushed) card: the response is only a snapshot —
    # execution continues detached
    snapshot = (await client.post(
        "/api/agent/approve", json={"plan_id": plan_id, "approved": True}
    )).json()
    assert snapshot["status"] == "executing"
    assert snapshot["task_id"] == task_id

    await task_runner.wait_for_task(task_id)
    final = (await client.get(f"/api/tasks/{task_id}")).json()
    assert final["status"] == "completed"
    assert target.read_text(encoding="utf-8") == "hi"
    assert pushed[-1][1]["status"] == "completed"

    # The pause and the outcome are both in the session history — visible
    # even if no window ever caught the pushes
    messages = (await client.get("/chat/sessions/s-http-bg/messages")).json()
    contents = [m["content"] for m in messages]
    assert any("needs your approval" in c for c in contents)
    assert any("Finished the background task" in c for c in contents)


async def test_typed_chat_answer_keeps_task_in_background(client, pushed, tmp_path):
    """A typed reply to a background task's question must not pull the plan
    back into the chat turn — it acks and continues detached."""
    the_file = tmp_path / "a.txt"
    the_file.write_text("real")
    question = {"text": "Which file?", "options": [str(the_file)]}
    read_step = [step("Read a.txt", "read_file", path=str(the_file))]
    use_provider(responses=[
        "TASK",
        plan_json([], question=question),  # draft asks → background pause
        plan_json(read_step),              # revise after the typed answer
    ])

    response = await client.post("/chat/stream", json={
        "messages": [{"role": "user", "content": "read my file in the background"}],
        "session_id": "s-http-answer",
    })
    assert response.status_code == 200
    task_id = (await client.get("/api/tasks")).json()[0]["id"]
    await task_runner.wait_for_task(task_id)
    assert pushed[-1][1]["status"] == "awaiting_choice"

    # The next chat message is the answer — deterministic ack, no plan chunk
    response = await client.post("/chat/stream", json={
        "messages": [{"role": "user", "content": str(the_file)}],
        "session_id": "s-http-answer",
    })
    events = sse_events(response.text)
    assert plan_events(events) == []
    assert "continuing that task in the background" in streamed_text(events)

    await task_runner.wait_for_task(task_id)
    final = (await client.get(f"/api/tasks/{task_id}")).json()
    assert final["status"] == "completed"
    assert pushed[-1][1]["status"] == "completed"
