"""
Jarvis OS — Routine Chat Routing (Phase 6, Part 5 — teachable routines)

Detects a TEACH ("save this as a routine called X") or RUN ("run my X
routine" / a bare name that matches a saved routine) request in the latest
user message. Mirrors reminder_router.py: deterministic (no LLM, no planner
for teach), short-circuits the chat turn (no memory extraction — a routine
command is not autobiography), and streams deterministic text.

Runs BETWEEN the reminder router and the task router in chat.py (precedence:
reminder → routine → task → Phase 2 chat). It must sit ahead of the task
router because a bare routine name ("clean my desktop") would otherwise be
caught by the task gate's strong-noun rule and re-planned as a one-off task
instead of the saved routine.

Two anchors, both tight literal triggers:
  - TEACH captures a name (quoted or trailing) plus a goal_template — the raw
    goal STRING re-fed to the planner, never a plan. The steps come from an
    inline procedure ("… that: delete tmp files") or, more commonly, the most
    recent task-shaped prior turn (conversation_context).
  - RUN loads the routine's goal_template and starts it as a BACKGROUND Task
    (start_task) — so the approval gate, path guards, and recipient/event-id
    locks all re-apply on the fresh plan automatically.

Fail-open to None on no literal trigger (tasks/chat untouched). Defers to an
already-open memory disambiguation/creation question or agent clarifying
question — the same rule the reminder router follows — so a reply owed
elsewhere is never swallowed here.
"""
import re
from typing import Optional

from fastapi.responses import StreamingResponse
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.routines import create_routine, get_routine_by_name
from app.db.models import Message
from app.db.schemas import ChatRequest, StreamChunk
from app.providers.base import LLMProvider

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

# ---------------------------------------------------------------- triggers

# TEACH — tight: requires the literal word "routine", OR the quoted
# "remember this as 'X'" form. "remember that meeting is at 3" matches
# neither (no "routine", no as-quoted name).
_TEACH_PATTERNS = [
    # save/remember this [as] [a] routine [called|named|as] NAME
    re.compile(
        r"\b(?:remember|save)\s+(?:this|that|it)\s+(?:as\s+)?(?:a\s+)?routine\s+"
        r"(?:(?:called|named|as)\s+)?(?P<name>.+?)\s*$",
        re.IGNORECASE,
    ),
    # save/remember this as [a] "NAME"  (quoted, no explicit "routine" word)
    re.compile(
        r"\b(?:remember|save)\s+(?:this|that|it)\s+as\s+(?:a\s+)?"
        r"(?P<name>[\"'‘“][^\"'’”]+[\"'’”])\s*$",
        re.IGNORECASE,
    ),
]

# RUN — requires the literal word "routine" (bare-name runs are handled by an
# exact normalized match against saved routines, below).
_RUN_PATTERNS = [
    # run [my|the] NAME routine
    re.compile(r"\brun\s+(?:my|the)\s+(?P<name>.+?)\s+routine\b", re.IGNORECASE),
    # run [my|the] routine [called|named] NAME
    re.compile(
        r"\brun\s+(?:(?:my|the)\s+)?routine\s+(?:(?:called|named)\s+)?(?P<name>.+?)\s*$",
        re.IGNORECASE,
    ),
]

# Split an inline procedure off the captured name span ("clean desktop that:
# delete tmp files" → name "clean desktop", inline "delete tmp files").
_INLINE_SPLIT_RE = re.compile(
    r"\s+(?:that\s+does|that\s+will|that\s+should|that|which)\s+|\s*:\s+",
    re.IGNORECASE,
)

# Leading/trailing connectives stripped off a captured routine name (the
# trailing one catches the redundant "called cleanup that: <steps>" form,
# where the ":" splits the inline goal but leaves a dangling "that").
_NAME_LEADING_RE = re.compile(r"^(?:to|for|that|about|the)\s+", re.IGNORECASE)
_NAME_TRAILING_RE = re.compile(r"\s+(?:that|which|to|for)$", re.IGNORECASE)
_QUOTES = "\"'‘’“”`"


def _clean_name(raw: str) -> str:
    name = (raw or "").strip().strip(_QUOTES).strip()
    name = _NAME_LEADING_RE.sub("", name).strip()
    name = _NAME_TRAILING_RE.sub("", name).strip()
    return name


def _match_teach(message: str) -> Optional[tuple[str, Optional[str]]]:
    """(name, inline_goal_or_None) when the message is a TEACH request, else
    None. inline_goal is a procedure written into the teach message itself."""
    for pattern in _TEACH_PATTERNS:
        m = pattern.search(message)
        if not m:
            continue
        raw = m.group("name")
        parts = _INLINE_SPLIT_RE.split(raw, maxsplit=1)
        name = _clean_name(parts[0])
        inline = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
        if name:
            return name, inline
    return None


def _match_run_name(message: str) -> Optional[str]:
    """The routine name when the message is an explicit RUN request, else
    None. Only fires with the literal word "routine"."""
    for pattern in _RUN_PATTERNS:
        m = pattern.search(message)
        if m:
            name = _clean_name(m.group("name"))
            if name:
                return name
    return None


# ---------------------------------------------------------------- entry point

