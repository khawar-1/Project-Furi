"""
Jarvis OS — Chat Task Router (Phase 3, Part 6)

Decides whether a chat message is a TASK REQUEST ("delete my temp files")
or normal conversation, and routes task requests to the agent planner.
Detection is two-stage so normal chat pays ZERO extra cost:

1. Deterministic gate — fires only when the message contains BOTH an action
   verb AND a computer-domain signal (file/folder/path/command/...). If the
   gate does not fire there is no LLM call at all: `maybe_handle_task`
   returns None and the message flows into the untouched Phase 2 chat path.
2. LLM classification — one tiny temperature-0 call returning a routing label
   (TASK / EMAIL / CALENDAR / WEB / CHAT; WEB added Phase 6 Part 1) that rejects
   gate false-positives ("my brother deleted my save file" fires the gate but is
   conversation). It judges the goal with any background-intent phrase already
   stripped — "…and remind me when you are done" would read as a reminder
   request (CHAT) and sink the real task. All ACTION labels feed the SAME
   planner and the same approval gates — one execution path; the label buys
   recall + telemetry and a seam for future per-domain handlers.

Fail-open to chat: classifier says CHAT, classifier errors OR returns an
unrecognized word, or an open disambiguation / create-contact question is
parked on the session — all fall through to normal chat. This router can only
ever ADD the task path; it can never break the conversation path.

Precedence: reminder routing runs BEFORE this in chat.py (its hook is invoked
first), and the reminder strong trigger requires the literal word
"reminder(s)"/"alarm". So "remind me to email Jamil at 6" is a reminder (text
"email Jamil"), while "schedule a meeting with Jamil at 3" is not a reminder
and falls through to this router's CALENDAR path. Do not reorder the chat.py
hooks without preserving that.

Task turns stream Server-Sent Events like normal chat, plus ONE special
chunk: {"type": "plan", "plan": {...}} carrying the serialized AgentPlan
(same shape as /api/agent/execute, including the parked plan id — the
approval UI answers it via POST /api/agent/approve). Regular StreamChunk
deltas follow so today's frontend still shows readable text.

Memory extraction does NOT run on task turns: the audit trail is
ActivityLog, and a command is not autobiography. A fact buried inside a task
request ("delete my essay — jamil and I finished it") is the one accepted
trade-off; stating it conversationally stores it as usual.

Clarifying questions: when a plan pauses with status awaiting_choice ("three
files are named notes.txt — which one?"), the NEXT chat message in that
session is routed as the answer (typed answers and clicked options are
equivalent). A plan question takes precedence over a parked Phase 2 memory
question — it is the one the user just saw.
"""
import re
from typing import Optional

from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import (
    AgentPlan,
    AgentPlanner,
    PlanStatus,
    answer_task_in_background,
    deterministic_plan_text,
    get_choice_plan_for_session,
    planner_memory_context,
    pop_plan,
    put_plan,
    start_task,
    steps_for_summary,
)
from app.api.agent import _plan_response
from app.db.models import Message
from app.db.schemas import ChatRequest, StreamChunk
from app.providers.base import LLMMessage, LLMProvider

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

# ============================================================ detection gate

