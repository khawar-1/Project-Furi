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
from types import SimpleNamespace
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
    is_action_followup,
    is_browse_followup,
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
async def db_factory(tmp_path_factory):
    """The session factory behind `client`, exposed so a test can inspect what
    a turn actually wrote (e.g. that a rescued turn persists ONE user row)."""
    db_dir = tmp_path_factory.mktemp("router-db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_dir / 'router-test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def client(db_factory, monkeypatch):
    """HTTP client against the real app: file-backed DB, no Qdrant, and the
    Phase 2 background extraction stubbed out (it would call the exhausted
    FakeProvider against the real dev database)."""
    factory = db_factory

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


def test_gate_fires_on_own_action_questions():
    # Questions about Jarvis's OWN actions name no domain noun ("what have
    # you done today?") — the object is Jarvis's action record. Live bug
    # 2026-07-13: "what was the name of folder that u created?" only reached
    # the classifier because it said "folder"; the chat LLM then asserted a
    # wrong folder from memory and denied the real jarvis_test one.
    assert looks_like_task("what was the name of the thing u created a while ago?") is True
    assert looks_like_task("what have you done today?") is True
    assert looks_like_task("did you delete anything today?") is True
    assert looks_like_task("you created something yesterday, what was it?") is True


def test_gate_bare_did_you_still_needs_an_action_verb():
    # "did you …" is everyday conversation — without an action verb it must
    # not cost a classifier call.
    assert looks_like_task("did you know giraffes only sleep 30 minutes?") is False
    assert looks_like_task("have you heard the new album?") is False


def test_gate_fires_on_current_info_questions():
    # A current-info QUESTION names no domain noun — the object is a fact out
    # in the world (live bug 2026-07-16: the Black Clover question missed the
    # gate and plain chat fabricated "Searching the web… One moment, sir.").
    # A time-sensitive marker + question shape reaches the classifier, which
    # makes the WEB/CHAT call.
    assert looks_like_task(
        "Hey Jarvis, when is the new season of Black Clover coming out?"
    ) is True
    assert looks_like_task("when is the next season of severance coming out") is True
    assert looks_like_task("what's the latest news on the election?") is True
    assert looks_like_task("who won the match today?") is True


def test_gate_current_info_tier_tolerates_typos():
    # Round 2 (live 2026-07-16): the same question typed fast — "seasom",
    # "comming" — missed every exact-word marker and fell to chat again.
    # Season-shaped words match on the stem; "coming" accepts a doubled m.
    assert looks_like_task("when is new seasom of black clover comming") is True
    assert looks_like_task("when is the new seasopn of blackclover comming out") is True


def test_gate_fires_on_bare_search_verb():
    # "search" / "look it up" as verbs are strong signals (live 2026-07-16
    # round 2: "yes search and tell me when…" — a go-ahead to the chat LLM's
    # own search offer — named no web noun, was over the follow-up word cap,
    # and fell to plain chat, which fabricated "I've started a search…").
    assert looks_like_task(
        "yes search and tell me when is the new seasopn of blackclover comming out"
    ) is True
    assert looks_like_task("search for cheap flights to karachi") is True
    assert looks_like_task("just look it up") is True


def test_gate_question_tier_ignores_statements():
    # A STATEMENT is never a question, however current its subject — small talk
    # about the new season must not cost a classifier call.
    assert looks_like_task("i love this season of the show") is False
    assert looks_like_task("the new season finally came out yesterday") is False
    assert looks_like_task("explain recursion") is False


def test_classify_prompt_routes_own_action_questions_to_task():
    # The classifier is TOLD that questions about Jarvis's own actions are
    # TASK — before this, its CHAT line ("talking ABOUT past actions") made
    # it route them to chat, which cannot see the audit log.
    from app.api.task_router import _CLASSIFY_PROMPT
    assert "JARVIS'S OWN actions is TASK" in _CLASSIFY_PROMPT
    assert "audit log" in _CLASSIFY_PROMPT


def test_gate_fires_on_knowledge_lookups():
    # A factual lookup about a specific entity names no domain noun and carries
    # no time-sensitive marker ("what do you know about black clover" — the
    # exact live bug 2026-07-16 that fell to plain chat and got a stale /
    # dead-end-offer answer). The explicit information-request lead-ins reach
    # the classifier, which makes the WEB/CHAT call.
    assert looks_like_task("hey what do you know about black clover") is True
    assert looks_like_task("tell me about the framework laptop") is True
    assert looks_like_task("who is the ceo of tesla?") is True
    assert looks_like_task("have you heard of the rivian r1t") is True
    assert looks_like_task("give me a rundown on the new pixel phone") is True
    assert looks_like_task("do you know anything about quokkas") is True


@pytest.mark.parametrize("message", [
    # THE INCIDENT, FROZEN — the user's own words, verbatim from the DB.
    # Both fell to plain chat and got "ask me to search the web" back.
    "which teams qualified for fifa finals 2026",
    "tell me when is the new season of black clover coming out, and who is the "
    "prime minister of pakistan and whihc teams have qualified for fifa finals 2026",
    # …and its first clause alone, which is what actually broke: the old shape
    # rule allowed ONE filler word before the question word, so "tell me when"
    # (two) missed. The full message only survived on "who is the prime minister".
    "tell me when is the new season of black clover coming out",
    # Ordinary external-fact questions the marker list never covered. Measured
    # 2026-07-17: the gate blocked 7 of these 8 while the classifier labelled
    # all 8 WEB — the list was the only thing between the user and an answer.
    "tell me who won the match last night",
    "let me know when the next iphone is out",
    "find out which teams qualified for the world cup",
    "i want to know the current gold price",
    "is bitcoin up today",
    "what is the population of karachi",
])
def test_gate_fires_for_external_questions(message):
    assert looks_like_task(message) is True


@pytest.mark.parametrize("message", [
    # A question about the USER, about JARVIS, or about the two of them is
    # answered from memory and context — the web cannot help, so these stay
    # free. This exclusion is the whole reason the wider tier is affordable.
    "how are you today",
    "what do you think of black clover?",
    "who are you?",
    "who is this?",
    "what should i name my project",
    "do you remember what i told you about jamil",
    "what's my brother's name?",
])
def test_gate_stays_closed_for_self_referential_questions(message):
    assert looks_like_task(message) is False


def test_question_frame_pronouns_do_not_count_as_self_reference():
    # The subtle one. "i want to know the gold price" is about the WORLD; the
    # "i" belongs to the request frame, not the subject. Stripping the frame
    # before the self-reference test is what separates it from "why is my
    # script slow" — without that, the pronoun would refuse the whole class.
    assert looks_like_task("i want to know the current gold price") is True
    assert looks_like_task("have you heard of the framework laptop") is True
    assert looks_like_task("what do you know about black clover") is True


def test_wider_question_tier_costs_one_call_on_concept_questions():
    # The accepted cost, stated out loud rather than discovered later: an
    # impersonal concept question now reaches the classifier, which answers
    # CHAT and falls open. One temperature-0 call buys the end of the
    # phrasing lottery. If this ever needs revisiting, revisit it knowingly.
    assert looks_like_task("how does anime production work?") is True
    assert looks_like_task("what is a monad") is True
    assert looks_like_task("tell me a joke") is True


def test_own_action_questions_survive_the_self_reference_test():
    # Order dependency, load-bearing: "what did you do today" is second-person,
    # so the question tier's self-reference test would refuse it — but it is a
    # real TASK answered from the audit log. The own-action tier must stay
    # AHEAD of the question tier in looks_like_task.
    assert looks_like_task("what did you do today") is True
    assert looks_like_task("what was the name of the folder that u created?") is True


@pytest.mark.parametrize("message", [
    # A named media/streaming site fires the strong gate alone (any wording).
    "play jane by the long faces on youtube",
    "search and play some lofi on youtube",
    "open youtube and play the new severance trailer",
    "watch this on youtube",
    "play despacito on spotify",
    # A media noun + a play/watch/listen verb fires via the weak-signal path,
    # no site named.
    "play the new taylor swift song",
    "watch the trailer",
    "listen to some music",
])
def test_gate_fires_for_play_and_media_requests(message):
    assert looks_like_task(message) is True


@pytest.mark.parametrize("message", [
    # Ordinary conversation that merely mentions a media site or activity — a
    # false fire here costs one temp-0 call that answers CHAT (recall-first),
    # but bare mentions with no action verb must not fire the weak path.
    "i love this song",
    "that was a great movie",
])
def test_gate_stays_closed_for_bare_media_mentions(message):
    assert looks_like_task(message) is False


def test_classify_prompt_has_browse_label():
    # BROWSE (Phase 14) routes "act on a live site" (play/watch a video) to the
    # planner, which drafts a `browse` step (rule 21). Distinct from WEB, which
    # only looks information up.
    from app.api.task_router import _CLASSIFY_PROMPT, _ACTION_LABELS
    assert "BROWSE" in _ACTION_LABELS
    assert "BROWSE —" in _CLASSIFY_PROMPT
    assert "play jane by the long faces on youtube" in _CLASSIFY_PROMPT.lower()


async def test_browse_inline_is_forced_to_background(client, monkeypatch):
    """A BROWSE task ALWAYS delegates to a background agent, even when the
    classifier mis-tags it INLINE. Live bug 2026-07-24: 'play latest episode of
    one piece on anikoto.cz' was classified BROWSE INLINE, ran in-turn, held the
    chat SSE open for the whole browse, and locked the user out of starting
    anything else — the multi-agent concurrency evaporated. A browse drives a
    real browser (Chromium launch + multi-step loop + kept-open media) and is
    NEVER a quick in-turn read; the code forces DELEGATE regardless of mode.

    Observable proof: the background path calls start_task and streams NO plan
    chunk (an INLINE dispatch would stream one). start_task is stubbed so no real
    browser or detached task spawns."""
    started: list[str] = []

    async def _fake_start_task(db, goal, session_id, **kwargs):
        started.append(goal)
        return SimpleNamespace(id="t-browse", status="running")

    monkeypatch.setattr("app.api.task_router.start_task", _fake_start_task)
    # classify → BROWSE INLINE (the mis-tag). No plan responses are needed: the
    # forced-delegate path never plans in-turn.
    use_provider(responses=["BROWSE INLINE"])

    events = await post_chat(
        client, "play latest episode of one piece on anikoto.cz", "s-browse-inline"
    )

    assert started, "BROWSE INLINE must still start a background task"
    assert plan_events(events) == []  # delegated → no in-turn plan chunk
    assert "background" in streamed_text(events).lower()


@pytest.mark.parametrize("message", [
    # The github sign-in incident (2026-07-18): named no file/media domain noun,
    # missed the gate, fell to plain chat which asked for the user's password.
    # Now github/gitlab are browse nouns and "sign in / log in" fires the gate.
    "sign in to github and open my oldest repo",
    "signin to github and open my oldest repo",
    "log into my linkedin and open my messages",
    "sign into my account and download the invoice",
    "open my oldest repo on github",
])
def test_gate_fires_for_sign_in_and_web_app_requests(message):
    assert looks_like_task(message) is True


@pytest.mark.parametrize("message", [
    # Live bug 2026-07-21: a named web app the user DRIVES ("open linkedin and
    # navigate it") names no file/media noun and no "sign in" verb — "open" is
    # only a weak-path verb with no weak noun beside it — so the gate missed and
    # it fell to plain chat (which offered a "magic word" rephrase and then
    # fabricated "that instruction has been passed to the system"). The generic
    # browse-intent tier (`_is_browse_intent` → grounding.ground_origins) must
    # fire when the user names a site to go to / act on.
    "hey open linkedin and go to the networks tab and open profile of anas mubashar",
    "Open LinkedIn, go to the networks tab, and open the profile of anas mubashar",
    # The FIX MUST BE GENERAL — the browser stack is Skyvern-class (any site, no
    # per-site code). These sites are in NO routing list; a nav cue + the named
    # destination is the whole signal:
    "open nytimes.com and read me the front page",
    "go to overleaf and open my latest document",
    "navigate to figma and open the design file",
])
def test_gate_fires_for_named_web_apps(message):
    assert looks_like_task(message) is True


def test_classify_prompt_covers_sign_in_and_lists_browse_in_the_answer():
    """BROWSE must appear in the CLOSING answer enumeration (it was omitted —
    'One word (TASK, EMAIL, CALENDAR, WEB, or CHAT)' — which biased the model
    against ever choosing it), and the prompt must carry a sign-in example."""
    from app.api.task_router import _CLASSIFY_PROMPT
    assert "one word (task, email, calendar, web, browse, or chat)" in _CLASSIFY_PROMPT.lower()
    assert "sign in to github and open my oldest repo" in _CLASSIFY_PROMPT.lower()


def test_classify_prompt_routes_entity_lookups_to_web():
    # The WEB label was broadened from time-sensitive-only to also cover a
    # factual question about a specific real-world entity — so a knowledge
    # lookup is looked up, not answered from stale training data.
    from app.api.task_router import _CLASSIFY_PROMPT
    assert "specific real-world" in _CLASSIFY_PROMPT.lower()
    assert "black clover" in _CLASSIFY_PROMPT.lower()


# --------------------------------------------------- short action follow-ups

_EMAIL_CONVO = (
    "user: mail anas that testing is complete\n"
    "assistant: Here is the draft email I'll send to anas@example.com — "
    "subject: Testing complete."
)


def test_followup_fires_for_short_steer_with_domain_conversation():
    # "send it" names no object of its own — the object lives in the
    # conversation (live bug 2026-07-13: it fell to plain chat, which
    # fabricated "I've started working on that in the background").
    assert is_action_followup("send it", _EMAIL_CONVO) is True
    assert is_action_followup("delete them", "assistant: found 3 files on your desktop") is True
    assert is_action_followup("yes, send it now please", _EMAIL_CONVO) is True


def test_followup_needs_a_conversation_with_a_domain_signal():
    assert is_action_followup("send it", "") is False
    assert is_action_followup("send it", "user: how are you\nassistant: great!") is False


def test_followup_needs_an_action_verb():
    assert is_action_followup("thanks, that worked", _EMAIL_CONVO) is False
    assert is_action_followup("nice", _EMAIL_CONVO) is False


def test_followup_ignores_long_messages():
    # A real new task names its object and fires the normal gate on its own
    # words — nine-plus words is not a follow-up steer.
    long_msg = "please send my warmest regards to everyone attending the party tonight"
    assert is_action_followup(long_msg, _EMAIL_CONVO) is False


# ------------------------------- browse follow-up (open agent window, 2026-07-21)
def test_browse_followup_fires_only_with_a_live_window(monkeypatch):
    """After Jarvis opens a page, a short browser-verb message ("message him
    'hi'") continues that session — but ONLY while a live window is held. The
    window is the domain signal, so no site keyword is needed. Live bug: it fell
    to plain chat, which offered a magic-word rephrase."""
    monkeypatch.setattr("app.api.task_router._browse_window_active", lambda: True)
    assert is_browse_followup("message him 'hi'") is True
    assert is_browse_followup("text him hi") is True
    assert is_browse_followup("click the first result") is True
    assert is_browse_followup("scroll down") is True
    # No browser-ish verb, or too long — not a steer.
    assert is_browse_followup("thanks that worked") is False
    assert is_browse_followup("send my warmest regards to everyone at the party tonight") is False

    # With no window open, the same short steer does NOT fire here (the normal
    # gate/classifier still sees a full new task on its own words).
    monkeypatch.setattr("app.api.task_router._browse_window_active", lambda: False)
    assert is_browse_followup("message him 'hi'") is False


def test_browse_window_active_reflects_the_registry(monkeypatch):
    from app.api import task_router
    from app.browser.registry import REGISTRIES

    REGISTRIES["browse"].clear_nowait()
    assert task_router._browse_window_active() is False
    # A held session (a fake object is enough — peek only reads the slot).
    REGISTRIES["browse"]._session = object()
    REGISTRIES["browse"]._meta = {"url": "https://www.linkedin.com/in/anas"}
    try:
        assert task_router._browse_window_active() is True
    finally:
        REGISTRIES["browse"].clear_nowait()


async def test_followup_send_it_reaches_the_classifier(client):
    """End to end: 'send it' after an email-flavored conversation reaches the
    classifier (which judges it WITH that conversation) instead of falling
    open to the chat path unheard (live bug 2026-07-13)."""
    provider = use_provider(responses=["CHAT"], streams=["Okay."])
    response = await client.post(
        "/chat/stream",
        json={
            "messages": [
                {"role": "user", "content": "mail anas that testing is complete"},
                {"role": "assistant", "content": "Here is the draft email I'll send."},
                {"role": "user", "content": "send it"},
            ],
            "session_id": "s-followup-send-it",
        },
    )
    assert response.status_code == 200
    assert provider.chat_calls == 1  # the classifier saw it
    assert "RECENT CONVERSATION" in provider.prompts[0]
    assert "send it" in provider.prompts[0]


async def test_followup_without_domain_conversation_never_costs_a_call(client):
    """'send it' in a conversation that never mentioned an actionable domain
    stays on the zero-cost chat path — the gate economics are preserved."""
    provider = use_provider(streams=["Send what?"])
    response = await client.post(
        "/chat/stream",
        json={
            "messages": [
                {"role": "user", "content": "i had a great day"},
                {"role": "assistant", "content": "Glad to hear it!"},
                {"role": "user", "content": "send it"},
            ],
            "session_id": "s-followup-no-domain",
        },
    )
    assert response.status_code == 200
    assert provider.chat_calls == 0


async def test_unknown_verb_phrasing_reaches_the_approval_gate(client, tmp_path):
    """End to end: wording no verb list could anticipate ("get rid of") still
    routes to the planner because it names its object — and the destructive
    step pauses at the structural approval gate; nothing runs."""
    victim = tmp_path / "notes.txt"
    victim.write_text("x")
    steps = [step("Delete notes.txt", "delete_file", path=str(victim))]
    use_provider(responses=["TASK INLINE", plan_json(steps), plan_json(steps)])

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
    ("BROWSE", "BROWSE"),
    ("CHAT", "CHAT"),
    # Real models append stray text/punctuation — startswith parsing handles it.
    ("EMAIL.", "EMAIL"),
    ("calendar", "CALENDAR"),
    ("web", "WEB"),
    ("browse", "BROWSE"),
    ("  TASK\n", "TASK"),
])
async def test_classify_message_returns_label(reply, expected):
    provider = FakeProvider([reply])
    label, _mode = await _classify_message(provider, "some message", "")
    assert label == expected
    assert provider.chat_calls == 1  # still exactly one temp-0 call


