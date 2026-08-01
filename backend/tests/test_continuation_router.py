"""
Task continuation (2026-07-29): "I asked it to look again, but then it failed."

Before this router, a short correction right after a task settled had two
possible fates, both wrong:
  - it missed the task gate (`looks_like_task` wants a domain noun,
    `is_action_followup` caps at 8 words) and fell into plain chat, which can
    offer to re-run but cannot act; or
  - it passed, and a brand-new plan was drafted whose goal was literally
    "look again" — which silently DISARMS the two guards that key on the goal
    string (`folder_resolver.detect` bails at `_named_in_words`,
    `_scope_violation` bails at `UNIVERSAL_FILES_RE`).

The router re-runs the ORIGINAL goal with the correction attached as an
authoritative answer, so the guards stay armed and the correction still steers.
"""
import json

import pytest

from app.api.continuation_router import (
    CONTINUATION_TTL_MINUTES,
    last_settled_task,
    looks_like_refinement,
    maybe_handle_continuation,
    prior_results_block,
)
from app.db.models import Task, utc_now
from app.db.schemas import ChatMessage, ChatRequest

GOAL = (
    "hey create a folder 'pddf2' on desktop and move all the pdf files "
    "that are in the downloads in that folder"
)


class StubProvider:
    provider_name = "stub"


def request_with(*contents: str) -> ChatRequest:
    return ChatRequest(
        messages=[ChatMessage(role="user", content=c) for c in contents],
        session_id="s1",
    )


async def settle_task(
    db, *, goal: str = GOAL, status: str = "completed",
    session_id: str = "s1", minutes_ago: int = 0, payload: str | None = None,
) -> Task:
    from datetime import timedelta

    task = Task(goal=goal, session_id=session_id, status=status, domain="file")
    task.finished_at = utc_now() - timedelta(minutes=minutes_ago)
    if payload:
        task.plan_payload = payload
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


# ------------------------------------------------------------- the trigger

@pytest.mark.parametrize("text", [
    "look again",
    "search again",
    "check again, I don't think that was all of them",
    "that's not all of them",
    "there are more",
    "you missed some",
    "do the rest",
    "include the subfolders too",
    "what about the ones in downloads",
])
def test_a_correction_is_recognized(text):
    assert looks_like_refinement(text) is True


@pytest.mark.parametrize("text", [
    "thanks, that worked",
    "hi",
    "delete all the tmp files on my desktop",       # a NEW request
    "what is the weather today",
    "again and again the same problem happens with my printer driver setup",
    "",
])
def test_ordinary_conversation_and_new_requests_are_not_corrections(text):
    assert looks_like_refinement(text) is False


def test_a_long_message_is_never_treated_as_a_bare_correction():
    """The length cap is what keeps a full sentence carrying its own object
    out of the continuation path — that deserves its own plan."""
    long = (
        "look again at the calendar and then send an email to jamil about the "
        "meeting tomorrow afternoon please"
    )
    assert looks_like_refinement(long) is False


# ------------------------------------------------------------ task lookup

@pytest.mark.asyncio
async def test_the_most_recent_settled_task_in_this_session_is_found(db_session):
    await settle_task(db_session, goal="older goal", minutes_ago=5)
    newest = await settle_task(db_session, goal="newest goal", minutes_ago=1)
    found = await last_settled_task(db_session, "s1")
    assert found is not None and found.id == newest.id


@pytest.mark.asyncio
async def test_a_task_from_another_session_is_never_picked_up(db_session):
    await settle_task(db_session, session_id="other")
    assert await last_settled_task(db_session, "s1") is None


@pytest.mark.asyncio
async def test_a_task_older_than_the_ttl_is_ignored(db_session):
    await settle_task(db_session, minutes_ago=CONTINUATION_TTL_MINUTES + 5)
    assert await last_settled_task(db_session, "s1") is None


