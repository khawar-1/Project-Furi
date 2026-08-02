"""
Jarvis OS — Chat API (Phase 2)
Now with memory context injection and background extraction pipeline.
"""
import asyncio
import json
import re
import uuid
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import StreamingResponse
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_db, get_llm_provider, get_qdrant
from app.core.task_status import active_tasks_context
from app.db.models import Message, utc_iso
from app.db.persist import persist_message_best_effort
from app.db.schemas import ChatRequest, ChatResponse, StreamChunk
from app.memory.engine import MemoryEngine, substitute_placeholders
from app.memory.extractor import run_extraction_pipeline
from app.providers.base import LLMMessage, LLMProvider

router = APIRouter()

# Deterministic yes/no reading of the user's reply to "want me to add them?"
_NEGATIVE_RE = re.compile(r"\b(no|nope|nah|don'?t|do not|not now|skip|leave it)\b")
_AFFIRMATIVE_RE = re.compile(r"\b(yes|yeah|yep|sure|ok|okay|of course|go ahead|do it|add (her|him|them|it))\b")


# The backend's own deterministic voices — phrases only the task runner,
# background ack, reminder router, or approval flow ever produce. A Phase 2
# chat response containing one is the LLM IMPERSONATING the system: by
# construction no task or reminder ran this turn (those paths short-circuit
# before this generator). Live bug 2026-07-09: "please del all files with
# '.txt' extension" missed the task gate, fell open to chat, and the LLM
# fabricated the entire background-task lifecycle — ack, 'Finished the
# background task "…". Done — 1 step(s) completed.', invented results — for
# a delete that never happened, with the round-5 "do NOT pretend you did it"
# rule already in the prompt. Prompt rules demonstrably don't stop this, so
# the guard is structural: the stream is cut at the first marker and a
# deterministic correction is appended. (A response merely QUOTING an old
# system message trips this too — rare, and the correction stays factually
# true: nothing ran this turn.)
_SYSTEM_VOICE_RE = re.compile(
    r"(finished the background task"
    r"|i've started working on that in the background"
    r"|done\s*[—–-]+\s*\d+\s*step\(s\) completed"
    r"|reminder set\s*[—–-]"
    r"|this needs your approval first"
    r"|nothing has been done yet\s*[—–-]\s*pick an option"
    # Initiation claims: chat cannot start anything, so "the task … has been
    # initiated" is always a fabrication (live bug 2026-07-10 — the LLM
    # claimed a deletion task was initiated after promising one in chat).
    # [^\n] not [^.\n]: the claimed span itself may contain dots (".txt").
    r"|the (?:task|deletion|operation)\b[^\n]{0,120}?\bhas been (?:initiated|started|queued|launched)"
    # Email/calendar completion claims (Phase 5, Part 5). Chat cannot send mail
    # or touch the calendar, so any past-tense "email sent"/"event created" is a
    # fabrication — we know from rounds 8-9 the LLM invents these on a routing
    # miss. Anchored to PAST-TENSE completions only, so a capability statement
    # ("I can send an email …") is never cut. Real completed email/calendar
    # plans stream through task_router/agent.py, not this chat path — they are
    # unaffected by this guard.
    r"|email sent\s*[—–-]"
    r"|(?:email|reply|draft) (?:has been |was )?(?:sent|created|saved)"
    r"|(?:i(?:'ve| have) )?sent (?:the |your |an |that )?(?:email|reply)"
    r"|event created\s*[—–-]"
    r"|(?:event|meeting|invite) (?:has been |was )?(?:created|scheduled|added|sent)"
    r"|(?:i(?:'ve| have) )?(?:created|added|scheduled|set up) (?:the |your |a |an )?(?:event|meeting)"
    r"|added\b[^\n]{0,30}?\bto your calendar"
    # Web-search action promises (Phase 6 WEB domain; live bug 2026-07-16 —
    # "Searching the web for the latest on Black Clover's next season. One
    # moment, sir." streamed from plain chat on a gate miss, and no answer
    # ever came: chat cannot search). Anchored to progressive/future claims
    # only, so capability statements ("I can search the web for you") never
    # trip it — the past-tense-only email/calendar discipline.
    r"|searching the (?:web|internet) for"
    r"|(?:i(?:'ll| will)|let me) (?:search|check|look)[^\n]{0,40}?\b(?:web|online|internet)\b"
    r"|looking (?:that|this|it) up (?:online|on the (?:web|internet))"
    # Round 2 (live, same day): the fabrication learned to avoid the word
    # "web" — "I've started a search for the latest news… I'll let you know
    # what I find." slipped every pattern above. Chat can never start a
    # search nor deliver anything later, so a first-person started-a-search
    # claim or a promise to report back findings is always a fabrication.
    # Conditional capability offers ("I can search…", "just say the word")
    # stay untouched.
    r"|i(?:'ve| have) (?:started|begun|initiated|kicked off) (?:a|the|my) search"
    r"|i(?:'ll| will) let you know what i find"
    # Hand-off-to-the-backend fabrication (live bug 2026-07-21): on a routing
    # miss the chat LLM told the user to re-say the request "as one direct
    # instruction … that will route it to the right system", then on the retry
    # streamed "That instruction has been passed to the system. Give me a moment,
    # sir." — chat cannot pass anything to any system, so a past-tense
    # passed/handed/routed/forwarded/sent "to the system/backend/planner" claim
    # is always a fabrication. A conditional offer ("I can pass this to …") is
    # not past-tense and is spared.
    r"|(?:has been|have been|been|it(?:'s| is)|that(?:'s| is)|i(?:'ve| have))\s+"
    r"(?:passed|handed|routed|forwarded|sent)\b[^\n]{0,40}?\bto (?:the )?"
    r"(?:right |correct |browser |domain |file |email |calendar )?"
    r"(?:system|backend|planner|agent)"
    # The same fabrication as a STATE claim rather than a hand-off verb (live
    # bug 2026-08-01): "open junaidjamshed.com" fell to chat on a classifier
    # coin flip and the LLM answered "Understood — opening junaidjamshed.com
    # now, sir. It's with the browser agent in the background; I'll confirm the
    # moment it's live." Nothing was with any agent; nothing ran. Every pattern
    # above missed it because it names no hand-off verb — it asserts the
    # finished state directly. Chat can put nothing in an agent's hands, so
    # "it's with the … agent" is always a fabrication. Requires the possessing
    # phrase, so a capability offer ("I can hand that to the browser agent")
    # is spared.
    r"|(?:it|that|this)(?:'s| is)\s+(?:now\s+)?(?:with|in the hands of)\s+(?:the\s+|our\s+)?"
    r"(?:browser|domain|file|email|calendar|background|task)?\s*agent"
    # Browser/media action claims (live bug 2026-07-23). After a REAL browse
    # ("play ep 170 of black clover on anikoto.cz"), a short follow-up
    # correction ("i meant ep 5 of season 2 in english dub") missed every
    # routing gate, fell to plain chat, and the chat LLM — seeing the prior
    # "opened and playing" turns in history — fabricated "I'll switch it over.
    # Episode 5 … is now open and playing on anikoto.cz." Chat cannot drive a
    # browser, open a page, or play/switch a video, so a first-person or
    # present-continuous SWITCH claim, a "now open and playing" STATE claim, or
    # a "playing on <site>" claim is always a fabrication. Anchored (the
    # email/calendar discipline) so a capability OFFER is spared: the switch
    # branch requires a first-person "I'll/I've…" or the present-continuous
    # "switching" (never bare "I can switch it over"), and the playing branch
    # requires a real domain, so "I can play videos on YouTube" never trips.
    # Real browse outcomes stream via task_router/summary.py, never this path.
    r"|(?:i(?:'ll| will|'ve| have|'m| am)\s+switch(?:ed)?|switching)\s+"
    r"(?:it|the (?:video|episode|show|stream|channel))\s+(?:over|to)\b"
    r"|now open and playing\b"
    r"|playing (?:it )?(?:on|in) [a-z0-9][\w.-]*\.[a-z]{2,}\b)",
    re.IGNORECASE,
)