# Recall-first gate (redesigned 2026-07-10). Users invent VERBS endlessly —
# "del", "yeet", "get rid of", "wipe out", typos — no list can enumerate
# them ("please del all files…" missed the old verb+domain rule, fell open
# to plain chat, and the chat LLM fabricated a whole task lifecycle, live
# bug 2026-07-09). But a computer task almost always NAMES ITS OBJECT, and
# object nouns are a small stable vocabulary. So: a STRONG domain signal
# (file/folder/terminal nouns, dev tools, an explicit drive path) fires the
# gate ALONE, regardless of verb — the temperature-0 classifier, which
# understands arbitrary wording, makes the real TASK/CHAT call. A false
# fire costs one tiny LLM call; a miss used to cost an unrouted request.
_STRONG_DOMAIN_RE = re.compile(
    # Files / terminal (Phase 3).
    r"(\bfiles?\b|\bfolders?\b|\bdirector(?:y|ies)\b|\bsubfolders?\b|"
    r"\bdesktop\b|\bdownloads?\b|\bdocuments\b|"
    r"\bterminal\b|\bconsole\b|\bshell\b|\bpowershell\b|\bcmd\b|"
    r"\bcommands?\b|\bscripts?\b|"
    r"\b(?:npm|pip|git|python|node|docker|pytest)\b|"
    r"\b[a-z]:[\\/]|"
    # Email domain (Phase 5, Part 5). Object-nouns fire the gate alone; the
    # multi-class classifier makes the EMAIL/CHAT call ("I got an email from
    # him" is a false fire that costs one temp-0 call answering CHAT — the
    # recall-first trade-off). "schedule" is deliberately NOT here: it is a
    # verb the reminder router already owns ("schedule a reminder") and it
    # collides with small talk ("reschedule my day").
    # "mail"/"e-mail" join "email" as strong nouns — "send a new mail to …"
    # missed the gate and fell to plain chat, whose model then imitated a real
    # approval message (caught by _SYSTEM_VOICE_RE, live 2026-07-12).
    r"\be-?mails?\b|\bmails?\b|\binbox\b|\bgmail\b|\bsubject\b|"
    # Calendar domain (Phase 5, Part 5).
    r"\bcalendar\b|\bmeetings?\b|\bevents?\b|\binvites?\b|"
    # Web domain (Phase 6, Part 1). Object-nouns fire the gate alone; the
    # multi-class classifier makes the WEB/CHAT call ("I saw a website" is a
    # false fire costing one temp-0 call). A bare URL is a strong signal.
    r"\bweb\b|\bwebsites?\b|\bweb\s?pages?\b|\bonline\b|\binternet\b|"
    r"\bgoogle\b|\burls?\b|https?://)"
)

# Weak signals — common in ordinary conversation (media nouns, URLs, "e.g.",
# decimals all brush against these) — still need an action verb to fire.
_WEAK_DOMAIN_RE = re.compile(
    r"(\bpictures\b|\bvideos\b|\bmusic\b|\bdrive\b|\bdisk\b|"
    r"\bprocess(?:es)?\b|[/\\]|~[/\\]?|\.\w{1,4}\b)"
)

# Common inflections listed explicitly — a stem regex either misses forms
# ("copies") or over-matches. Only consulted for weak-signal messages; a
# strong noun no longer needs any verb.
_ACTION_VERB_RE = re.compile(
    r"\b(create|creates|created|creating|make|makes|making|write|writes|writing|"
    r"save|saves|saving|delete|deletes|deleted|deleting|remove|removes|removed|"
    r"removing|erase|erases|erased|erasing|clean|cleans|cleaned|cleaning|cleanup|"
    r"clear|clears|clearing|empty|empties|move|moves|moved|moving|rename|renames|"
    r"renamed|renaming|copy|copies|copied|copying|organize|organizes|organized|"
    r"organizing|organise|organises|organised|organising|sort|sorts|sorted|sorting|"
    r"run|runs|running|execute|executes|executed|executing|launch|launches|launched|"
    r"launching|install|installs|installed|installing|search|searches|searched|"
    r"searching|find|finds|found|finding|locate|locates|located|locating|list|lists|"
    r"listing|read|reads|reading|open|opens|opened|opening|show|shows|showing|"
    r"check|checks|checking|look|"
    r"tell|tells|telling|count|counts|counted|counting|"
    # Email verbs (Phase 5) — "send"/"reply"/"forward"/"draft" were absent, so
    # a weak-noun email request ("send that mail", "forward it") never fired.
    r"send|sends|sending|sent|reply|replies|replied|replying|"
    r"forward|forwards|forwarded|forwarding|draft|drafts|drafted|drafting|"
    r"del|rm|rmdir|mkdir|mv|cp|trash|trashes|trashed|trashing)\b"
)


