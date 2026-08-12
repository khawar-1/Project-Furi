"""
Furi OS — Chat Task Router (Phase 3, Part 6)

Decides whether a chat message is a TASK REQUEST ("delete my temp files")
or normal conversation, and routes task requests to the agent planner.
Detection is two-stage so normal chat pays ZERO extra cost:

1. Deterministic gate — fires only when the message contains BOTH an action
   verb AND a computer-domain signal (file/folder/path/command/...). If the
   gate does not fire there is no LLM call at all: `maybe_handle_task`
   returns None and the message flows into the untouched Phase 2 chat path.
2. LLM classification — one tiny temperature-0 call returning a routing label
   (TASK / EMAIL / CALENDAR / WEB / BROWSE / CHAT; BROWSE added Phase 14) that rejects
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
import asyncio
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

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
)
from app.agents.agent_registry import GENERAL, AgentSpec, agent_for_key, agent_for_label
from app.agents.summary import stream_completed_summary
from app.api.agent import _PARKABLE, _plan_response
from app.browser import publicsuffix
from app.browser.grounding import ground_origins
from app.core import routing_trace
from app.db.persist import persist_message_best_effort
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
    r"\bgoogle\b|\burls?\b|https?://|"
    # Browse / live-site domain (Phase 14). A named media/streaming site fires
    # the gate ALONE (the object-noun rule); the classifier makes the
    # BROWSE/CHAT call ("I saw it on youtube" is a false fire costing one temp-0
    # call — the recall-first trade-off). These are the sites a user says "play
    # X on ___" about — a small, stable vocabulary.
    r"\byoutube\b|\byou\s?tube\b|\bspotify\b|\bnetflix\b|\bvimeo\b|"
    r"\bsoundcloud\b|\btwitch\b|\bgithub\b|\bgitlab\b|"
    # NOTE: naming individual sites here is a dead end — the browser stack is
    # Skyvern-class ("act on ANY site, no per-site code", BROWSER_REFACTOR.md).
    # A general navigation-intent signal (`_is_browse_intent` below, reusing
    # grounding.ground_origins) fires for a site the user names WHETHER OR NOT it
    # is in any list; do not extend this vocabulary — extend that.
    # Sign in to / operate a web app the user names (Phase 14 BROWSE, the github
    # sign-in incident 2026-07-18: "sign in to github and open my oldest repo"
    # named no domain noun, missed the gate, fell to plain chat — which then
    # asked the user for their password). "sign in / log in / sign into / log
    # into" (incl. signin/login/sign-in) is an act-on-a-site intent that fires
    # the gate ALONE; the classifier makes the BROWSE/CHAT call. Rare in small
    # talk, so a false fire costs one temp-0 call — the recall-first trade-off.
    r"\bsign(?:ing|ed)?[\s-]?in(?:to)?\b|\blog(?:ging|ged)?[\s-]?in(?:to)?\b|"
    # Home & IoT domain (Feature 1). Only words that are UNAMBIGUOUSLY about a
    # controllable home device fire alone: "thermostat" and "smart <thing>" are
    # never said about anything else. The everyday home nouns people also use in
    # ordinary conversation ("the lights were beautiful", "he knocked on the
    # door") are WEAK below and need an action verb — the same split the media
    # nouns use, for the same measured reason.
    r"\bthermostats?\b|\bsmart[\s-]?(?:home|bulbs?|plugs?|lights?|locks?|switch(?:es)?)\b|"
    r"\bhome[\s-]?assistant\b|\bair[\s-]?con(?:ditioning|ditioner)?\b|"
    # Desktop control (Feature 2). Only words nobody uses about anything
    # else: "clipboard" and "screenshot" are never said in ordinary
    # conversation, so they fire alone. The everyday machine nouns
    # ("window", "app", "volume") are WEAK below — "a window of
    # opportunity", "there's an app for that" and "the sheer volume of
    # email" are all real sentences.
    r"\bclip[\s-]?board\b|\bscreen[\s-]?shots?\b|\bscreen[\s-]?grabs?\b|"
    r"\btask[\s-]?bar\b|\bstart[\s-]?menu\b|"
    # "mute"/"unmute" fire ALONE, unlike the other desktop verbs. MEASURED
    # 2026-08-04: "mute it" was the one miss in a 20-phrase recall set, because
    # its object is a pronoun and the weak tier needs a noun. English has no
    # everyday non-audio use of the word — "you're on mute" is still about
    # sound control — so the cost of firing alone is near zero.
    r"\bmutes?\b|\bunmutes?\b|"
    # "search" / "look it up" as verbs fire alone (live bug 2026-07-16,
    # round 2: "yes search and tell me when is the new seasopn of blackclover
    # comming out" — a go-ahead to the chat LLM's own "I can search the web,
    # just say the word" offer — named no web noun, exceeded the 8-word
    # follow-up cap, and fell to plain chat, which fabricated "I've started a
    # search…"). A user telling their assistant to SEARCH almost always means
    # a lookup; "I've been searching for a new job" is a false fire costing
    # one temp-0 call answering CHAT — the recall-first trade-off.
    r"\bsearch(?:es|ed|ing)?\b|\blook(?:ed|ing)?\s+(?:it|that|this|them)\s+up\b)"
)

# Weak signals — common in ordinary conversation (media nouns, URLs, "e.g.",
# decimals all brush against these) — still need an action verb to fire.
_WEAK_DOMAIN_RE = re.compile(
    r"(\bpictures\b|\bvideos?\b|\bmusic\b|\bsongs?\b|\btracks?\b|\bepisodes?\b|"
    r"\bmovies?\b|\btrailers?\b|\bpodcasts?\b|\bdrive\b|\bdisk\b|"
    r"\bprocess(?:es)?\b|"
    # Home & IoT (Feature 1) — the everyday nouns. "turn off the lights" fires
    # (verb + noun); "the lights were beautiful" and "he knocked on the door" do
    # not. "scene" sits here rather than strong because a scene in a film is far
    # commoner than a scene on a hub.
    r"\blights?\b|\blamps?\b|\bbulbs?\b|\bdoors?\b|\bblinds?\b|"
    r"\bcurtains?\b|\bshades?\b|\bgarage\b|\bheating\b|\bheater\b|"
    r"\bradiators?\b|\bscenes?\b|\bplugs?\b|\bsockets?\b|\bac\b|"
    # Desktop control (Feature 2) — the everyday machine nouns. Each needs
    # an action verb, so "close the chrome window" fires and "a window of
    # opportunity" does not.
    r"\bwindows?\b|\bapps?\b|\bapplications?\b|\bprograms?\b|"
    r"\bvolume\b|\bspeakers?\b|\bsound\b|"
    r"[/\\]|~[/\\]?|\.\w{1,4}\b)"
)

# The file vocabulary above names CONTAINERS — file, folder, directory,
# desktop, downloads. It never named the things kept INSIDE them, and that is
# what a user actually says: "find the pdf about cloud computing", "find my
# notes about the architecture review", "open the doc about onboarding".
#
# FOUND BY scripts/route_bench.py + scripts/plan_bench.py, 2026-08-03, and it
# was not a near-miss: "find the FILE about X" fired while "find the PDF about
# X" was CLOSED, so semantic_file_search — Phase 6 Parts 2/3, the entire file
# index, and plan rule 17's own example phrasing — was unreachable from chat
# unless the user happened to say the literal word file/folder/desktop. The
# 2026-07-10 lesson ("users invent VERBS endlessly, but a task NAMES ITS
# OBJECT") was applied to verbs and then never re-checked against the object
# vocabulary it rests on. MEASURED before/after on a 42-message corpus:
# recall 2/18 -> 18/18, with ONE new false fire in 24 conversational controls.
#
# ⚠️ WEAK, NOT STRONG, and that placement is measured rather than cautious.
# These words appear in ordinary autobiography far more than "terminal" or
# "directory" do: as strong nouns (firing alone) they fired on "my resume is
# finally done", "that presentation was painful" and "he sent me an invoice
# last week". Requiring an action verb drops all three and costs no recall,
# because a request for a document is imperative or interrogative by nature.
#
# Deliberately NOT derived from file_extract.INDEXABLE_EXTS, and no invariant
# test binds the two — because EVERY extension is already covered without one.
# `_WEAK_DOMAIN_RE` above ends in `\.\w{1,4}\b`, so a user who says the format
# WITH its dot (".rst", ".log", ".md") already reaches the classifier whatever
# it is. This list only has to carry the DOTLESS forms people actually speak,
# which is why bare `md` and `log` are absent: they are covered in dotted form
# and, as bare words, one means a doctor and the other is half of "log in".
_DOCUMENT_NOUN_RE = re.compile(
    r"(\bpdfs?\b|\bdocx?\b|\bdocs\b|\bdocuments?\b|\bwrite-?ups?\b|"
    r"\bspread\s?sheets?\b|\bxlsx?\b|\bcsvs?\b|\btsvs?\b|\btxt\b|"
    r"\bworkbooks?\b|\bpresentations?\b|\bslide\s?decks?\b|\bslides\b|"
    r"\bpptx?\b|\bnotes?\b|\breports?\b|\bresumes?\b|\bcvs?\b|"
    r"\binvoices?\b|\breceipts?\b|\bcontracts?\b|\bessays?\b|"
    r"\btranscripts?\b|\bmarkdown\b|\breadme\b|\bebooks?\b|\bepub\b)"
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
    r"launching|install|installs|installed|installing|"
    # Media / live-site verbs (Phase 14) — "play"/"watch"/"listen"/"stream"
    # only fire with a weak media noun ("play the song", "watch the trailer");
    # a named site ("play X on youtube") fires the strong gate above on its own.
    r"play|plays|played|playing|watch|watches|watched|watching|"
    r"listen|listens|listened|listening|stream|streams|streamed|streaming|"
    # Home-control verbs (Feature 1). "turn" was absent from this list
    # entirely, so "turn off the lights" could never fire the weak gate no
    # matter which nouns it named. Each only fires WITH a weak noun, so "turn
    # left" and "I set the table" stay closed.
    r"turn|turns|turned|turning|switch|switches|switched|switching|"
    r"dim|dims|dimmed|dimming|brighten|brightens|brightened|brightening|"
    r"lock|locks|locked|locking|unlock|unlocks|unlocked|unlocking|"
    r"shut|shuts|shutting|set|sets|setting|"
    r"close|closes|closed|closing|activate|activates|activated|activating|"
    # Desktop verbs (Feature 2). "mute"/"pause"/"skip" were absent, so
    # "mute the sound" and "pause the music" could not fire whatever nouns
    # they named. Each still needs a weak noun beside it.
    r"mute|mutes|muted|muting|unmute|unmutes|unmuted|unmuting|"
    r"pause|pauses|paused|pausing|"
    # ⚠️ `resume` IS DELIBERATELY ABSENT, and it was here for one run. It is a
    # DOCUMENT NOUN as well as a media verb, and `_DOCUMENT_NOUN_RE` carries it
    # — so the word matches itself, the weak tier fires on "my resume is finally
    # done", and the very case the comment above that list cites as the reason
    # those nouns are WEAK rather than STRONG comes straight back. A CV is
    # discussed far more often than playback is resumed, and `play`/`pause`
    # already cover the media case.
    r"skip|skips|skipped|skipping|focus|focuses|focused|focusing|"
    r"minimi[sz]e|minimi[sz]es|minimi[sz]ed|minimi[sz]ing|"
    r"maximi[sz]e|maximi[sz]es|maximi[sz]ed|maximi[sz]ing|"
    r"paste|pastes|pasted|pasting|bring|brings|bringing|start|starts|starting|"
    r"search|searches|searched|"
    r"searching|find|finds|found|finding|locate|locates|located|locating|list|lists|"
    r"listing|read|reads|reading|open|opens|opened|opening|show|shows|showing|"
    r"check|checks|checking|look|"
    r"tell|tells|telling|count|counts|counted|counting|"
    # Past tenses of verbs already listed above, which this list promised to
    # carry ("common inflections listed explicitly") and did not: `save|saves|
    # saving` had no `saved`, so "where is that pdf I SAVED yesterday" fired
    # nothing. They matter in a relative clause, which is how people describe
    # a document they are looking for — "the report I WROTE", "the doc he
    # SHOWED me". MEASURED 2026-08-03: recall 4/5 -> 5/5 on that shape, one
    # extra temperature-0 call in 8 conversational controls.
    r"saved|wrote|written|showed|shown|checked|looked|listed|told|"
    # Email verbs (Phase 5) — "send"/"reply"/"forward"/"draft" were absent, so
    # a weak-noun email request ("send that mail", "forward it") never fired.
    r"send|sends|sending|sent|reply|replies|replied|replying|"
    r"forward|forwards|forwarded|forwarding|draft|drafts|drafted|drafting|"
    r"del|rm|rmdir|mkdir|mv|cp|trash|trashes|trashed|trashing)\b"
)


# Past conversations are STORED CONTENT, and asking about them names no file
# noun at all. Phase 6 Part 4 embeds every message into Qdrant and
# semantic_file_search searches it, but "what did we discuss about the
# database migration" fired no tier: is_external_question refuses it (its
# subject is "we", correctly — it is not an external-FACT question) and no
# other tier has a word for it. So the one feature built to answer it could
# not be reached, and the chat model — which sees a 30-message window and no
# further — answered from that window or not at all.
#
# The object noun here is the conversation itself, named either by a
# first/second-person recall verb ("what did WE DISCUSS", "when did I
# MENTION") or literally ("the CONVERSATION WHERE we talked about X").
_STORED_RECALL_RE = re.compile(
    r"\b(?:we|i|u|you)\s+(?:\w+\s+){0,2}?"
    r"(?:discuss|discussed|talk|talked|mention|mentioned|say|said|told|"
    r"agree|agreed|decide|decided)\b"
    r"|\b(?:conversations?|chats?)\s+(?:where|about|in\s+which)\b"
)

# …EXCEPT when the message addresses Furi's MEMORY directly. "do you
# remember what i told you about jamil" is stored-content recall by every
# test above, and routing it would be a worse answer rather than merely a
# wasted call: `MemoryEngine.retrieve_context()` runs on EVERY chat turn and
# injects the facts, contacts and episodes bundle into the prompt, so the chat
# path already holds what that question wants and replies conversationally,
# where the planner would spend a task on it.
#
# There is no deterministic line between "recall the fact you know about
# Jamil" and "search our conversations about the migration" — both are stored
# content, and trying to tell a PERSON from a TOPIC by keyword is the
# judgement this file has measured at zero three times. Naming the faculty is
# a different test entirely: the user said the word "remember", so they are
# asking the thing that already has the answer loaded.
#
# This is the exclusion `test_gate_stays_closed_for_self_referential_questions`
# pins, and it was written BEFORE this tier existed — the tier's first draft
# broke it, which is how the distinction got found. MEASURED: the exclusion
# drops 4 of 4 memory-faculty phrasings.
#
# KNOWN COST, measured and accepted rather than discovered later: it also
# closes "do you remember what WE DISCUSSED about the migration", which IS a
# conversation search and which chat will answer poorly (a topic discussion is
# not an extracted memory fact). Both phrasings wear the same clothes and no
# deterministic rule separates them. Kept closed because the two failure modes
# are not equal — a memory question answered as a background TASK is worse
# than a search phrasing the user can restate ("what did we discuss about the
# migration" routes correctly) — and because the classifier is unmeasured on
# memory questions, so removing this would be an unmeasured change to a
# documented boundary. Revisit WITH A MEASUREMENT if it bites.
_MEMORY_ADDRESS_RE = re.compile(
    r"\b(?:do|did|does)\s+(?:you|u)\s+(?:remember|recall|know)\b"
    r"|\bremember\s+(?:when|what|that|how)\b"
    r"|\b(?:you|u)\s+(?:remember|recall)\b"
)

# A recall phrase alone is not a request — "we discussed this already", "i say
# we ship it" and "i talked to my brother yesterday" all contain one and are
# plain conversation. What separates the real asks is that every one of them
# is imperative or interrogative, so the tier needs an action verb or a bare
# wh-word alongside. MEASURED: this single condition drops all three of those
# false fires and costs ZERO recall across the 18-message ask corpus.
#
# This is a REQUEST-SHAPE test over seven closed-class words, NOT the
# intent-keyword shape falsified three times in this file's history: it never
# tries to judge what the message is about, only whether it is asking.
_WH_WORD_RE = re.compile(r"\b(?:what|when|where|which|who|whose|why|how)\b")


# Questions about Furi's OWN actions ("what have you done today?", "did you
# delete anything?") often name no domain noun at all — the object is Furi's
# action record, not a file. With recall_actions available they are TASK-class,
# so a second-person action phrase is a strong signal in its own right (live
# bug 2026-07-13: "what was the name of folder that u created?" reached the
# classifier only because it happened to say "folder"; the chat LLM then
# asserted a wrong folder from memory and denied the real one). Two tiers,
# like the reminder triggers: a phrase that EMBEDS the action verb ("you
# created", "what did you do") fires alone; a bare auxiliary ("did you …")
# is everyday conversation ("did you know…?") and needs an action verb too.
_OWN_ACTION_RE = re.compile(
    r"\b(?:you|u)\s+(?:created?|deleted?|made|moved|renamed|removed|ran|sent|did)\b|"
    r"\bwhat\s+(?:did|have)\s+(?:you|u)\s+(?:do|done)\b"
)
_OWN_ACTION_AUX_RE = re.compile(r"\b(?:did|have|had)\s+(?:you|u)\b")


# ------------------------------------------------------- external questions
#
# A QUESTION is the one message shape this gate cannot pre-judge, and three
# rounds of trying proved it the hard way. 2026-07-16 added a time-sensitive
# MARKER list ("latest", "release date", "new season"); round 2 the same day
# added typo tolerance to that list; 2026-07-17 added an information-request
# LEAD-IN list ("tell me about", "who is"). Every round, the next phrasing the
# user typed missed again.
#
# MEASURED 2026-07-17 against the user's own transcripts, which is what ended
# the argument: of 8 ordinary external-fact questions the gate blocked 7 —
# "which teams qualified for fifa finals 2026" (no marker word), "is bitcoin
# up today" (no marker word), "tell me who won the match last night" (marker
# present, but the shape rule allowed only ONE filler word before the question
# word and "tell me" is two). Handed those same 8, the classifier labelled
# 8/8 WEB — and 9/9 control questions ("explain recursion", "what do you think
# of vector databases", "how are you today") CHAT.
#
# So the lists were not merely incomplete. They were the ONLY thing standing
# between the user and a correct answer, and they INVERTED this gate's own
# doctrine (fire wide, let the classifier prune — a strong domain noun already
# fires alone, in any wording). "Does this need current information?" is not
# answerable from keywords; it is the exact question the classifier exists to
# answer. The rule is therefore now: A QUESTION REACHES THE CLASSIFIER UNLESS
# IT IS ABOUT THE USER OR FURI THEMSELVES.
#
# Cost, accepted deliberately: one temperature-0 call on impersonal
# conversational questions ("what is a monad") that the classifier answers
# CHAT. Self-referential small talk ("how are you", "why is my script slow")
# stays free, and an imperative that is not a question ("explain recursion")
# never enters this tier at all.
#
# KNOWN LIMIT, measured not assumed: a TYPO'd question word hides the question
# ("whihc teams have qualified for fifa finals 2026" — the user's own words).
# Fuzzy-matching the first word against the question set was tried and
# REJECTED on the numbers: rapidfuzz scores "whihc"→which at 80, but "here"→
# where at 89 and "there"→where at 80, so no threshold separates a typo from
# an ordinary opening word — it would fire on half of all English sentences.
# This class is left to the chat.py dead-end backstop, which re-routes on the
# model's own admission and therefore needs no keyword to recognize.

# Leading noise a question may hide behind: greetings, vocatives, fillers.
_QUESTION_GREETING_RE = re.compile(
    r"^\W*(?:(?:hey|hi|hello|yo|ok|okay|so|well|now|yes|yeah|yep|sure|please|"
    r"and|also|but|um+|uh+|furi|jarvis)\b[\s,!.]*)*"
)

# An explicit information-request frame. Two jobs, and the second is the
# subtle one: matching it fires the tier (an imperative request for
# information is not question-SHAPED — "tell me about black clover" has no
# question word), and stripping it removes the pronouns that belong to the
# REQUEST rather than to its subject. That strip is what lets "i want to know
# the gold price" (about the world) fire while "why is my script slow" (about
# the user) stays free — without it the self-reference test below would read
# the "i" in the frame and refuse the whole class.
_QUESTION_REQUEST_RE = re.compile(
    r"(?:"
    r"tell\s+me|show\s+me|let\s+me\s+know|find\s+out|look\s+up|"
    r"i\s+(?:just\s+)?(?:want|need|wanna)\s+to\s+know|"
    r"i(?:'d|\s+would)\s+like\s+to\s+know|"
    r"what\s+do\s+(?:you|u)\s+know\s+about|"
    r"do\s+(?:you|u)\s+know|"
    r"(?:have|has)\s+(?:you|u)\s+heard\s+(?:of|about)|"
    r"give\s+me\s+(?:a\s+|an\s+)?(?:rundown|summary|overview|briefing|info|"
    r"information|details)\s+(?:on|about|of)|"
    r"any\s+idea"
    r")\b[\s,!.:]*"
)

_QUESTION_WORD_RE = re.compile(
    r"(?:when|what|what's|whats|who|who's|whos|where|which|why|how|"
    r"is|are|was|were|did|does|do|has|have|had|will|can|could|should|any)\b"
)

# A question about the user, about Furi, or about the two of them is CHAT by
# construction — Furi's own memory and context answer it, the web cannot.
# Applied to the SUBJECT (after the request frame is stripped), never to the
# raw message.
_SELF_REFERENTIAL_RE = re.compile(
    r"\b(?:you|your|yours|you're|u|ur|i|i'm|me|my|mine|myself|we|we're|our|"
    r"ours|us|let's)\b"
)

# A question whose subject is a bare POINTER ("who is this?", "what is that?",
# "who are they?") refers to something in the conversation, not out in the
# world: Furi answers it from context and the web could not help. Matched
# only immediately after the question word and its copula — so "what's the
# latest on that iphone rumour", where "that" is a determiner rather than a
# pointer, still reaches the classifier.
#
# ⚠️ A DEMONSTRATIVE IS NOT ALWAYS A POINTER, and this used to treat it as one.
# The comment above already claimed a determiner "still reaches the classifier",
# and that was true only by accident — it holds when the demonstrative is not
# adjacent to the question word ("what's the latest on that iphone rumour"), and
# fails the moment it is: "where is that spreadsheet with the budget" and "what
# is that movie everyone is talking about" both matched and were refused, one a
# file question and one a plain web question. MEASURED 2026-08-03 on an 18-case
# corpus: 5 wrong before, 0 after.
#
# The split is grammatical, not a judgement: it/they/them/he/she/him/her/there
# can never determine a noun, so they are always pointers. this/that/these/those
# can, and do whenever a content word follows — so they count as pointers only
# when the subject ENDS there, or continues with a closed-class tail that no
# noun could head ("what is this ABOUT", "what was that AGAIN").
_DEICTIC_SUBJECT_RE = re.compile(
    r"^(?:who|whos|who's|what|whats|what's|which|where)"
    r"(?:'s|\s+(?:is|are|was|were))?\s+"
    r"(?:"
    r"(?:it|they|them|he|she|him|her|there)\b"
    r"|(?:this|that|these|those)"
    r"(?=\W*$|\s+(?:about|for|then|again|anyway|exactly|really|though|all|"
    r"even|actually|supposed|mean|means|meant)\b)"
    r")"
)

# A one- or two-word question ("really?", "why?", "how come?") is a
# conversational reaction, never an external-fact lookup — an external
# question always names its subject. Cheap guard against paying a classifier
# call for a shrug.
_QUESTION_MIN_WORDS = 3


def _question_subject(text: str) -> Optional[str]:
    """The thing a question is ABOUT, with greeting noise and any
    information-request frame stripped — or None when the message is not a
    question or information request at all."""
    t = text.strip().lower()
    if len(t.split()) < _QUESTION_MIN_WORDS:
        return None
    greeting = _QUESTION_GREETING_RE.match(t)
    rest = t[greeting.end():] if greeting else t
    frame = _QUESTION_REQUEST_RE.match(rest)
    if frame:
        return rest[frame.end():]
    if _QUESTION_WORD_RE.match(rest) or t.rstrip().endswith("?"):
        return rest
    return None


def is_external_question(text: str) -> bool:
    """Deterministic: is this a question the classifier should judge? True for
    any question or information request whose subject is neither the user, nor
    Furi, nor a pointer back into the conversation. The classifier makes the
    real WEB/CHAT call."""
    subject = _question_subject(text)
    if subject is None:
        return False
    if _SELF_REFERENTIAL_RE.search(subject):
        return False
    return not _DEICTIC_SUBJECT_RE.match(subject.lstrip())


def _is_browse_intent(text: str) -> bool:
    """Does the user name a website to go to / act on? GENERAL, not a site list —
    the browser stack is Skyvern-class (act on ANY site, no per-site code,
    BROWSER_REFACTOR.md). `ground_origins` is the canonical "the user's OWN words
    name a web origin" function the grounding layer already uses: a literal domain
    ("open nytimes.com"), or a bare name directed at a navigation verb ("open
    linkedin", "go to workday") — WHETHER OR NOT the site is in any map. Reusing it
    means routing and grounding agree by construction: if the words ground a site,
    the gate sends the turn to the planner, and the planner's grounding accepts
    that same site. A false fire (a bare name that is not really a site) costs one
    temp-0 classifier call answering CHAT — the recall-first trade.

    Live bug 2026-07-21: "open linkedin and go to the networks tab and open profile
    of anas mubashar" named no strong noun and no "sign in" verb, so the gate
    missed and it fell to plain chat — which offered a "magic word" rephrase and,
    on the retry, fabricated "that instruction has been passed to the system."
    Adding `linkedin` to the strong-noun list was the WRONG fix (per-site code the
    refactor forbids; it would miss the next site named); this general signal is
    the right one. NB an EARLIER "go to linkedin and SEARCH anas…" only worked by
    the accident of containing "search" (a strong verb), not by any site logic."""
    try:
        return bool(ground_origins(text))
    except Exception:  # grounding is best-effort; a gate miss is never a crash
        return False


# A launch instruction, anchored to the START of the message. Anchored because
# "open" mid-sentence is usually about something else entirely ("the file is
# open", "keep an open mind"), while a launch is what a person opens a sentence
# with. `run` is deliberately ABSENT: "run the build script" is a terminal task,
# and letting it reach the registry lookup would spend a Start Menu walk on
# every shell request.
_LAUNCH_VERB_RE = re.compile(
    r"^\s*(?:please\s+|can\s+you\s+|could\s+you\s+|hey\s+(?:furi|jarvis)[,\s]+"
    r"|(?:furi|jarvis)[,\s]+)*"
    r"(?:open|launch|start|fire\s+up|pull\s+up|bring\s+up)\s+(?:the\s+|my\s+)?(.+)$",
    re.IGNORECASE,
)


def _is_desktop_intent(text: str) -> bool:
    """Does the user name an INSTALLED APPLICATION to open?

    The desktop twin of `_is_browse_intent`, and it exists for the identical
    reason: the headline phrasing for this feature — "open spotify" — names no
    domain noun at all, so no vocabulary the gate could carry would fire on it.
    A per-app keyword list would be the per-site list the browser refactor
    forbids, and would miss the next app installed.

    So the registry answers instead: the message must OPEN with a launch verb,
    and what follows must resolve against the Start Menu (`resolve_app`). That
    is the same registry `launch_app` itself uses, so the gate and the tool
    agree by construction — a name the gate accepts is a name the tool can
    launch.

    ⚠️ THE PREFILTER IS A COST GUARD, NOT A HEURISTIC. `gate_tier` runs on every
    chat turn, and `discover_apps()` walks the Start Menu (~150ms cold, cached
    for 5 minutes). The anchored verb test is a cheap regex that keeps that walk
    off the ordinary conversational path; only a message already shaped like a
    launch ever pays for it.

    A false fire costs one temp-0 classifier call answering CHAT — the
    recall-first trade. Best-effort: a registry read that fails is a gate miss,
    never a crash."""
    match = _LAUNCH_VERB_RE.match(text or "")
    if not match:
        return False
    target = match.group(1).strip().rstrip("?.!,")
    # A whole clause is not an app name ("open the file I saved yesterday").
    if not target or len(target.split()) > 4:
        return False
    try:
        from app.core.desktop import resolve_app

        found = resolve_app(target)
        # An AMBIGUOUS name counts too: "open code" matching two editors is
        # still unmistakably a launch request, and the plan will ask which.
        return found.entry is not None or bool(found.candidates)
    except Exception:  # noqa: BLE001 — the gate never crashes a chat turn
        return False


# ---------------------------------------------------------- bare navigation
#
# "open junaidjamshed.com" — a message whose ENTIRE content is "take me to this
# site". Routed to BROWSE in CODE, with NO classifier call at all.
#
# ⚠️ THIS IS NOT A RECALL FIX — the gate already fired on it. It is a fix for a
# COIN FLIP. Live 2026-08-01 the user asked "open junaidjamshed.com" and got
# "Understood — opening junaidjamshed.com now, sir. It's with the browser agent
# in the background" from the plain CHAT path, which can open nothing: the
# classifier had answered CHAT. MEASURED on that exact message and its real
# conversation: CHAT, BROWSE, CHAT. On a cleaned-up conversation: BROWSE,
# BROWSE, CHAT, CHAT, CHAT. With no conversation at all: WEB, WEB, WEB. Three
# different labels for one unambiguous instruction — deepseek's temp-0 is not
# deterministic, and the prompt has no line for a bare navigation instruction
# (its WEB and BROWSE definitions both plausibly cover "open <site>").
#
# So the model is not asked. This is the placeholder_resolver principle —
# nothing to DECIDE, only to DO: the user named a site, in their own words, and
# asked to be taken to it. The whole classifier call (6.5s live) is skipped.
#
# The predicate is the router's twin of browser_loop._names_only_the_destination,
# the same structural question one layer up: does the message name anything the
# site is NOT? "open youtube and play lofi" does, and keeps today's path.
#
# ⚠️ A DOTTED DOMAIN IS REQUIRED, and that bound is load-bearing. ground_origins
# also grounds BARE names ("open indeed"), which are indistinguishable from a
# local application — "open notepad" grounds `notepad`, and routing that to a
# browser would try to navigate to a host that does not exist. A bare name that
# is not in grounding's known-sites map falls through to the classifier, i.e.
# exactly today's behaviour: this rule only ever REPLACES a coin flip with a
# certainty, never widens what reaches the browser.
_NAV_ONLY_PREFIX_RE = re.compile(
    r"^(?:(?:hey|ok|okay|yo)\s+)?(?:(?:furi|jarvis)\b[\s,]*)?"
    r"(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+|pls\s+|just\s+)*"
    r"(?:go(?:ing)?\s+to|goto|navigate\s+to|head\s+(?:over\s+)?to|take\s+me\s+to|"
    r"bring\s+up|pull\s+up|visit|browse\s+to|launch|load|open\s+up|open)\s+",
    re.IGNORECASE,
)

# Words that can trail a bare navigation instruction without adding an errand
# ("open youtube please", "go to amazon.com in the browser", "open the
# junaidjamshed website"). Stripped before the residue is compared.
_NAV_ONLY_TRAILER_RE = re.compile(
    r"\b(?:please|pls|for\s+me|sir|now|quickly|thanks|thank\s+you|"
    r"in\s+(?:the\s+)?browser|in\s+chrome|on\s+(?:the\s+)?browser|"
    r"web\s?site|web\s?page|website|webpage|homepage|home\s+page|"
    r"the|a|an|and|dot|site|page)\b",
    re.IGNORECASE,
)

# Pieces of a written-out address that are part of the destination, not an
# errand added to it.
_NAV_ADDRESS_NOISE = frozenset({"www", "http", "https"})

# ⚠️ "Contains a dot" is NOT enough: `open report.txt` grounds `report.txt`, whose
# residue names only itself, and routing that to a browser would try to navigate
# to a host that does not exist. A domain and a filename are the same shape; only
# the suffix tells them apart, and publicsuffix.py deliberately bundles multi-label
# suffixes only (`public_suffix("report.txt")` is "txt").
#
# So the final label must be a TLD we know. The list lives in publicsuffix.py —
# the module that already owns "what is a TLD" — because the site-question gate
# needs the identical fact and two private copies would drift (2026-08-02). It is
# partial ON PURPOSE and fails CLOSED: a TLD missing there means the message goes
# to the classifier, i.e. exactly today's behaviour. It grants no capability,
# relaxes no guard, and decides one thing only: whether an LLM call is worth
# making. That is why a literal list is acceptable here and is not the
# intent-keyword shape this router has measured at zero three times.
_NAV_TLDS = publicsuffix.KNOWN_TLDS


def _nav_tokens(text: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", (text or "").lower()) if w}


def _is_bare_navigation(text: str) -> bool:
    """Is this message ONLY "take me to <site>"? Requires a navigation verb at
    the very start, a site the user's own words ground to a real DOTTED domain,
    and a residue that names nothing the site is not."""
    t = (text or "").strip()
    if not t:
        return False
    m = _NAV_ONLY_PREFIX_RE.match(t)
    if not m:
        return False
    try:
        origins = ground_origins(t)
    except Exception:  # grounding is best-effort; a miss is never a crash
        return False
    dotted = [o for o in origins if o.rsplit(".", 1)[-1] in _NAV_TLDS and "." in o]
    if not dotted:
        return False
    destination = set()
    for origin in dotted:
        destination |= _nav_tokens(origin)
    residue = _NAV_ONLY_TRAILER_RE.sub(" ", t[m.end():])
    tokens = _nav_tokens(residue) - _NAV_ADDRESS_NOISE
    return bool(tokens) and tokens <= destination


def gate_tier(text: str) -> str:
    """WHICH tier of the deterministic pre-filter fires, or "" for none.

    Same logic and same order as the bool `looks_like_task` below, which is now
    a thin wrapper — one implementation, so the audited reason can never drift
    from the decision it explains (the registry.mutates lesson: a second copy of
    a rule is a hole). The tier name is what the routing audit trail records and
    what route_bench.py scores gate recall on, and it is genuinely diagnostic:
    "the strong-noun tier fired" and "the external-question tier fired" fail in
    completely different ways.
    """
    t = text.lower()
    if _STRONG_DOMAIN_RE.search(t):
        return "strong_domain"
    # Own-action questions are checked BEFORE the question tier and must stay
    # that way: "what did you do today" is second-person, so the question
    # tier's self-reference test would refuse it — but it is a real TASK,
    # answered from the audit log by recall_actions.
    if _OWN_ACTION_RE.search(t):
        return "own_action"
    if _OWN_ACTION_AUX_RE.search(t) and _ACTION_VERB_RE.search(t):
        return "own_action_aux"
    if is_external_question(text):
        return "external_question"
    # An installed application to open — general, no per-app list.
    #
    # ⚠️ BEFORE browse_intent, and MEASURED rather than reasoned. "open" is a
    # nav cue in `ground_origins`, which grounds ANY bare name after one — so
    # with the obvious ordering (browse first) `open photoshop`, `open slack`,
    # `open calculator` and `open notepad` ALL audited as "the user named a
    # website", and only `launch`/`start`/`fire up` ever reached this tier. That
    # is 5 of 6 installed-app phrasings mislabelled, and "open X" is the single
    # commonest way anyone asks for this feature.
    #
    # Recall is IDENTICAL either way — both tiers return truthy and the
    # classifier still decides DESKTOP vs BROWSE — so the only thing at stake is
    # the audited reason, which is exactly what the routing trail exists to get
    # right (the 2026-08-03 round records the same mislabelling for
    # `_NAV_STOPWORDS` as an open defect). Discriminating on "is this actually
    # installed?" is a fact about the machine rather than a guess.
    #
    # A name that is BOTH an app and a site ("slack", "discord") audits as
    # desktop when it is installed — which is what the user most likely meant by
    # opening it — and a name that is only a site is not in the registry, so it
    # falls straight through.
    if _is_desktop_intent(text):
        return "desktop_intent"
    # A named website to navigate to / operate — general, no per-site list.
    if _is_browse_intent(text):
        return "browse_intent"
    asking = _ACTION_VERB_RE.search(t)
    if (
        (asking or _WH_WORD_RE.search(t))
        and _STORED_RECALL_RE.search(t)
        and not _MEMORY_ADDRESS_RE.search(t)
    ):
        # Asking about something said in a past conversation (Phase 6 Part 4).
        # Its own tier rather than a line inside weak_verb_domain: gate_tier is
        # the audited reason AND what route_bench scores recall on, so a
        # vocabulary whose cost has never been measured needs its own name —
        # otherwise the day it turns out to be expensive, nothing can tell it
        # apart from the weak tier it was hiding in.
        return "stored_recall"
    if asking and (_WEAK_DOMAIN_RE.search(t) or _DOCUMENT_NOUN_RE.search(t)):
        return "weak_verb_domain"
    return ""


def looks_like_task(text: str) -> bool:
    """Deterministic pre-filter, tuned for RECALL: a strong computer-domain
    noun fires alone (any verb, any phrasing); an external question fires
    alone; weak signals — media nouns, paths, and the DOCUMENT vocabulary —
    need an action verb, as does a question about a past conversation.
    Deliberately over-inclusive: the LLM confirmation prunes it."""
    return bool(gate_tier(text))


# A short follow-up steering an action under discussion names NO object of its
# own ("send it", "delete them", "run it now") — the gate above can never fire
# on it, so it fell to plain chat, whose LLM cannot act but fabricated that it
# had ("I've started working on that in the background", live bug 2026-07-13,
# caught by _SYSTEM_VOICE_RE). The object lives in the CONVERSATION, so the
# recall-first rule extends there: a short message with an action verb, in a
# conversation carrying a strong domain signal, reaches the classifier — which
# judges it WITH that conversation (the round-9 context rule). A false fire
# costs one temp-0 call; the word cap keeps this to genuine follow-ups (a real
# new task names its object and fires the normal gate on its own words).
_FOLLOWUP_MAX_WORDS = 8


def is_action_followup(goal: str, conversation: str) -> bool:
    """Deterministic: does this short message look like it steers a computer
    action the conversation was just discussing?"""
    if not conversation:
        return False
    words = goal.split()
    if not words or len(words) > _FOLLOWUP_MAX_WORDS:
        return False
    if not _ACTION_VERB_RE.search(goal.lower()):
        return False
    return bool(_STRONG_DOMAIN_RE.search(conversation.lower()))


# A live agent browser window is the ground truth that the user is mid-session
# on a site. After Furi opens a page, a short next message that steers the
# browser ("message him 'hi'", "click the first result", "scroll down") names
# no site and no strong noun, so neither looks_like_task nor is_action_followup
# (which keys on a STRONG-domain conversation noun, and a site NAME like
# "linkedin" is not one) can fire on it. Live bug 2026-07-21: after Furi
# opened Anas's LinkedIn profile, "message him 'hi'" fell to plain chat, which
# offered a magic-word rephrase instead of continuing from the open profile.
# The open window lets the recall-first rule reach the classifier without a
# per-site list — the browse tool then REUSES that window and continues from the
# page it is already on. A false fire costs one temp-0 call answering CHAT.
_BROWSE_FOLLOWUP_VERB_RE = re.compile(
    r"\b(message|messages|messaging|messaged|text|texts|texting|texted|dm|dms|"
    r"post|posts|posting|posted|comment|comments|commented|reply|replies|replied|"
    r"like|likes|follow|follows|connect|connects|"
    r"click|clicks|clicking|clicked|scroll|scrolls|scrolling|scrolled|"
    r"type|types|typing|typed|write|writes|writing|fill|fills|filling|"
    r"send|sends|sending|open|opens|opening|search|searches|searching|go|goes)\b",
    re.I,
)


def _browse_window_active() -> bool:
    """True when at least one agent browser TAB is open (the user is mid-session
    on a site). Cheap and lock-free; import-light (window pulls no Playwright —
    it imports session lazily, inside its functions); best-effort — a probe
    failure is never a crash.

    Reads the tab registry rather than the old single `browse` held slot, which
    2026-08-01 removed: agent tabs live in the shared window now, and 'is a
    browser window open' is 'is any tab open'."""
    try:
        from app.browser import window as browser_window

        return browser_window.tab_count() > 0
    except Exception:
        return False


def is_browse_followup(goal: str) -> bool:
    """Deterministic: a short message steering an OPEN agent browser window. The
    live window is the domain signal, so — unlike is_action_followup — this needs
    no conversation keyword, only a browser-ish action verb in a short message.
    The classifier then judges it BROWSE (or CHAT) with the conversation.
    General, no per-site code."""
    words = goal.split()
    if not words or len(words) > _FOLLOWUP_MAX_WORDS:
        return False
    if not _BROWSE_FOLLOWUP_VERB_RE.search(goal):
        return False
    return _browse_window_active()


# ======================================================== LLM confirmation

_CLASSIFY_PROMPT = """You route messages for Furi OS, a personal AI that can act on the user's computer and accounts with exactly these tool groups:
- FILES/SYSTEM: search/read/list files and folders, OPEN A FOLDER in a file-explorer window on screen (or show the user where a file lives), create/move/rename/delete files, run terminal commands and scripts, recall Furi's OWN past actions from its audit log (what it created, deleted, moved, sent, or ran), find a saved document by its CONTENT or topic rather than its name, and search the user's OWN past conversations with Furi by what was discussed in them.
- EMAIL: search and read Gmail; draft, send, or reply to email.
- CALENDAR: list/find Google Calendar events; create, update, or delete events.
- WEB: search the web and open/read a web page to look up online information.
- HOME: read and control the devices in the user's home through their Home Assistant hub — lights, switches, fans, locks, covers/blinds, thermostats, media players, and the scenes they have defined.
- DESKTOP: see and control THIS computer — list, focus or close open windows, open an installed application, set the system volume or mute it, send play/pause/next to whatever is playing, take a screenshot, read or replace the clipboard.
- BROWSE: drive a real web browser to ACT on a live site the user names, or stop something it is already playing — play or watch a video (YouTube and the like), sign in to a site and navigate it, open something in a web app (a repo on GitHub, a page in an account), or fill in and submit a web form (e.g. apply to jobs).

Reply with EXACTLY one word:
TASK — asks Furi to perform a FILES/SYSTEM action now, INCLUDING opening a folder on screen ("open my downloads folder", "show me the phase3test folder", "where does that file live"), OR asks what Furi ITSELF did on the machine (the folder/file it created, what it deleted, what it has done today), OR asks Furi to FIND something it has stored: a document by what it is about, or what was said in an earlier conversation.
EMAIL — asks Furi to search, read, draft, send, or reply to email now.
CALENDAR — asks Furi to look at or change calendar events now.
WEB — asks Furi to search the web or open/read a web page now, OR asks a factual question better answered from the live internet than from stale built-in knowledge. This covers two cases: (a) anything CURRENT or time-sensitive (news, release dates, upcoming seasons or products, prices, scores, weather), and (b) a factual question about a SPECIFIC real-world entity — a person, company, product, place, organization, or a creative work such as a show, anime, movie, game, or book ("what do you know about Black Clover", "who is the CEO of X", "tell me about the Framework laptop"). Furi looks these up rather than guessing, promising, or reciting possibly-outdated training data.
DESKTOP — asks Furi to look at or change something on the COMPUTER IN FRONT OF THEM right now: open an app ("open spotify"), switch to or close a window, turn the volume up/down or mute it, pause/skip what is playing, take a screenshot, or read/replace the clipboard. This is the user's machine, not their house and not a website.

HOME — asks Furi to look at or change something in the user's HOME right now: turn lights/switches/fans on or off, dim or colour a light, lock or unlock a door, open or close blinds/curtains/a garage, set the thermostat or heating/AC, or run a scene ("goodnight", "movie night"). This is the user's physical home, not their computer.
BROWSE — asks Furi to DO something on a live website by driving a browser: play or watch a video ("play jane by the long faces on youtube", "watch the new trailer on youtube", "open youtube and play some lofi"); sign in to a site and then navigate or open something in it ("sign in to github and open my oldest repo", "log into my account and download the invoice"); operate an interactive web app; or fill in and submit a web form ("apply to the first 3 python jobs on weworkremotely"). This is ACTING on a live site — distinct from WEB, which only LOOKS UP information. Furi never asks the user for a password: if a site needs signing in, it opens the sign-in page for the user and continues after — so "sign in to X and ..." is BROWSE, never a request for credentials.
CHAT — anything else: casual conversation; OPINION, reasoning, or general/timeless concepts Furi can reason about ("what do you think of vector databases", "explain recursion", "how does TCP work"); help writing or debugging code; questions about the user's own life or about Furi itself; sharing information about their life; talking ABOUT the user's own past or hypothetical actions; an answer to an earlier question; or a request none of these tools can do (reminders — handled elsewhere).

Judge the INTENT, not the vocabulary:
- "I sent him the files yesterday" or "my desktop is such a mess" is CHAT (mentioning files while talking), while "get rid of the txt files in that folder" is TASK even though it names no tool.
- "I emailed him yesterday" or "my inbox is out of control" is CHAT, while "email jamil about dinner" is EMAIL even though it names no tool.
- An instruction to SEND is EMAIL even when the text to send reads like a statement or is written on someone's behalf: "email i221538@nu.edu.pk that the report is done", "send Ali a mail saying I'll be late", and "email him that this is Furi writing on behalf of my master" are all EMAIL, not CHAT.
- "the heating in here is awful" or "I should get smart bulbs" is CHAT (talking about the home), while "turn off the kitchen lights", "lock the front door", "set the thermostat to 21" and "run movie night" are HOME even though they name no tool.
- "this laptop is so slow" or "I love that app" is CHAT (talking about the machine), while "open spotify", "close the chrome window", "turn the volume down", "mute it", "take a screenshot" and "what is on my clipboard" are DESKTOP. "turn off the lights" is HOME, not DESKTOP — the house, not the screen.
- "my calendar is packed this week" is CHAT, while "put a meeting with jamil on my calendar tomorrow at 3" is CALENDAR.
- "what do you think of vector databases?" is CHAT (answerable from knowledge), while "search the web for the latest LangGraph release" or "look up who won the match today" or "open https://example.com and summarize it" is WEB.
- "play jane by the long faces on youtube", "watch the new severance trailer on youtube", or "open youtube and play some lofi" is BROWSE (act on a live site), while "what's the most-viewed youtube video" or "who owns youtube" is WEB (just look it up).
- "sign in to github and open my oldest repo", "log into linkedin and open my messages", or "apply to the first 3 python jobs on weworkremotely" is BROWSE (act on a live site — signing in, navigating, or submitting a form), while "what is github" or "who founded linkedin" is WEB (just look it up).
- A question about something CURRENT is WEB even when it never says "search": "when is the new season of Black Clover coming out?" or "what's the latest iPhone price?" needs up-to-date information — never answer it from stale knowledge or promise to look it up later.
- A factual question about a SPECIFIC real-world thing is WEB even when it isn't time-sensitive and never says "search": "what do you know about Black Clover", "who is Grigori Perelman", "tell me about the Framework laptop" — look them up for an accurate, current answer rather than reciting possibly-stale training data. But a question of OPINION, REASONING, or a general/timeless concept is CHAT: "what do you think of Black Clover", "how does anime production work", "what is recursion".
- Asking Furi to RETRIEVE something it has stored is TASK, not CHAT — it searches an index that reaches far further back than the messages still on screen: "find the pdf about cloud computing", "which report mentioned the outage" (a saved document, by its content) and "what did we discuss about the database migration", "find the conversation where we talked about pricing" (a past conversation, by its topic) are all TASK. But "we discussed this already" or "thanks for explaining that" is CHAT — commenting on a conversation rather than asking Furi to go and find one.
- A question about FURI'S OWN actions is TASK, not CHAT — Furi answers it from its action record, never from memory: "what was the name of the folder you created?", "did you delete anything today?", "who created the jarvis_test folder?" (Furi may have) are all TASK; "I deleted a bunch of files yesterday" is CHAT (the user talking about their own actions).
Any wording that asks for one of those actions now — or asks about actions Furi itself performed — gets its action label; anything else is CHAT.

Then, for an ACTION label only (never for CHAT), add a SECOND word for how to run it:
INLINE — a quick lookup Furi can answer in essentially one read, right now, that only READS and changes nothing: "what's on my desktop", "list my downloads", "open a folder on screen", "any new emails?", "what's my next meeting", "look up today's weather", "who is the CEO of X". The user waits a moment and gets the answer in the chat. Opening a folder is INLINE even when Furi has to search for it first — finding it is part of the same one quick answer.
DELEGATE — real work: anything that CREATES, MOVES, DELETES, SENDS, or CHANGES something; drives a browser (every BROWSE); or clearly takes several steps. "organize my downloads", "email jamil about dinner", "delete the temp files", "apply to 3 jobs", "play a song on youtube". It runs in the background as its own agent while the user keeps talking, and Furi notifies them when it is done.
When unsure, choose DELEGATE.

{context_block}USER MESSAGE:
{message}

One word (TASK, EMAIL, CALENDAR, WEB, HOME, DESKTOP, BROWSE, or CHAT) — and for any action label (not CHAT) add its mode, INLINE or DELEGATE, e.g. "TASK INLINE", "EMAIL DELEGATE", "BROWSE DELEGATE":"""

# The recognized action labels. All three feed the SAME planner and the same
# approval gates — there is one execution path. The label buys recall +
# telemetry and is the clean seam for future per-domain handlers.
_ACTION_LABELS = ("TASK", "EMAIL", "CALENDAR", "WEB", "HOME", "DESKTOP", "BROWSE")

# See the cap's reasoning at the call site. A FLOOR, never a target: it bounds
# how much the model may THINK before answering, and the answer itself is one
# or two words.
_CLASSIFY_MAX_TOKENS = 1024

# Appended on the retry only. The model is told what went wrong with its last
# reply, the way browser/loop.py `_decide` does — a bare repeat of the same
# prompt is a coin flip, while naming the failure makes the second attempt
# meaningfully different from the first.
_CLASSIFY_RETRY_NUDGE = (
    "\n\nYour previous reply was empty or did not begin with one of the "
    "allowed words. Reply now with EXACTLY one of: TASK, EMAIL, CALENDAR, "
    "WEB, HOME, DESKTOP, BROWSE, CHAT — optionally followed by INLINE or "
    "DELEGATE. No explanation, no reasoning, nothing else."
)

# Shown to the classifier when the conversation has earlier turns. A message
# is part of a conversation, not an island: "its in my downloads folder" after
# a failed delete is the user steering that task, not small talk (live bug
# 2026-07-10 — it fell open to chat, whose LLM promised the deletion and then
# fabricated "the task has been initiated").
_CLASSIFY_CONTEXT_TEMPLATE = """RECENT CONVERSATION (context only — the user message below is the NEXT message in it):
{context}

A short follow-up that continues an action being discussed in that conversation — supplying a detail it was missing ("its in my downloads folder"), correcting it, or telling Furi to go ahead with it ("send it", "yes do that", "play it") — gets that action's label (TASK, EMAIL, CALENDAR, WEB, HOME, DESKTOP, or BROWSE): "send it" after an email was being discussed is EMAIL; "play it" after a song on YouTube was being discussed is BROWSE. A message merely commenting on a finished action ("thanks, that worked") is CHAT.

"""


async def _classify_message(
    provider: LLMProvider, message: str, context: str = ""
) -> tuple[str, str]:
    """One tiny temperature-0 call returning (label, mode): label is a routing
    label ("TASK"/"EMAIL"/"CALENDAR"/"WEB"/"HOME"/"DESKTOP"/"BROWSE"/"CHAT"), mode is
    "INLINE" (a quick read answered in this turn) or "DELEGATE" (real work handed
    to a background agent). A failure — an exception OR an unrecognized reply —
    is RETRIED once (see the call site) and, if it fails again, means
    ("CHAT", "DELEGATE") (fail open): the message flows into the untouched
    Phase 2 chat path, never a broken action route. A recognized action label
    with no/blank mode defaults to DELEGATE — chat is never left blocked, and a
    quick read mis-tagged DELEGATE only costs one extra notification (both paths
    share the same approval gate, so the mode is a UX choice, never a safety one)."""
    context_block = (
        _CLASSIFY_CONTEXT_TEMPLATE.format(context=context) if context else ""
    )
    model = getattr(provider, "model_name", None)
    # Started BEFORE the retry loop on purpose: `classifier_ms` is the latency
    # the TURN actually paid, not one attempt's. So a p50 that jumps in the
    # audit means retries are firing, which is the signal worth seeing.
    started = time.perf_counter()

    def _stamp(label: str, mode: str, error: Optional[str] = None) -> tuple[str, str]:
        """Record the verdict on the turn's routing trace (a no-op when there is
        none) and return it unchanged. The trace is how the audit trail tells
        "the model judged this conversation" apart from "the call failed and
        CHAT was the fail-open default" — outcomes that are identical to the
        user and need completely different fixes."""
        routing_trace.note_classified(
            label, mode,
            ms=int((time.perf_counter() - started) * 1000),
            error=error,
            model=model,
        )
        return label, mode

    prompt = _CLASSIFY_PROMPT.format(message=message, context_block=context_block)

    # ⚠️ ONE RETRY, AND ONLY FOR THE FAILURE CLASSES (2026-08-06). Live: "furi
    # open fomi folder" spent 7384ms and came back EMPTY, so routing fell open
    # to chat and the user was told to rephrase. The audit row already told
    # these apart — fail_open_reason was `classifier_error`, not
    # `classifier_chat` — so the distinction was RECORDED and then acted on
    # nowhere: the most consequential LLM call in the product treated "the
    # model judged this conversation" and "the call produced nothing" as the
    # same outcome.
    #
    # An empty string is what a reasoning model returns when the cap runs out
    # mid-thought, and this provider's temp-0 is not deterministic — so it is
    # plausibly transient, which is exactly the argument browser/loop.py
    # `_decide` made for its own one-retry on 2026-07-24. A clean CHAT is
    # deliberately NOT retried: that is a judgement the model made with the
    # whole conversation in view, not a hiccup, and retrying it would double
    # the cost of every ordinary conversational turn.
    last_error: Optional[str] = None
    for attempt in (1, 2):
        try:
            response = await provider.chat(
                messages=[LLMMessage(
                    role="user",
                    content=prompt if attempt == 1 else prompt + _CLASSIFY_RETRY_NUDGE,
                )],
                temperature=0.0,
                # NOT a tiny cap: on thinking models reasoning tokens count
                # against max_tokens, so 8 produced ZERO output
                # (finish_reason=MAX_TOKENS) and EVERY message fell open to chat
                # — Furi stopped doing tasks entirely on Gemini (2026-07-13).
                #
                # RAISED 512 → 1024 (2026-08-06), self-inflicted in the same way
                # the browse decision cap was: this prompt gained two whole tool
                # groups (HOME, DESKTOP) on 2026-08-04 while the cap had not
                # moved since 2026-07-13. A richer catalog raises the reasoning
                # cost of USING it, so the cap has to move with it. The cap
                # bounds thinking, not output — the parser reads the first word
                # and temp-0 keeps replies short, so a normal call is unchanged.
                max_tokens=_CLASSIFY_MAX_TOKENS,
            )
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"[:256]
            logger.warning(
                f"Message classification call failed (attempt {attempt}): {e}"
            )
            if attempt == 2:
                return _stamp("CHAT", "DELEGATE", error=last_error)
            continue

        def _recovered(verdict: str) -> None:
            """A verdict reached on attempt 2 means attempt 1 failed. The audit
            column deliberately does NOT record that (it would mark a
            successful classification as a fail-open, destroying the very
            distinction this round restored) — so the log is the only place a
            rising retry rate is visible, and BOTH outcomes have to say it. A
            recovered CHAT is otherwise indistinguishable from a first-try
            CHAT, which is exactly the blindness that let this incident sit."""
            if attempt == 2:
                logger.warning(
                    "Routing classifier RECOVERED on retry (first reply was "
                    f"unusable: {last_error}) — verdict {verdict}. A rising "
                    "rate here is a provider/model problem, not a prompt one."
                )

        reply = response.content.strip().upper()
        for label in _ACTION_LABELS:
            if reply.startswith(label):
                # Mode is the second word; anything but an explicit INLINE (or a
                # blank/garbled mode) falls to DELEGATE — the safe UX default.
                mode = "INLINE" if "INLINE" in reply else "DELEGATE"
                _recovered(label)
                return _stamp(label, mode)
        # A reply starting with CHAT is a real judgement; anything else (blank,
        # garbled, a thinking model that spent its budget) is a FAILURE wearing
        # the same fail-open clothes. Recorded apart, because a rising rate of
        # the second is a provider/model problem, not a prompt one.
        if reply.startswith("CHAT"):
            _recovered("CHAT")
            return _stamp("CHAT", "DELEGATE")

        last_error = f"unrecognized reply: {reply[:60]!r}"
        logger.warning(f"Routing classifier returned no verdict (attempt {attempt}): {last_error}")

    # Both attempts failed. `classifier_error` is stamped ONLY here and on the
    # exception path, so `fail_open_reason` keeps meaning exactly what the
    # routing table was built to distinguish; a RECOVERED retry is reported in
    # the log above rather than this column, which would otherwise mark a
    # successful classification as a failure.
    return _stamp("CHAT", "DELEGATE", error=last_error)


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

@dataclass
class RouteDecision:
    """What routing decided, plus everything the audit trail says about why.

    The audited fields live on ONE object — the turn's ``RouteTrace`` — instead
    of being copied onto this one. Two copies of the same fact drift, and this
    project has paid for exactly that four times (`registry.mutates`, the
    `_settle` status tuple, `_OPEN_STATUSES`, `KNOWN_TLDS`). So this wraps the
    trace and adds only what a trace has no business holding: the agent object,
    the planner's memory string, and the goal actually handed to the planner.
    """

    trace: routing_trace.RouteTrace
    agent: AgentSpec = GENERAL
    memory: str = ""
    run_goal: str = ""

    @property
    def routed(self) -> bool:
        """True when an action route was chosen; False = fall open to chat."""
        return not self.trace.fail_open_reason

    @property
    def label(self) -> str:
        return self.trace.label or "CHAT"

    @property
    def mode(self) -> str:
        return self.trace.mode or "DELEGATE"

    @property
    def delegate(self) -> bool:
        return self.trace.execution == "delegate"

    @property
    def fail_open_reason(self) -> str:
        return self.trace.fail_open_reason


async def decide_route(
    goal: str,
    conversation: str = "",
    provider: Optional[LLMProvider] = None,
    *,
    memory_loader: Optional[Callable[[str], Awaitable[str]]] = None,
    defer_check: Optional[Callable[[], bool]] = None,
) -> RouteDecision:
    """Decide WHERE a chat message goes — and nothing else.

    Runs the deterministic gate, the code-owned overrides and the LLM
    confirmation, then returns the verdict. It starts nothing, streams nothing
    and touches no session state; dispatch is ``maybe_handle_task``'s job.

    Split out on 2026-08-03 for two reasons that are really one. The routing
    audit trail needs a decision RECORD, and ``scripts/route_bench.py`` needs to
    score the decision without a request, a StreamingResponse or a database. A
    bench that scored a *re-implementation* of routing would be this project's
    fifth "the test drove a shape the product doesn't use" defect (2026-07-17
    fan-out, 07-30 bulk tools, 08-01 page fakes, 08-02 grid fakes) — so the
    bench calls THIS, the same function the chat turn calls.

    ``memory_loader`` preserves the production concurrency: the planner's memory
    context is built while the classifier is in flight (exactly one of them uses
    each contended resource, so the gather is safe and the memory build hides
    inside the network wait). A caller that wants only the decision passes None
    and pays for neither.

    ``defer_check`` is consulted BETWEEN the gate and the classifier — where
    ``maybe_handle_task``'s parked-question check sits. The position is
    load-bearing: after the classifier it would spend an LLM call on a turn that
    is owed to a question somewhere else.
    """
    if routing_trace.current() is None:
        # A caller outside a chat turn (the bench, a test) gets a trace of its
        # own, so stamping is uniform and the returned decision is fully
        # populated either way. Nothing here flushes it — writing is chat.py's.
        routing_trace.begin(None, goal, has_conversation=bool(conversation))
    trace = routing_trace.current()
    decision = RouteDecision(trace=trace, run_goal=goal)

    goal = (goal or "").strip()
    if not goal:
        routing_trace.note_fail_open(routing_trace.FAIL_NO_GOAL)
        return decision

    # The gate fires on the message's own words; a short follow-up steering an
    # action under discussion ("send it") borrows its object from the
    # conversation instead. Either way the classifier makes the real call.
    tier = gate_tier(goal)
    if not tier and is_action_followup(goal, conversation):
        tier = "action_followup"
    if not tier and is_browse_followup(goal):
        tier = "browse_followup"
    routing_trace.note_gate(bool(tier), tier)
    if not tier:
        routing_trace.note_fail_open(routing_trace.FAIL_GATE_CLOSED)
        return decision

    # Never hijack a reply owed to a parked question.
    if defer_check is not None and defer_check():
        routing_trace.note_fail_open(routing_trace.FAIL_PARKED_QUESTION)
        return decision

    # Phase 4, Part 5: background intent ("…and tell me when you're done")
    # escapes the chat turn entirely — the plan runs as a persisted Task and
    # every pause/outcome arrives by push + persisted message, not by stream.
    # Stripped BEFORE classification: the intent phrase is routing metadata,
    # not part of the task, and "…and remind me when you are done" makes the
    # classifier read the whole message as a reminder request (listed as
    # CHAT) — a real file task fell open to the chat path, whose LLM then
    # denied having file access (live bug, 2026-07-09).
    background, cleaned_goal = wants_background(goal)
    if background:
        routing_trace.note_background()
    effective_goal = cleaned_goal if background else goal
    decision.run_goal = effective_goal

    # A bare navigation instruction ("open junaidjamshed.com") is decided in code
    # — see _is_bare_navigation. The classifier is not asked, because on this
    # message shape it does not agree with itself.
    if _is_bare_navigation(effective_goal):
        logger.info(f"Bare navigation routed in code [BROWSE]: '{goal[:80]}'")
        routing_trace.note_bare_navigation()
        routing_trace.note_label("BROWSE", "DELEGATE")
        label, mode = "BROWSE", "DELEGATE"
        if memory_loader is not None:
            decision.memory = await memory_loader(effective_goal)
    elif memory_loader is not None:
        # Multi-class routing (Phase 5, Part 5): TASK / EMAIL / CALENDAR / WEB /
        # BROWSE / CHAT. The classifier round trip and the planner's memory
        # context ("one brain", Phase 3.5) run CONCURRENTLY: the classifier never
        # touches the request's DB session and the context build makes no LLM
        # call, so exactly one of them uses each contended resource. On a CHAT
        # label the memory string is discarded; that wasted work is local and
        # cheap, while the saved wall time is paid on every action turn. Neither
        # coroutine raises by contract (classify fails to CHAT, memory to "").
        (label, mode), decision.memory = await asyncio.gather(
            _classify_message(provider, effective_goal, conversation),
            memory_loader(effective_goal),
        )
    else:
        label, mode = await _classify_message(provider, effective_goal, conversation)

    if label == "CHAT":
        # THE DISTINCTION THIS TABLE EXISTS FOR: "the model read this as
        # conversation" and "the call failed and CHAT is the fail-open default"
        # are identical from the user's seat and need completely different
        # fixes. classifier_error is set only on the failure paths.
        routing_trace.note_fail_open(
            routing_trace.FAIL_CLASSIFIER_ERROR if trace.classifier_error
            else routing_trace.FAIL_CLASSIFIER_CHAT
        )
        return decision

    # The boss assigns the domain agent from the classifier's label; the
    # cross-domain fallback (general, all tools) covers an unmapped label.
    decision.agent = agent_for_label(label)

    # Explicit background intent ("…tell me when you're done") always delegates —
    # a user override. Otherwise the classifier's mode decides: DELEGATE hands
    # the goal to its domain agent as a background Task (the user keeps talking
    # and can start another agent concurrently); INLINE is a quick read streamed
    # in this turn. Safety is identical either way — the approval gate applies
    # on both paths, so a mis-tagged write just pauses instead of running.
    #
    # BROWSE is ALWAYS delegated, whatever mode the classifier returned. A browse
    # drives a real browser — it launches Chromium, runs a multi-step
    # observe→decide→act loop with several LLM calls, navigates pages, and (for
    # play/watch) keeps a window open. It is NEVER "a quick read answered in one
    # turn": run INLINE it holds the chat SSE open for the entire browse and locks
    # the user out of starting anything else (live bug 2026-07-24 — "play latest
    # episode of one piece on anikoto.cz" was tagged BROWSE INLINE by the LLM, the
    # browse ran in-turn, and the chat froze "processing and processing" until it
    # finished; the multi-agent concurrency the user relies on evaporates because
    # it depends entirely on DELEGATE). The prompt already says "every BROWSE →
    # DELEGATE"; this is the structural backstop for when the LLM ignores it —
    # structural-over-prompt, the house rule.
    delegate = background or mode == "DELEGATE" or label == "BROWSE"
    routing_trace.note_outcome(
        routing_trace.OUTCOME_TASK_BACKGROUND if delegate
        else routing_trace.OUTCOME_TASK_INLINE,
        agent=decision.agent.key,
        execution="delegate" if delegate else "inline",
    )
    return decision


async def maybe_handle_task(
    request: ChatRequest,
    session_id: str,
    db: AsyncSession,
    provider: LLMProvider,
) -> Optional[StreamingResponse]:
    """Return a StreamingResponse when the latest user message is a task
    request; None sends the message down the untouched Phase 2 chat path.

    Deciding lives in ``decide_route``; this function dispatches on its verdict."""
    user_msgs = [m.content for m in request.messages if m.role == "user"]
    goal = user_msgs[-1].strip() if user_msgs else ""
    if not goal:
        routing_trace.note_fail_open(routing_trace.FAIL_NO_GOAL)
        return None

    # An open plan owns the next message: a clarifying question ("which
    # notes.txt?"), a plan the user paused, or — since 2026-08-03 — one holding
    # an APPROVAL card. The user is answering Furi, not starting a new task.
    # Typed answers and clicked buttons are equivalent (a click posts to
    # /api/agent/choose or /approve and consumes the plan first — hence the
    # second, atomic pop check).
    choice_plan = await get_choice_plan_for_session(db, session_id)
    if choice_plan is not None:
        # ⚠️ A TYPED WORD NEVER GRANTS APPROVAL. Checked BEFORE the pop, so the
        # card survives and the user can still click it. Two independent
        # reasons, both structural rather than a preference:
        #
        #  1. Consent to a write is consent to a SIGNATURE — the exact command
        #     and paths rendered on the card. That is the whole approval
        #     model ("approval never transfers to actions the user hasn't
        #     seen"), and no free-text parse can be that specific.
        #  2. MEASURED against the phrase list we would have to reuse: the
        #     PAUSED continue detector (_CARRY_ON_RE) accepts "never mind" and
        #     "nvm", because at a pause they mean "forget I interrupted, carry
        #     on". At an approval card the same words mean "forget it, DON'T".
        #     One word set, opposite meanings — so borrowing it would have
        #     flipped a delete ON for a user asking to drop it.
        #
        # A false positive here costs one nudge and a button click. Getting it
        # wrong the other way runs something irreversible.
        if choice_plan.status == PlanStatus.AWAITING_APPROVAL and _is_typed_approval(goal):
            logger.info(
                f"Typed approval refused for plan {choice_plan.id} — the card stands"
            )
            routing_trace.note_outcome(
                routing_trace.OUTCOME_APPROVAL_REFUSED, plan_id=choice_plan.id
            )
            return _stream_static_text(
                goal, session_id, db, provider, _typed_approval_nudge,
            )
        plan = await pop_plan(db, choice_plan.id)
        if plan is not None:
            logger.info(
                f"Chat message routed to plan {plan.id} ({plan.status.value}) "
                "as the user's answer"
            )
            routing_trace.note_outcome(
                routing_trace.OUTCOME_PLAN_ANSWER, plan_id=plan.id
            )
            return _stream_answer(goal, plan, session_id, db, provider)

    # Built before the gate: the follow-up check reads it, and the classifier
    # below judges the message IN its conversation either way. Pure string
    # work — no LLM, no DB.
    conversation = conversation_context(request)

    def _owed_elsewhere() -> bool:
        """Never hijack a reply to a parked question ("which jamil?" / "add
        daud?"). Peek without get_session() — that would create sessions as a
        side effect."""
        from app.memory.conversation_state import CONVERSATION_SESSIONS
        sess = CONVERSATION_SESSIONS.get(session_id)
        return sess is not None and (
            sess.pending_resolution is not None or sess.pending_creation is not None
        )

    decision = await decide_route(
        goal,
        conversation,
        provider,
        memory_loader=lambda g: planner_memory_context(db, g),
        defer_check=_owed_elsewhere,
    )
    if not decision.routed:
        return None

    logger.info(
        f"Chat message routed to {decision.agent.display_name} "
        f"[{decision.label}/{'DELEGATE' if decision.delegate else 'INLINE'}]: "
        f"'{goal[:80]}'"
    )

    if decision.delegate:
        return _stream_task_background(
            goal, decision.run_goal, conversation, decision.memory,
            session_id, db, provider, decision.agent,
        )
    return _stream_task(
        goal, conversation, decision.memory, session_id, db, provider, decision.agent,
    )


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
    agent: Optional[AgentSpec] = None,
) -> StreamingResponse:
    async def run(planner: AgentPlanner) -> AgentPlan:
        return await planner.start(goal)

    return _stream_plan_run(goal, conversation, memory, run, session_id, db, provider, agent=agent)