# ------------------------------------------------------ dead-end offer guard
#
# _SYSTEM_VOICE_RE above deliberately spares "conditional capability offers
# ('I can search…', 'just say the word')" — and it is right to: those are not
# fabrications. Jarvis genuinely CAN search; the sentence is true. It is also
# a dead end, and that is a different defect needing a different remedy.
#
# Live 2026-07-17, twice in one evening: "which teams qualified for fifa
# finals 2026" → "Worth looking up for the latest — ask me to search the web
# for it, sir." The user then has to guess the magic phrase that would have
# routed, which is not a thing anyone should have to learn about their own
# assistant. Routing had already failed for that turn (a gate miss the first
# time; the second message routes correctly in code, so its live miss was the
# classifier's single fail-open call — which no amount of gate work can rule
# out, since ANY error there degrades silently to CHAT).
#
# So this is the recall backstop for every routing miss, whatever its cause —
# gate hole, typo'd question word, classifier flake, provider error. The
# signal is the model's own admission that the answer needs the live web, and
# unlike a keyword list that admission needs no vocabulary to recognize: the
# model produced it having seen the whole conversation. Rather than correct
# it (there is nothing false to correct) we take it at its word and DO the
# search — see task_router.rescue_web_turn.
#
# Anchored to OFFERS and REQUESTS-FOR-PERMISSION only. A past-tense report
# ("I searched the web and found…") is the plan path's own voice and must
# never trip this; those come from task_router, not here.
#
# Every branch must name a LOOKUP. A bare "just say the word" was in the first
# draft and the test suite caught it within the minute: the existing fixture
# "I can help with that — just say the word." answers "i got an email from
# jamil yesterday", where the offer is about mail, not the web — rescuing it
# would have run the planner on a turn that was working correctly. An offer is
# only a dead end when it offers something chat cannot do.
_DEAD_END_OFFER_RE = re.compile(
    r"(?:"
    # "ask me to search…" / "ask me to look it up" — instructing the user to
    # re-ask IS the magic-word pattern, whatever the object; chat can no more
    # search files or mail on its own than it can search the web.
    r"ask me to (?:search|look|check|find)"
    # An offer or permission-request that names an external lookup.
    r"|(?:i can|i could|i'd be happy to|shall i|want me to|"
    r"(?:would|do) you (?:want|like) me to|if you(?:'d| would)? (?:like|want) me to|"
    r"(?:let me know|tell me) if you(?:'d| would)? (?:like|want) me to|"
    r"i(?:'d| would) (?:need|have) to)"
    r"[^\n]{0,24}?"
    r"(?:search(?:ing)? (?:the )?(?:web|internet)|search online|look online|"
    r"look (?:it|that|them|this) up|check online|google (?:it|that))"
    r"|say the word[^\n]{0,30}?(?:search|look (?:it|that) up)"
    r")",
    re.IGNORECASE,
)

# How much of the stream's tail is held back so an offer can still be un-said
# once it becomes recognizable. Must exceed the longest matchable prefix of
# _DEAD_END_OFFER_RE ("(let me know|tell me) if you would like me to " ≈ 40).
_OFFER_LOOKBEHIND = 64

# Used only when the rescue itself fails (planner/provider down). Deterministic
# and honest: it states what happened and never asks the user for a password.
_DEAD_END_FALLBACK = (
    "\n\nI tried to look that up and the search itself failed, so I have no "
    "answer for you rather than a guessed one. Worth trying again in a moment."
)

_IMPERSONATION_CORRECTION = (
    "\n\n⚠️ **Correction from the Jarvis system:** the text above imitated a "
    "system message but was generated by the conversation model — no task ran, "
    "no message was sent, no web search happened, no browser opened or video "
    "played, no reminder or calendar event was created, and nothing was "
    "changed. To actually do this, say it as a direct instruction, e.g. "
    '"delete the .txt files in my Downloads folder", "email Jamil about '
    'dinner", "search the web for when the next season comes out", or "play ep '
    '5 of My Hero Academia season 2 dub on anikoto.cz".'
)


def _interpret_yes_no(text: str) -> Optional[bool]:
    """True/False for a clear answer, None when the reply is something else."""
    t = text.strip().lower()
    if _NEGATIVE_RE.search(t):
        return False
    if _AFFIRMATIVE_RE.search(t):
        return True
    return None


def _still_open_note(reply: str, mentions: list) -> str:
    """
    Note injected when a reply settles none of the parked names. The parked
    facts are NEVER destroyed here — two live regressions ("yes" after the
    LLM's own confirmation question, then "its also happening") proved no
    heuristic can safely tell a failed name attempt from an unrelated reply.
    The question stays parked and simply expires via its TTL if never
    answered.
    """
    open_questions = "; ".join(
        f'"{m["name"]}" (options: {", ".join(c["name"] for c in m["candidates"])})'
        for m in mentions
    )
    return (
        f"DISAMBIGUATION STILL OPEN: The user's latest reply '{reply}' did not name any saved contact, "
        f"so the earlier question is still unanswered: {open_questions}. The pending information is NOT "
        f"saved yet. If the reply looks like it was naming a person, tell the user that name isn't in "
        f"their contacts and re-ask with the listed options. Otherwise respond to their message naturally "
        f"first, then briefly re-ask the pending question."
    )


def _saved_just_now_clause(saved_texts: list) -> str:
    """
    Spell out exactly what was written this turn. The parked writes land in
    the DB BEFORE format_context renders the memory block, so the just-saved
    fact already shows up in MEMORY CONTEXT of the same prompt — without this
    clause the LLM reads it as an old memory ("we had previously noted...").
    """
    texts = [t for t in saved_texts if t]
    if not texts:
        return ""
    listed = "; ".join(f'"{t}"' for t in texts)
    return (
        f" SAVED JUST NOW (this very moment, from the user's current confirmation): {listed}. "
        f"If this same information appears in the MEMORY CONTEXT above, that entry was written seconds ago — "
        f"it is NOT something you knew before and NOT a previous plan. The user is telling you this for the FIRST time."
    )


_BUSY_NOTE = (
    "The user appears to be busy or under load right now. Keep this reply "
    "especially short and direct — lead with the answer, drop optional extras "
    "and any wit. Do not comment on their state or that you are being brief."
)


async def _affective_note(db) -> str:
    """Best-effort brevity steer from the World Model (Phase 13.2). Returns the
    busy note only when affective sensing reads the user as busy/stressed with
    enough confidence; '' otherwise (and on ANY failure — a context read must
    never block or break a chat turn)."""
    try:
        from app.core.context_store import get_world_model, high_load
        world = await get_world_model(db)
        return _BUSY_NOTE if high_load(world.user_state) else ""
    except Exception as e:
        logger.debug(f"Affective note skipped (non-critical): {e}")
        return ""


async def _screen_note(db) -> str:
    """Best-effort screen context for chat (screen-aware chat — the
    _affective_note pattern). When the user opted in (master + screen_ocr +
    screen_in_chat, enforced in context_store.screen_context_for_chat) returns
    either the labeled screen-context block or the honest no-fresh-capture
    note (never leave an opted-in LLM guessing — it invents trigger phrases);
    '' when the gate is off (and on ANY failure — a context read must never
    block or break a chat turn)."""
    try:
        from app.core.context_store import screen_context_for_chat
        return await screen_context_for_chat(db)
    except Exception as e:
        logger.debug(f"Screen note skipped (non-critical): {e}")
        return ""