def looks_like_task(text: str) -> bool:
    """Deterministic pre-filter, tuned for RECALL: a strong computer-domain
    noun fires alone (any verb, any phrasing); weak signals need an action
    verb. Deliberately over-inclusive — the LLM confirmation prunes it."""
    t = text.lower()
    if _STRONG_DOMAIN_RE.search(t):
        return True
    return bool(_ACTION_VERB_RE.search(t)) and bool(_WEAK_DOMAIN_RE.search(t))


# ======================================================== LLM confirmation

_CLASSIFY_PROMPT = """You route messages for Jarvis OS, a personal AI that can act on the user's computer and accounts with exactly these tool groups:
- FILES/SYSTEM: search/read/list files and folders, create/move/rename/delete files, run terminal commands and scripts.
- EMAIL: search and read Gmail; draft, send, or reply to email.
- CALENDAR: list/find Google Calendar events; create, update, or delete events.
- WEB: search the web and open/read a web page to look up online information.

Reply with EXACTLY one word:
TASK — asks Jarvis to perform a FILES/SYSTEM action now.
EMAIL — asks Jarvis to search, read, draft, send, or reply to email now.
CALENDAR — asks Jarvis to look at or change calendar events now.
WEB — asks Jarvis to search the web or open/read a web page now.
CHAT — anything else: conversation, questions Jarvis can answer from its own knowledge, sharing information about their life, talking ABOUT past or hypothetical actions, an answer to an earlier question, or a request none of these tools can do (reminders — handled elsewhere).

Judge the INTENT, not the vocabulary:
- "I sent him the files yesterday" or "my desktop is such a mess" is CHAT (mentioning files while talking), while "get rid of the txt files in that folder" is TASK even though it names no tool.
- "I emailed him yesterday" or "my inbox is out of control" is CHAT, while "email jamil about dinner" is EMAIL even though it names no tool.
- An instruction to SEND is EMAIL even when the text to send reads like a statement or is written on someone's behalf: "email i221538@nu.edu.pk that the report is done", "send Ali a mail saying I'll be late", and "email him that this is Furi writing on behalf of my master" are all EMAIL, not CHAT.
- "my calendar is packed this week" is CHAT, while "put a meeting with jamil on my calendar tomorrow at 3" is CALENDAR.
- "what do you think of vector databases?" is CHAT (answerable from knowledge), while "search the web for the latest LangGraph release" or "look up who won the match today" or "open https://example.com and summarize it" is WEB.
Any wording that asks for one of those actions NOW gets its action label; anything else is CHAT.

{context_block}USER MESSAGE:
{message}

One word (TASK, EMAIL, CALENDAR, WEB, or CHAT):"""

# The recognized action labels. All three feed the SAME planner and the same
# approval gates — there is one execution path. The label buys recall +
# telemetry and is the clean seam for future per-domain handlers.
_ACTION_LABELS = ("TASK", "EMAIL", "CALENDAR", "WEB")

# Shown to the classifier when the conversation has earlier turns. A message
# is part of a conversation, not an island: "its in my downloads folder" after
# a failed delete is the user steering that task, not small talk (live bug
# 2026-07-10 — it fell open to chat, whose LLM promised the deletion and then
# fabricated "the task has been initiated").
_CLASSIFY_CONTEXT_TEMPLATE = """RECENT CONVERSATION (context only — the user message below is the NEXT message in it):
{context}

A short follow-up that continues a computer task being discussed in that conversation — supplying a detail it was missing ("its in my downloads folder"), correcting it, or telling Jarvis to go ahead with it — is TASK. A message merely commenting on a finished task ("thanks, that worked") is CHAT.

"""


async def _classify_message(
    provider: LLMProvider, message: str, context: str = ""
) -> str:
    """One tiny temperature-0 call returning a routing label: "TASK", "EMAIL",
    "CALENDAR", "WEB", or "CHAT". Any failure — an exception OR an unrecognized reply —
    means CHAT (fail open): the message flows into the untouched Phase 2 chat
    path, never a broken action route."""
    context_block = (
        _CLASSIFY_CONTEXT_TEMPLATE.format(context=context) if context else ""
    )
    try:
        response = await provider.chat(
            messages=[LLMMessage(
                role="user",
                content=_CLASSIFY_PROMPT.format(
                    message=message, context_block=context_block
                ),
            )],
            temperature=0.0,
            max_tokens=8,
        )
    except Exception as e:
        logger.warning(f"Message classification failed — treating as chat: {e}")
        return "CHAT"
    reply = response.content.strip().upper()
    for label in _ACTION_LABELS:
        if reply.startswith(label):
            return label
    return "CHAT"