@pytest.mark.parametrize("reply,expected", [
    ("TASK INLINE", ("TASK", "INLINE")),
    ("EMAIL DELEGATE", ("EMAIL", "DELEGATE")),
    ("BROWSE DELEGATE", ("BROWSE", "DELEGATE")),
    ("WEB inline", ("WEB", "INLINE")),          # case-insensitive
    ("TASK", ("TASK", "DELEGATE")),             # no mode → DELEGATE (safe default)
    ("CALENDAR.\n", ("CALENDAR", "DELEGATE")),  # stray punctuation, no mode
])
async def test_classify_message_parses_mode(reply, expected):
    provider = FakeProvider([reply])
    assert await _classify_message(provider, "some message", "") == expected


@pytest.mark.parametrize("reply", ["I think this is email", "", "unsure", "yes"])
async def test_classify_message_unrecognized_fails_open_to_chat(reply):
    # An unrecognized word is NOT an action label — fail open to CHAT, exactly
    # as an exception would. Mode is DELEGATE but irrelevant for CHAT.
    provider = FakeProvider([reply])
    assert await _classify_message(provider, "some message", "") == ("CHAT", "DELEGATE")


async def test_classify_message_exception_fails_open_to_chat():
    provider = FakeProvider([])  # chat() raises AssertionError when exhausted
    assert await _classify_message(provider, "some message", "") == ("CHAT", "DELEGATE")


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
    use_provider(responses=["EMAIL INLINE", plan_json(steps), plan_json(steps)])

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
    # Web-search action promises (live bug 2026-07-16 — chat cannot search,
    # so a progressive/future search claim is always a fabrication).
    "Searching the web for the latest on Black Clover's next season.\n\nOne moment, sir.",
    "Let me check online for the release date.",
    "I'll search the web and get back to you shortly, sir.",
    "Looking that up online now.",
    # Round 2 (live 2026-07-16): the fabrication learned to avoid the word
    # "web" — a started-a-search claim or a promise to report back findings
    # is always a fabrication from the chat path.
    "I've started a search for the latest news on the *Black Clover* anime "
    "release date. I'll let you know what I find.",
    "I have begun a search for the latest updates on that.",
    # Hand-off-to-the-backend fabrication (live bug 2026-07-21): on a routing
    # miss the chat LLM claimed it had dispatched the request — chat cannot pass
    # anything to any system, so a past-tense passed/handed/routed claim is a
    # fabrication.
    "That instruction has been passed to the system. Give me a moment, sir.",
    "I have handed that off to the planner.",
    "It's been routed to the right system, sir.",
    # Browser/media action claims (live bug 2026-07-23): a follow-up correction
    # ("i meant ep 5 of season 2 in english dub") missed routing, fell to plain
    # chat, and the LLM fabricated a completed browser switch. Chat cannot drive
    # a browser, so a switch/now-playing/playing-on-<site> claim is a fabrication.
    "I'll switch it over. Episode 5 of My Hero Academia Season 2 in English "
    "dub is now open and playing on anikoto.cz. All done, sir.",
    "Switching it over to the dub now, sir.",
    "I've switched the episode over — enjoy, sir.",
    "Episode 5 is now open and playing on anikoto.cz.",
    "It's now open and playing, sir.",
    "Done — the video is now playing on youtube.com.",
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
    "I can search the web for you — just say 'search the web for the release date'.",
    "You can ask me to search the web for anything current, sir.",
    "I can start a search for the latest news if you'd like — just say the word.",
    # A conditional offer to route / the honest "say it as a direct instruction"
    # rephrase hint must NOT be cut (it is true, not a fabrication) — the belt is
    # anchored to PAST-TENSE dispatch claims only.
    "I can pass this to the system if you like, sir.",
    "To do this, say it as a direct instruction so it routes to the right system.",
    # Browser/media capability OFFERS must NOT trip the guard — a routing miss
    # should ask the user to rephrase, not get cut.
    "I can play videos on YouTube for you — just say 'play jane on youtube'.",
    "Would you like me to switch it to the dub? Just say the word, sir.",
    "I can open that page in a browser if you'd like.",
    "I can switch it over if you tell me the season and episode.",
    "You can ask me to play any episode — just say it as a direct instruction.",
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
        responses=["TASK INLINE", plan_json(steps), plan_json(steps)],
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
        responses=["TASK INLINE", plan_json(steps), plan_json(steps)],  # classify, plan, reflect
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
        responses=["TASK INLINE", plan_json(steps), plan_json(steps)],
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
    provider = use_provider(responses=["TASK INLINE", plan_json(steps), plan_json(steps)])
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
    use_provider(responses=["TASK INLINE", plan_json(steps), plan_json(steps)])
    events = await post_chat(client, f"delete {victim}", "s-task-destroy")

    assert "DESTRUCTIVE" in streamed_text(events)
    assert plan_events(events)[0]["plan"]["status"] == "awaiting_approval"
    assert victim.exists()