def _build_system_prompt(
    memory_context: str = "",
    pending_resolution: dict = None,
    disambiguation_resolved_note: str = None,
    pending_creation: dict = None,
    ambiguous_mentions: list = None,
    affective_note: str = "",
    screen_note: str = "",
    background_note: str = "",
) -> str:
    """Build the Jarvis OS system prompt, with optional memory context block.
    `affective_note` (Phase 13.2) is an optional, best-effort brevity steer when
    the World Model reads the user as busy/stressed — appended AFTER the honesty
    rules so it can never soften them. `screen_note` (screen-aware chat) is the
    opt-in on-screen-text context block, appended in the same position with
    context-only framing — the honesty rules still outrank it."""
    from datetime import datetime
    current_datetime = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
    
    base = f"""You are Jarvis, a personal AI operating system — a digital extension of the user, not a generic assistant.

IDENTITY:
- You are calm, precise, and intelligent — the composed, quietly capable register of a trusted butler who happens to run the house's computers. Unflappable, economical, never obsequious.
- Address the user as "Sir" — naturally, as an opening or closing beat ("Of course, sir.", "Anything else, sir?"), not in every sentence and never more than once per response.
- Dry, understated wit is welcome in small doses — one light touch at most, and never when delivering bad news, errors, or anything the user needs to act on. Clarity always outranks charm.
- You are anticipatory but never presumptuous: you may note what the user will likely need next ("You may also want the totals, sir — say the word."), but you never claim to have done anything unasked.
- You are direct. No filler phrases like "Certainly!", "Of course!", "Great question!" as throat-clearing openers.
- Never refer to yourself as an AI, assistant, or chatbot. You are Jarvis.

CAPABILITIES:
- You CAN act on the user's computer and accounts through a separate tool system: search/read/list files and folders, create/move/rename/delete files, run terminal commands and scripts, read and send email, manage the user's Google Calendar, search the web and read web pages, drive a real web browser to act on live sites (play or watch a video, sign in and navigate, fill in and submit forms), set reminders, and run long tasks in the background. Never claim you lack file-system, email, calendar, web, browser, or computer access.
- Action requests are detected and routed to that system BEFORE the message reaches you. If a request to act still reaches you here, it was not recognized as a task: do NOT pretend you did it and do NOT deny you can — tell the user you can do it and ask them to rephrase it as a direct instruction (e.g. "list the files in <folder>", "email Jamil about dinner", "search the web for X").
- CREDENTIAL SAFETY: NEVER ask the user for a password, PIN, passphrase, personal access token, 2FA code, or any other credential — and never accept one if it is offered. You cannot and must not collect credentials. Signing in to a website is a browser action: Jarvis opens the site's OWN sign-in page in a window, the user signs in there themselves, and Jarvis continues the task afterwards — Jarvis never sees or handles the password. So a request like "sign in to github and open my oldest repo" or "log into my account and download the invoice" is something you CAN do (via that browser sign-in hand-off); if it reaches you here, do NOT ask for a username/password and do NOT claim you signed in — tell the user you can do it and ask them to say it as one direct instruction (e.g. "sign in to github and open my oldest repo").
- TASK OUTCOME HONESTY: task and background-task outcome messages in this conversation are the COMPLETE record of what was done and found. Never add, infer, or embellish results (file names, counts, contents, emails, events) beyond what those messages literally state. If an outcome message does not contain the answer the user wants, say the task did not report it and offer to run it again — never fill the gap yourself.
- OWN-ACTION HONESTY: Jarvis's own action history lives in an audit log this conversation cannot see. Never assert or deny from memory what Jarvis itself created, deleted, moved, renamed, sent, or ran ("the folder you created", "did you delete X?") — memory holds the user's life, not Jarvis's actions, and a name found there may be stale or wrong. Say you need to check the action record and ask the user to say it as a direct question (e.g. "tell me what you created today"), which routes to that record.
- NEVER imitate system-generated messages ("Finished the background task…", "I've started working on that in the background…", "Reminder set — …", "Done — N step(s) completed.", "Email sent — …", "Event created — …", "Episode X is now open and playing on <site>", "I'll switch it over", approval prompts). Those texts are produced by the backend only, after real actions — writing them yourself is claiming actions that never happened.
- You cannot start, queue, or schedule any action from this conversation — not a file operation, not an email send, not a calendar event, not opening a web page or playing/switching a video in a browser. Never say you "will" perform an action, that a task "has been initiated", that an email "has been sent", that an event "has been created", that a video is "now playing" or that you've "switched it over"/"opened it", or otherwise promise action — nothing you write here makes anything happen. This applies to follow-up corrections too: if the user corrects a browser/media request ("i meant ep 5 of season 2 in the dub"), do NOT claim you switched it — say you can do it and ask them to say it as one direct instruction. When the user wants an action, the ONLY honest reply is to ask them to say it as one direct instruction (e.g. "delete the .txt files in the phase3test folder on my desktop", "email Jamil that I'll be late", "play ep 5 of My Hero Academia season 2 dub on anikoto.cz").
- EXTERNAL-FACT HONESTY: for a factual question about the outside world — a person, company, product, place, show/anime/movie/game/book, current events, news, prices, or scores — that your built-in knowledge cannot answer accurately and up to date, do NOT answer from possibly-stale training data and do NOT invent specifics (dates, numbers, names, plots). These questions are normally looked up on the web BEFORE they reach you; if one still arrives here, say plainly that it's worth checking for the current facts and invite the user to have you search it (e.g. "Worth looking up for the latest — ask me to search the web for it, sir."). Do NOT claim you are searching, have searched, or will search: only a direct "search the web for X" instruction runs a real search.
- SCREEN AWARENESS: when the user has enabled screen sensing + screen-aware chat in Settings, what's on their screen appears in a SCREEN CONTEXT block below — captured automatically, with no action from you or them. There is NO command, request, or trigger phrase that makes you see the screen: NEVER tell the user to say "take a screenshot" or any similar magic words. If no SCREEN CONTEXT block is present at all, screen awareness is turned off — say you can't see the screen right now and that it can be enabled in Settings → Context & sensing.

MEMORY RULES:
- Everything in the MEMORY CONTEXT below is confirmed fact. Use it naturally in responses.
- When a contact has multiple facts in the same category with different values, the most recent fact is the current truth. Older facts are historical context only.
- Never say "according to my memory" or "I remember that". Just use the knowledge naturally.
- If the memory context is empty, you know nothing about the user yet. Ask to learn.
- Never fabricate facts, past interactions, emails, reminders, or events not in memory.
- If the user tells you something new about themselves, acknowledge it naturally.
- TIMELINE HONESTY: memory entries may have been written seconds ago from THIS very conversation. Never claim the user told you something "previously" or that something "was already in our plans" unless you are certain it came from an earlier conversation — when in doubt, treat it as new information the user just gave you.
- Current date and time: {current_datetime}

CLARIFICATION RULES:
- Never assume the identity of vague references ("he", "this project", "that file").
- AMBIGUITY DETECTION (this is mandatory, not optional): Before acting on ANY contact name, scan ALL the names in the PEOPLE YOU KNOW section. If there are 2 or more entries whose names contain the same word (e.g. "jamil" appears in "jamil", "jami", "Jamil Ali", "jamil ali khan"), they are ALL considered ambiguous — even if one is an exact character match. You MUST ask the user which specific person they mean BEFORE proceeding. Do not assume the exact match is correct. Do not act on any information until the user explicitly picks one.
- Once the user explicitly names which person (e.g. "I meant Jamil Ali" or "the second one"), accept it immediately and proceed. Do NOT re-question or hedge.
- If the user asks to save information for a person, check if that exact name OR a very similar spelling exists in the MEMORY CONTEXT. If there is a clear match, use it naturally. If there is NO reasonably close match, inform the user the person is not in your contacts.
- If a project is mentioned without an explicit name, confirm which project before acting.
- If someone is described as helping/working without naming the project, ask which project.
- One clarifying question at a time. Never interrogate the user.

GROUNDING RULE:
- If the user uses a pronoun ("he", "she", "his", "her", "they", "it") and the active entity/topic is shown in the memory context, treat that pronoun as referring to that active entity. If there is NO active entity in context and the referent is unclear, you MUST ask "Who are you referring to?" — never guess or fabricate.

MEMORY HONESTY RULE:
- NEVER say "I've noted", "I've saved", "I've updated", "I'll remember", "I've added", or any similar confirmation that implies you stored something.
- You do NOT have direct control over memory. Memory is written by a separate background system after your response.
- When the user tells you a fact, simply acknowledge it naturally in conversation (e.g. "Got it" or "That's August 6th then."). Do NOT claim you stored it.
- If the user asks you to save something, respond with what you understood (e.g. "Jamil Ali's birthday is August 6th — noted.") but do not claim it was written.
- The ONLY exception: when a [SYSTEM NOTE — BACKEND RESOLVED] below explicitly states that something WAS saved or created, the backend has already written it — you may (and should) confirm that naturally.

RESPONSE STYLE:
- Be concise by default. Expand only when the topic requires depth. Composure reads as brevity, not verbosity.
- Use plain language. Avoid jargon unless the user uses it first.
- Never add unnecessary caveats or disclaimers.
- Format with markdown only when it genuinely helps readability.
- The persona colors your phrasing only — it never overrides the honesty rules above. A charming fabrication is still a fabrication.
"""

    if affective_note:
        base += f"""
ADAPTIVE NOTE (context signal — never mention it to the user):
{affective_note}
"""

    if screen_note:
        base += f"""
SCREEN CONTEXT (what's on the user's screen right now and recently — or an honest note that no fresh capture exists) — context only; the user may or may not be asking about it. If the answer isn't here, say so — never invent screen contents. WHEN you actually use this to answer, briefly and naturally acknowledge the source (e.g. 'Based on what's on your screen in VS Code…' / 'From the article open in Chrome…') — at most once per reply, and only when you genuinely used it. Do NOT narrate the screen unprompted or mention this block when it's irrelevant.
{screen_note}
"""

    if background_note:
        base += f"""
{background_note}

When the user asks how a task or an agent is doing, answer from the BACKGROUND WORK block above. If a task they mention is not listed, say you don't see it running — never invent progress or results.
"""

    if memory_context:
        base += f"""
{memory_context}

Use the above context naturally. Do not recite it back. Do not reference it explicitly.
Simply let it inform how you respond, as a person would use their own memory.
"""

    # Disambiguation resolved — inject clear directive instead of the pending block
    if disambiguation_resolved_note:
        base += f"""
[SYSTEM NOTE — BACKEND RESOLVED]: {disambiguation_resolved_note}
"""
    elif pending_resolution:
        mentions = pending_resolution.get("mentions") or [{
            "name": pending_resolution["original_name"],
            "candidates": pending_resolution["candidates"],
        }]
        mention_lines = [
            f'- "{m["name"]}" → possible matches: {", ".join(c["name"] for c in m["candidates"])}'
            for m in mentions
        ]
        example = " And ".join(
            f'by \'{m["name"]}\' did you mean {" or ".join(c["name"] for c in m["candidates"][:3])}?'
            for m in mentions[:2]
        )
        base += f"""
PENDING DISAMBIGUATION:
A background process flagged the following name(s) from the user's earlier message as ambiguous:
{chr(10).join(mention_lines)}
However, check the latest user message first:
- If the user has ALREADY clearly stated which person they mean for EVERY name above, accept it and proceed — do NOT ask again.
- Otherwise, ask ONE short question that handles EACH unresolved name SEPARATELY, listing only that name's own options (e.g. "Quick check — {example}"). NEVER merge different names' options into combined guesses, and do not invent alternatives.
"""
    elif ambiguous_mentions and not pending_creation:
        mention_lines = []
        for m in ambiguous_mentions:
            names = ", ".join(c["name"] for c in m["candidates"])
            mention_lines.append(f'- "{m["mention"]}" could be any of: {names}')
        example = " And ".join(
            f'by \'{m["mention"]}\' did you mean {" or ".join(c["name"] for c in m["candidates"][:3])}?'
            for m in ambiguous_mentions[:2]
        )
        base += f"""
AMBIGUOUS NAME(S) IN THE CURRENT MESSAGE (backend-verified — this is mandatory):
{chr(10).join(mention_lines)}
The user's latest message names one or more people who each match MULTIPLE saved contacts. You MUST ask which one they mean before confirming, acting on, or discussing any information about those people. Do NOT assume — not even the exact-spelling match. Nothing has been saved yet; the information is held until the user answers.
Ask exactly ONE short question, but handle EACH ambiguous name SEPARATELY inside it: for every name above, list only that name's own options (e.g. "Quick check — {example}"). NEVER merge different names' options into combined guesses like "is it X and one of Y or Z".
"""

    # NOT an elif on the resolved note: resolving one question can park a
    # create-contact question in the SAME turn (live regression: resolving
    # "which jamil?" parked "add daud?", which was then never asked). Only a
    # still-open disambiguation defers it (one clarifying question at a time).
    if pending_creation and not pending_resolution:
        base += f"""
PENDING CONTACT CREATION:
The user mentioned "{pending_creation['name']}", who is NOT in their contacts. Information about this person is on hold.
Ask the user exactly one question: "{pending_creation['name']} isn't in your contacts — want me to add them?"
Do not save or assume anything about this person until the user answers. If the user's latest message already answers this (yes/no), acknowledge and move on — do not ask again.
"""

    return base