async def maybe_handle_routine(
    request: ChatRequest, session_id: str, db: AsyncSession, provider: LLMProvider
) -> Optional[StreamingResponse]:
    """Returns a StreamingResponse when the latest message teaches or runs a
    routine; None sends the message down the normal task/chat path unchanged."""
    user_msgs = [m.content for m in request.messages if m.role == "user"]
    goal = user_msgs[-1].strip() if user_msgs else ""
    if not goal:
        return None

    # Never hijack a reply owed to an already-open question elsewhere — peek
    # without creating sessions (get_session() would be a side effect).
    from app.memory.conversation_state import CONVERSATION_SESSIONS
    from app.agents import get_choice_plan_for_session
    sess = CONVERSATION_SESSIONS.get(session_id)
    if sess is not None and (
        sess.pending_resolution is not None or sess.pending_creation is not None
    ):
        return None
    if await get_choice_plan_for_session(db, session_id) is not None:
        return None

    # TEACH — deterministic, no planner.
    teach = _match_teach(goal)
    if teach is not None:
        name, inline = teach
        return await _handle_teach(request, name, inline, goal, session_id, db)

    # RUN — explicit ("run my X routine") or an exact saved-name match.
    run_name = _match_run_name(goal)
    if run_name is not None:
        routine = await get_routine_by_name(db, run_name)
        if routine is None:
            return None  # "run …" may still be a real task — fall through
    else:
        routine = await get_routine_by_name(db, goal)  # bare-name exact match
        if routine is None:
            return None

    return _stream_run(request, routine, goal, session_id, db, provider)


# ---------------------------------------------------------------- teach

_NO_GOAL_TEXT = (
    "I don't see a task to save as a routine yet — run it once first (e.g. "
    '"delete the .tmp files in my Downloads folder"), then say: save this as '
    'a routine called "<name>".'
)


def _capture_prior_goal(request: ChatRequest) -> Optional[str]:
    """The goal_template for "save this" — the most recent task-shaped prior
    USER message. Reuses the task gate's own recall filter."""
    from app.api.task_router import looks_like_task
    for m in reversed(request.messages[:-1]):
        if m.role == "user" and m.content and looks_like_task(m.content):
            return m.content.strip()
    return None


def _saved_text(name: str) -> str:
    return (
        f'Saved routine "{name}". Say "run my {name} routine" any time and '
        f"I'll re-plan it fresh — asking before anything destructive."
    )


async def _handle_teach(
    request: ChatRequest,
    name: str,
    inline: Optional[str],
    goal: str,
    session_id: str,
    db: AsyncSession,
) -> StreamingResponse:
    goal_template = inline or _capture_prior_goal(request)
    if not goal_template:
        return _stream_text(_NO_GOAL_TEXT, session_id, db, persist_user=goal)
    routine = await create_routine(db, name, goal_template)
    logger.info(f"Routine '{routine.name}' taught from chat (session {session_id})")
    return _stream_text(_saved_text(routine.name), session_id, db, persist_user=goal)


# ---------------------------------------------------------------- run

def _stream_run(
    request: ChatRequest,
    routine,
    goal: str,
    session_id: str,
    db: AsyncSession,
    provider: LLMProvider,
) -> StreamingResponse:
    """Start the routine's goal as a background Task and acknowledge. The plan
    is re-derived from the stored goal STRING, so every gate re-applies; if it
    pauses for approval the PlanCard arrives via push, and the outcome message
    does when it finishes."""
    from app.api.task_router import conversation_context
    from app.agents import planner_memory_context, start_task

    goal_template = routine.goal_template
    routine_name = routine.name
    conversation = conversation_context(request)

    async def event_generator():
        try:
            db.add(Message(session_id=session_id, role="user", content=goal))
            await db.commit()
        except Exception as e:
            logger.warning(f"Persisting routine-run user message failed (non-critical): {e}")

        try:
            memory = await planner_memory_context(db, goal_template)
        except Exception:
            memory = ""

        try:
            await start_task(
                db, goal_template, session_id,
                conversation=conversation, memory=memory, provider=provider,
            )
            text = (
                f'Running your "{routine_name}" routine in the background. '
                "I'll notify you when it's done — or first, if any step needs "
                "your approval."
            )
        except Exception as e:
            logger.error(f"Starting routine '{routine_name}' failed: {e}")
            text = (
                f'I couldn\'t start the "{routine_name}" routine, so nothing '
                "was changed. Please try again."
            )

        chunk = StreamChunk(
            delta=text, done=False, session_id=session_id,
            model=provider.model_name, provider=provider.provider_name,
        )
        yield f"data: {chunk.model_dump_json()}\n\n"
        done = StreamChunk(
            delta="", done=True, session_id=session_id,
            model=provider.model_name, provider=provider.provider_name,
        )
        yield f"data: {done.model_dump_json()}\n\n"

        try:
            db.add(Message(
                session_id=session_id, role="assistant",
                content=text, model=provider.model_name,
            ))
            await db.commit()
        except Exception as e:
            logger.warning(f"Persisting routine-run response failed (non-critical): {e}")

    return StreamingResponse(
        event_generator(), media_type="text/event-stream", headers=_SSE_HEADERS
    )


# ---------------------------------------------------------------- shared

def _stream_text(
    text: str, session_id: str, db: AsyncSession, persist_user: str
) -> StreamingResponse:
    """A routine TEACH turn's response is always deterministic text — never an
    LLM paraphrase of a confirmation of something that may not have been saved."""

    async def event_generator():
        try:
            db.add(Message(session_id=session_id, role="user", content=persist_user))
            await db.commit()
        except Exception as e:
            logger.warning(f"Persisting routine-turn user message failed (non-critical): {e}")

        chunk = StreamChunk(delta=text, done=False, session_id=session_id)
        yield f"data: {chunk.model_dump_json()}\n\n"
        done_chunk = StreamChunk(delta="", done=True, session_id=session_id)
        yield f"data: {done_chunk.model_dump_json()}\n\n"

        try:
            db.add(Message(session_id=session_id, role="assistant", content=text))
            await db.commit()
        except Exception as e:
            logger.warning(f"Persisting routine-turn assistant message failed (non-critical): {e}")

    return StreamingResponse(
        event_generator(), media_type="text/event-stream", headers=_SSE_HEADERS
    )