async def test_failed_plan_reports_honestly(client):
    use_provider(responses=["TASK INLINE", plan_json([], reason="No email tool is available")])
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
            "TASK INLINE",                           # classify turn 1 (quick read → inline)
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
        responses=["TASK INLINE", plan_json([], question=question), plan_json(read_step)],
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
        responses=["TASK INLINE", plan_json([], question=question), plan_json(read_step)],
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
    use_provider(responses=["TASK INLINE", plan_json([], question=question)])

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
    use_provider(responses=["TASK INLINE", plan_json(steps), plan_json(steps)], streams=[])
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
    assert "Never claim you lack file-system, email, calendar, web, browser, or computer access" in prompt
    assert "do NOT pretend you did it" in prompt
    # 2026-07-23: browser/media is a real capability and a fabrication vector
    assert "drive a real web browser to act on live sites" in prompt
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
        responses=["TASK INLINE", plan_json(steps), plan_json(steps)],
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


async def test_impersonated_browser_media_switch_is_corrected(client):
    """Live bug 2026-07-23: after a real 'play ep X on anikoto.cz', the
    follow-up correction 'i meant ep 5 of season 2 in english dub' missed
    routing, fell to plain chat, and the LLM fabricated 'I'll switch it over.
    Episode 5 … is now open and playing on anikoto.cz.' — nothing happened.
    Chat cannot drive a browser, so the claim is cut and corrected, and the
    fabricated tail (the false 'now playing on <site>') never reaches the user."""
    use_provider(streams=[
        "I'll switch it over. Episode 5 of My Hero Academia Season 2 in "
        "English dub is now open and playing on anikoto.cz. All done, sir."
    ])
    events = await post_chat(
        client, "i meant ep 5 of season 2 in english dub", "s-imp-media"
    )

    text = streamed_text(events)
    assert "Correction from the Jarvis system" in text
    assert "no browser opened or video played" in text
    # Cut at the marker: the false "now playing" claim and the fabricated tail
    # never reached the user. (The correction text names anikoto.cz as an
    # example, so assert on the fabrication-only phrasing instead.)
    assert "now open and playing" not in text
    assert "All done, sir" not in text
    assert events[-1]["done"] is True


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