# The provider sees a WINDOW of the conversation, never the unbounded whole:
# the frontend sends the full session history every turn, so a long chat grew
# the prompt linearly until every reply was noticeably slow (live complaint
# 2026-07-13). Durable long-range recall is the memory engine's job (MEMORY
# CONTEXT + conversation search), not the raw transcript's. Oldest messages
# are trimmed first; the latest message is always kept.
_HISTORY_MAX_MESSAGES = 30
_HISTORY_MAX_CHARS = 24_000


def _provider_history(request_messages) -> list[LLMMessage]:
    """The capped slice of the request's history that goes to the LLM. Empty-
    content entries are dropped first: the frontend may include a PlanCard-hosting
    message (an approval / clarifying-question card, no text) in the history, and
    the LLM has no use for an empty turn — nor should one ever reach the provider."""
    window = [m for m in request_messages if (m.content or "").strip()]
    window = window[-_HISTORY_MAX_MESSAGES:]
    total = sum(len(m.content or "") for m in window)
    while len(window) > 1 and total > _HISTORY_MAX_CHARS:
        total -= len(window.pop(0).content or "")
    return [LLMMessage(role=m.role, content=m.content) for m in window]


async def _persist_message(
    db: AsyncSession,
    session_id: str,
    role: str,
    content: str,
    model: Optional[str] = None,
    tokens_used: Optional[int] = None,
) -> None:
    """Save a message to the SQLite messages table — best-effort: a failed
    history write must never 500 the chat turn or poison the session for the
    work that follows it (live bug 2026-07-12: a schema-drifted messages
    table made this raise, killing whole turns; see app/db/persist.py)."""
    msg = await persist_message_best_effort(
        db, session_id, role, content, model=model, tokens_used=tokens_used,
    )
    if msg is None:
        return
    # Phase 6 Part 4 — index this turn for content search, so a just-said
    # message is findable via semantic_file_search. Scheduled as a DETACHED
    # task with its own session (latency, 2026-07-13: awaiting the embed +
    # vector upsert here delayed the first streamed token every turn). No-op
    # unless the index is enabled (same privacy toggle as files); never
    # raises; anything missed is swept by the reindex pass's backfill, like
    # the other message writers (tasks, reminders, briefings).
    from app.core.conversation_index import schedule_message_embed
    schedule_message_embed(msg.id)


