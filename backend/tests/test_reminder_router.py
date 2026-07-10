"""
Phase 4 Part 4 — reminder chat routing, exercised over real HTTP against
/chat/stream (mirrors test_task_router.py's approach). No LLM provider is
scripted for the reminder path itself — detection and confirmation text are
entirely deterministic — but the classifier/planner FakeProvider is still
wired in since /chat/stream depends on it for the fall-through paths.
"""
import json
from datetime import datetime, timedelta
from typing import AsyncIterator, List, Optional

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agents import plan_store
from app.api import reminder_router
from app.core.dependencies import get_db, get_llm_provider, get_qdrant
from app.core.reminders import REMINDER_JOB_KIND, _reminder_job_handler
from app.core.scheduler import scheduler as app_scheduler
from app.db.database import Base
from app.db.models import Reminder
from app.memory.conversation_state import (
    CONVERSATION_SESSIONS,
    ConversationSession,
    PendingCreation,
)
from app.providers.base import EmbeddingResponse, LLMMessage, LLMProvider, LLMResponse
from main import app


class FakeProvider(LLMProvider):
    def __init__(self, responses: Optional[List[str]] = None, streams: Optional[List[str]] = None) -> None:
        self._responses = list(responses or [])
        self._streams = list(streams or [])
        self.chat_calls = 0
        self.stream_calls = 0

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model_name(self) -> str:
        return "fake-model"

    async def chat(self, messages, temperature: float = 0.7, max_tokens=None) -> LLMResponse:
        self.chat_calls += 1
        if not self._responses:
            raise AssertionError("FakeProvider.chat exhausted")
        return LLMResponse(content=self._responses.pop(0), model="fake-model", provider="fake")

    async def stream_chat(self, messages, temperature: float = 0.7, max_tokens=None) -> AsyncIterator[str]:
        self.stream_calls += 1
        if not self._streams:
            raise AssertionError("FakeProvider.stream_chat exhausted")
        for word in self._streams.pop(0).split(" "):
            yield word + " "

    async def embed(self, text: str) -> EmbeddingResponse:
        return EmbeddingResponse(embedding=[0.0] * 384, model="fake", provider="fake")


def use_provider(responses=None, streams=None) -> FakeProvider:
    provider = FakeProvider(responses, streams)
    app.dependency_overrides[get_llm_provider] = lambda: provider
    return provider


def sse_events(body: str) -> list[dict]:
    return [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]


def streamed_text(events: list[dict]) -> str:
    return "".join(e.get("delta", "") for e in events)


async def post_chat(client, message: str, session_id: str) -> list[dict]:
    response = await client.post(
        "/chat/stream",
        json={"messages": [{"role": "user", "content": message}], "session_id": session_id},
    )
    assert response.status_code == 200
    return sse_events(response.text)


@pytest_asyncio.fixture
async def client(tmp_path_factory, monkeypatch):
    db_dir = tmp_path_factory.mktemp("reminder-router-db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_dir / 'reminder-router-test.db'}")
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
    monkeypatch.setattr("app.db.database.AsyncSessionLocal", factory)
    original_factory = app_scheduler._session_factory
    app_scheduler._session_factory = factory

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_qdrant] = lambda: None
    plan_store._PENDING_PLANS.clear()
    reminder_router.PENDING_REMINDERS.clear()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
    plan_store._PENDING_PLANS.clear()
    reminder_router.PENDING_REMINDERS.clear()
    app_scheduler._session_factory = original_factory
    try:
        app_scheduler._aps.remove_all_jobs()
    except Exception:
        pass
    await engine.dispose()


# ============================================================= happy path

async def test_reminder_request_schedules_and_confirms(client):
    events = await post_chat(client, "remind me in 20 minutes to call jamil", "s-rem")
    text = streamed_text(events)

    assert "call jamil" in text
    assert events[-1]["done"] is True

    # No LLM call at all — the reminder path is entirely deterministic.
    reminders = (await client.get("/api/reminders")).json()
    assert len(reminders) == 1
    assert reminders[0]["text"] == "call jamil"
    assert reminders[0]["status"] == "pending"
    assert reminders[0]["session_id"] == "s-rem"

    # Persisted like a normal chat turn.
    messages = (await client.get("/chat/sessions/s-rem/messages")).json()
    assert [m["role"] for m in messages] == ["user", "assistant"]