# Whole-message affirmations that READ as "approve this card". Deliberately NOT
# planner._CARRY_ON_RE: that set answers a different question and contains
# "never mind"/"nvm", which mean the OPPOSITE here (see the call site). Used
# only to REFUSE — a false positive costs a nudge, never an action — so it can
# afford to be generous.
_TYPED_APPROVAL_RE = re.compile(
    r"^\W*(?:yes|yeah|yep|yup|ya|sure|ok|okay|k|fine|alright|"
    r"go\s*ahead|go\s*for\s*it|go\s*on|do\s*it|send\s*it|"
    r"proceed|continue|carry\s*on|confirm(?:ed)?|approve[d]?|"
    r"permission\s*granted|you\s*(?:can|may)|please\s*do)\b",
    re.IGNORECASE,
)
# Filler that may trail an approval without making it an instruction.
_TYPED_APPROVAL_NOISE = frozenset({
    "then", "please", "now", "furi", "jarvis", "thanks", "thank", "you", "it", "that",
    "sir", "and", "just", "go", "ahead", "on", "with", "the", "task", "sure",
    "do", "this", "all", "of", "them", "yes", "ok", "okay", "fine",
})


def _is_typed_approval(message: str) -> bool:
    """True when a message typed at an APPROVAL card reads as consent and
    carries no correction. Whole-message by construction: "yes" is consent,
    "yes but use the D drive" has substantive words left over and is a STEER.

    Erring toward STEER is the safe direction here, the mirror of
    _is_bare_continue's reasoning: a misread steer replans (and pauses again
    for approval), while a misread consent would let a delete through on words
    that were actually a correction."""
    text = (message or "").strip()
    match = _TYPED_APPROVAL_RE.match(text)
    if match is None:
        return False
    rest = re.findall(r"[\w'-]+", text[match.end():].lower())
    return not [w for w in rest if w not in _TYPED_APPROVAL_NOISE]


