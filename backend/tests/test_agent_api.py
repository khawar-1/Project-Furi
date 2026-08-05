"""
Part 5 — Agent + Activity API endpoints, exercised over real HTTP (httpx
ASGITransport against the actual FastAPI app). get_db is overridden with a
file-backed test database (shared across requests, unlike :memory:) and
get_llm_provider with a scripted FakeProvider — everything below the LLM is
real: planner graph, tool registry, approval gate, ActivityLog writes.
"""
import json
from typing import AsyncIterator, List, Optional

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.tools  # noqa: F401 — registers the real file/terminal tools
from app.agents import plan_store
from app.core.dependencies import get_db, get_llm_provider
from app.db.database import Base
from app.providers.base import (
    EmbeddingResponse,
    LLMMessage,
    LLMProvider,
    LLMResponse,
)
from main import app


class FakeProvider(LLMProvider):
    """Returns scripted responses in order; fails loudly if over-called."""

    def __init__(self, responses: List[str]) -> None:
        self._responses = list(responses)
        self.calls = 0

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
        self.calls += 1
        if not self._responses:
            raise AssertionError(f"FakeProvider exhausted after {self.calls - 1} responses")
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


def plan_json(steps: list, reason: Optional[str] = None) -> str:
    return json.dumps({"steps": steps, "unachievable_reason": reason})


def use_provider(responses: List[str]) -> FakeProvider:
    provider = FakeProvider(responses)
    app.dependency_overrides[get_llm_provider] = lambda: provider
    return provider


@pytest_asyncio.fixture
async def client(tmp_path_factory):
    """HTTP client against the real app; file-backed DB shared across requests.
    The DB lives OUTSIDE the test's tmp_path so it never pollutes directory
    listings the tests make."""
    db_dir = tmp_path_factory.mktemp("api-db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_dir / 'api-test.db'}")
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

    app.dependency_overrides[get_db] = _get_db
    plan_store._PENDING_PLANS.clear()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
    plan_store._PENDING_PLANS.clear()
    await engine.dispose()


# ================================================================ /agent/tools

async def test_tools_endpoint_lists_all_registered_tools(client):
    response = await client.get("/api/agent/tools")
    assert response.status_code == 200
    tools = {t["name"]: t for t in response.json()}
    assert set(tools) == {
        "search_files", "read_file", "list_directory",
        "move_file", "move_files", "rename_file", "delete_file", "delete_files",
        "create_folder",
        "create_file", "run_command", "execute_script",
        "recall_memory", "lookup_contact", "recall_actions",
        "semantic_file_search",
        "search_emails", "read_email", "read_thread",
        "create_email_draft", "send_email", "reply_email",
        "list_events", "find_events",
        "create_event", "update_event", "delete_event",
        "web_search", "read_webpage", "browse_page",
        "browse", "browse_commit", "stop_media",
        "list_devices", "get_device_state",
        "set_device_state", "run_scene", "set_climate",
        # Desktop control (Feature 2)
        "list_windows", "take_screenshot", "read_clipboard",
        "focus_window", "close_window", "launch_app",
        "set_volume", "media_key", "write_clipboard",
    }
    assert tools["read_file"]["permission_level"] == "read"
    assert tools["recall_memory"]["permission_level"] == "read"
    assert tools["lookup_contact"]["permission_level"] == "read"
    assert tools["create_file"]["permission_level"] == "write"
    assert tools["create_folder"]["permission_level"] == "write"
    assert tools["delete_file"]["permission_level"] == "destructive"
    # The batch twins must carry the SAME level as their singular form — a
    # bulk delete is no less destructive for being one step.
    assert tools["move_files"]["permission_level"] == "write"
    assert tools["delete_files"]["permission_level"] == "destructive"
    # Phase 5 Part 3: reading mail is read, drafting is a reversible write,
    # anything that leaves the machine is destructive.
    assert tools["search_emails"]["permission_level"] == "read"
    assert tools["read_email"]["permission_level"] == "read"
    assert tools["read_thread"]["permission_level"] == "read"
    assert tools["create_email_draft"]["permission_level"] == "write"
    assert tools["send_email"]["permission_level"] == "destructive"
    assert tools["reply_email"]["permission_level"] == "destructive"
    # Phase 5 Part 4: calendar reads are read, create/update are write,
    # deleting an event is destructive.
    assert tools["list_events"]["permission_level"] == "read"
    assert tools["find_events"]["permission_level"] == "read"
    assert tools["create_event"]["permission_level"] == "write"
    assert tools["update_event"]["permission_level"] == "write"
    assert tools["delete_event"]["permission_level"] == "destructive"
    # Phase 14 Part 1: driving a browser is READ, and that is a claim about
    # CODE, not a judgement call — browser_session's interceptor aborts every
    # non-GET, so the tool cannot submit anything. If this ever needs to become
    # write/destructive, the guarantee has been broken, not the classification.
    assert tools["browse_page"]["permission_level"] == "read"
    # Phase 14 Part 2: the browse LOOP and stop_media are READ for the same
    # structural reason — the interceptor aborts every non-GET, so the loop can
    # navigate and click but never submit. stop_media only closes a window Jarvis
    # itself opened. Neither needs approval.
    assert tools["browse"]["permission_level"] == "read"
    assert tools["stop_media"]["permission_level"] == "read"
    # Phase 14 Part 5: submitting a form is the one browser action that mutates —
    # it sends data that leaves the machine — so browse_commit is DESTRUCTIVE and
    # pauses for signature approval on the code-read form contract, while `browse`
    # stays READ. This split is the whole point of COMMIT mode.
    assert tools["browse_commit"]["permission_level"] == "destructive"
    assert "parameters" in tools["search_files"]


