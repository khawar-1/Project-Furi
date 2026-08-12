"""
Furi OS — Mid-run Interrupt Routing (2026-08-03)

"When Furi is performing a task and it got something wrong, I want to pause
it and tell it what to do."

Until now a message typed while an agent was working had two possible fates,
both bad:

  - "pause" / "stop" / "wait" carry no domain noun and no action verb, so
    `looks_like_task` returns False and they fell through to plain chat —
    which cannot stop anything, and whose model would happily reassure the
    user while the agent kept going; or
  - a correction that DOES name a noun ("use the D drive downloads instead")
    passed the task gate and started a SECOND concurrent agent, while the
    first carried on doing the wrong thing.

So an explicit stop is routed here: set the cooperative pause flag on the live
run, and — if the message carried an instruction ("stop, use the D drive one")
— hand that along so it is applied the moment the run stops.

Placement: after the reminder / routine / continuation routers, before the task
router (chat.py). It must precede the task gate for the same reason its
siblings do — otherwise "stop the download" is replanned as a fresh one-off.

WHAT MAKES A GENEROUS TRIGGER SAFE: this router only fires when the session
actually has a RUNNING task. With no live run it returns None and the message
behaves exactly as it does today, so the words "stop" and "wait" keep their
ordinary meaning in ordinary conversation. That is the same guard
continuation_router uses (it requires a recently SETTLED task).

Safety: pausing runs nothing and changes nothing. Continuing a paused plan goes
through the existing approval gate, and steering it re-derives the remaining
steps from a goal string, so every guard re-applies. No LLM call is made here;
the trigger and the acknowledgement are deterministic.
"""
import re
from typing import Optional

from fastapi.responses import StreamingResponse
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.task_runner import request_task_pause
from app.db.models import Task
from app.db.persist import persist_message_best_effort
from app.db.schemas import ChatRequest, StreamChunk

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

# The stop trigger, anchored to the START of the message. Anchoring is what
# keeps it narrow: "stop" as an interjection opens a sentence ("stop, that's
# the wrong folder"), whereas the same word buried mid-sentence is usually
# about the task's own subject ("find the files that stop the build").
#
# Deliberately NOT a general "is the user unhappy?" classifier — that is a
# judgement, and this codebase has measured keyword lists standing in for
# judgements at zero three times. This asks one certain question: did the user
# open with an explicit stop word?
_PAUSE_RE = re.compile(
    r"^\s*(?:hey\s+|ok(?:ay)?[,\s]+|no[,\s]+|oi\s+|furi[,\s]+|jarvis[,\s]+)*"
    r"(?:"
    r"pause|hold\s+on|hold\s+up|hang\s+on|halt|"
    r"wait(?:\s+a\s+(?:sec(?:ond)?|min(?:ute)?|moment))?|"
    r"stop|stahp|abort"
    r")"
    r"\b",
    re.IGNORECASE,
)

# ...unless the stop is aimed at MEDIA. "stop the music" / "stop playing" is a
# stop_media task and must keep reaching the task router, not pause the agent
# that is playing it. Checked against the whole message.
_MEDIA_STOP_RE = re.compile(
    r"\bstop\b[\s\w]*\b(?:music|song|songs|video|track|playback|playing|"
    r"audio|it\s+playing)\b",
    re.IGNORECASE,
)

# The words between the stop and the instruction ("stop — and use D:" →
# "use D:"). Stripped so the steer reads as a plain instruction.
_STEER_LEAD_RE = re.compile(
    r"^[\s,.!;:—–-]*(?:and\s+|then\s+|instead\s+|please\s+|"
    r"i\s+(?:want|need)\s+you\s+to\s+|you\s+(?:should|need\s+to)\s+)*",
    re.IGNORECASE,
)

# A trailing remnant that is not really an instruction ("stop it", "stop
# please", "wait a moment now"). Anything this short is a bare pause.
_STEER_MIN_WORDS = 2
_STEER_NOISE = frozenset({
    "it", "that", "this", "please", "now", "furi", "jarvis", "everything", "them",
    "the", "task", "there", "ok", "okay", "right", "yourself",
})


def looks_like_pause(message: str) -> tuple[bool, str]:
    """(is a stop request, the instruction that came with it).

    The instruction is "" for a bare pause. Both halves are decided in code —
    a steer is only ever the user's own remaining words, never anything
    inferred."""
    text = (message or "").strip()
    if not text:
        return False, ""
    if _MEDIA_STOP_RE.search(text):
        return False, ""
    match = _PAUSE_RE.match(text)
    if match is None:
        return False, ""
    remainder = _STEER_LEAD_RE.sub("", text[match.end():]).strip()
    words = [w for w in re.findall(r"[\w'&.:\\/-]+", remainder.lower()) if w]
    meaningful = [w for w in words if w not in _STEER_NOISE]
    if len(words) < _STEER_MIN_WORDS or not meaningful:
        return True, ""
    return True, remainder


