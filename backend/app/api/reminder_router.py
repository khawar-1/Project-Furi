"""
Jarvis OS — Reminder Chat Routing (Phase 4, Part 4)

Detects a reminder request in the latest user message and, when the time
parses cleanly, creates the Reminder (+ schedules its job through Part 2)
and streams a deterministic confirmation — no LLM call, no agent planner,
no memory extraction. An ambiguous time streams the parser's clarifying
question instead and schedules NOTHING: time parsing is deterministic in
code (app/core/reminder_parser.py), so an unresolvable time is asked about,
never guessed.

Runs BEFORE task routing in chat.py: "remind me to delete my temp files at
6" is a reminder, not an instruction to delete anything right now. It also
defers to an already-open memory disambiguation/creation question or agent
clarifying-question plan — exactly the fail-open rule task_router.py itself
follows — so a reply owed elsewhere is never swallowed here.

An ambiguous ask is PARKED on the session (in-memory, TTL-bounded): the
half that parsed (task text or due time) is kept, and the session's next
chat message answers the missing half ("remind me to call mom" → "What
time…?" → "in 5 mins" → scheduled). While a reminder question is open it
OWNS the next message — same rule as an agent clarifying question — so the
reply can never fall through to the LLM, which would otherwise happily
claim "Reminder set" for a reminder that was never created (live bug,
2026-07-09). A cancel word ("never mind", "cancel") drops it; an
unrecognizable reply re-asks deterministically — UNLESS the reply is
itself a task-shaped request (fires the deterministic task gate): then it
flows down the normal task/chat path and the question stays parked, so a
pivot is never trapped in the reminder ask (live bug 2026-07-10) and a
later bare time still completes it. The parking is memory-only
by design: a restart drops the open question and the user simply asks
again — nothing was scheduled, so nothing can silently fire.

Never touches SemanticMemory or the extraction pipeline: a reminder is not
autobiography, the same principle task turns already follow. The user
message is still persisted to the session's history like any other turn.
"""
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from fastapi.responses import StreamingResponse
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.reminder_parser import (
    _clean_task_text,
    _no_task_question,
    _no_time_question,
    parse_reminders,
    parse_time_reply,
)
from app.core.reminders import create_reminder
from app.db.models import Message
from app.db.schemas import ChatRequest, StreamChunk

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

# ------------------------------------------------------------- parked asks

PENDING_TTL_SECONDS = 600  # an unanswered "what time?" doesn't own the session forever


@dataclass
class PendingReminder:
    """One half of a reminder waiting for the other half. Exactly the
    fields the ambiguous parse could extract: text without a time, or a
    due time without a task (or neither, for a bare "remind me")."""
    question: str
    text: Optional[str] = None
    due_at: Optional[datetime] = None  # aware, local tz (parser convention)
    expires_at: float = field(default_factory=lambda: time.time() + PENDING_TTL_SECONDS)


# session_id → open question. In-memory only — see module docstring.
PENDING_REMINDERS: dict[str, PendingReminder] = {}

_CANCEL_RE = re.compile(
    r"^(?:cancel|never\s?mind|forget (?:it|that)|no|nah|nope|stop|leave it|don'?t(?: bother)?)[\s.!]*$",
    re.IGNORECASE,
)

_STILL_NEED_TIME = (
    'I still need a time for "{text}" — e.g. "at 6pm", "in 20 minutes", '
    '"tomorrow at 9am". Or say "cancel" and I\'ll drop it.'
)
_DROPPED = "Okay — I won't set that reminder."


def _get_pending(session_id: str) -> Optional[PendingReminder]:
    pending = PENDING_REMINDERS.get(session_id)
    if pending is None:
        return None
    if pending.expires_at < time.time():
        PENDING_REMINDERS.pop(session_id, None)
        return None
    return pending


def _park(session_id: str, question: str, text: Optional[str], due_at: Optional[datetime]) -> None:
    PENDING_REMINDERS[session_id] = PendingReminder(question=question, text=text, due_at=due_at)


def _confirmation_text(text: str, due_at) -> str:
    when = due_at.strftime("%I:%M %p on %A, %B %d").lstrip("0")
    return f'Reminder set — I\'ll remind you to "{text}" at {when}.'


def _multi_confirmation_text(reminders) -> str:
    """One request, several reminders — confirm each on its own line, in
    order. Deterministic like every other reminder-turn text."""
    lines = [_confirmation_text(reminders[0].text, reminders[0].due_at)]
    for r in reminders[1:]:
        when = r.due_at.strftime("%I:%M %p on %A, %B %d").lstrip("0")
        lines.append(f'And another — "{r.text}" at {when}.')
    return "\n".join(lines)