async def test_reminder_request_never_calls_the_classifier(client):
    # If this fell through to task routing, /chat/stream would need a
    # provider response for TASK/CHAT — none is scripted, so a stray call
    # would blow up with FakeProvider's "exhausted" assertion.
    use_provider(responses=[], streams=[])
    events = await post_chat(client, "remind me at 6pm to check the oven", "s-rem2")
    assert "check the oven" in streamed_text(events)


async def test_bare_hour_reminder_resolves_deterministically(client):
    events = await post_chat(client, "remind me at 6 to call jamil", "s-rem3")
    text = streamed_text(events)
    assert "call jamil" in text
    assert "Reminder set" in text


# ============================================================ ambiguous ask

async def test_ambiguous_reminder_asks_and_schedules_nothing(client):
    events = await post_chat(client, "remind me to call jamil", "s-amb")
    text = streamed_text(events)

    assert "what time" in text.lower()
    reminders = (await client.get("/api/reminders")).json()
    assert reminders == []


async def test_past_time_reminder_asks_and_schedules_nothing(client):
    events = await post_chat(client, "remind me on 2020-01-01 at 5pm to call jamil", "s-past")
    text = streamed_text(events)

    assert "already passed" in text.lower()
    assert (await client.get("/api/reminders")).json() == []


# ============================================== parked ask across two turns

async def test_time_reply_completes_a_parked_reminder(client):
    """The live bug of 2026-07-09: 'remind me to call mom' → 'what time?' →
    'in 5 mins' used to fall through to the LLM, which claimed 'Reminder
    set' while nothing was ever scheduled. The parked ask now owns the
    reply and actually creates the reminder."""
    sid = "s-mom"
    ask = streamed_text(await post_chat(client, "remind me to call mom", sid))
    assert "what time" in ask.lower()
    assert (await client.get("/api/reminders")).json() == []

    # No provider scripted — a stray LLM call would blow up FakeProvider.
    use_provider(responses=[], streams=[])
    confirm = streamed_text(await post_chat(client, "in 5 mins", sid))
    assert "Reminder set" in confirm
    assert "call mom" in confirm

    reminders = (await client.get("/api/reminders")).json()
    assert len(reminders) == 1
    assert reminders[0]["text"] == "call mom"
    assert reminders[0]["status"] == "pending"
    assert reminders[0]["session_id"] == sid


async def test_bare_clock_reply_completes_a_parked_reminder(client):
    sid = "s-bare"
    await post_chat(client, "remind me to water the plants", sid)
    confirm = streamed_text(await post_chat(client, "6pm", sid))
    assert "Reminder set" in confirm
    assert (await client.get("/api/reminders")).json()[0]["text"] == "water the plants"


async def test_unrecognizable_reply_reasks_and_keeps_the_park(client):
    sid = "s-reask"
    await post_chat(client, "remind me to call mom", sid)

    use_provider(responses=[], streams=[])  # owning the reply means no LLM
    text = streamed_text(await post_chat(client, "yeah sounds good", sid))
    assert "still need a time" in text.lower()
    assert (await client.get("/api/reminders")).json() == []

    confirm = streamed_text(await post_chat(client, "in 10 minutes", sid))
    assert "Reminder set" in confirm
    assert len((await client.get("/api/reminders")).json()) == 1


async def test_task_shaped_reply_escapes_the_parked_ask(client):
    """Live bug 2026-07-10: with 'what time?' parked, the user pivoted to a
    full file task — the park swallowed it and re-asked for a time, trapping
    them until 'cancel'. A reply that isn't a time but fires the task gate
    now flows down the normal task/chat path; the question stays parked so
    a later bare time still completes it."""
    sid = "s-pivot"
    ask = streamed_text(await post_chat(client, "remind me to call mom", sid))
    assert "what time" in ask.lower()

    # The pivot fires the task gate → one classifier call (scripted CHAT so
    # the fall-through is a plain chat stream, not a scripted planner).
    provider = use_provider(responses=["CHAT"], streams=["Here are your files."])
    text = streamed_text(await post_chat(client, "list all the files in my desktop", sid))
    assert "still need a time" not in text.lower()
    assert provider.stream_calls == 1  # the reply reached the normal path

    # The park survived the pivot — a bare time still completes it.
    use_provider(responses=[], streams=[])
    confirm = streamed_text(await post_chat(client, "in 10 minutes", sid))
    assert "Reminder set" in confirm
    assert (await client.get("/api/reminders")).json()[0]["text"] == "call mom"


