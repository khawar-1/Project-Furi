"""
Part 6 — Chat task routing. The deterministic gate is unit-tested as a pure
function; everything else runs over real HTTP against /chat/stream with only
the DB, Qdrant, and LLM provider overridden — so the router, the planner,
the tools, and the untouched Phase 2 chat path all execute for real.

The invariant under test everywhere: a message that is NOT a confirmed task
flows into the Phase 2 path exactly as before (fail-open), and a message
that IS a task streams a {"type": "plan"} chunk plus readable text.
"""
import json
from typing import AsyncIterator, List, Optional

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.tools  # noqa: F401 — registers the real file/terminal tools
from app.agents import plan_store
from app.api.chat import _SYSTEM_VOICE_RE
from app.api.task_router import (
    _classify_message,
    conversation_context,
    looks_like_task,
    wants_background,
)
from app.core.dependencies import get_db, get_llm_provider, get_qdrant
from app.db.database import Base
from app.memory.conversation_state import (
    CONVERSATION_SESSIONS,
    ConversationSession,
    PendingCreation,
)
from app.providers.base import (
    EmbeddingResponse,
    LLMMessage,
    LLMProvider,
    LLMResponse,
)
from main import app


class FakeProvider(LLMProvider):
    """chat() pops scripted responses; stream_chat() pops scripted streams."""

    def __init__(
        self,
        responses: Optional[List[str]] = None,
        streams: Optional[List[str]] = None,
    ) -> None:
        self._responses = list(responses or [])
        self._streams = list(streams or [])
        self.chat_calls = 0
        self.stream_calls = 0
        self.prompts: List[str] = []         # first message of every chat() call
        self.stream_prompts: List[str] = []  # first message of every stream_chat() call

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
        self.stream_calls += 1
        self.stream_prompts.append(messages[0].content)
        if not self._streams:
            raise AssertionError(f"FakeProvider.stream_chat exhausted after {self.stream_calls - 1}")
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


def use_provider(
    responses: Optional[List[str]] = None, streams: Optional[List[str]] = None
) -> FakeProvider:
    provider = FakeProvider(responses, streams)
    app.dependency_overrides[get_llm_provider] = lambda: provider
    return provider


def sse_events(body: str) -> list[dict]:
    return [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]


def plan_events(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("type") == "plan"]


def streamed_text(events: list[dict]) -> str:
    return "".join(e.get("delta", "") for e in events if e.get("type") != "plan")


async def post_chat(client, message: str, session_id: str) -> list[dict]:
    response = await client.post(
        "/chat/stream",
        json={"messages": [{"role": "user", "content": message}], "session_id": session_id},
    )
    assert response.status_code == 200
    return sse_events(response.text)


@pytest_asyncio.fixture
async def client(tmp_path_factory, monkeypatch):
    """HTTP client against the real app: file-backed DB, no Qdrant, and the
    Phase 2 background extraction stubbed out (it would call the exhausted
    FakeProvider against the real dev database)."""
    db_dir = tmp_path_factory.mktemp("router-db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_dir / 'router-test.db'}")
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
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_qdrant] = lambda: None
    plan_store._PENDING_PLANS.clear()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
    plan_store._PENDING_PLANS.clear()
    await engine.dispose()


# ======================================================= the deterministic gate

@pytest.mark.parametrize("message", [
    "delete old.log",
    "create a file called notes.txt on my desktop",
    "run npm install in the frontend folder",
    "move report.pdf into Documents",
    "list the files in my downloads folder",
    "search my desktop for tekken saves",
    "can you clean up the temp files",
    "rename holiday.jpg to beach.jpg",
    # Shell-style abbreviations — "del" missed the gate and the message fell
    # open to plain chat, which fabricated a deletion (live bug 2026-07-09)
    "please del all files with '.txt' extension from this folder",
    "rm the tmp files in my downloads folder",
    # Recall-first: verbs nobody can enumerate — the OBJECT noun fires the
    # gate alone and the classifier judges the wording
    "please yeet all files with '.txt' extension from this folder",
    "get rid of the txt files in the phase3test folder",
    "wipe out everything in my temp folder",
    "put every pdf from my desktop into one place",
    "can you take care of the mess in my downloads",
    # Email / calendar object-nouns fire the gate alone (Phase 5, Part 5) —
    # the multi-class classifier makes the EMAIL/CALENDAR/CHAT call.
    "email jamil about the dinner tonight",
    "any new emails in my inbox?",
    "check my gmail",
    "reply to that email with a yes",
    # "mail"/"e-mail" nouns + send/reply/forward verbs (fix 2026-07-12 — these
    # missed the gate and the message fell to plain chat, which then imitated a
    # real approval message)
    "now send a new mail to that same address",
    "send jamil a mail saying i'll be late",
    "e-mail ali the report when you can",
    "forward that email to my manager",
    "put a meeting with jamil on my calendar tomorrow at 3",
    "what's on my calendar this week",
    "delete the standup event",
    "send jamil an invite for friday",
    # Web domain (Phase 6 Part 1) — strong web nouns / a bare URL fire alone.
    "search the web for the latest langgraph release",
    "look this up online",
    "open https://example.com and summarize it",
    "find a good pizza recipe on the internet",
])
def test_gate_fires_for_task_messages(message):
    assert looks_like_task(message) is True