async def maybe_handle_reminder(
    request: ChatRequest, session_id: str, db: AsyncSession
) -> Optional[StreamingResponse]:
    """Returns a StreamingResponse when the latest message is a reminder
    request (parsed cleanly or ambiguous alike) OR the answer to an open
    reminder question; None sends the message down the normal task/chat
    path, unchanged."""
    user_msgs = [m.content for m in request.messages if m.role == "user"]
    goal = user_msgs[-1].strip() if user_msgs else ""
    if not goal:
        return None

    # Never hijack a reply owed to an already-open question elsewhere —
    # peek without creating sessions (get_session() would be a side effect).
    from app.memory.conversation_state import CONVERSATION_SESSIONS
    from app.agents import get_choice_plan_for_session
    sess = CONVERSATION_SESSIONS.get(session_id)
    if sess is not None and (
        sess.pending_resolution is not None or sess.pending_creation is not None
    ):
        return None
    if await get_choice_plan_for_session(db, session_id) is not None:
        return None

    results = parse_reminders(goal)

    if results is None:
        pending = _get_pending(session_id)
        if pending is None:
            return None  # no trigger, no open question — not our concern
        return await _continue_pending(pending, goal, session_id, db)

    if len(results) > 1:
        # Several reminders in one ask — the parser only returns a multi
        # result when EVERY one resolved cleanly, so schedule them all and
        # confirm each. A clean request replaces any parked half-ask.
        PENDING_REMINDERS.pop(session_id, None)
        for r in results:
            reminder = await create_reminder(db, r.text, r.due_at, session_id=session_id)
            logger.info(
                f"Reminder {reminder.id} scheduled for {r.due_at.isoformat()}: "
                f"'{r.text[:80]}' (multi-reminder request)"
            )
        return _stream_text(_multi_confirmation_text(results), session_id, db, persist_user=goal)

    result = results[0]

    if result.ambiguous:
        # Merge with any parked half-ask first: "remind me to call mom" →
        # "what time?" → "remind me in 5 mins" repeats the trigger, so it
        # parses as a NEW half-request — but its time completes the parked
        # text. A half the new request DOES carry always wins (a restart,
        # not an answer).
        old = _get_pending(session_id)
        PENDING_REMINDERS.pop(session_id, None)
        text = result.text or (old.text if old else None)
        due_at = result.due_at or (old.due_at if old else None)
        if due_at is not None and due_at <= datetime.now().astimezone():
            due_at = None  # a parked time that lapsed while waiting is not reusable
        if text and due_at is not None:
            return await _finish(text, due_at, session_id, db, persist_user=goal)
        question = _no_task_question() if due_at is not None else result.question
        logger.info(f"Reminder request ambiguous, parking and asking: '{goal[:80]}'")
        _park(session_id, question, text, due_at)
        return _stream_text(question, session_id, db, persist_user=goal)

    # A clean fresh request replaces any parked half-ask — the user
    # restarted rather than answered.
    PENDING_REMINDERS.pop(session_id, None)

    reminder = await create_reminder(db, result.text, result.due_at, session_id=session_id)
    logger.info(f"Reminder {reminder.id} scheduled for {result.due_at.isoformat()}: '{result.text[:80]}'")
    text = _confirmation_text(reminder.text, result.due_at)
    return _stream_text(text, session_id, db, persist_user=goal)


def _pivots_to_task(reply: str) -> bool:
    """A reply that isn't a time but IS a task-shaped request ("list my
    desktop files…") is the user pivoting away from the reminder ask, not
    answering it. Re-asking "I still need a time" would trap them in the
    reminder question until they type "cancel" (live bug 2026-07-10: the
    retry of a full multi-step file task was swallowed twice). The same
    deterministic gate task routing itself uses — never an LLM call here."""
    from app.api.task_router import looks_like_task
    return looks_like_task(reply)