@router.post("/stream", summary="Streaming chat completion (SSE)")
async def chat_stream(
    request: ChatRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    provider: LLMProvider = Depends(get_llm_provider),
    qdrant=Depends(get_qdrant),
) -> StreamingResponse:
    """
    Stream a chat completion response as Server-Sent Events.
    Phase 2: Injects memory context into system prompt.
    Fires background extraction after streaming completes.
    """
    session_id = request.session_id or str(uuid.uuid4())

    # Per-stage latency observability (2026-07-13, "Jarvis feels slow"): one
    # summary INFO line per turn. Best-effort by construction — see timing.py.
    from app.core.timing import TurnTimer
    timer = TurnTimer("chat turn", session_id)

    # --- Phase 3.5: resurrect a cold session's parked questions from SQLite
    # BEFORE anything peeks at the session — the task gate below defers to an
    # open memory question, so it must see a restored one too.
    from app.memory.session_persistence import restore_pending_state, save_pending_state
    with timer.stage("restore"):
        await restore_pending_state(db, session_id)

    # --- Phase 4 Part 4: reminder detection (app/api/reminder_router.py).
    # Runs BEFORE task routing — "remind me to delete my temp files at 6" is
    # a reminder, not an instruction to delete anything right now. Returns a
    # response for a recognized reminder trigger (clean or ambiguous alike);
    # None falls through unchanged to task routing then Phase 2 chat.
    from app.api.reminder_router import maybe_handle_reminder
    with timer.stage("reminder_route"):
        reminder_response = await maybe_handle_reminder(request=request, session_id=session_id, db=db)
    if reminder_response is not None:
        timer.log()
        return reminder_response

    # --- Phase 6 Part 5: routine routing (app/api/routine_router.py). Runs
    # BETWEEN reminders and tasks — a bare routine name ("clean my desktop")
    # must be intercepted here before the task gate re-plans it as a one-off.
    # TEACH is deterministic (no planner); RUN starts a background Task, so the
    # approval gate and path guards re-apply on the fresh plan automatically.
    from app.api.routine_router import maybe_handle_routine
    with timer.stage("routine_route"):
        routine_response = await maybe_handle_routine(
            request=request, session_id=session_id, db=db, provider=provider
        )
    if routine_response is not None:
        timer.log()
        return routine_response

    # --- 2026-07-29: task continuation (app/api/continuation_router.py). Runs
    # between routines and tasks. A short correction right after a task settled
    # ("look again", "that's not all of them") re-runs the ORIGINAL goal with
    # the correction attached, instead of falling into chat (which cannot act)
    # or drafting a new plan whose goal is literally "look again" — which
    # silently disarms every guard that keys on the goal string.
    from app.api.continuation_router import maybe_handle_continuation
    with timer.stage("continuation_route"):
        continuation_response = await maybe_handle_continuation(
            request=request, session_id=session_id, db=db, provider=provider
        )
    if continuation_response is not None:
        timer.log()
        return continuation_response

    # --- Phase 3: task-request routing (app/api/task_router.py). Returns a
    # response ONLY for confirmed task requests; None (the overwhelmingly
    # common case — the deterministic gate makes no LLM call) continues into
    # the Phase 2 path below, which is untouched. When the gate fires, this
    # stage's duration is dominated by the classify∥memory gather.
    from app.api.task_router import maybe_handle_task
    with timer.stage("task_route"):
        task_response = await maybe_handle_task(
            request=request, session_id=session_id, db=db, provider=provider
        )
    if task_response is not None:
        timer.log()
        return task_response

    # --- Phase 2: Build memory context and update state
    from app.memory.conversation_state import get_session, touch_session, ActiveEntity
    import time
    
    memory_engine = MemoryEngine(db=db, qdrant=qdrant)
    # Get the last 3 user messages to provide better context for short replies (like "yes")
    user_msgs = [m.content for m in request.messages if m.role == "user"]
    recent_user_text = "\n".join(user_msgs[-3:])
    
    memory_context = ""
    pending_resolution = None
    pending_creation = None
    ambiguous_mentions = []
    disambiguation_resolved_note = None  # Injected into system prompt when resolved

    timer.start("context")  # retrieve + disambiguation + format + snapshot
    if recent_user_text:
        try:
            bundle = await memory_engine.retrieve_context(
                recent_user_text,
                session_id=session_id,
                current_message=user_msgs[-1] if user_msgs else None,
            )

            # --- DETERMINISTIC DISAMBIGUATION RESOLUTION ---
            # If there's an active pending_resolution in the session, try to resolve the
            # user's latest message against the candidates IN PYTHON — before calling the LLM.
            # This prevents the LLM from asking the same question twice.
            sess = get_session(session_id)
            last_user_msg_content = user_msgs[-1] if user_msgs else ""
            
            if sess.pending_resolution is not None and last_user_msg_content:
                from app.memory.engine import resolve_confirmation_multi
                pr = sess.pending_resolution
                all_contacts = await memory_engine.get_all_contacts()
                contacts_by_id = {c.id: c for c in all_contacts}
                mentions = pr.mentions()

                # One reply may settle SEVERAL pending names at once
                # ("i meant hamil and ali raza" answering both "ali" and "jamil").
                assignment = resolve_confirmation_multi(last_user_msg_content, mentions, all_contacts)

                if not assignment:
                    # The question stays parked; the note handles both "user
                    # named someone unknown" and "user said something else".
                    disambiguation_resolved_note = _still_open_note(
                        last_user_msg_content, mentions
                    )
                else:
                    # Confirmed contacts: names settled in prior turns plus this reply.
                    preresolved = {
                        as_said: contacts_by_id[cid]
                        for as_said, cid in pr.resolved_so_far.items()
                        if cid in contacts_by_id
                    }
                    for as_said, cid in assignment.items():
                        if cid in contacts_by_id:
                            preresolved[as_said.lower()] = contacts_by_id[cid]
                    # Remember the user's picks for the whole session — a
                    # later fact saying the same name must never re-ask.
                    sess.confirmed_names.update(
                        {as_said.lower(): cid for as_said, cid in assignment.items()}
                    )

                    resolved_contacts = [
                        contacts_by_id[cid] for cid in assignment.values() if cid in contacts_by_id
                    ]
                    primary = preresolved.get(pr.original_name.lower()) or (
                        resolved_contacts[0] if resolved_contacts else None
                    )
                    if resolved_contacts:
                        entities = [
                            ActiveEntity(
                                id=c.id, type="contact", name=c.name,
                                confidence=1.0, last_mentioned=time.time(),
                            )
                            for c in resolved_contacts
                        ]
                        sess.active_entities = entities
                        sess.focus_entity = entities[0] if len(entities) == 1 else None

                    # Apply the parked writes deterministically. Facts are
                    # re-routed through store_shared_fact with the confirmed
                    # contacts pinned: fully-resolved facts write both
                    # perspectives; facts with names STILL unresolved re-park
                    # and the question below asks only about those.
                    parked_facts = pr.pending_shared_facts
                    pending_update = pr.pending_update
                    sess.pending_resolution = None  # clear before re-routing; re-parking recreates it
                    touch_session(session_id)

                    saved_something = False
                    saved_texts: list = []
                    try:
                        if pending_update and primary:
                            await memory_engine.update_contact(primary.id, pending_update)
                            saved_something = True
                            user_name_note = await memory_engine.get_user_name()
                            saved_texts.extend(
                                substitute_placeholders(
                                    f.get("fact", ""), user_name=user_name_note,
                                    default_contact_name=primary.name,
                                )
                                for f in pending_update.get("new_facts", []) if f.get("fact")
                            )
                        for parked_fact in parked_facts:
                            text = await memory_engine.store_shared_fact(
                                parked_fact, session_id=session_id, preresolved=preresolved
                            )
                            if text:
                                saved_texts.append(text)
                                saved_something = True
                    except Exception as e:
                        logger.warning(f"Applying parked writes after disambiguation failed: {e}")

                    resolved_names = ", ".join(dict.fromkeys(c.name for c in resolved_contacts))
                    saved_note = (
                        f"The pending information from their previous message has now been successfully linked and saved to: {resolved_names}. "
                        if saved_something else
                        f"The original pending fact is now securely linked to: {resolved_names}. "
                    )
                    disambiguation_resolved_note = (
                        f"DISAMBIGUATION RESOLVED: The user meant: {resolved_names}. "
                        + saved_note +
                        f"CRITICAL: Acknowledge this naturally (e.g., 'Got it. Noted.'), but DO NOT phrase your response as if this is an old memory you just remembered (e.g. do NOT say 'we had previously noted' or 'that's already in our plans'). The user literally just told you this in the previous turn! Do NOT ask for clarification again."
                        + _saved_just_now_clause(saved_texts)
                    )
                    # If some names are STILL unresolved, the fact re-parked —
                    # tell the LLM to ask about those names only.
                    still_pending = get_session(session_id).pending_resolution
                    if still_pending is not None:
                        remaining = "; ".join(
                            f'"{m["name"]}" (options: {", ".join(c["name"] for c in m["candidates"])})'
                            for m in still_pending.mentions()
                        )
                        disambiguation_resolved_note += (
                            f" HOWEVER, the fact also involves name(s) still ambiguous: {remaining}. "
                            f"After acknowledging, ask ONE short question — for EACH remaining name separately, "
                            f"list that name's options and ask which one they meant."
                        )

            # --- PENDING CONTACT CREATION: resolve the yes/no in Python ---
            elif sess.pending_creation is not None and last_user_msg_content:
                pc = sess.pending_creation
                if time.time() > pc.expires:
                    sess.pending_creation = None
                else:
                    # Sanity recheck: never ask to create a name that actually
                    # resolves against the contact list (the extractor may have
                    # normalized/expanded a name it saw in memory context).
                    from app.memory.engine import identify_contact, ResolutionStatus
                    from app.memory.conversation_state import PendingResolution
                    all_contacts_chk = await memory_engine.get_all_contacts()
                    chk = identify_contact(pc.name, all_contacts_chk)
                    if chk.status == ResolutionStatus.RESOLVED:
                        parked_facts = pc.pending_shared_facts
                        all_contacts_map = {c.id: c for c in all_contacts_chk}
                        preresolved = {
                            as_said: all_contacts_map[cid]
                            for as_said, cid in pc.resolved_so_far.items()
                            if cid in all_contacts_map
                        }
                        preresolved[pc.name.lower()] = chk.contact
                        sess.confirmed_names[pc.name.lower()] = chk.contact.id
                        sess.pending_creation = None  # clear before re-routing
                        touch_session(session_id)
                        try:
                            saved_any = False
                            saved_texts = []
                            if any(v for v in pc.pending_update.values()):
                                await memory_engine.update_contact(chk.contact.id, pc.pending_update)
                                saved_any = True
                                user_name_note = await memory_engine.get_user_name()
                                saved_texts.extend(
                                    substitute_placeholders(
                                        f.get("fact", ""), user_name=user_name_note,
                                        default_contact_name=chk.contact.name,
                                    )
                                    for f in pc.pending_update.get("new_facts", []) if f.get("fact")
                                )
                            for parked_fact in parked_facts:
                                text = await memory_engine.store_shared_fact(
                                    parked_fact, session_id=session_id, preresolved=preresolved
                                )
                                if text:
                                    saved_texts.append(text)
                                    saved_any = True
                            if saved_any:
                                disambiguation_resolved_note = (
                                    f"NAME MATCHED EXISTING CONTACT: \"{pc.name}\" is the saved contact "
                                    f"\"{chk.contact.name}\" — they were NOT missing. The pending information "
                                    f"from their previous message has now been successfully saved to \"{chk.contact.name}\". "
                                    f"CRITICAL: Acknowledge this naturally without sounding like it is an old memory (e.g. do NOT say 'we had previously noted'). Do NOT offer to create a contact."
                                    + _saved_just_now_clause(saved_texts)
                                )
                        except Exception as e:
                            logger.warning(f"Applying rechecked pending creation failed: {e}")
                        if sess.pending_creation is pc:  # re-routing may have parked a NEW question
                            sess.pending_creation = None
                        touch_session(session_id)
                    elif chk.status == ResolutionStatus.AMBIGUOUS:
                        # Multiple matches now — convert into a disambiguation question
                        if sess.pending_resolution is None:
                            sess.pending_resolution = PendingResolution(
                                original_name=pc.name,
                                pending_update=pc.pending_update,
                                candidates=chk.candidates,
                                pending_shared_facts=pc.pending_shared_facts,
                                unresolved_mentions=[{"name": pc.name, "candidates": chk.candidates}],
                                resolved_so_far=dict(pc.resolved_so_far),
                            )
                        sess.pending_creation = None
                        touch_session(session_id)
                    else:
                        answer = _interpret_yes_no(last_user_msg_content)
                        if answer is True:
                            try:
                                new_contact = await memory_engine.create_contact_manual(
                                    pc.name,
                                    {k: v for k, v in pc.pending_update.items() if k != "new_facts"},
                                )
                                parked_facts = pc.pending_shared_facts
                                all_contacts_map = {c.id: c for c in all_contacts_chk}
                                preresolved = {
                                    as_said: all_contacts_map[cid]
                                    for as_said, cid in pc.resolved_so_far.items()
                                    if cid in all_contacts_map
                                }
                                preresolved[pc.name.lower()] = new_contact
                                sess.confirmed_names[pc.name.lower()] = new_contact.id
                                sess.pending_creation = None  # clear before re-routing
                                touch_session(session_id)
                                saved_texts = []
                                if pc.pending_update.get("new_facts"):
                                    await memory_engine.update_contact(
                                        new_contact.id, {"new_facts": pc.pending_update["new_facts"]}
                                    )
                                    user_name_note = await memory_engine.get_user_name()
                                    saved_texts.extend(
                                        substitute_placeholders(
                                            f.get("fact", ""), user_name=user_name_note,
                                            default_contact_name=new_contact.name,
                                        )
                                        for f in pc.pending_update.get("new_facts", []) if f.get("fact")
                                    )
                                for parked_fact in parked_facts:
                                    text = await memory_engine.store_shared_fact(
                                        parked_fact, session_id=session_id, preresolved=preresolved
                                    )
                                    if text:
                                        saved_texts.append(text)
                                new_entity = ActiveEntity(
                                    id=new_contact.id, type="contact",
                                    name=new_contact.name, confidence=1.0,
                                    last_mentioned=time.time()
                                )
                                sess.active_entities = [new_entity]
                                sess.focus_entity = new_entity
                                disambiguation_resolved_note = (
                                    f"CONTACT CREATED: \"{pc.name}\" has been added to the user's contacts and the pending "
                                    f"information from their previous message has now been successfully saved. "
                                    f"CRITICAL: Confirm this naturally without sounding like it is an old memory (e.g. do NOT say 'we had previously noted'). Do NOT ask again."
                                    + _saved_just_now_clause(saved_texts)
                                )
                            except Exception as e:
                                logger.warning(f"Pending contact creation failed: {e}")
                            if sess.pending_creation is pc:  # re-routing may have parked a NEW question
                                sess.pending_creation = None
                            touch_session(session_id)
                        elif answer is False:
                            saved_texts = []
                            try:
                                for parked_fact in pc.pending_shared_facts:
                                    saved_texts.append(
                                        await memory_engine.apply_shared_fact_user_only(parked_fact, as_said_name=pc.name)
                                    )
                            except Exception as e:
                                logger.warning(f"User-only fact write after declined creation failed: {e}")
                            disambiguation_resolved_note = (
                                f"CONTACT CREATION DECLINED: The user chose not to add \"{pc.name}\" as a contact. "
                                f"Any fact involving the user from their previous message was successfully saved to their own memory instead. "
                                f"CRITICAL: Acknowledge naturally without sounding like it is an old memory. Do NOT ask again."
                                + _saved_just_now_clause(saved_texts)
                            )
                            sess.pending_creation = None
                            touch_session(session_id)
                        # answer is None → leave pending; the system prompt keeps the question alive
            # --- END DISAMBIGUATION / CREATION ---
            
            # Update ConversationSession deterministically.
            # Only replace active_entities when the current message resolves new entities;
            # otherwise retain existing pinned state.
            if bundle.resolved_entities and sess.pending_resolution is None:
                active_entities = [
                    ActiveEntity(
                        id=e.id, type=e.type, name=e.name,
                        confidence=e.confidence, last_mentioned=time.time()
                    )
                    for e in bundle.resolved_entities
                ]
                sess.active_entities = active_entities
                # focus_entity: single entity when unambiguous; None in comparison mode
                sess.focus_entity = active_entities[0] if len(active_entities) == 1 else None
                touch_session(session_id)
                
            memory_context = await memory_engine.format_context(bundle)
            pending_resolution = bundle.pending_resolution
            # Re-read from the session: the deterministic block above may have
            # just resolved/cleared what retrieve_context snapshotted earlier.
            sess_after = get_session(session_id)
            if sess_after.pending_resolution is None:
                pending_resolution = None
            else:
                # Always re-serialize: the block above may have re-parked with
                # fewer mentions, or converted a creation into a disambiguation.
                pr_now = sess_after.pending_resolution
                pending_resolution = {
                    "original_name": pr_now.original_name,
                    "candidates": pr_now.candidates,
                    "mentions": pr_now.mentions(),
                }
            if sess_after.pending_creation is not None and time.time() <= sess_after.pending_creation.expires:
                pending_creation = {"name": sess_after.pending_creation.name}
            ambiguous_mentions = bundle.ambiguous_mentions
        except Exception as e:
            logger.warning(f"Memory context build failed (non-critical): {e}")

        # Phase 3.5: snapshot the pending state as it now stands (consumed,
        # re-parked, or unchanged) so a restart never loses an open question.
        await save_pending_state(db, session_id)
    timer.stop("context")

    # Build message history with memory-enhanced system prompt
    affective_note = await _affective_note(db)
    screen_note = await _screen_note(db)
    background_note = await active_tasks_context(db, session_id)
    messages: list[LLMMessage] = [
        LLMMessage(role="system", content=_build_system_prompt(
            memory_context, pending_resolution, disambiguation_resolved_note,
            pending_creation=pending_creation,
            ambiguous_mentions=ambiguous_mentions,
            affective_note=affective_note,
            screen_note=screen_note,
            background_note=background_note,
        ))
    ]
    messages.extend(_provider_history(request.messages))

    # Persist the user's message. The timestamp anchors the late-reply check:
    # any user message persisted AFTER this moment arrived while the
    # background extraction below was still running.
    from datetime import datetime, timezone
    last_user_msg = user_msgs[-1] if user_msgs else None
    user_msg_persisted_at = None
    if last_user_msg:
        with timer.stage("persist"):
            await _persist_message(db, session_id, "user", last_user_msg)
        user_msg_persisted_at = datetime.now(timezone.utc).replace(tzinfo=None)

    async def event_generator():
        """Yields SSE-formatted chunks from the provider stream."""
        full_response = []
        impersonation = False
        dead_end = False
        # Text received but deliberately not emitted yet — see the dead-end
        # guard below.
        pending = ""
        started = False

        def _chunk(text: str) -> str:
            chunk = StreamChunk(
                delta=text, done=False, session_id=session_id,
                model=provider.model_name, provider=provider.provider_name,
            )
            return f"data: {chunk.model_dump_json()}\n\n"

        try:
            async for delta in provider.stream_chat(messages):
                if not started:
                    started = True
                    timer.mark("ttft")  # request start → first model delta
                pending += delta

                # Dead-end guard. Unlike the impersonation guard below — which
                # cuts AFTER yielding, wanting its marker visible so the
                # correction has a referent — this one is about to answer the
                # question for real, so the offer must never reach the screen
                # at all: Jarvis appearing to ask permission and then acting
                # anyway reads worse than either alone.
                #
                # An offer is only recognizable once its whole phrase has
                # arrived, and it spans deltas — so a naive emitter has
                # already sent its opening words by then. Live 2026-07-17,
                # first cut of this guard: "…as matches are played. Ask me to"
                # appeared on screen, immediately followed by the real answer.
                # The tail is therefore held back until it is provably not the
                # start of an offer. Costs a constant sub-word lag, invisible
                # at streaming speed and harmless to Phase 7's sentence
                # segmenter, which reads accumulated text rather than deltas.
                offer = _DEAD_END_OFFER_RE.search(pending)
                if offer:
                    head = pending[: offer.start()]
                    if head:
                        full_response.append(head)
                        yield _chunk(head)
                    dead_end = True
                    break
                if len(pending) <= _OFFER_LOOKBEHIND:
                    continue
                ready, pending = pending[:-_OFFER_LOOKBEHIND], pending[-_OFFER_LOOKBEHIND:]
                full_response.append(ready)
                yield _chunk(ready)
                # Impersonation guard: cut the stream at the first system-voice
                # marker — the rest of a fabricated task/reminder lifecycle is
                # never delivered, and the correction below sets the record
                # straight. Checked AFTER yielding so the marker itself is
                # visible; markers can span deltas, so the accumulated text is
                # searched, not the delta.
                if _SYSTEM_VOICE_RE.search("".join(full_response)):
                    impersonation = True
                    logger.warning(
                        "Chat LLM impersonated a system message — stream cut "
                        "and corrected (session {})", session_id,
                    )
                    break

            # Flush whatever the look-behind was still holding. A clean stream
            # ends here, so this is the ordinary path, not an edge case.
            #
            # The in-loop cut above can only see text that has been EMITTED, and
            # emission lags by _OFFER_LOOKBEHIND — so a marker in the last 64
            # characters was never cut at all, and the whole fabricated tail
            # shipped (found 2026-08-01 while adding the agent-hand-off pattern:
            # "It's with the browser agent in the background; I'll confirm the
            # moment it's live." is 130 chars, and the marker sits past the
            # 66-char emitted prefix). Search the held-back buffer too and cut
            # there, keeping the marker itself visible — the correction below
            # needs a referent, which is why this guard cuts AFTER the marker
            # rather than before it.
            if not dead_end and not impersonation and pending:
                emitted = "".join(full_response)
                late = _SYSTEM_VOICE_RE.search(emitted + pending)
                if late and late.end() > len(emitted):
                    pending = pending[: late.end() - len(emitted)]
                full_response.append(pending)
                yield _chunk(pending)
                if _SYSTEM_VOICE_RE.search("".join(full_response)):
                    impersonation = True
                    logger.warning(
                        "Chat LLM impersonated a system message — corrected "
                        "(session {})", session_id,
                    )

        except Exception as e:
            logger.error(f"Streaming error: {e}")
            error_chunk = StreamChunk(
                delta=f"\n\n[Error: {str(e)}]",
                done=True,
                session_id=session_id,
            )
            yield f"data: {error_chunk.model_dump_json()}\n\n"
            timer.log()
            return

        if dead_end and last_user_msg:
            # The model says this needs the live web. Take it at its word and
            # run the search, rather than handing the user a phrase to guess.
            logger.warning(
                "Chat LLM offered a web search it cannot run — routing missed "
                "this turn; rescuing it through the planner (session {})",
                session_id,
            )
            from app.api.task_router import rescue_web_turn
            try:
                async for sse in rescue_web_turn(
                    last_user_msg, request, session_id, db, provider
                ):
                    yield sse
                timer.log()
                # The plan path sent its own done chunk and persisted its own
                # outcome. The chat prefix is deliberately NOT persisted: it
                # was a false start, and history should hold the real answer.
                # Extraction is skipped for the same reason task turns skip it
                # — a lookup is not autobiography.
                return
            except Exception as e:
                # Never let the rescue break the turn: fall through to the
                # honest deterministic text below, which at least does not
                # send the user hunting for a magic word.
                logger.error(f"Web rescue failed: {e}")
                full_response.append(_DEAD_END_FALLBACK)
                fallback_chunk = StreamChunk(
                    delta=_DEAD_END_FALLBACK, done=False, session_id=session_id,
                    model=provider.model_name, provider=provider.provider_name,
                )
                yield f"data: {fallback_chunk.model_dump_json()}\n\n"

        if impersonation:
            full_response.append(_IMPERSONATION_CORRECTION)
            correction_chunk = StreamChunk(
                delta=_IMPERSONATION_CORRECTION,
                done=False,
                session_id=session_id,
                model=provider.model_name,
                provider=provider.provider_name,
            )
            yield f"data: {correction_chunk.model_dump_json()}\n\n"

        # Send final done event
        done_chunk = StreamChunk(
            delta="",
            done=True,
            session_id=session_id,
            model=provider.model_name,
            provider=provider.provider_name,
        )
        yield f"data: {done_chunk.model_dump_json()}\n\n"
        timer.log()

        # Persist the complete assistant response
        complete_response = "".join(full_response)
        if complete_response:
            await _persist_message(
                db,
                session_id,
                "assistant",
                complete_response,
                model=provider.model_name,
            )

            # --- Phase 2: Fire background extraction pipeline
            # Never on an impersonated turn: the assistant text is a known
            # fabrication and must not seed memories.
            if last_user_msg and complete_response and not impersonation:
                # Pass the full conversation so extractor can resolve vague references
                conversation_history = [
                    {"role": m.role, "content": m.content}
                    for m in request.messages
                ]
                background_tasks.add_task(
                    _run_extraction,
                    user_message=last_user_msg,
                    assistant_message=complete_response,
                    conversation_history=conversation_history,
                    session_id=session_id,
                    provider=provider,
                    qdrant=qdrant,
                    extracted_at=user_msg_persisted_at,
                )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def apply_pending_resolution_reply(
    memory_engine: MemoryEngine, session_id: str, reply_text: str
) -> list[str]:
    """
    Deterministically resolve the session's parked disambiguation against a
    user reply and apply the parked writes (same re-routing as the foreground
    block in chat_stream). Returns the saved user-perspective texts; empty
    list when the reply settles nothing — the question stays parked.
    """
    from app.memory.conversation_state import get_session, touch_session
    from app.memory.engine import resolve_confirmation_multi

    sess = get_session(session_id)
    pr = sess.pending_resolution
    if pr is None or not reply_text:
        return []

    all_contacts = await memory_engine.get_all_contacts()
    assignment = resolve_confirmation_multi(reply_text, pr.mentions(), all_contacts)
    if not assignment:
        return []

    contacts_by_id = {c.id: c for c in all_contacts}
    preresolved = {
        as_said: contacts_by_id[cid]
        for as_said, cid in pr.resolved_so_far.items()
        if cid in contacts_by_id
    }
    for as_said, cid in assignment.items():
        if cid in contacts_by_id:
            preresolved[as_said.lower()] = contacts_by_id[cid]
    # Session-wide memory of the user's picks — never re-ask the same name
    sess.confirmed_names.update(
        {as_said.lower(): cid for as_said, cid in assignment.items()}
    )
    primary = preresolved.get(pr.original_name.lower())

    parked_facts = pr.pending_shared_facts
    pending_update = pr.pending_update
    sess.pending_resolution = None  # clear before re-routing; re-parking recreates it
    touch_session(session_id)

    saved_texts: list[str] = []
    if pending_update and primary:
        await memory_engine.update_contact(primary.id, pending_update)
    for parked_fact in parked_facts:
        text = await memory_engine.store_shared_fact(
            parked_fact, session_id=session_id, preresolved=preresolved
        )
        if text:
            saved_texts.append(text)
    return saved_texts