# ======================================================== background intent

# Deterministic detection of "do this in the background / tell me when done"
# (Phase 4, Part 5) — same philosophy as the reminder trigger: a regex, never
# an LLM judgement. The matched phrase is STRIPPED from the goal so the
# planner never sees "tell me when you're done" and invents an unachievable
# notify step — the task runner's completion push IS the telling.
_BACKGROUND_RE = re.compile(
    r"(?:\s*(?:,|;|\band\b|\bthen\b)\s+)?"  # joiner, removed with the phrase
    r"(?:please\s+)?"
    r"(?:"
    r"(?:let\s+me\s+know|tell\s+me|notify\s+me|ping\s+me|remind\s+me)\s+(?:when(?:ever)?|once|after)\s+"
    r"(?:you(?:'re|\s+are)?\s+|it(?:'s|\s+is)?\s+|this\s+is\s+|everything(?:'s|\s+is)?\s+)?"
    r"(?:all\s+)?(?:done|finish(?:ed)?|complete[d]?|ready)"
    # Reversed order — the condition BEFORE the verb ("after doing all this
    # remind me", "once everything is done let me know", live bug
    # 2026-07-10). The completion verbs are deliberately generic
    # (doing/finishing/…): "after deleting all files create…" is a step in
    # the task itself and must never match.
    r"|(?:when(?:ever)?|once|after)\s+"
    r"(?:you(?:'re|'ve|\s+are|\s+have)?\s+|it(?:'s|\s+is)?\s+|(?:this|that)(?:'s|\s+is)\s+"
    r"|everything(?:'s|\s+is)?\s+)?"
    r"(?:all\s+)?(?:done|finish(?:ed)?|complete[d]?|ready)[\s,;]*"
    r"(?:please\s+)?(?:let\s+me\s+know|tell\s+me|notify\s+me|ping\s+me|remind\s+me)"
    r"|after\s+(?:doing|finishing|completing|running|executing)\s+"
    r"(?:all\s+(?:of\s+)?)?(?:this|that|these|those|them|it|everything|the\s+tasks?|all)[\s,;]*"
    r"(?:please\s+)?(?:let\s+me\s+know|tell\s+me|notify\s+me|ping\s+me|remind\s+me)"
    r"|(?:do|run)\s+(?:it|this|that)\s+in\s+the\s+background"
    r"|in\s+the\s+background"
    r"|as\s+a\s+background\s+task"
    r")\b[.!]*",
    re.IGNORECASE,
)


def wants_background(goal: str) -> tuple[bool, str]:
    """(True, cleaned_goal) when the goal carries background intent. The
    cleaned goal has the intent phrase removed; if stripping would leave
    nothing actionable, the original goal is kept."""
    match = _BACKGROUND_RE.search(goal)
    if match is None:
        return False, goal
    cleaned = (goal[: match.start()] + " " + goal[match.end():]).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"^(?:and|then|,|;|\.|—|–|-)\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+(?:and|then|,|;)$", "", cleaned, flags=re.IGNORECASE).strip(" ,;")
    if not cleaned:
        return True, goal
    return True, cleaned


# ====================================================== conversation context

_CONTEXT_TURNS = 6    # recent messages shown to the planner
_CONTEXT_CHARS = 500  # per-message cap — plans and file lists get long


def conversation_context(request: ChatRequest) -> str:
    """Render the chat turns BEFORE the goal message for the planner. Without
    this every task starts amnesiac: 'rename the file in the phase3test
    folder' loses the Desktop path the previous turn just found, and the LLM
    guesses one instead."""
    lines: list[str] = []
    for m in request.messages[:-1][-_CONTEXT_TURNS:]:
        content = " ".join((m.content or "").split())
        if not content:
            continue
        if len(content) > _CONTEXT_CHARS:
            content = content[:_CONTEXT_CHARS] + "…"
        lines.append(f"{m.role}: {content}")
    return "\n".join(lines)