@pytest.mark.parametrize("message", [
    "how are you today",
    "jamil and i played tekken yesterday",
    "my sister is a doctor in lahore",
    "what's my brother's name?",
    "i want to learn guitar",
    "tell me a joke",
    "yes",
    "i meant jamil ali",
])
def test_gate_stays_closed_for_conversation(message):
    assert looks_like_task(message) is False


def test_gate_fires_on_tell_and_count_questions():
    # "tell me how many files…" is a real tool task (list_directory) — the
    # gate must pass it to the classifier (live bug, 2026-07-09).
    assert looks_like_task("tell me how many files are in phase3test folder") is True
    assert looks_like_task("count the files on my desktop") is True


def test_gate_false_positives_exist_by_design():
    # These DO fire the gate — the LLM confirmation stage exists to catch them.
    assert looks_like_task("my brother deleted my save file yesterday") is True
    assert looks_like_task("i finally organized my desktop") is True
    # Recall-first: noun-only conversation fires too (strong noun, no verb) —
    # the cost is one tiny temp-0 call that answers CHAT, by design
    assert looks_like_task("i sent him the files yesterday") is True


def test_gate_weak_signals_still_need_a_verb():
    # Weak domain signals (URLs, "e.g.", decimals, media nouns) alone must
    # NOT fire — else every message with a link costs a classifier call.
    assert looks_like_task("check out youtube.com/watch for the trailer") is True  # verb "check"
    assert looks_like_task("that video was e.g. 3.5 hours long") is False
    assert looks_like_task("i love music and videos") is False


async def test_unknown_verb_phrasing_reaches_the_approval_gate(client, tmp_path):
    """End to end: wording no verb list could anticipate ("get rid of") still
    routes to the planner because it names its object — and the destructive
    step pauses at the structural approval gate; nothing runs."""
    victim = tmp_path / "notes.txt"
    victim.write_text("x")
    steps = [step("Delete notes.txt", "delete_file", path=str(victim))]
    use_provider(responses=["TASK", plan_json(steps), plan_json(steps)])

    events = await post_chat(
        client, f"get rid of the txt files in {tmp_path}", "s-recall-e2e"
    )
    plan = plan_events(events)[0]["plan"]
    assert plan["status"] == "awaiting_approval"
    assert victim.exists()  # nothing was deleted without approval


# ===================================================== multi-class classifier

@pytest.mark.parametrize("reply,expected", [
    ("TASK", "TASK"),
    ("EMAIL", "EMAIL"),
    ("CALENDAR", "CALENDAR"),
    ("WEB", "WEB"),
    ("CHAT", "CHAT"),
    # Real models append stray text/punctuation — startswith parsing handles it.
    ("EMAIL.", "EMAIL"),
    ("calendar", "CALENDAR"),
    ("web", "WEB"),
    ("  TASK\n", "TASK"),
])
async def test_classify_message_returns_label(reply, expected):
    provider = FakeProvider([reply])
    label = await _classify_message(provider, "some message", "")
    assert label == expected
    assert provider.chat_calls == 1  # still exactly one temp-0 call


@pytest.mark.parametrize("reply", ["I think this is email", "", "unsure", "yes"])
async def test_classify_message_unrecognized_fails_open_to_chat(reply):
    # An unrecognized word is NOT an action label — fail open to CHAT, exactly
    # as an exception would.
    provider = FakeProvider([reply])
    assert await _classify_message(provider, "some message", "") == "CHAT"


async def test_classify_message_exception_fails_open_to_chat():
    provider = FakeProvider([])  # chat() raises AssertionError when exhausted
    assert await _classify_message(provider, "some message", "") == "CHAT"