async def running_tasks(db: AsyncSession, session_id: str) -> list[Task]:
    """The session's live background tasks, newest first. Only `running` — a
    task already stopped at an approval card or a question owns the next
    message through its own channel, and hijacking that would break a flow
    that works (the continuation_router rule, inverted).

    ALL of them, not just the newest: several domain agents can work at once,
    and "stop" plainly means "stop what you're doing". Pausing is LOSSLESS —
    over-pausing costs one "carry on", while pausing the wrong one of two
    leaves the agent the user is actually watching doing the wrong thing,
    which is the entire defect this router exists for."""
    if not session_id:
        return []
    rows = await db.execute(
        select(Task)
        .where(Task.session_id == session_id, Task.status == "running")
        .order_by(Task.created_at.desc())
    )
    return list(rows.scalars().all())


async def maybe_handle_interrupt(
    request: ChatRequest, session_id: str, db: AsyncSession
) -> Optional[StreamingResponse]:
    """Returns a StreamingResponse when the latest message stops a running
    task; None sends it down the normal routing chain unchanged."""
    user_msgs = [m.content for m in request.messages if m.role == "user"]
    message = user_msgs[-1].strip() if user_msgs else ""
    if not message:
        return None
    is_pause, steer = looks_like_pause(message)
    if not is_pause:
        return None

    # Never hijack a reply owed to an already-open question elsewhere — peek
    # without creating sessions (get_session() would be a side effect). A
    # paused/asking plan already owns the next message via its own path.
    from app.agents import get_choice_plan_for_session
    from app.memory.conversation_state import CONVERSATION_SESSIONS

    sess = CONVERSATION_SESSIONS.get(session_id)
    if sess is not None and (
        sess.pending_resolution is not None or sess.pending_creation is not None
    ):
        return None
    if await get_choice_plan_for_session(db, session_id) is not None:
        return None

    tasks = await running_tasks(db, session_id)
    if not tasks:
        return None

    return _stream_pause(tasks, message, steer, session_id, db)


def _stream_pause(
    tasks: list[Task],
    message: str,
    steer: str,
    session_id: str,
    db: AsyncSession,
) -> StreamingResponse:
    from app.agents.agent_registry import agent_for_key

    newest = tasks[0]
    agent = agent_for_key(newest.domain)

    async def event_generator():
        await persist_message_best_effort(
            db, session_id, "user", message, what="interrupt user message",
        )
        # The steer rides on the NEWEST run only — the one the ack names. A
        # correction is about one piece of work, and silently applying it to
        # every paused plan would replan tasks the user was not talking about.
        # The others simply hold; the Agents panel resumes them.
        stopped = [t for i, t in enumerate(tasks) if request_task_pause(t.id, steer if i == 0 else "")]
        if stopped:
            # HONEST about the cooperative rule: the step in flight finishes.
            # Promising an instant stop would be a claim we cannot keep, and a
            # 30s command or a browser action mid-click is exactly when a user
            # reaches for this.
            who = (
                agent.display_name.lower()
                if len(stopped) == 1
                else f"all {len(stopped)} agents"
            )
            text = (
                f"Stopping — {who} will finish the step "
                f"{'it' if len(stopped) == 1 else 'each'} is on and then hold. "
                f"Nothing further will run."
            )
            text += (
                f' I\'ll pick "{_clip(stopped[0].goal, 48)}" back up with your '
                "correction as soon as it stops."
                if steer
                else " Tell me what to change, or say carry on."
            )
        else:
            # The run settled between the DB read and the flag — say so rather
            # than promise a pause that will never arrive.
            text = (
                f'That task ("{_clip(newest.goal)}") just finished on its own, so '
                "there was nothing left to stop. Tell me what you'd like "
                "changed and I'll take another run at it."
            )
        await persist_message_best_effort(
            db, session_id, "assistant", text, what="interrupt ack",
        )
        yield f"data: {StreamChunk(delta=text).model_dump_json()}\n\n"
        yield "data: [DONE]\n\n"

    logger.info(
        f"Chat message interrupts {len(tasks)} running task(s) "
        f"(newest {newest.id}): '{message[:60]}'"
        + (f" (steer: '{steer[:60]}')" if steer else "")
    )
    return StreamingResponse(
        event_generator(), media_type="text/event-stream", headers=_SSE_HEADERS,
    )


def _clip(text: str, cap: int = 70) -> str:
    text = (text or "").strip()
    return text if len(text) <= cap else text[: cap - 1] + "…"