# ==================================================== dead-end offer backstop
#
# Live 2026-07-17, twice in one evening: "which teams qualified for fifa
# finals 2026" → "Worth looking up for the latest — ask me to search the web
# for it, sir." The user is left guessing the phrase that would have routed.
#
# Routing had already failed by then, and this backstop deliberately does not
# care WHY: a gate hole, a typo'd question word ("whihc"), a flaked classifier
# call, a provider error — every one of them degrades silently to CHAT, and
# every one of them ends here. The signal is the model's own admission that
# the answer needs the live web, which needs no keyword list to recognize.


async def test_dead_end_offer_is_rescued_into_a_real_search(client):
    """THE INCIDENT, FROZEN. The gate cannot see this is a question — "whihc"
    is a typo, and fuzzy-matching question words was measured and rejected
    (see is_external_question). So chat answers, offers a search it cannot
    run, and the backstop turns that offer into the search itself."""
    provider = use_provider(
        streams=["Worth looking up for the latest — ask me to search the web for it, sir."],
        responses=[plan_json([step("search the web", "web_search", query="fifa 2026 finalists")])],
    )
    events = await post_chat(
        client, "whihc teams have qualified for fifa finals 2026", "s-rescue-1"
    )

    # Routing never fired, so the planner was reached only by the rescue.
    plans = plan_events(events)
    assert plans, "the dead-end offer should have been rescued into a plan"
    assert provider.stream_calls == 1   # chat answered once…
    assert provider.chat_calls >= 1     # …and the planner then really ran