async def test_email_intent_routes_to_planner(client, tmp_path):
    """End to end: an EMAIL classification routes to the SAME planner (a plan
    chunk is streamed), and a send step pauses at the structural approval gate
    — the recipient lock means nothing leaves the machine unapproved. The
    address is in the user's own words, so the recipient-grounding guard
    passes (that guard is exercised in test_email_tools.py)."""
    steps = [step(
        "Email jamil@example.com about dinner", "send_email",
        to="jamil@example.com", subject="Dinner", body="Dinner tonight?",
    )]
    use_provider(responses=["EMAIL", plan_json(steps), plan_json(steps)])

    events = await post_chat(
        client, "email jamil@example.com about dinner tonight", "s-email-e2e"
    )
    plan = plan_events(events)[0]["plan"]
    assert plan["status"] == "awaiting_approval"


async def test_chat_label_yields_no_plan_chunk(client):
    """A CHAT classification falls open to the Phase 2 chat path — no plan
    chunk, just streamed conversation text."""
    # Gate fires on "email", classifier answers CHAT, then Phase 2 streams.
    use_provider(responses=["CHAT"], streams=["I can help with that — just say the word."])
    events = await post_chat(client, "i got an email from jamil yesterday", "s-chat-open")
    assert plan_events(events) == []


# ================================================ impersonation guard (email/cal)

@pytest.mark.parametrize("text", [
    "Email sent — to jamil@example.com.",
    "Your email has been sent to Jamil.",
    "I've sent the email to Jamil about dinner.",
    "Done. The draft was saved to your drafts.",
    "Event created — Dinner with Jamil, tomorrow 7pm.",
    "The event has been created on your calendar.",
    "I've added the meeting to your calendar for 3pm.",
    "I added it to your calendar.",
    "The invite was sent to the team.",
])
def test_impersonation_guard_catches_email_calendar_fabrications(text):
    assert _SYSTEM_VOICE_RE.search(text) is not None


@pytest.mark.parametrize("text", [
    # Capability statements must NOT trip the guard — a routing miss should ask
    # the user to rephrase, not get cut.
    "I can send an email for you — just say 'email Jamil about dinner'.",
    "Would you like me to create a calendar event for that meeting?",
    "I can add that meeting to your calendar if you tell me the time.",
    "Do you want me to email Jamil about it?",
])
def test_impersonation_guard_allows_capability_statements(text):
    assert _SYSTEM_VOICE_RE.search(text) is None


# ========================================================== background intent

@pytest.mark.parametrize("goal,cleaned", [
    ("organize my downloads folder and tell me when you're done",
     "organize my downloads folder"),
    ("organize my downloads folder and tell me when you are done",
     "organize my downloads folder"),
    ("clean up my temp files, let me know when it's finished",
     "clean up my temp files"),
    ("sort my desktop and notify me when done", "sort my desktop"),
    ("run the cleanup in the background", "run the cleanup"),
    ("delete the old logs as a background task", "delete the old logs"),
    ("please tell me when it's done — organize my downloads",
     "organize my downloads"),
    # "remind me when you're done" is background intent with a different
    # verb — the reminder router used to hijack it and ask "what time?"
    # (live bug, 2026-07-09).
    ("hey tell me how many files are in phase3test folder and tell me there names "
     "and remind me when you are done",
     "hey tell me how many files are in phase3test folder and tell me there names"),
    # The user's exact live message, comma spacing and all (2026-07-09).
    ("hey tell me how many files are in phase3test folder , and also tell me "
     "there names and remind me when you are done",
     "hey tell me how many files are in phase3test folder , and also tell me "
     "there names"),
    ("organize my downloads and remind me once it's finished",
     "organize my downloads"),
    ("count my desktop files and let me know after you're done",
     "count my desktop files"),
    # The condition can precede the verb — "after doing all this remind me"
    # (the user's exact live message, 2026-07-10: the reminder router
    # hijacked it and parked "What time should I remind you?").
    ("hey list me all the files in the desktop and then delete all files in "
     "phase3test, and after deleting all files create a file 'test.txt' in "
     "phase3test, after doing all this remind me",
     "hey list me all the files in the desktop and then delete all files in "
     "phase3test, and after deleting all files create a file 'test.txt' in "
     "phase3test"),
    ("run the tests, once everything is done let me know", "run the tests"),
    ("sort my desktop and when you're done tell me", "sort my desktop"),
])
def test_background_intent_detected_and_stripped(goal, cleaned):
    background, rest = wants_background(goal)
    assert background is True
    assert rest == cleaned