async def _continue_pending(
    pending: PendingReminder, reply: str, session_id: str, db: AsyncSession
) -> Optional[StreamingResponse]:
    """The open reminder question owns this message. Every outcome streams
    deterministic text — the reply never falls through to the LLM as an
    unrouted answer. The one escape: a reply that is clearly a NEW
    task-shaped request (not a time, fires the task gate) returns None so
    it flows down the normal task/chat path; the parked question stays
    open (TTL-bounded), so a later bare time still completes it."""
    if _CANCEL_RE.match(reply):
        PENDING_REMINDERS.pop(session_id, None)
        return _stream_text(_DROPPED, session_id, db, persist_user=reply)

    now = datetime.now().astimezone()

    # Waiting on a time for a known task ("call mom").
    if pending.text and pending.due_at is None:
        t = parse_time_reply(reply, now)
        if t is None:
            if _pivots_to_task(reply):
                return None
            pending.expires_at = time.time() + PENDING_TTL_SECONDS
            return _stream_text(
                _STILL_NEED_TIME.format(text=pending.text), session_id, db, persist_user=reply
            )
        if t.ambiguous:
            pending.question = t.question
            pending.expires_at = time.time() + PENDING_TTL_SECONDS
            return _stream_text(t.question, session_id, db, persist_user=reply)
        return await _finish(pending.text, t.due_at, session_id, db, persist_user=reply)

    # Waiting on a task for a known time ("remind me in 10 minutes" … of what?).
    if pending.due_at is not None and pending.text is None:
        if pending.due_at <= now:
            # The parked time lapsed while we waited for the task — ask for
            # a fresh time rather than scheduling into the past.
            pending.text = _clean_task_text(reply) or None
            pending.due_at = None
            pending.question = _no_time_question()
            pending.expires_at = time.time() + PENDING_TTL_SECONDS
            return _stream_text(
                "That time has already passed while I was waiting. " + _no_time_question(),
                session_id, db, persist_user=reply,
            )
        text = _clean_task_text(reply)
        if not text:
            pending.expires_at = time.time() + PENDING_TTL_SECONDS
            return _stream_text(_no_task_question(), session_id, db, persist_user=reply)
        return await _finish(text, pending.due_at, session_id, db, persist_user=reply)

    # Neither half known (a bare "remind me") — the time was asked first.
    t = parse_time_reply(reply, now)
    if t is not None and not t.ambiguous:
        pending.due_at = t.due_at
        pending.question = _no_task_question()
        pending.expires_at = time.time() + PENDING_TTL_SECONDS
        return _stream_text(_no_task_question(), session_id, db, persist_user=reply)
    if t is not None and t.ambiguous:
        pending.question = t.question
        pending.expires_at = time.time() + PENDING_TTL_SECONDS
        return _stream_text(t.question, session_id, db, persist_user=reply)
    if _pivots_to_task(reply):
        return None
    pending.expires_at = time.time() + PENDING_TTL_SECONDS
    return _stream_text(
        _no_time_question() + ' Or say "cancel" and I\'ll drop it.',
        session_id, db, persist_user=reply,
    )


async def _finish(
    text: str, due_at: datetime, session_id: str, db: AsyncSession, persist_user: str
) -> StreamingResponse:
    """Both halves known — schedule for real, THEN confirm. The parked
    question is only cleared once create_reminder has actually written the
    row: 'Reminder set' is never streamed ahead of the reminder existing."""
    reminder = await create_reminder(db, text, due_at, session_id=session_id)
    PENDING_REMINDERS.pop(session_id, None)
    logger.info(f"Reminder {reminder.id} scheduled for {due_at.isoformat()}: '{text[:80]}' (parked ask resolved)")
    return _stream_text(
        _confirmation_text(reminder.text, due_at), session_id, db, persist_user=persist_user
    )


def _stream_text(
    text: str, session_id: str, db: AsyncSession, persist_user: str
) -> StreamingResponse:
    """A reminder turn's response is always deterministic text — never an
    LLM paraphrase of a time or a confirmation of something that may not
    have actually been scheduled."""

    async def event_generator():
        try:
            db.add(Message(session_id=session_id, role="user", content=persist_user))
            await db.commit()
        except Exception as e:
            logger.warning(f"Persisting reminder-turn user message failed (non-critical): {e}")

        chunk = StreamChunk(delta=text, done=False, session_id=session_id)
        yield f"data: {chunk.model_dump_json()}\n\n"
        done_chunk = StreamChunk(delta="", done=True, session_id=session_id)
        yield f"data: {done_chunk.model_dump_json()}\n\n"

        try:
            db.add(Message(session_id=session_id, role="assistant", content=text))
            await db.commit()
        except Exception as e:
            logger.warning(f"Persisting reminder-turn assistant message failed (non-critical): {e}")

    return StreamingResponse(
        event_generator(), media_type="text/event-stream", headers=_SSE_HEADERS
    )