async def _resolve_pending_with_late_replies(
    db: AsyncSession, memory_engine: MemoryEngine, session_id: str, extracted_at
) -> None:
    """
    Close the park-after-answer race: the disambiguation question is asked in
    the SAME turn (deterministic ambiguity scan), but the fact is only parked
    when this background extraction finishes — a fast reply ("jamil", 3s
    later) lands before the park and resolves nothing. Replay every user
    message that arrived while extraction was running against the freshly
    parked question (live regression: the Jamil gym fact was lost this way).
    """
    from sqlalchemy import select
    from app.memory.conversation_state import get_session

    if extracted_at is None or get_session(session_id).pending_resolution is None:
        return
    result = await db.execute(
        select(Message)
        .where(
            Message.session_id == session_id,
            Message.role == "user",
            Message.created_at > extracted_at,
        )
        .order_by(Message.created_at.asc())
    )
    for reply in result.scalars().all():
        saved = await apply_pending_resolution_reply(memory_engine, session_id, reply.content)
        if saved:
            logger.info(
                f"Late disambiguation resolved from reply '{reply.content[:40]}': "
                f"{len(saved)} fact(s) saved"
            )
        if get_session(session_id).pending_resolution is None:
            break


async def _run_extraction(
    user_message: str,
    assistant_message: str,
    session_id: str,
    provider: LLMProvider,
    qdrant,
    conversation_history: list[dict] | None = None,
    extracted_at=None,
) -> None:
    """Background task: creates its own DB session to run extraction pipeline."""
    from app.db.database import AsyncSessionLocal
    from app.memory.session_persistence import save_pending_state
    async with AsyncSessionLocal() as db:
        engine = MemoryEngine(db=db, qdrant=qdrant)
        await run_extraction_pipeline(
            user_message=user_message,
            assistant_message=assistant_message,
            conversation_history=conversation_history or [],
            session_id=session_id,
            db=db,
            engine=engine,
            provider=provider,
        )
        try:
            await _resolve_pending_with_late_replies(db, engine, session_id, extracted_at)
        except Exception as e:
            logger.warning(f"Late disambiguation resolution failed (non-critical): {e}")
        # Phase 3.5: extraction is where new questions PARK — snapshot them so
        # a restart before the user answers never loses the parked fact.
        await save_pending_state(db, session_id)