@pytest.mark.parametrize("goal", [
    "tell me when the file was created",       # a question, not intent
    "delete my temp files",                    # plain task
    "list the files when the download is done",  # subject isn't you/it/this
    "change my background image to blue",      # "background" ≠ "in the background"
    # "after deleting…" is a STEP of the task, not completion intent — the
    # reversed-order branch only accepts generic completion verbs
    # (doing/finishing/…), never concrete task verbs.
    "after deleting all files create a file 'test.txt' in phase3test",
])
def test_background_intent_stays_closed(goal):
    background, rest = wants_background(goal)
    assert background is False
    assert rest == goal


def test_background_intent_alone_keeps_original_goal():
    # Stripping would leave nothing actionable — keep the original text.
    background, rest = wants_background("in the background")
    assert background is True
    assert rest == "in the background"


# ========================================================= conversation context

def test_conversation_context_excludes_goal_and_truncates():
    from app.db.schemas import ChatRequest

    request = ChatRequest(messages=[
        {"role": "user", "content": "  tell me   the folders\non desktop  "},
        {"role": "assistant", "content": "x" * 900},
        {"role": "user", "content": "rename the file in that folder"},  # the goal
    ])
    context = conversation_context(request)

    lines = context.splitlines()
    assert lines[0] == "user: tell me the folders on desktop"  # whitespace folded
    assert lines[1].startswith("assistant: " + "x" * 100)
    assert lines[1].endswith("…") and len(lines[1]) < 900  # long turns capped
    assert "rename the file" not in context  # the goal message is NOT context


def test_conversation_context_keeps_only_recent_turns():
    from app.api.task_router import _CONTEXT_TURNS
    from app.db.schemas import ChatRequest

    request = ChatRequest(messages=[
        {"role": "user", "content": f"message number {i}"} for i in range(20)
    ])
    context = conversation_context(request)
    assert "message number 0" not in context
    assert f"message number {19 - _CONTEXT_TURNS}" in context
    assert "message number 18" in context
    assert "message number 19" not in context  # the goal


async def test_task_planner_receives_conversation_context(client, tmp_path):
    """The regression scenario: an earlier turn located the folder; the task
    turn refers to it. The planner prompt must carry those turns."""
    (tmp_path / "a.txt").write_text("x")
    steps = [step("List the files", "list_directory", path=str(tmp_path))]
    provider = use_provider(
        responses=["TASK", plan_json(steps), plan_json(steps)],
        streams=["There is 1 file: a.txt"],
    )
    response = await client.post("/chat/stream", json={
        "messages": [
            {"role": "user", "content": "where is my phase3test folder?"},
            {"role": "assistant", "content": f"phase3test is at {tmp_path}."},
            {"role": "user", "content": "list the files in that folder"},
        ],
        "session_id": "s-ctx",
    })
    assert response.status_code == 200
    events = sse_events(response.text)

    assert plan_events(events)[0]["plan"]["status"] == "completed"
    # prompts[0] is the TASK/CHAT classifier; [1] is the plan draft prompt
    plan_prompt = provider.prompts[1]
    assert "RECENT CONVERSATION" in plan_prompt
    assert f"phase3test is at {tmp_path}" in plan_prompt
    # And the context never leaks into the serialized plan sent to the UI
    assert "phase3test is at" not in json.dumps(plan_events(events)[0]["plan"])


# ==================================================== fail-open to normal chat

async def test_plain_conversation_never_calls_classifier(client):
    provider = use_provider(streams=["Doing well — how can I help?"])
    events = await post_chat(client, "how are you today", "s-plain")

    assert provider.chat_calls == 0            # gate closed → zero extra LLM calls
    assert provider.stream_calls == 1          # normal Phase 2 streaming ran
    assert plan_events(events) == []
    assert "how can I help" in streamed_text(events)
    assert events[-1]["done"] is True


async def test_classifier_chat_verdict_falls_through(client):
    # Gate fires ("deleted" + "file") but it's conversation about the past.
    provider = use_provider(
        responses=["CHAT"], streams=["That's rough — was it backed up?"]
    )
    events = await post_chat(client, "my brother deleted my save file yesterday", "s-story")

    assert provider.chat_calls == 1            # exactly one classification call
    assert plan_events(events) == []
    assert "backed up" in streamed_text(events)


async def test_classifier_failure_falls_through_to_chat(client):
    # chat() raises (no scripted responses) → fail open, conversation streams.
    provider = use_provider(responses=[], streams=["Normal reply."])
    events = await post_chat(client, "delete my temp files", "s-clsfail")

    assert provider.chat_calls == 1
    assert plan_events(events) == []
    assert "Normal reply." in streamed_text(events)