async def test_completion_conditioned_remind_me_never_parks(client):
    """The other half of the 2026-07-10 live bug: '…after doing all this
    remind me' is background-task intent (the completion push IS the
    telling), never a timed reminder — this router must fall through
    without parking anything."""
    sid = "s-cond"
    goal = (
        "hey list me all the files in the desktop and then delete all files "
        "in phase3test, and after deleting all files create a file "
        "'test.txt' in phase3test, after doing all this remind me"
    )
    # Falls through to task routing (gate fires on files/desktop); the
    # scripted CHAT verdict keeps the rest of the turn a plain chat stream.
    use_provider(responses=["CHAT"], streams=["ok"])
    text = streamed_text(await post_chat(client, goal, sid))
    assert "what time" not in text.lower()
    assert reminder_router.PENDING_REMINDERS == {}
    assert (await client.get("/api/reminders")).json() == []


async def test_cancel_word_drops_the_parked_ask(client):
    sid = "s-drop"
    await post_chat(client, "remind me to call mom", sid)
    text = streamed_text(await post_chat(client, "never mind", sid))
    assert "won't set" in text
    assert (await client.get("/api/reminders")).json() == []

    # The park is gone — the next message flows to normal chat again.
    provider = use_provider(streams=["Sure thing!"])
    events = await post_chat(client, "how are you", sid)
    assert provider.stream_calls == 1
    assert "Sure thing" in streamed_text(events)


async def test_task_reply_completes_a_time_only_park(client):
    sid = "s-task-half"
    ask = streamed_text(await post_chat(client, "remind me in 20 minutes", sid))
    assert "what should i remind you" in ask.lower()

    confirm = streamed_text(await post_chat(client, "to call mom", sid))
    assert "Reminder set" in confirm
    reminders = (await client.get("/api/reminders")).json()
    assert reminders[0]["text"] == "call mom"


async def test_repeated_trigger_reply_merges_with_the_parked_text(client):
    # Answering "what time?" with the trigger repeated ("remind me in 5
    # mins") must complete the parked 'call mom', not restart from scratch.
    sid = "s-merge"
    await post_chat(client, "remind me to call mom", sid)
    confirm = streamed_text(await post_chat(client, "remind me in 5 mins", sid))
    assert "Reminder set" in confirm
    assert (await client.get("/api/reminders")).json()[0]["text"] == "call mom"


async def test_due_at_is_serialized_with_utc_offset(client):
    """Regression: naive-UTC due_at serialized without a timezone marker
    made the frontend's `new Date()` read it as local time — every due
    time in the panel shifted by the machine's UTC offset."""
    await post_chat(client, "remind me in 20 minutes to call jamil", "s-tz")
    reminder = (await client.get("/api/reminders")).json()[0]
    assert reminder["due_at"].endswith("+00:00")
    assert reminder["created_at"].endswith("+00:00")


# ==================================================== fail-open elsewhere

async def test_non_reminder_message_falls_through_to_chat(client):
    provider = use_provider(streams=["Doing well, thanks!"])
    events = await post_chat(client, "how are you today", "s-plain")

    assert provider.chat_calls == 0
    assert provider.stream_calls == 1
    assert "Doing well" in streamed_text(events)
    assert (await client.get("/api/reminders")).json() == []


async def test_reminder_phrase_never_hijacks_a_parked_memory_question(client):
    sid = "s-parked"
    sess = ConversationSession()
    sess.pending_creation = PendingCreation(name="Daud")
    CONVERSATION_SESSIONS[sid] = sess
    try:
        provider = use_provider(streams=["About Daud — should I add them?"])
        events = await post_chat(client, "remind me at 6 to ask about daud", sid)
        assert provider.chat_calls == 0  # not classified, not reminder-routed
        assert (await client.get("/api/reminders")).json() == []
    finally:
        CONVERSATION_SESSIONS.pop(sid, None)