# ============================================================== /agent/execute

async def test_execute_read_only_goal_completes(client, tmp_path):
    (tmp_path / "a.txt").write_text("x")
    steps = [step("List the files", "list_directory", path=str(tmp_path))]
    use_provider([plan_json(steps), plan_json(steps)])  # plan + reflect

    response = await client.post(
        "/api/agent/execute", json={"goal": "list my files", "session_id": "s-api-read"}
    )
    assert response.status_code == 200
    plan = response.json()
    assert plan["status"] == "completed"
    assert plan["requires_approval"] is False
    assert plan["steps"][0]["status"] == "completed"
    assert plan["steps"][0]["result"]["output"]["count"] == 1


async def test_execute_write_goal_pauses_and_parks_plan(client, tmp_path):
    target = tmp_path / "notes.txt"
    steps = [step("Create notes.txt", "create_file", path=str(target), content="hi")]
    use_provider([plan_json(steps), plan_json(steps)])

    response = await client.post("/api/agent/execute", json={"goal": "create notes.txt"})
    assert response.status_code == 200
    plan = response.json()
    assert plan["status"] == "awaiting_approval"
    assert plan["requires_approval"] is True
    assert plan["steps"][0]["status"] == "pending"
    assert plan["steps"][0]["permission_level"] == "write"
    assert not target.exists()  # nothing ran without approval
    assert plan_store.get_plan(plan["id"]) is not None  # parked for /approve


async def test_execute_unachievable_goal_fails_with_reason(client):
    use_provider([plan_json([], reason="No email tool is available")])
    response = await client.post("/api/agent/execute", json={"goal": "email Ali"})
    assert response.status_code == 200
    plan = response.json()
    assert plan["status"] == "failed"
    assert "email" in plan["message"].lower()
    assert plan_store.get_plan(plan["id"]) is None  # failed plans are not parked


async def test_execute_rejects_empty_goal(client):
    response = await client.post("/api/agent/execute", json={"goal": ""})
    assert response.status_code == 422  # Pydantic min_length


# ============================================================== /agent/approve