async def test_classifier_sees_goal_with_background_intent_stripped(client):
    # "…and remind me when you are done" made the classifier read the whole
    # message as a reminder request (listed as CHAT in its prompt) and a real
    # file task fell open to chat, whose LLM then denied having file access
    # (live bug, 2026-07-09). The intent phrase is routing metadata — it must
    # be stripped BEFORE classification, not after.
    provider = use_provider(responses=["CHAT"], streams=["fallback chat reply"])
    await post_chat(
        client,
        "hey tell me how many files are in phase3test folder , "
        "and also tell me there names and remind me when you are done",
        "s-bg-classify",
    )
    assert provider.chat_calls == 1
    classify_prompt = provider.prompts[0]
    assert "phase3test" in classify_prompt
    assert "remind me when you are done" not in classify_prompt


async def test_classifier_sees_original_goal_without_background_intent(client):
    # No background intent → the classifier judges the message verbatim.
    provider = use_provider(responses=["CHAT"], streams=["fallback chat reply"])
    await post_chat(client, "delete my temp files", "s-nobg-classify")
    assert provider.chat_calls == 1
    assert "delete my temp files" in provider.prompts[0]


async def test_parked_question_is_never_hijacked(client):
    # An open "add them as a contact?" question owns the next reply, even one
    # that looks like a task. The router must not consume it.
    sid = "s-parked"
    sess = ConversationSession()
    sess.pending_creation = PendingCreation(name="Daud")
    CONVERSATION_SESSIONS[sid] = sess
    try:
        provider = use_provider(streams=["About Daud — should I add them?"])
        events = await post_chat(client, "sure, also clean up my temp files", "s-parked")
        assert provider.chat_calls == 0        # not even classified
        assert plan_events(events) == []
    finally:
        CONVERSATION_SESSIONS.pop(sid, None)


# ============================================================= the task path

async def test_read_only_task_streams_plan_and_summary(client, tmp_path):
    (tmp_path / "a.txt").write_text("x")
    steps = [step("List the files", "list_directory", path=str(tmp_path))]
    provider = use_provider(
        responses=["TASK", plan_json(steps), plan_json(steps)],  # classify, plan, reflect
        streams=["You have 1 file there: a.txt"],                # summary
    )
    events = await post_chat(client, f"list the files in {tmp_path}", "s-task-read")

    plans = plan_events(events)
    assert len(plans) == 1
    assert plans[0]["plan"]["status"] == "completed"
    assert plans[0]["plan"]["requires_approval"] is False
    assert plans[0]["plan"]["steps"][0]["result"]["output"]["count"] == 1
    assert "a.txt" in streamed_text(events)    # LLM summary streamed as deltas
    assert events[-1]["done"] is True
    assert provider.stream_calls == 1

    # The turn is persisted like any chat turn, and audited in ActivityLog.
    messages = (await client.get("/chat/sessions/s-task-read/messages")).json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert "a.txt" in messages[1]["content"]
    activity = (await client.get("/api/activity/s-task-read")).json()
    assert [row["tool_name"] for row in activity] == ["list_directory"]


async def test_summary_llm_receives_rendered_results_never_raw_json(client, tmp_path):
    """Live display bug 2026-07-10: the summary prompt carried json.dumps of
    the step output (cut at 2000 chars), so the LLM pasted escaped JSON into
    the chat and showed ~11 of 52 search matches. The prompt must carry the
    code-rendered readable text — the LLM cannot paste JSON it never saw."""
    (tmp_path / "khawar-resume.pdf").write_text("x")
    (tmp_path / "audio1.wav").write_text("x")
    steps = [step("List the files", "list_directory", path=str(tmp_path))]
    provider = use_provider(
        responses=["TASK", plan_json(steps), plan_json(steps)],
        streams=["Your folder has audio1.wav and khawar-resume.pdf"],
    )
    await post_chat(client, f"show me the files in {tmp_path}", "s-task-render")

    assert provider.stream_calls == 1
    prompt = provider.stream_prompts[0]
    assert "ACTION: List the files" in prompt
    assert "khawar-resume.pdf" in prompt and "audio1.wav" in prompt
    assert "2 file(s)" in prompt              # the code-derived rendering
    assert "{" not in prompt                  # no JSON anywhere in the prompt
    assert '"entries"' not in prompt and "size_bytes" not in prompt