# ============================================================= entry point

async def maybe_handle_task(
    request: ChatRequest,
    session_id: str,
    db: AsyncSession,
    provider: LLMProvider,
) -> Optional[StreamingResponse]:
    """Return a StreamingResponse when the latest user message is a task
    request; None sends the message down the untouched Phase 2 chat path."""
    user_msgs = [m.content for m in request.messages if m.role == "user"]
    goal = user_msgs[-1].strip() if user_msgs else ""
    if not goal:
        return None

    # An open clarifying question ("which notes.txt?") owns the next message:
    # the user is answering it, not starting a new task. Typed answers and
    # clicked options are equivalent (a click posts to /api/agent/choose and
    # consumes the plan first — hence the second, atomic pop check).
    choice_plan = await get_choice_plan_for_session(db, session_id)
    if choice_plan is not None:
        plan = await pop_plan(db, choice_plan.id)
        if plan is not None:
            logger.info(f"Chat message routed as the answer to plan {plan.id}'s question")
            return _stream_answer(goal, plan, session_id, db, provider)

    if not looks_like_task(goal):
        return None

    # Never hijack a reply to a parked question ("which jamil?" / "add daud?").
    # Peek without get_session() — that would create sessions as a side effect.
    from app.memory.conversation_state import CONVERSATION_SESSIONS
    sess = CONVERSATION_SESSIONS.get(session_id)
    if sess is not None and (
        sess.pending_resolution is not None or sess.pending_creation is not None
    ):
        return None

    # Phase 4, Part 5: background intent ("…and tell me when you're done")
    # escapes the chat turn entirely — the plan runs as a persisted Task and
    # every pause/outcome arrives by push + persisted message, not by stream.
    # Stripped BEFORE classification: the intent phrase is routing metadata,
    # not part of the task, and "…and remind me when you are done" makes the
    # classifier read the whole message as a reminder request (listed as
    # CHAT) — a real file task fell open to the chat path, whose LLM then
    # denied having file access (live bug, 2026-07-09).
    background, cleaned_goal = wants_background(goal)

    # The classifier judges the message IN its conversation — the same view
    # the planner gets. A follow-up steering a task ("its in my downloads
    # folder") reads as CHAT in isolation and used to fall open to the chat
    # LLM, which cannot act but promised to (live bug, 2026-07-10).
    conversation = conversation_context(request)

    # Multi-class routing (Phase 5, Part 5): TASK / EMAIL / CALENDAR / CHAT.
    # CHAT fails open to Phase 2. The three action labels all route to the SAME
    # planner below — the tool registry already contains the file, email, and
    # calendar tools, so the planner picks the right ones from the goal. The
    # label buys recall + telemetry and is the documented insertion point for
    # future per-domain handlers (do not add a dispatcher until one is needed).
    label = await _classify_message(
        provider, cleaned_goal if background else goal, conversation
    )
    if label == "CHAT":
        return None

    logger.info(f"Chat message routed to agent planner [{label}]: '{goal[:80]}'")

    # Phase 3.5 "one brain": the planner sees the same long-term memory the
    # chat path would (people, preferences, facts) — as data, not instructions.
    memory = await planner_memory_context(db, cleaned_goal if background else goal)

    if background:
        return _stream_task_background(
            goal, cleaned_goal, conversation, memory, session_id, db, provider,
        )
    return _stream_task(goal, conversation, memory, session_id, db, provider)


# ============================================================== task stream

class PlanChunk(BaseModel):
    """The special SSE message type carrying the plan. Regular text still
    arrives as StreamChunk deltas, so chunks without "type" render as before."""
    type: str = "plan"
    plan: dict
    delta: str = ""
    done: bool = False
    session_id: Optional[str] = None
    model: Optional[str] = None
    provider: Optional[str] = None