async def test_approve_executes_parked_plan(client, tmp_path):
    target = tmp_path / "notes.txt"
    steps = [step("Create notes.txt", "create_file", path=str(target), content="hi")]
    use_provider([plan_json(steps), plan_json(steps)])
    plan = (await client.post("/api/agent/execute", json={"goal": "create notes.txt"})).json()

    response = await client.post(
        "/api/agent/approve", json={"plan_id": plan["id"], "approved": True}
    )
    assert response.status_code == 200
    final = response.json()
    assert final["status"] == "completed"
    assert final["steps"][0]["status"] == "completed"
    assert target.read_text(encoding="utf-8") == "hi"


async def test_cancel_skips_all_pending_steps(client, tmp_path):
    target = tmp_path / "notes.txt"
    steps = [step("Create notes.txt", "create_file", path=str(target), content="hi")]
    use_provider([plan_json(steps), plan_json(steps)])
    plan = (await client.post("/api/agent/execute", json={"goal": "create notes.txt"})).json()

    response = await client.post(
        "/api/agent/approve", json={"plan_id": plan["id"], "approved": False}
    )
    assert response.status_code == 200
    final = response.json()
    assert final["status"] == "cancelled"
    assert final["steps"][0]["status"] == "skipped"
    assert not target.exists()


async def test_approve_unknown_plan_returns_404(client):
    response = await client.post(
        "/api/agent/approve", json={"plan_id": "nope", "approved": True}
    )
    assert response.status_code == 404


async def test_approval_is_consumed_second_answer_404s(client, tmp_path):
    target = tmp_path / "once.txt"
    steps = [step("Create once.txt", "create_file", path=str(target), content="x")]
    use_provider([plan_json(steps), plan_json(steps)])
    plan = (await client.post("/api/agent/execute", json={"goal": "create once.txt"})).json()

    first = await client.post(
        "/api/agent/approve", json={"plan_id": plan["id"], "approved": True}
    )
    assert first.status_code == 200
    second = await client.post(
        "/api/agent/approve", json={"plan_id": plan["id"], "approved": True}
    )
    assert second.status_code == 404  # pop_plan consumed it