# =================================================== cancel + end-to-end fire

async def test_cancel_reminder_via_api(client):
    await post_chat(client, "remind me in 20 minutes to call jamil", "s-cancel")
    reminders = (await client.get("/api/reminders")).json()
    reminder_id = reminders[0]["id"]

    cancelled = (await client.delete(f"/api/reminders/{reminder_id}")).json()
    assert cancelled == {"cancelled": True}

    again = (await client.delete(f"/api/reminders/{reminder_id}")).json()
    assert again == {"cancelled": False}

    listed = (await client.get("/api/reminders", params={"status": "cancelled"})).json()
    assert [r["id"] for r in listed] == [reminder_id]


async def test_reminder_end_to_end_fire_delivers_push_and_chat_message(client, monkeypatch):
    """The phase's 'moment', minus the native toast (Part 3, Electron-only):
    a reminder request schedules a job; firing it pushes an event AND drops
    a chat message into the session — the part that survives a closed
    window, since the push channel itself has no queue."""
    app_scheduler.register_handler(REMINDER_JOB_KIND, _reminder_job_handler)

    events = await post_chat(client, "remind me in 20 minutes to call jamil", "s-fire")
    reminder_id = (await client.get("/api/reminders")).json()[0]["id"]

    await app_scheduler._fire((await client.get("/api/reminders")).json()[0]["job_id"])

    fired = (await client.get("/api/reminders", params={"status": "fired"})).json()
    assert [r["id"] for r in fired] == [reminder_id]

    messages = (await client.get("/chat/sessions/s-fire/messages")).json()
    assert [m["role"] for m in messages] == ["user", "assistant", "assistant"]
    assert "call jamil" in messages[-1]["content"]


# ======================================================== multiple reminders

async def test_multi_reminder_message_creates_every_reminder(client):
    """The 2026-07-09 live bug, verbatim: 'set reminders …' (plural) used to
    miss the trigger, fall to the LLM, and get a fabricated double
    confirmation with ZERO reminders created. Now both are real — and the
    unscripted provider proves no LLM was consulted."""
    use_provider(responses=[], streams=[])

    events = await post_chat(
        client,
        "set reminders for calling ceo at 6 04 pm and a reminder for meeting with cto at 7",
        "s-multi",
    )
    text = streamed_text(events)
    assert "calling ceo" in text
    assert "meeting with cto" in text
    assert text.count("6:04") == 1

    reminders = (await client.get("/api/reminders")).json()
    texts = sorted(r["text"] for r in reminders)
    assert texts == ["calling ceo", "meeting with cto"]
    assert all(r["status"] == "pending" for r in reminders)
    assert all(r["job_id"] for r in reminders)


async def test_multi_reminder_with_relative_anchor(client):
    use_provider(responses=[], streams=[])

    events = await post_chat(
        client,
        "set reminder for calling friend at 6:01 pm and for calling dad 30 mins after it",
        "s-anchor",
    )
    text = streamed_text(events)
    assert "calling friend" in text
    assert "calling dad" in text

    reminders = (await client.get("/api/reminders")).json()
    by_text = {r["text"]: r for r in reminders}
    assert set(by_text) == {"calling friend", "calling dad"}
    friend = datetime.fromisoformat(by_text["calling friend"]["due_at"])
    dad = datetime.fromisoformat(by_text["calling dad"]["due_at"])
    assert dad - friend == timedelta(minutes=30)


async def test_two_reminders_at_the_same_time_both_exist_and_fire(client):
    """Same due time is not a conflict: each reminder is its own row and its
    own scheduled job, so both fire independently."""
    app_scheduler.register_handler(REMINDER_JOB_KIND, _reminder_job_handler)
    use_provider(responses=[], streams=[])

    await post_chat(client, "remind me in 20 minutes to call mom", "s-same")
    await post_chat(client, "remind me in 20 minutes to take pills", "s-same")

    reminders = (await client.get("/api/reminders")).json()
    assert len(reminders) == 2
    assert len({r["job_id"] for r in reminders}) == 2  # two independent jobs

    for r in reminders:
        await app_scheduler._fire(r["job_id"])

    fired = (await client.get("/api/reminders", params={"status": "fired"})).json()
    assert len(fired) == 2