def _stream_task(
    goal: str,
    conversation: str,
    memory: str,
    session_id: str,
    db: AsyncSession,
    provider: LLMProvider,
) -> StreamingResponse:
    async def run(planner: AgentPlanner) -> AgentPlan:
        return await planner.start(goal)

    return _stream_plan_run(goal, conversation, memory, run, session_id, db, provider)


def _stream_answer(
    answer_text: str,
    plan: AgentPlan,
    session_id: str,
    db: AsyncSession,
    provider: LLMProvider,
) -> StreamingResponse:
    """Continue a question-paused plan with the user's typed chat answer.
    A task-owned plan (Phase 4, Part 5) continues in the BACKGROUND — a typed
    reply to a background task's question must not pull it back into the
    chat turn; the outcome arrives by push like every other task transition."""
    if plan.task_id:
        return _stream_background_answer(answer_text, plan, session_id, db, provider)

    async def run(planner: AgentPlanner) -> AgentPlan:
        return await planner.answer(plan, answer_text)

    return _stream_plan_run(
        answer_text, plan.conversation, plan.memory_context, run,
        session_id, db, provider,
    )


# ========================================================= background stream

def _stream_static_text(
    user_text: str,
    session_id: str,
    db: AsyncSession,
    provider: LLMProvider,
    reply,  # async () -> str: runs AFTER the user message is persisted
) -> StreamingResponse:
    """SSE turn whose assistant text is one deterministic string: persist the
    user message, compute/emit the reply, persist it. The background-task
    paths use this — their real output arrives later, by push."""
    async def event_generator():
        try:
            db.add(Message(session_id=session_id, role="user", content=user_text))
            await db.commit()
        except Exception as e:
            logger.warning(f"Persisting task message failed (non-critical): {e}")

        text = await reply()
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
            logger.warning(f"Persisting task response failed (non-critical): {e}")

    return StreamingResponse(
        event_generator(), media_type="text/event-stream", headers=_SSE_HEADERS,
    )


def _stream_task_background(
    goal: str,
    cleaned_goal: str,
    conversation: str,
    memory: str,
    session_id: str,
    db: AsyncSession,
    provider: LLMProvider,
) -> StreamingResponse:
    """Start a background Task for the goal and acknowledge immediately. No
    plan chunk here — if the plan pauses, the PlanCard arrives via the push
    channel; if it completes, the outcome message does."""
    async def reply() -> str:
        try:
            await start_task(
                db, cleaned_goal, session_id,
                conversation=conversation, memory=memory, provider=provider,
            )
        except Exception as e:
            logger.error(f"Starting background task failed for '{goal[:80]}': {e}")
            return (
                "I couldn't start that as a background task, so nothing was "
                "changed. Please try again."
            )
        return (
            "I've started working on that in the background. I'll notify you "
            "when it's done — or first, if any step needs your approval."
        )

    return _stream_static_text(goal, session_id, db, provider, reply)


def _stream_background_answer(
    answer_text: str,
    plan: AgentPlan,
    session_id: str,
    db: AsyncSession,
    provider: LLMProvider,
) -> StreamingResponse:
    """Feed a typed chat answer to a task-owned question plan, in the
    background. Deterministic ack; the continuation's pause/outcome pushes."""
    async def reply() -> str:
        try:
            task = await answer_task_in_background(db, plan, answer_text, provider)
        except Exception as e:
            logger.error(f"Background answer failed for plan {plan.id}: {e}")
            task = None
        if task is None:
            return (
                "I couldn't continue that background task — its record is "
                "gone. Check the Activity timeline for anything that already "
                "ran, and ask again if you still want it done."
            )
        return (
            "Got it — I'm continuing that task in the background. I'll notify "
            "you when it's done, or if I need anything else."
        )

    return _stream_static_text(answer_text, session_id, db, provider, reply)