async def test_the_offer_itself_never_reaches_the_user(client):
    """Jarvis must not appear to ask permission and then act anyway. The cut
    happens BEFORE the offer is emitted — unlike the impersonation guard,
    which deliberately shows its marker so the correction has a referent.

    Asserts NO WORD of the offer escapes, not merely the whole phrase. The
    first version of this test checked only the full string and passed while
    the guard was in fact broken: live, "…as matches are played. Ask me to"
    reached the screen and the answer followed it, because an offer is not
    recognizable until its last word arrives and the opening words had already
    been emitted by then. Hence the look-behind buffer."""
    use_provider(
        streams=["Worth looking up for the latest — ask me to search the web for it, sir."],
        responses=[plan_json([step("search the web", "web_search", query="q")])],
    )
    events = await post_chat(client, "whihc teams qualified for fifa 2026", "s-rescue-2")

    text = streamed_text(events).lower()
    assert "ask me to search" not in text
    assert "ask me to" not in text     # the leak the first cut shipped
    assert "ask me" not in text
    assert "Worth looking up for the latest" in streamed_text(events)  # honest prefix stays


async def test_an_offer_chat_can_actually_keep_is_not_a_dead_end(client):
    """The false positive the suite caught on the first draft: a bare "just
    say the word" answering an EMAIL message offers mail help, not a web
    lookup. An offer is only a dead end when it offers what chat cannot do."""
    provider = use_provider(
        responses=["CHAT"],
        streams=["I can help with that — just say the word."],
    )
    events = await post_chat(client, "i got an email from jamil yesterday", "s-rescue-3")

    assert plan_events(events) == []
    assert "just say the word" in streamed_text(events)
    assert provider.chat_calls == 1  # the classifier only; no planner run