@router.post("", response_model=ChatResponse, summary="Non-streaming chat completion")
async def chat(
    request: ChatRequest,
    db: AsyncSession = Depends(get_db),
    provider: LLMProvider = Depends(get_llm_provider),
    qdrant=Depends(get_qdrant),
) -> ChatResponse:
    """Non-streaming chat completion with memory context."""
    session_id = request.session_id or str(uuid.uuid4())

    # Phase 3.5: resurrect a cold session's parked questions before use
    from app.memory.session_persistence import restore_pending_state
    await restore_pending_state(db, session_id)

    memory_engine = MemoryEngine(db=db, qdrant=qdrant)
    last_user_msg = next(
        (m for m in reversed(request.messages) if m.role == "user"), None
    )
    memory_context = ""
    pending_resolution = None
    if last_user_msg:
        try:
            from app.memory.conversation_state import get_session, touch_session, ActiveEntity
            import time
            bundle = await memory_engine.retrieve_context(last_user_msg.content, session_id=session_id)
            
            # Update ConversationSession deterministically.
            # Only replace active_entities when the current message resolves new entities;
            # otherwise retain existing pinned state.
            if bundle.resolved_entities:
                sess = get_session(session_id)
                active_entities = [
                    ActiveEntity(
                        id=e.id, type=e.type, name=e.name,
                        confidence=e.confidence, last_mentioned=time.time()
                    )
                    for e in bundle.resolved_entities
                ]
                sess.active_entities = active_entities
                # focus_entity: single entity when unambiguous; None in comparison mode
                sess.focus_entity = active_entities[0] if len(active_entities) == 1 else None
                touch_session(session_id)
                
            memory_context = await memory_engine.format_context(bundle)
            pending_resolution = bundle.pending_resolution
        except Exception as e:
            logger.warning(f"Memory context build failed (non-critical): {e}")

    affective_note = await _affective_note(db)
    screen_note = await _screen_note(db)
    background_note = await active_tasks_context(db, session_id)
    messages: list[LLMMessage] = [
        LLMMessage(role="system", content=_build_system_prompt(
            memory_context, pending_resolution, affective_note=affective_note,
            screen_note=screen_note, background_note=background_note,
        ))
    ]
    messages.extend(_provider_history(request.messages))

    if last_user_msg:
        await _persist_message(db, session_id, "user", last_user_msg.content)

    try:
        response = await provider.chat(messages)
    except Exception as e:
        logger.error(f"Chat error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    # Impersonation guard (same as the streaming path); nothing has been
    # delivered yet, so the fabricated part is cut BEFORE the marker.
    content = response.content
    match = _SYSTEM_VOICE_RE.search(content or "")
    if match:
        logger.warning(
            "Chat LLM impersonated a system message — response corrected "
            "(session {})", session_id,
        )
        content = content[: match.start()].rstrip() + _IMPERSONATION_CORRECTION

    await _persist_message(
        db, session_id, "assistant", content,
        model=response.model, tokens_used=response.tokens_used,
    )

    return ChatResponse(
        content=content,
        model=response.model,
        provider=response.provider,
        session_id=session_id,
        tokens_used=response.tokens_used,
    )


@router.get("/sessions/{session_id}/messages", summary="Get chat history for a session")
async def get_session_messages(
    session_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Retrieve all messages for a given session ID."""
    from sqlalchemy import select
    result = await db.execute(
        select(Message)
        .where(Message.session_id == session_id)
        .order_by(Message.created_at.asc())
    )
    messages = result.scalars().all()
    return [
        {
            "id": m.id,
            "role": m.role,
            "content": m.content,
            "model": m.model,
            "created_at": utc_iso(m.created_at),
        }
        for m in messages
    ]