def _stream_plan_run(
    user_text: str,
    conversation: str,
    memory: str,
    run,  # async (AgentPlanner) -> AgentPlan
    session_id: str,
    db: AsyncSession,
    provider: LLMProvider,
) -> StreamingResponse:

    def _text_chunk(delta: str, done: bool = False) -> str:
        chunk = StreamChunk(
            delta=delta, done=done, session_id=session_id,
            model=provider.model_name, provider=provider.provider_name,
        )
        return f"data: {chunk.model_dump_json()}\n\n"

    async def event_generator():
        # Persist the user message here — the Phase 2 path that normally does
        # this was bypassed. Same Message row it would have written.
        try:
            db.add(Message(session_id=session_id, role="user", content=user_text))
            await db.commit()
        except Exception as e:
            logger.warning(f"Persisting task message failed (non-critical): {e}")

        try:
            planner = AgentPlanner(
                db, provider, session_id=session_id,
                conversation=conversation, memory=memory,
            )
            plan = await run(planner)
        except Exception as e:
            logger.error(f"Agent planning failed for '{user_text[:80]}': {e}")
            yield _text_chunk(
                "I ran into a problem while planning that task, so nothing "
                "was changed. Please try again.", done=True,
            )
            return

        if plan.status in (PlanStatus.AWAITING_APPROVAL, PlanStatus.AWAITING_CHOICE):
            await put_plan(db, plan)  # answered via /api/agent/approve, /choose, or chat

        # The special message type: the full serialized plan, first.
        plan_chunk = PlanChunk(
            plan=_plan_response(plan), session_id=session_id,
            model=provider.model_name, provider=provider.provider_name,
        )
        yield f"data: {plan_chunk.model_dump_json()}\n\n"

        # Then readable text — streamed for completed plans, deterministic
        # otherwise (never let an LLM paraphrase an approval request or spin
        # a failure).
        collected: list[str] = []
        if plan.status == PlanStatus.COMPLETED:
            async for delta in _summarize_completed(provider, plan):
                collected.append(delta)
                yield _text_chunk(delta)
        else:
            text = _deterministic_text(plan)
            collected.append(text)
            yield _text_chunk(text)

        yield _text_chunk("", done=True)

        assistant_text = "".join(collected)
        if assistant_text:
            try:
                db.add(Message(
                    session_id=session_id, role="assistant",
                    content=assistant_text, model=provider.model_name,
                ))
                await db.commit()
            except Exception as e:
                logger.warning(f"Persisting task response failed (non-critical): {e}")

    return StreamingResponse(
        event_generator(), media_type="text/event-stream", headers=_SSE_HEADERS,
    )


# ================================================================ rendering

# Moved to app/agents/rendering.py in Phase 4 Part 5 (the background task
# runner renders the SAME words); the alias keeps this module's public shape.
_deterministic_text = deterministic_plan_text


_SUMMARY_PROMPT = """You are Jarvis, the user's personal AI. You just finished executing a task for them. Report the outcome.

THE USER ASKED:
{goal}

WHAT WAS DONE AND WHAT IT FOUND (already rendered as readable text — this is the COMPLETE record):
{steps}

Write the reply to the user:
- Start with one short first-person sentence saying what was done.
- When the user asked to SEE data (file/folder names, file contents, command output), present ALL of it from the results above: names as a markdown bullet list (you may group folders and files), file contents and command output in a fenced code block. Never summarize the data away.
- Copy names, paths, numbers, and contents EXACTLY as written above — never invent, drop, round, or embellish anything.
- Only call a list truncated if the results above literally say so — otherwise it is complete.
- Never output JSON, curly braces, or escaped backslashes; do not mention tools, steps, or plans."""


async def _summarize_completed(provider: LLMProvider, plan: AgentPlan):
    """Stream a natural-language summary of a completed plan. Falls back to
    the deterministic text if the LLM stream fails before producing anything.
    The step results are handed over as code-rendered readable text
    (steps_for_summary), NEVER raw JSON — the LLM cannot paste JSON it never
    received (live display bug, 2026-07-10)."""
    prompt = _SUMMARY_PROMPT.format(goal=plan.goal, steps=steps_for_summary(plan))
    produced = False
    try:
        async for delta in provider.stream_chat(
            messages=[LLMMessage(role="user", content=prompt)], temperature=0.3,
        ):
            produced = True
            yield delta
    except Exception as e:
        logger.warning(f"Task summary stream failed: {e}")
    if not produced:
        yield _deterministic_text(plan)