async def test_write_task_pauses_with_plan_chunk_then_approves(client, tmp_path):
    target = tmp_path / "notes.txt"
    steps = [step("Create notes.txt", "create_file", path=str(target), content="hi")]
    provider = use_provider(responses=["TASK", plan_json(steps), plan_json(steps)])
    events = await post_chat(client, f"create a file {target} saying hi", "s-task-write")

    plans = plan_events(events)
    assert len(plans) == 1
    plan = plans[0]["plan"]
    assert plan["status"] == "awaiting_approval"
    assert plan["requires_approval"] is True
    assert not target.exists()                 # nothing executed without approval
    text = streamed_text(events)
    assert "Create notes.txt" in text          # the exact step, shown verbatim
    assert "WRITE" in text
    assert "approval" in text
    assert provider.stream_calls == 0          # approval text is deterministic

    # The plan chunk carries the parked plan id — the approval endpoint
    # (Part 5) completes the cycle.
    final = (await client.post(
        "/api/agent/approve", json={"plan_id": plan["id"], "approved": True}
    )).json()
    assert final["status"] == "completed"
    assert target.read_text(encoding="utf-8") == "hi"


async def test_destructive_step_labeled_in_approval_text(client, tmp_path):
    victim = tmp_path / "old.log"
    victim.write_text("bye")
    steps = [step("Delete old.log", "delete_file", path=str(victim))]
    use_provider(responses=["TASK", plan_json(steps), plan_json(steps)])
    events = await post_chat(client, f"delete {victim}", "s-task-destroy")

    assert "DESTRUCTIVE" in streamed_text(events)
    assert plan_events(events)[0]["plan"]["status"] == "awaiting_approval"
    assert victim.exists()


async def test_failed_plan_reports_honestly(client):
    use_provider(responses=["TASK", plan_json([], reason="No email tool is available")])
    events = await post_chat(client, "run a command to email my files to ali", "s-task-fail")

    plans = plan_events(events)
    assert plans[0]["plan"]["status"] == "failed"
    text = streamed_text(events)
    assert "couldn't" in text.lower()
    assert "email" in text.lower()             # the real reason, not spin


async def test_question_pause_streams_choice_chunk_and_chat_answer_resumes(client, tmp_path):
    """The clarifying-question loop end to end over HTTP: the task pauses with
    a question, the NEXT chat message is routed as the answer (no classifier
    call), and the plan continues to completion."""
    the_one = tmp_path / "docs_notes.txt"
    the_one.write_text("real notes")
    other = tmp_path / "desktop_notes.txt"
    other.write_text("decoy")
    question = {
        "text": "Two files are named notes.txt — which one?",
        "options": [str(the_one), str(other)],
    }
    read_step = [step("Read the chosen file", "read_file", path=str(the_one))]
    provider = use_provider(
        responses=[
            "TASK",                                  # classify turn 1
            plan_json([], question=question),        # draft asks
            plan_json(read_step),                    # revise after the answer
        ],
        streams=["It says: real notes"],             # summary of the completed plan
    )

    # Turn 1: the task pauses on the question
    events = await post_chat(client, "read notes.txt from my files", "s-ask")
    plan = plan_events(events)[0]["plan"]
    assert plan["status"] == "awaiting_choice"
    assert plan["question"]["text"].startswith("Two files")
    assert plan["question"]["options"] == [str(the_one), str(other)]
    text = streamed_text(events)
    assert "which one?" in text
    assert str(the_one) in text
    assert "Nothing has been done yet" in text

    # Turn 2: a typed chat reply answers the question — no classification,
    # straight into the plan continuation
    events = await post_chat(client, str(the_one), "s-ask")
    final = plan_events(events)[0]["plan"]
    assert final["status"] == "completed"
    assert "real notes" in streamed_text(events)
    assert provider.chat_calls == 3  # classify, draft, revise — answer not classified
    # The revise prompt carried the typed answer as authoritative
    assert "THE USER'S ANSWER" in provider.prompts[2]

    # Both turns persisted like normal chat
    messages = (await client.get("/chat/sessions/s-ask/messages")).json()
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]


async def test_choose_endpoint_answers_question(client, tmp_path):
    """The clicked-option path: POST /api/agent/choose with the option text."""
    target = tmp_path / "a.txt"
    target.write_text("x")
    question = {"text": "Which file?", "options": [str(target)]}
    read_step = [step("Read a.txt", "read_file", path=str(target))]
    use_provider(
        responses=["TASK", plan_json([], question=question), plan_json(read_step)],
        streams=[],
    )

    events = await post_chat(client, "read that file of mine", "s-click")
    plan = plan_events(events)[0]["plan"]
    assert plan["status"] == "awaiting_choice"

    final = (await client.post(
        "/api/agent/choose", json={"plan_id": plan["id"], "answer": str(target)}
    )).json()
    assert final["status"] == "completed"
    assert final["steps"][0]["result"]["output"]["content"] == "x"


