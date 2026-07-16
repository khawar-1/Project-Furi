"""
Phase 6 Part 5 — Teachable routines: normalization, CRUD, parse triggers,
replan-fresh-keeps-approval, and the offer-to-save threshold.

Unit level throughout: the domain module (app/core/routines.py) and the
router's pure parse helpers (app/api/routine_router.py) are exercised
directly; the replan-fresh invariant is proven by starting a routine's
goal_template as a real background Task and asserting the fresh plan pauses
at the approval gate (the stored STRING is never a pre-approved plan).
"""
import json
from typing import AsyncIterator, List, Optional

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.tools  # noqa: F401 — registers the real file/terminal tools
from app.agents import plan_store, task_runner
from app.agents.task_runner import start_task, wait_for_task
from app.api.routine_router import (
    _capture_prior_goal,
    _match_run_name,
    _match_teach,
)
from app.core import routines
from app.core.routines import (
    ROUTINE_OFFER_THRESHOLD,
    count_matching_goals,
    create_routine,
    delete_routine,
    get_routine_by_name,
    list_routines,
    maybe_offer_routine,
    normalize_name,
)
from app.db.database import Base
from app.db.models import Message, ParkedPlan, Routine, Task
from app.db.schemas import ChatRequest
from app.providers.base import EmbeddingResponse, LLMMessage, LLMProvider, LLMResponse


# ================================================================ normalization

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Clean Desktop", "clean desktop"),
        ('"Clean Desktop"', "clean desktop"),
        ("clean-desktop!", "clean desktop"),
        ("  clean   desktop  ", "clean desktop"),
        ("Morning Routine", "morning routine"),
        ("‘quoted’", "quoted"),
        ("", ""),
    ],
)
def test_normalize_name(raw, expected):
    assert normalize_name(raw) == expected


def test_normalize_collapses_equivalent_forms():
    assert normalize_name("Clean Desktop") == normalize_name("clean  desktop!")


# ======================================================================= CRUD

async def test_create_and_get_routine(db_session):
    r = await create_routine(db_session, "Clean Desktop", "delete the .tmp files on my desktop")
    assert r.normalized_name == "clean desktop"
    assert r.name == "Clean Desktop"
    assert r.is_active is True

    # Normalized lookup — different casing/punctuation resolves the same row.
    found = await get_routine_by_name(db_session, "clean  DESKTOP!")
    assert found is not None and found.id == r.id


async def test_create_routine_upserts_not_duplicates(db_session):
    first = await create_routine(db_session, "clean desktop", "old goal")
    again = await create_routine(db_session, "Clean Desktop", "new goal")

    assert again.id == first.id  # re-teach, not a duplicate
    assert again.goal_template == "new goal"
    assert len(await list_routines(db_session)) == 1


async def test_delete_routine(db_session):
    r = await create_routine(db_session, "temp", "list my downloads")
    assert await delete_routine(db_session, r.id) is True
    assert await get_routine_by_name(db_session, "temp") is None
    assert await delete_routine(db_session, r.id) is False  # already gone


# ============================================================== TEACH parsing

@pytest.mark.parametrize(
    "message,expected_name",
    [
        ('save this as a routine called "clean desktop"', "clean desktop"),
        ("save this as a routine called clean desktop", "clean desktop"),
        ("remember this as a routine named morning cleanup", "morning cleanup"),
        ("save this routine as weekly report", "weekly report"),
        ("remember this as 'clean desktop'", "clean desktop"),
    ],
)
def test_match_teach_extracts_name(message, expected_name):
    result = _match_teach(message)
    assert result is not None
    assert result[0] == expected_name


def test_match_teach_splits_inline_goal():
    result = _match_teach('save this as a routine called cleanup that: delete the tmp files')
    assert result is not None
    name, inline, spec = result
    assert name == "cleanup"
    assert inline == "delete the tmp files"
    assert spec is None  # no recurrence phrase


def test_match_teach_extracts_recurrence():
    result = _match_teach('save this as a routine called weekly report that runs every friday at 4pm')
    assert result is not None
    name, inline, spec = result
    assert name == "weekly report"
    assert inline is None
    assert spec is not None and spec.schedule_type == "weekly" and spec.schedule_weekday == 4


@pytest.mark.parametrize(
    "message",
    [
        "remember that the meeting is at 3",
        "save the file to my desktop",
        "what routines do I have",
        "let's clean the desktop",
    ],
)
def test_match_teach_ignores_non_teach(message):
    assert _match_teach(message) is None


# ================================================================ RUN parsing

@pytest.mark.parametrize(
    "message,expected_name",
    [
        ("run my clean desktop routine", "clean desktop"),
        ("run the morning routine", "morning"),
        ("run routine clean desktop", "clean desktop"),
        ("run my routine called weekly report", "weekly report"),
    ],
)
def test_match_run_name(message, expected_name):
    assert _match_run_name(message) == expected_name


@pytest.mark.parametrize(
    "message",
    ["run the build script", "delete my temp files", "run pytest"],
)
def test_match_run_ignores_non_routine(message):
    assert _match_run_name(message) is None