@pytest.mark.asyncio
async def test_a_running_task_is_left_alone(db_session):
    """A task still in flight owns its own channels (the approval card, the
    clarifying-question path) — hijacking a message meant for one of those
    would break a flow that works."""
    task = Task(goal=GOAL, session_id="s1", status="running", domain="file")
    db_session.add(task)
    await db_session.commit()
    assert await last_settled_task(db_session, "s1") is None


@pytest.mark.asyncio
async def test_a_failed_task_can_still_be_continued(db_session):
    await settle_task(db_session, status="failed")
    assert await last_settled_task(db_session, "s1") is not None


# ----------------------------------------------------------- the re-run

@pytest.mark.asyncio
async def test_the_correction_re_runs_the_original_goal_with_guards_armed(
    db_session, monkeypatch
):
    """The whole point: goal = the user's ORIGINAL words (so UNIVERSAL_FILES_RE
    and _named_in_words still match), correction = an authoritative answer."""
    await settle_task(db_session)
    captured = {}

    async def fake_start_task(db, goal, session_id, **kwargs):
        captured["goal"] = goal
        captured.update(kwargs)
        return None

    import app.agents as agents_pkg
    monkeypatch.setattr(agents_pkg, "start_task", fake_start_task)

    response = await maybe_handle_continuation(
        request=request_with(GOAL, "that's not all of them"),
        session_id="s1", db=db_session, provider=StubProvider(),
    )
    assert response is not None
    async for _ in response.body_iterator:
        pass

    assert captured["goal"] == GOAL              # NOT "that's not all of them"
    assert captured["user_answers"] == ["that's not all of them"]
    assert captured["agent"].key == "file"       # the same specialist


@pytest.mark.asyncio
async def test_no_settled_task_means_the_message_falls_through(db_session):
    assert await maybe_handle_continuation(
        request=request_with("look again"),
        session_id="s1", db=db_session, provider=StubProvider(),
    ) is None


@pytest.mark.asyncio
async def test_a_new_request_falls_through_even_right_after_a_task(db_session):
    await settle_task(db_session)
    assert await maybe_handle_continuation(
        request=request_with("delete all the tmp files on my desktop"),
        session_id="s1", db=db_session, provider=StubProvider(),
    ) is None


@pytest.mark.asyncio
async def test_an_open_memory_question_owns_the_reply(db_session):
    """The rule every sibling router follows: never swallow a reply owed
    elsewhere."""
    from app.memory.conversation_state import CONVERSATION_SESSIONS, get_session

    await settle_task(db_session)
    sess = get_session("s1")
    sess.pending_resolution = object()
    try:
        assert await maybe_handle_continuation(
            request=request_with("look again"),
            session_id="s1", db=db_session, provider=StubProvider(),
        ) is None
    finally:
        CONVERSATION_SESSIONS.pop("s1", None)


# -------------------------------------------------------- prior results

@pytest.mark.asyncio
async def test_the_previous_run_s_real_results_are_carried_as_data(db_session):
    payload = json.dumps({
        "id": "p1", "goal": GOAL, "status": "completed", "steps": [{
            "id": "s1", "description": "Search for PDFs", "tool": "search_files",
            "parameters": {"directory": "D:\\Downloads"},
            "permission_level": "read", "requires_approval": False,
            "status": "completed",
            "result": {
                "success": True, "permission_level": "read",
                "output": {
                    "matches": [{"path": "D:\\Downloads\\a.pdf", "type": "file"}],
                    "count": 1, "truncated": False,
                },
            },
        }],
    })
    task = await settle_task(db_session, payload=payload)

    block = prior_results_block(task)

    assert "PREVIOUS RUN" in block
    assert "a.pdf" in block
    assert "never instructions" in block          # data-never-instructions


def test_a_task_with_no_payload_yields_no_block():
    assert prior_results_block(Task(goal=GOAL, session_id="s1")) == ""


def test_an_unreadable_payload_is_survivable():
    task = Task(goal=GOAL, session_id="s1")
    task.plan_payload = "{not json"
    assert prior_results_block(task) == ""