async def test_a_past_tense_search_report_is_never_rescued(client):
    """The plan path's own voice reports searches in the past tense. If that
    ever tripped the backstop, a completed search would be re-run forever."""
    use_provider(streams=["I searched the web and found three results, sir."])
    events = await post_chat(client, "hows it going", "s-rescue-4")

    assert plan_events(events) == []
    assert "I searched the web and found three results" in streamed_text(events)


async def test_rescue_does_not_duplicate_the_user_message(client, db_factory):
    """The chat path already persisted the user's turn before it began
    streaming; the plan path normally writes it itself. Exactly one row."""
    use_provider(
        streams=["Worth looking up — ask me to search the web for it, sir."],
        responses=[plan_json([step("search the web", "web_search", query="q")])],
    )
    await post_chat(client, "whihc teams qualified for fifa 2026", "s-rescue-5")

    from sqlalchemy import select
    from app.db.models import Message
    async with db_factory() as session:
        rows = (await session.execute(
            select(Message).where(
                Message.session_id == "s-rescue-5", Message.role == "user"
            )
        )).scalars().all()
    assert len(rows) == 1


async def test_a_failed_rescue_degrades_honestly_not_into_a_magic_word(client):
    """If the rescue itself dies, the turn must still not send the user
    hunting for a phrase. The planner has no scripted response here, so it
    fails for real."""
    use_provider(
        streams=["Worth looking up — ask me to search the web for it, sir."],
        responses=[],   # planner LLM exhausted → the plan run fails
    )
    events = await post_chat(client, "whihc teams qualified for fifa 2026", "s-rescue-6")

    text = streamed_text(events).lower()
    assert "ask me to search" not in text
    assert events[-1]["done"] is True