async def test_approve_survives_backend_restart(client, tmp_path):
    """Phase 3.5: a parked plan is persisted to SQLite, so approving it after
    the in-memory store is gone (restart) still executes it."""
    target = tmp_path / "survivor.txt"
    steps = [step("Create survivor.txt", "create_file", path=str(target), content="alive")]
    use_provider([plan_json(steps), plan_json(steps)])
    plan = (await client.post(
        "/api/agent/execute", json={"goal": "create survivor.txt"}
    )).json()
    assert plan["status"] == "awaiting_approval"

    plan_store._PENDING_PLANS.clear()  # simulate a backend restart

    response = await client.post(
        "/api/agent/approve", json={"plan_id": plan["id"], "approved": True}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert target.read_text(encoding="utf-8") == "alive"
    # Consumed for good — SQLite row deleted with the answer
    second = await client.post(
        "/api/agent/approve", json={"plan_id": plan["id"], "approved": True}
    )
    assert second.status_code == 404


async def test_destructive_goal_full_cycle(client, tmp_path):
    victim = tmp_path / "old.log"
    victim.write_text("bye")
    steps = [step("Delete old.log", "delete_file", path=str(victim))]
    use_provider([plan_json(steps), plan_json(steps)])

    plan = (await client.post(
        "/api/agent/execute", json={"goal": "delete old.log", "session_id": "s-destroy"}
    )).json()
    assert plan["status"] == "awaiting_approval"
    assert plan["steps"][0]["permission_level"] == "destructive"
    assert victim.exists()

    final = (await client.post(
        "/api/agent/approve", json={"plan_id": plan["id"], "approved": True}
    )).json()
    assert final["status"] == "completed"
    assert not victim.exists()


# ================================================================= /activity

async def test_activity_lists_newest_first(client, tmp_path):
    (tmp_path / "a.txt").write_text("x")
    read_steps = [step("List files", "list_directory", path=str(tmp_path))]
    file_steps = [
        step("Read a.txt", "read_file", path=str(tmp_path / "a.txt")),
    ]
    use_provider([plan_json(read_steps), plan_json(read_steps)])
    await client.post("/api/agent/execute", json={"goal": "list", "session_id": "s-act"})
    use_provider([plan_json(file_steps), plan_json(file_steps)])
    await client.post("/api/agent/execute", json={"goal": "read", "session_id": "s-act"})

    response = await client.get("/api/activity")
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 2
    assert rows[0]["tool_name"] == "read_file"   # newest first
    assert rows[1]["tool_name"] == "list_directory"
    assert rows[0]["success"] is True
    assert rows[0]["permission_level"] == "read"
    assert isinstance(rows[0]["parameters"], dict)  # JSON text → parsed dict
    assert rows[0]["duration_ms"] >= 0
    assert rows[0]["created_at"]  # ISO timestamp present


async def test_activity_respects_limit(client, tmp_path):
    steps = [step("List files", "list_directory", path=str(tmp_path))]
    for _ in range(3):
        use_provider([plan_json(steps), plan_json(steps)])
        await client.post("/api/agent/execute", json={"goal": "list"})

    response = await client.get("/api/activity", params={"limit": 2})
    assert len(response.json()) == 2
    assert (await client.get("/api/activity", params={"limit": 0})).status_code == 422


async def test_activity_filtered_by_session(client, tmp_path):
    steps = [step("List files", "list_directory", path=str(tmp_path))]
    use_provider([plan_json(steps), plan_json(steps)])
    await client.post("/api/agent/execute", json={"goal": "list", "session_id": "s-one"})
    use_provider([plan_json(steps), plan_json(steps)])
    await client.post("/api/agent/execute", json={"goal": "list", "session_id": "s-two"})

    one = (await client.get("/api/activity/s-one")).json()
    assert len(one) == 1
    assert one[0]["session_id"] == "s-one"
    assert (await client.get("/api/activity/s-none")).json() == []


async def test_blocked_unapproved_attempt_is_audited(client, tmp_path):
    """The approval gate logs blocked WRITE attempts — visible via /api/activity."""
    target = tmp_path / "notes.txt"
    steps = [step("Create notes.txt", "create_file", path=str(target), content="hi")]
    use_provider([plan_json(steps), plan_json(steps)])
    plan = (await client.post(
        "/api/agent/execute", json={"goal": "create notes.txt", "session_id": "s-gate"}
    )).json()
    # The planner pauses BEFORE calling the tool, so no blocked row yet —
    # nothing in the log until the user answers.
    assert (await client.get("/api/activity/s-gate")).json() == []

    await client.post("/api/agent/approve", json={"plan_id": plan["id"], "approved": True})
    rows = (await client.get("/api/activity/s-gate")).json()
    assert len(rows) == 1
    assert rows[0]["tool_name"] == "create_file"
    assert rows[0]["success"] is True
    assert rows[0]["permission_level"] == "write"


# =============================== inline outcome delivery (live bug 2026-07-12)
# "find all PDF files in downloads ... tell me how many" paused on a folder
# question; the user CLICKED an option; the plan completed and the answer
# arrived NOWHERE — the summary/persistence machinery lived only in the
# typed-chat SSE path. The approve/choose endpoints must return the outcome
# (outcome_text) and persist it, so clicked and typed answers are equivalent
# end to end.

class StreamingFakeProvider(FakeProvider):
    """FakeProvider whose stream_chat yields real deltas — exercises the
    LLM-summary path of completed_plan_text (base class yields '' → fallback)."""

    def __init__(self, responses: List[str], stream_deltas: List[str]) -> None:
        super().__init__(responses)
        self._stream_deltas = list(stream_deltas)

    async def stream_chat(self, messages, temperature=0.7, max_tokens=None):
        for delta in self._stream_deltas:
            yield delta


def question_json(text: str, options: list) -> str:
    return json.dumps({"steps": [], "question": {"text": text, "options": options}})


async def history(client, session_id: str) -> list:
    return (await client.get(f"/chat/sessions/{session_id}/messages")).json()


async def park_question_plan(client, tmp_path, session_id: str, provider_cls=None, **kw):
    """Execute a goal whose draft pauses on a which-folder question with two
    REAL directories as options (invented option paths are rejected in code)."""
    dir_a = tmp_path / "DownloadsA"
    dir_b = tmp_path / "DownloadsB"
    dir_a.mkdir()
    dir_b.mkdir()
    (dir_a / "one.pdf").write_bytes(b"x" * 10)
    (dir_a / "two.pdf").write_bytes(b"y" * 999)
    list_step = [step("List the chosen folder", "list_directory", path=str(dir_a))]
    responses = [
        question_json("Which folder did you mean?", [str(dir_a), str(dir_b)]),
        plan_json(list_step),  # the revise round the answer feeds
    ]
    if provider_cls is None:
        use_provider(responses)
    else:
        provider = provider_cls(responses, **kw)
        app.dependency_overrides[get_llm_provider] = lambda: provider
    plan = (await client.post(
        "/api/agent/execute",
        json={"goal": "how many pdf files are in my downloads folder", "session_id": session_id},
    )).json()
    assert plan["status"] == "awaiting_choice"
    assert plan["outcome_text"] is None  # paused — the card carries the question
    return plan, dir_a


async def test_choose_completed_returns_outcome_and_persists_history(client, tmp_path):
    plan, dir_a = await park_question_plan(client, tmp_path, "s-choose-done")

    final = (await client.post(
        "/api/agent/choose", json={"plan_id": plan["id"], "answer": str(dir_a)}
    )).json()
    assert final["status"] == "completed"
    # The outcome text IS the answer (deterministic fallback here — the fake
    # provider streams nothing): it must carry the real results.
    assert final["outcome_text"]
    assert "one.pdf" in final["outcome_text"]
    assert "two.pdf" in final["outcome_text"]

    # History parity with the typed path: the clicked answer as a user
    # message, the outcome as an assistant message — a reload still has both.
    rows = await history(client, "s-choose-done")
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[0]["content"] == str(dir_a)
    assert "one.pdf" in rows[1]["content"]


async def test_choose_outcome_uses_llm_summary_when_available(client, tmp_path):
    plan, dir_a = await park_question_plan(
        client, tmp_path, "s-choose-llm",
        provider_cls=StreamingFakeProvider,
        stream_deltas=["There are 2 PDFs — ", "the largest is two.pdf."],
    )
    final = (await client.post(
        "/api/agent/choose", json={"plan_id": plan["id"], "answer": str(dir_a)}
    )).json()
    assert final["status"] == "completed"
    assert final["outcome_text"] == "There are 2 PDFs — the largest is two.pdf."
    rows = await history(client, "s-choose-llm")
    assert rows[-1]["content"] == "There are 2 PDFs — the largest is two.pdf."


async def test_choose_leading_to_approval_pause_has_no_outcome_yet(client, tmp_path):
    """Answer → revise emits a WRITE step → the plan re-parks for approval:
    no outcome_text (the card carries the ask live), but the deterministic
    approval text is persisted so a reload still shows the pending ask.
    Approving then delivers the outcome."""
    dir_a = tmp_path / "A"
    dir_b = tmp_path / "B"
    dir_a.mkdir()
    dir_b.mkdir()
    target = dir_a / "notes.txt"
    use_provider([
        question_json("Which folder?", [str(dir_a), str(dir_b)]),
        plan_json([step("Create notes.txt", "create_file", path=str(target), content="hi")]),
    ])
    plan = (await client.post(
        "/api/agent/execute",
        json={"goal": "create notes.txt in the right folder", "session_id": "s-repause"},
    )).json()

    paused = (await client.post(
        "/api/agent/choose", json={"plan_id": plan["id"], "answer": str(dir_a)}
    )).json()
    assert paused["status"] == "awaiting_approval"
    assert paused["outcome_text"] is None
    rows = await history(client, "s-repause")
    assert rows[0]["content"] == str(dir_a)                    # the answer
    assert "needs your approval" in rows[1]["content"]         # the pending ask
    assert not target.exists()

    final = (await client.post(
        "/api/agent/approve", json={"plan_id": paused["id"], "approved": True}
    )).json()
    assert final["status"] == "completed"
    assert final["outcome_text"]
    assert target.read_text(encoding="utf-8") == "hi"
    rows = await history(client, "s-repause")
    assert rows[-1]["role"] == "assistant"
    assert rows[-1]["content"] == final["outcome_text"]


async def test_approve_completed_returns_outcome_without_user_message(client, tmp_path):
    """The Approve button is not an utterance — only the assistant outcome is
    persisted (the choose path persists the clicked answer, this one does not)."""
    target = tmp_path / "made.txt"
    steps = [step("Create made.txt", "create_file", path=str(target), content="ok")]
    use_provider([plan_json(steps), plan_json(steps)])
    plan = (await client.post(
        "/api/agent/execute", json={"goal": "create made.txt", "session_id": "s-approve-out"}
    )).json()

    final = (await client.post(
        "/api/agent/approve", json={"plan_id": plan["id"], "approved": True}
    )).json()
    assert final["status"] == "completed"
    assert final["outcome_text"]
    rows = await history(client, "s-approve-out")
    assert [r["role"] for r in rows] == ["assistant"]
    assert rows[0]["content"] == final["outcome_text"]


async def test_cancel_persists_text_but_returns_no_outcome(client, tmp_path):
    """The cancelled banner on the card is the live feedback (no duplicate
    bubble), but history still records that nothing ran."""
    target = tmp_path / "never.txt"
    steps = [step("Create never.txt", "create_file", path=str(target), content="x")]
    use_provider([plan_json(steps), plan_json(steps)])
    plan = (await client.post(
        "/api/agent/execute", json={"goal": "create never.txt", "session_id": "s-cancel-out"}
    )).json()

    final = (await client.post(
        "/api/agent/approve", json={"plan_id": plan["id"], "approved": False}
    )).json()
    assert final["status"] == "cancelled"
    assert final["outcome_text"] is None
    rows = await history(client, "s-cancel-out")
    assert len(rows) == 1
    assert "cancel" in rows[0]["content"].lower()
    assert not target.exists()


async def test_sessionless_plan_returns_outcome_without_persistence(client, tmp_path):
    """Direct API callers (no session) still get the answer in the response;
    nothing is written to any chat history."""
    target = tmp_path / "nosess.txt"
    steps = [step("Create nosess.txt", "create_file", path=str(target), content="x")]
    use_provider([plan_json(steps), plan_json(steps)])
    plan = (await client.post(
        "/api/agent/execute", json={"goal": "create nosess.txt"}
    )).json()

    final = (await client.post(
        "/api/agent/approve", json={"plan_id": plan["id"], "approved": True}
    )).json()
    assert final["status"] == "completed"
    assert final["outcome_text"]


async def test_choose_failed_plan_returns_deterministic_failure(client, tmp_path):
    """A step that fails after the answer (read_file on a directory — a
    non-recoverable class, no ask-not-fail) and a replan that declares the
    goal impossible fail the plan: the failure text (never LLM-paraphrased)
    must arrive as the outcome."""
    dir_a = tmp_path / "FA"
    dir_b = tmp_path / "FB"
    dir_a.mkdir()
    dir_b.mkdir()
    use_provider([
        question_json("Which folder?", [str(dir_a), str(dir_b)]),
        plan_json([step("Read the folder", "read_file", path=str(dir_a))]),
        plan_json([], reason="That folder cannot be processed."),
    ])
    plan = (await client.post(
        "/api/agent/execute",
        json={"goal": "process the folder", "session_id": "s-choose-fail"},
    )).json()

    final = (await client.post(
        "/api/agent/choose", json={"plan_id": plan["id"], "answer": str(dir_a)}
    )).json()
    assert final["status"] == "failed"
    assert final["outcome_text"]
    assert "couldn't" in final["outcome_text"].lower()
    rows = await history(client, "s-choose-fail")
    assert rows[-1]["content"] == final["outcome_text"]