# =================================================== goal capture from history

def _req(*turns: tuple[str, str], session_id: str = "s") -> ChatRequest:
    return ChatRequest(
        messages=[{"role": role, "content": content} for role, content in turns],
        session_id=session_id,
    )


def test_capture_prior_goal_finds_task_shaped_turn():
    req = _req(
        ("user", "delete all the .tmp files in my Downloads folder"),
        ("assistant", "Finished the background task. Done — 1 step completed."),
        ("user", 'save this as a routine called cleanup'),
    )
    assert _capture_prior_goal(req) == "delete all the .tmp files in my Downloads folder"


def test_capture_prior_goal_none_when_no_task_turn():
    req = _req(
        ("user", "hi there"),
        ("user", 'save this as a routine called cleanup'),
    )
    assert _capture_prior_goal(req) is None


# ============================================ replan-fresh keeps the approval

class _FakeProvider(LLMProvider):
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
async def task_db(tmp_path_factory, monkeypatch):
    """File-backed DB shared by the test session AND the runner's own sessions
    (SESSION_FACTORY), like test_background_tasks.py."""
    db_dir = tmp_path_factory.mktemp("routines-db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_dir / 'routines.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(task_runner, "SESSION_FACTORY", factory)
    plan_store._PENDING_PLANS.clear()

    async def _noop_push(*args, **kwargs):
        return 1

    monkeypatch.setattr(task_runner, "push", _noop_push)
    async with factory() as session:
        yield session
    plan_store._PENDING_PLANS.clear()
    await engine.dispose()


async def test_running_a_routine_replans_fresh_and_pauses_for_approval(task_db, tmp_path):
    """The invariant that makes routines safe: we stored a GOAL STRING, so a
    destructive routine re-plans from scratch and pauses at the approval gate —
    a routine can never smuggle a pre-approved write past the gate."""
    target = tmp_path / "out.txt"
    goal = f"create a file at {target}"
    routine = await create_routine(task_db, "make my file", goal)

    steps = [{"description": "Create out.txt", "tool": "create_file",
              "parameters": {"path": str(target), "content": "hi"}}]
    provider = _FakeProvider([_plan_json(steps), _plan_json(steps)])  # draft, reflect

    task = await start_task(task_db, routine.goal_template, "s-routine-run", provider=provider)
    await wait_for_task(task.id)
    await task_db.refresh(task)

    assert task.status == "awaiting_approval"
    assert not target.exists()  # nothing ran — write steps need approval
    assert await task_db.get(ParkedPlan, task.plan_id) is not None


# ====================================================== offer-to-save threshold

@pytest.fixture
def captured_push(monkeypatch):
    events: list[tuple[str, dict]] = []

    async def _push(event_type, payload=None):
        events.append((event_type, payload or {}))
        return 1

    monkeypatch.setattr(routines, "push", _push)
    return events


async def _seed_completed_tasks(db, goal: str, n: int) -> None:
    for _ in range(n):
        db.add(Task(goal=goal, session_id="s-hist", status="completed"))
    await db.commit()


async def test_count_matching_goals_normalizes(db_session):
    await _seed_completed_tasks(db_session, "Clean my Desktop", 2)
    db_session.add(Task(goal="clean  my desktop!", session_id="s", status="completed"))
    db_session.add(Task(goal="something else", session_id="s", status="completed"))
    await db_session.commit()
    assert await count_matching_goals(db_session, "clean my desktop") == 3


async def test_offer_fires_at_threshold_once(db_session, captured_push):
    goal = "clean my desktop"
    await _seed_completed_tasks(db_session, goal, ROUTINE_OFFER_THRESHOLD)

    assert await maybe_offer_routine(db_session, goal, "s-hist") is True
    assert [e[0] for e in captured_push] == ["routine_offer"]
    # A chat Message was persisted (durable copy).
    msgs = (await db_session.execute(
        select(Message).where(Message.session_id == "s-hist")
    )).scalars().all()
    assert any("reusable routine" in m.content for m in msgs)

    # Throttled: a second completion of the SAME goal never re-offers.
    assert await maybe_offer_routine(db_session, goal, "s-hist") is False
    assert len(captured_push) == 1


async def test_no_offer_below_threshold(db_session, captured_push):
    goal = "rare goal"
    await _seed_completed_tasks(db_session, goal, ROUTINE_OFFER_THRESHOLD - 1)
    assert await maybe_offer_routine(db_session, goal, "s-hist") is False
    assert captured_push == []


async def test_no_offer_when_routine_already_exists(db_session, captured_push):
    goal = "clean my desktop"
    await _seed_completed_tasks(db_session, goal, ROUTINE_OFFER_THRESHOLD)
    await create_routine(db_session, "clean my desktop", goal)
    assert await maybe_offer_routine(db_session, goal, "s-hist") is False
    assert captured_push == []


async def test_no_offer_without_session(db_session, captured_push):
    goal = "clean my desktop"
    await _seed_completed_tasks(db_session, goal, ROUTINE_OFFER_THRESHOLD)
    assert await maybe_offer_routine(db_session, goal, None) is False
    assert captured_push == []