async def test_typed_answer_survives_backend_restart(client, tmp_path):
    """Phase 3.5: the question plan is persisted, so a typed chat answer
    still resumes it after the in-memory store is wiped (restart)."""
    target = tmp_path / "a.txt"
    target.write_text("still here")
    question = {"text": "Which file?", "options": [str(target)]}
    read_step = [step("Read a.txt", "read_file", path=str(target))]
    use_provider(
        responses=["TASK", plan_json([], question=question), plan_json(read_step)],
        streams=["It says: still here"],
    )

    events = await post_chat(client, "read that file of mine", "s-restart")
    assert plan_events(events)[0]["plan"]["status"] == "awaiting_choice"

    plan_store._PENDING_PLANS.clear()  # simulate a backend restart

    events = await post_chat(client, str(target), "s-restart")
    final = plan_events(events)[0]["plan"]
    assert final["status"] == "completed"
    assert final["steps"][0]["result"]["output"]["content"] == "still here"


async def test_approve_click_cannot_destroy_an_open_question(client, tmp_path):
    """A stray approve on a question plan re-parks it; cancel resolves it."""
    question = {"text": "Which file?", "options": ["a", "b"]}
    use_provider(responses=["TASK", plan_json([], question=question)])

    events = await post_chat(client, "delete that file of mine", "s-stray")
    plan_id = plan_events(events)[0]["plan"]["id"]

    # approve=True on a question: ignored AND re-parked, not consumed
    still = (await client.post(
        "/api/agent/approve", json={"plan_id": plan_id, "approved": True}
    )).json()
    assert still["status"] == "awaiting_choice"

    # …so cancelling it afterwards still works
    cancelled = (await client.post(
        "/api/agent/approve", json={"plan_id": plan_id, "approved": False}
    )).json()
    assert cancelled["status"] == "cancelled"


async def test_summary_stream_failure_falls_back_to_deterministic_text(client, tmp_path):
    steps = [step("List the files", "list_directory", path=str(tmp_path))]
    # No scripted streams → the summary stream raises immediately.
    use_provider(responses=["TASK", plan_json(steps), plan_json(steps)], streams=[])
    events = await post_chat(client, f"list the files in {tmp_path}", "s-task-fb")

    assert plan_events(events)[0]["plan"]["status"] == "completed"
    assert "1 step(s) completed" in streamed_text(events)
    assert events[-1]["done"] is True


# ================================================== chat prompt capabilities

def test_chat_prompt_carries_capabilities_and_task_outcome_honesty():
    """The chat LLM safety net for routing misses AND for restating task
    outcomes. Live bug 2026-07-09: a background completion said only
    'Done — 1 step(s) completed.'; on the next turn ('hi') the chat LLM
    fabricated the missing answer — '2 files: image.png, text.txt' for a
    folder holding 3 differently-named files. The prompt must forbid
    inventing results beyond what outcome messages literally state."""
    from app.api.chat import _build_system_prompt

    prompt = _build_system_prompt()
    # Round 5: never deny access, never pretend a missed task ran
    assert "Never claim you lack file-system, email, calendar, web, or computer access" in prompt
    assert "do NOT pretend you did it" in prompt
    # Phase 5 Part 5: email + calendar are real capabilities now
    assert "read and send email" in prompt
    assert "manage the user's Google Calendar" in prompt
    # Phase 6 Part 1: web search is a real capability now
    assert "search the web and read web pages" in prompt
    assert '"Email sent — …"' in prompt and '"Event created — …"' in prompt
    # Round 7: never embellish task outcomes
    assert "TASK OUTCOME HONESTY" in prompt
    assert "Never add, infer, or embellish results" in prompt
    assert "never fill the gap yourself" in prompt
    # Round 8: never write the backend's own system-message formats
    assert "NEVER imitate system-generated messages" in prompt
    # Round 9: chat can never promise or claim to initiate actions
    assert "You cannot start, queue, or schedule any action" in prompt
    assert 'has been initiated' in prompt


# ------------------------------------------------- classifier conversation context

