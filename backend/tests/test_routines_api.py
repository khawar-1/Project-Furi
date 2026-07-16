"""
Phase 6 Part 5 — /api/routines HTTP behavior.

Exercised over real HTTP (httpx ASGITransport, the test_settings_api.py
pattern). A StaticPool in-memory DB is shared by the request sessions AND the
background runner's own sessions (task_runner.SESSION_FACTORY), so POST
/{id}/run's detached Task sees the same database. No test touches the real
jarvis.db, arms a real timer, or hits an LLM/Google (the provider is a scripted
fake and planner_memory_context is stubbed).
"""
import json
from typing import AsyncIterator, List, Optional

import httpx
import pytest_asyncio
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.tools  # noqa: F401 — registers the real file/terminal tools
from app.agents import plan_store, task_runner
from app.agents.task_runner import wait_for_task
from app.core.dependencies import get_db, get_llm_provider, get_qdrant
from app.core.scheduler import JarvisScheduler
from app.db.database import Base
from app.providers.base import EmbeddingResponse, LLMMessage, LLMProvider, LLMResponse
from main import app


class FakeProvider(LLMProvider):
    def __init__(self, responses: Optional[List[str]] = None) -> None:
        self._responses = list(responses or [])

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model_name(self) -> str:
        return "fake-model"

    async def chat(self, messages, temperature: float = 0.7, max_tokens=None) -> LLMResponse:
        if not self._responses:
            raise AssertionError("FakeProvider.chat exhausted")
        return LLMResponse(content=self._responses.pop(0), model="fake-model", provider="fake")

    async def stream_chat(self, messages, temperature: float = 0.7, max_tokens=None) -> AsyncIterator[str]:
        yield ""

    async def embed(self, text: str) -> EmbeddingResponse:
        return EmbeddingResponse(embedding=[0.0] * 384, model="fake", provider="fake")


def _plan_json(steps: list) -> str:
    return json.dumps({"steps": steps, "unachievable_reason": None, "question": None})


@pytest_asyncio.fixture
async def client(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    monkeypatch.setattr("app.db.database.AsyncSessionLocal", factory)
    monkeypatch.setattr(task_runner, "SESSION_FACTORY", factory)
    # The run path builds memory context — stub it away (no fastembed/qdrant).
    async def _no_memory(*args, **kwargs):
        return ""
    monkeypatch.setattr("app.api.routines.planner_memory_context", _no_memory)

    # Isolate the app-wide scheduler (PUT /schedule arms a job) onto a throwaway
    # scheduler sharing this test's in-memory DB (the contacts-API rule) — no
    # real jarvis.db, no real timer.
    import app.core.scheduled_routines as sr
    import app.core.scheduler as scheduler_module
    sched = JarvisScheduler(session_factory=factory)
    monkeypatch.setattr(sr, "scheduler", sched)
    monkeypatch.setattr(scheduler_module, "scheduler", sched)
    sched.register_handler(sr.ROUTINE_JOB_KIND, sr._routine_job_handler)

    plan_store._PENDING_PLANS.clear()

    async def _get_db():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    provider = FakeProvider()
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_qdrant] = lambda: None
    app.dependency_overrides[get_llm_provider] = lambda: provider

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        c._provider = provider  # tests script it per-call
        yield c
    app.dependency_overrides.clear()
    plan_store._PENDING_PLANS.clear()
    await sched.shutdown()
    await engine.dispose()


# ==================================================================== CRUD

async def test_list_empty(client):
    r = await client.get("/api/routines")
    assert r.status_code == 200
    assert r.json() == []