async def _typed_approval_nudge() -> str:
    """Deterministic, never LLM-paraphrased — the same rule every other consent
    text in this codebase follows. Says what did NOT happen, and what to do."""
    return (
        "I'd rather you confirmed that on the card itself, sir — approval is "
        "tied to the exact steps shown there, and a typed word can't be. "
        "Nothing has run. Use **Approve** on the card above to go ahead, or "
        "**Cancel** to drop it — or just tell me what to change instead."
    )


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
        # Resume the same specialist the paused plan was drafted as.
        agent=agent_for_key(plan.agent_key),
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
        await persist_message_best_effort(
            db, session_id, "user", user_text, what="task user message",
        )

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

        await persist_message_best_effort(
            db, session_id, "assistant", text,
            model=provider.model_name, what="task response",
        )

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
    agent: Optional[AgentSpec] = None,
) -> StreamingResponse:
    """Hand the goal to its domain agent as a background Task and acknowledge
    immediately. No plan chunk here — if the plan pauses, the PlanCard arrives
    via the push channel; if it completes, the outcome message does."""
    agent = agent or GENERAL

    async def reply() -> str:
        try:
            await start_task(
                db, cleaned_goal, session_id,
                conversation=conversation, memory=memory, provider=provider,
                agent=agent,
            )
        except Exception as e:
            logger.error(f"Starting background task failed for '{goal[:80]}': {e}")
            return (
                "I couldn't start that as a background task, so nothing was "
                "changed. Please try again."
            )
        # Agent-aware ack: name the specialist for the general case, keep it
        # natural ("working on that") for the general agent.
        who = (
            f"the {agent.display_name.lower()}" if agent.key != "general"
            else "one of my agents"
        )
        return (
            f"I've handed that to {who} — it's running in the background. I'll "
            "notify you when it's done, or first if any step needs your approval."
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
    persist_user: bool = True,
    agent: Optional[AgentSpec] = None,
) -> StreamingResponse:
    return StreamingResponse(
        plan_run_events(
            user_text, conversation, memory, run, session_id, db, provider,
            persist_user=persist_user, agent=agent,
        ),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


def plan_run_events(
    user_text: str,
    conversation: str,
    memory: str,
    run,  # async (AgentPlanner) -> AgentPlan
    session_id: str,
    db: AsyncSession,
    provider: LLMProvider,
    persist_user: bool = True,
    agent: Optional[AgentSpec] = None,
):
    """The plan-run SSE generator, exposed so a caller that is ALREADY
    streaming can delegate to it mid-flight (chat.py's dead-end backstop).
    `persist_user=False` is for exactly that caller: the chat path has already
    written the user's Message row, and writing it twice would duplicate the
    turn in history."""

    def _text_chunk(delta: str, done: bool = False) -> str:
        chunk = StreamChunk(
            delta=delta, done=done, session_id=session_id,
            model=provider.model_name, provider=provider.provider_name,
        )
        return f"data: {chunk.model_dump_json()}\n\n"

    async def event_generator():
        # Persist the user message here — the Phase 2 path that normally does
        # this was bypassed. Same Message row it would have written.
        if persist_user:
            await persist_message_best_effort(
                db, session_id, "user", user_text, what="task user message",
            )

        try:
            planner = AgentPlanner(
                db, provider, session_id=session_id,
                conversation=conversation, memory=memory,
                agent=agent or GENERAL,
            )
            plan = await run(planner)
        except Exception as e:
            logger.error(f"Agent planning failed for '{user_text[:80]}': {e}")
            yield _text_chunk(
                "I ran into a problem while planning that task, so nothing "
                "was changed. Please try again.", done=True,
            )
            return

        # ONE list of "stopped without settling, so it must stay answerable",
        # shared with the /api/agent endpoints. A hand-kept second copy is how
        # `paused` was nearly missed in _settle (2026-08-03) — and this was the
        # fourth copy in the tree.
        if plan.status in _PARKABLE:
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
            await persist_message_best_effort(
                db, session_id, "assistant", assistant_text,
                model=provider.model_name, what="task response",
            )

    return event_generator()


async def rescue_unrouted_turn(
    goal: str,
    request: ChatRequest,
    session_id: str,
    db: AsyncSession,
    provider: LLMProvider,
):
    """Re-run a chat turn as a plan after the chat model itself admitted it
    needs a capability chat does not have (chat.py's `_DEAD_END_OFFER_RE`) —
    a web lookup, or any action it just asked the user to re-say.

    RENAMED from `rescue_web_turn` (2026-08-06). Only the name and the trigger
    were ever web-shaped: the body below runs the GENERAL planner on the goal
    with the full tool catalog and always could have opened a folder, read
    mail or driven a browser. The web-only vocabulary in `_DEAD_END_OFFER_RE`
    was the whole limit, and it left every other capability dead-ending on a
    magic-word demand.

    Deliberately does NOT re-classify. Routing already had its chance at this
    message and got it wrong — that failure is the entire reason this path
    exists, and asking the same classifier the same question a second time
    would just buy the same answer. (Where the classifier FAILED rather than
    judged, `_classify_message` has already retried it once, which is the
    cheaper and earlier fix; this is the net under everything that survives
    it.) The model's own admission IS the label, and it is a better one: it
    was produced with the whole conversation, the memory context, and its own
    knowledge in view.

    Everything downstream is the ordinary task path — same planner, same
    registry, same structural approval gate. A rescued turn therefore has
    exactly the authority the front door would have given it: a read runs, and
    a write PAUSES for approval on a card the user must click. Widening the
    trigger past web widens RECALL, never authority — the same property that
    lets a scheduled routine or an accepted suggestion re-derive a plan from a
    goal string safely."""
    conversation = conversation_context(request)
    memory = await planner_memory_context(db, goal)

    async def run(planner: AgentPlanner) -> AgentPlan:
        return await planner.start(goal)

    async for sse in plan_run_events(
        goal, conversation, memory, run, session_id, db, provider,
        # The chat path persisted the user's message before it started
        # streaming; writing it again would double the turn in history.
        persist_user=False,
    ):
        yield sse


# The pre-2026-08-06 name, kept because this module's public shape is what
# tests and callers patch (the `_deterministic_text` / `_summarize_completed`
# convention above). It was never web-specific in behaviour.
rescue_web_turn = rescue_unrouted_turn


# ================================================================ rendering

# Moved to app/agents/rendering.py in Phase 4 Part 5 (the background task
# runner renders the SAME words); the alias keeps this module's public shape.
_deterministic_text = deterministic_plan_text


# Moved to app/agents/summary.py (2026-07-12) so the agent HTTP endpoints —
# /api/agent/choose (clicked options) and /approve — can render the SAME
# completion words this streaming path does; the alias keeps this module's
# public shape for the tests that patch it here.
_summarize_completed = stream_completed_summary