async def test_classifier_sees_the_conversation_for_followups(client, tmp_path):
    """Live bug 2026-07-10: after a failed delete plan, 'its present in
    desktop' was classified in isolation as CHAT and fell open to the chat
    LLM, which promised the deletion and later claimed a task 'has been
    initiated'. The classifier must judge the message IN its conversation."""
    (tmp_path / "firstname.txt").write_text("x")
    steps = [step("Search for txt files in phase3test", "search_files",
                  directory=str(tmp_path), file_type=".txt")]
    provider = use_provider(
        responses=["TASK", plan_json(steps), plan_json(steps)],
        streams=["Found 1 txt file: firstname.txt"],
    )
    response = await client.post(
        "/chat/stream",
        json={
            "messages": [
                {"role": "user",
                 "content": "delete the files in phase3test folder that have .txt extension"},
                {"role": "assistant",
                 "content": "I couldn't find 'phase3test' on this computer — where is it?"},
                {"role": "user", "content": "its present in desktop"},
            ],
            "session_id": "s-followup-ctx",
        },
    )
    assert response.status_code == 200
    events = sse_events(response.text)

    # Routed as a task (not chat) — the plan chunk proves it
    assert plan_events(events), "follow-up was not routed to the task path"
    # The classifier prompt carried the conversation and the follow-up rule
    classify_prompt = provider.prompts[0]
    assert "RECENT CONVERSATION" in classify_prompt
    assert "delete the files in phase3test folder" in classify_prompt
    assert "its present in desktop" in classify_prompt


async def test_single_message_classifier_prompt_has_no_context_block(client):
    """First message of a session: nothing to show, no context block."""
    provider = use_provider(responses=["CHAT"], streams=["Just talking."])
    await post_chat(client, "i sent him the files yesterday", "s-no-ctx")

    assert "RECENT CONVERSATION" not in provider.prompts[0]


# ============================================= system-voice impersonation guard

async def test_impersonated_task_completion_is_cut_and_corrected(client):
    """Live bug 2026-07-09: 'please del all files…' missed the task gate,
    fell open to plain chat, and the LLM streamed a fabricated background-task
    lifecycle — ack, completion, invented deleted-file names — for a delete
    that never ran. The stream must be CUT at the first system-voice marker
    (the invented results are never delivered) and a deterministic correction
    appended and persisted."""
    fabricated = (
        "I can do that. I'll delete the files with the '.txt' extension. "
        'Finished the background task "please del all files". '
        "Done — 1 step(s) completed. 1 file(s) deleted: firstname.txt "
        "The folder now contains 2 file(s): fisrtname.tmp, fstname.tmp"
    )
    use_provider(streams=[fabricated])
    events = await post_chat(client, "hows your day going", "s-impersonate")

    text = streamed_text(events)
    assert "Correction from the Jarvis system" in text
    assert "no task ran" in text
    # Cut at the marker: the fabricated results after it never reached the user
    assert "fisrtname.tmp" not in text
    assert "1 file(s) deleted" not in text
    assert events[-1]["done"] is True

    # The persisted chat history carries the correction too
    history = (await client.get("/chat/sessions/s-impersonate/messages")).json()
    assistant = [m for m in history if m["role"] == "assistant"][-1]
    assert "Correction from the Jarvis system" in assistant["content"]


async def test_impersonated_reminder_confirmation_is_corrected(client):
    """'Reminder set —' is the reminder router's deterministic voice; the
    chat LLM writing it means a reminder that does not exist."""
    use_provider(streams=['Reminder set — I\'ll remind you to "call mom" at 6:00 PM.'])
    events = await post_chat(client, "thanks for the help earlier", "s-imp-rem")

    text = streamed_text(events)
    assert "Correction from the Jarvis system" in text
    assert "no reminder or calendar event was created" in text


async def test_impersonated_task_initiation_claim_is_corrected(client):
    """Live bug 2026-07-10 (transcript): after promising a deletion in chat,
    the LLM answered 'yes delete' with 'The task to delete .txt files in the
    phase3test folder on your desktop has been initiated.' — no task existed.
    Chat cannot initiate anything, so an initiation claim is always a
    fabrication and must be corrected."""
    use_provider(streams=[
        "I can do that. The task to delete .txt files in the phase3test "
        "folder on your desktop has been initiated."
    ])
    events = await post_chat(client, "yes delete", "s-imp-init")

    text = streamed_text(events)
    assert "Correction from the Jarvis system" in text
    assert "no task ran" in text


async def test_normal_chat_stream_is_untouched_by_the_guard(client):
    use_provider(streams=[
        "Pretty good. I finished reviewing the notes you mentioned — "
        "want to go over them together?"
    ])
    events = await post_chat(client, "hows your day going", "s-imp-clean")

    text = streamed_text(events)
    assert "Correction" not in text
    assert "reviewing the notes" in text
    assert events[-1]["done"] is True