async def test_create_and_list(client):
    r = await client.post(
        "/api/routines",
        json={"name": "Clean Desktop", "goal_template": "delete the .tmp files on my desktop"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Clean Desktop"
    assert body["normalized_name"] == "clean desktop"
    assert body["goal_template"] == "delete the .tmp files on my desktop"
    # utc_iso timestamps carry an explicit offset.
    assert body["created_at"].endswith("+00:00")

    listed = (await client.get("/api/routines")).json()
    assert len(listed) == 1 and listed[0]["id"] == body["id"]


async def test_create_upserts_on_name(client):
    a = (await client.post("/api/routines", json={"name": "cleanup", "goal_template": "old"})).json()
    b = (await client.post("/api/routines", json={"name": "Cleanup", "goal_template": "new"})).json()
    assert a["id"] == b["id"]
    assert b["goal_template"] == "new"
    assert len((await client.get("/api/routines")).json()) == 1


async def test_delete(client):
    created = (await client.post(
        "/api/routines", json={"name": "temp", "goal_template": "list my downloads"}
    )).json()
    r = await client.delete(f"/api/routines/{created['id']}")
    assert r.status_code == 200 and r.json() == {"deleted": True}
    assert (await client.get("/api/routines")).json() == []


async def test_create_rejects_empty(client):
    r = await client.post("/api/routines", json={"name": "", "goal_template": "x"})
    assert r.status_code == 422


# ===================================================================== run

async def test_run_missing_routine_404(client):
    r = await client.post("/api/routines/does-not-exist/run", json={})
    assert r.status_code == 404


async def test_run_starts_background_task(client, tmp_path):
    (tmp_path / "a.txt").write_text("x")
    goal = f"list the files in {tmp_path}"
    created = (await client.post(
        "/api/routines", json={"name": "list files", "goal_template": goal}
    )).json()

    # A read-only plan (list_directory) — draft + reflect, then it completes
    # with no approval needed.
    steps = [{"description": "List the files", "tool": "list_directory",
              "parameters": {"path": str(tmp_path)}}]
    client._provider._responses = [_plan_json(steps), _plan_json(steps)]

    r = await client.post(f"/api/routines/{created['id']}/run", json={"session_id": "s-run"})
    assert r.status_code == 200
    payload = r.json()
    assert payload["status"] == "running"
    task_id = payload["task_id"]

    await wait_for_task(task_id)
    task = (await client.get(f"/api/tasks/{task_id}")).json()
    assert task["status"] == "completed"
    assert task["goal"] == goal


# ================================================================ schedule

async def test_set_weekly_schedule(client):
    created = (await client.post(
        "/api/routines", json={"name": "weekly report", "goal_template": "compile the week"}
    )).json()
    r = await client.put(
        f"/api/routines/{created['id']}/schedule",
        json={"schedule_type": "weekly", "schedule_weekday": 4,
              "schedule_hour": 16, "schedule_minute": 0},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["schedule_type"] == "weekly" and body["schedule_weekday"] == 4
    assert body["next_run_at"] is not None and body["next_run_at"].endswith("+00:00")
    # schedule_job_id is internal plumbing — never serialized.
    assert "schedule_job_id" not in body

    listed = (await client.get("/api/routines")).json()
    assert listed[0]["schedule_type"] == "weekly"


async def test_clear_schedule(client):
    created = (await client.post(
        "/api/routines", json={"name": "digest", "goal_template": "make digest"}
    )).json()
    await client.put(
        f"/api/routines/{created['id']}/schedule",
        json={"schedule_type": "daily", "schedule_hour": 8},
    )
    r = await client.put(
        f"/api/routines/{created['id']}/schedule", json={"schedule_type": None}
    )
    assert r.status_code == 200
    assert r.json()["schedule_type"] is None
    assert r.json()["next_run_at"] is None


async def test_schedule_missing_routine_404(client):
    r = await client.put(
        "/api/routines/nope/schedule", json={"schedule_type": "daily", "schedule_hour": 8}
    )
    assert r.status_code == 404


async def test_schedule_invalid_type_400(client):
    created = (await client.post(
        "/api/routines", json={"name": "x", "goal_template": "g"}
    )).json()
    r = await client.put(
        f"/api/routines/{created['id']}/schedule", json={"schedule_type": "hourly"}
    )
    assert r.status_code == 400
