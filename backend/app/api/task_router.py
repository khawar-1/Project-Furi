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
)
from app.agents.summary import stream_completed_summary
from app.api.agent import _plan_response
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
    # Sign in to / operate a web app the user names (Phase 14 BROWSE, the github
    # sign-in incident 2026-07-18: "sign in to github and open my oldest repo"
    # named no domain noun, missed the gate, fell to plain chat — which then
    # asked the user for their password). "sign in / log in / sign into / log
    # into" (incl. signin/login/sign-in) is an act-on-a-site intent that fires
    # the gate ALONE; the classifier makes the BROWSE/CHAT call. Rare in small
    # talk, so a false fire costs one temp-0 call — the recall-first trade-off.
    r"\bsign(?:ing|ed)?[\s-]?in(?:to)?\b|\blog(?:ging|ged)?[\s-]?in(?:to)?\b|"
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
    r"launching|install|installs|installed|installing|"
    # Media / live-site verbs (Phase 14) — "play"/"watch"/"listen"/"stream"
    # only fire with a weak media noun ("play the song", "watch the trailer");
    # a named site ("play X on youtube") fires the strong gate above on its own.
    r"play|plays|played|playing|watch|watches|watched|watching|"
    r"listen|listens|listened|listening|stream|streams|streamed|streaming|"
    r"search|searches|searched|"
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


# Questions about Jarvis's OWN actions ("what have you done today?", "did you
# delete anything?") often name no domain noun at all — the object is Jarvis's
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
# IT IS ABOUT THE USER OR JARVIS THEMSELVES.
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
    r"and|also|but|um+|uh+|jarvis)\b[\s,!.]*)*"
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

# A question about the user, about Jarvis, or about the two of them is CHAT by
# construction — Jarvis's own memory and context answer it, the web cannot.
# Applied to the SUBJECT (after the request frame is stripped), never to the
# raw message.
_SELF_REFERENTIAL_RE = re.compile(
    r"\b(?:you|your|yours|you're|u|ur|i|i'm|me|my|mine|myself|we|we're|our|"
    r"ours|us|let's)\b"
)

# A question whose subject is a bare POINTER ("who is this?", "what is that?",
# "who are they?") refers to something in the conversation, not out in the
# world: Jarvis answers it from context and the web could not help. Matched
# only immediately after the question word and its copula — so "what's the
# latest on that iphone rumour", where "that" is a determiner rather than a
# pointer, still reaches the classifier.
_DEICTIC_SUBJECT_RE = re.compile(
    r"^(?:who|whos|who's|what|whats|what's|which|where)"
    r"(?:'s|\s+(?:is|are|was|were))?\s+"
    r"(?:this|that|it|these|those|they|them|he|she|him|her|there)\b"
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
    Jarvis, nor a pointer back into the conversation. The classifier makes the
    real WEB/CHAT call."""
    subject = _question_subject(text)
    if subject is None:
        return False
    if _SELF_REFERENTIAL_RE.search(subject):
        return False
    return not _DEICTIC_SUBJECT_RE.match(subject.lstrip())

def looks_like_task(text: str) -> bool:
    """Deterministic pre-filter, tuned for RECALL: a strong computer-domain
    noun fires alone (any verb, any phrasing); an external question fires
    alone; weak signals need an action verb. Deliberately over-inclusive —
    the LLM confirmation prunes it."""
    t = text.lower()
    if _STRONG_DOMAIN_RE.search(t):
        return True
    # Own-action questions are checked BEFORE the question tier and must stay
    # that way: "what did you do today" is second-person, so the question
    # tier's self-reference test would refuse it — but it is a real TASK,
    # answered from the audit log by recall_actions.
    if _OWN_ACTION_RE.search(t):
        return True
    if _OWN_ACTION_AUX_RE.search(t) and _ACTION_VERB_RE.search(t):
        return True
    if is_external_question(text):
        return True
    return bool(_ACTION_VERB_RE.search(t)) and bool(_WEAK_DOMAIN_RE.search(t))


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


# ======================================================== LLM confirmation

_CLASSIFY_PROMPT = """You route messages for Jarvis OS, a personal AI that can act on the user's computer and accounts with exactly these tool groups:
- FILES/SYSTEM: search/read/list files and folders, create/move/rename/delete files, run terminal commands and scripts, and recall Jarvis's OWN past actions from its audit log (what it created, deleted, moved, sent, or ran).
- EMAIL: search and read Gmail; draft, send, or reply to email.
- CALENDAR: list/find Google Calendar events; create, update, or delete events.
- WEB: search the web and open/read a web page to look up online information.
- BROWSE: drive a real web browser to ACT on a live site the user names — play or watch a video (YouTube and the like), sign in to a site and navigate it, open something in a web app (a repo on GitHub, a page in an account), or fill in and submit a web form (e.g. apply to jobs).

Reply with EXACTLY one word:
TASK — asks Jarvis to perform a FILES/SYSTEM action now, OR asks what Jarvis ITSELF did on the machine (the folder/file it created, what it deleted, what it has done today).
EMAIL — asks Jarvis to search, read, draft, send, or reply to email now.
CALENDAR — asks Jarvis to look at or change calendar events now.
WEB — asks Jarvis to search the web or open/read a web page now, OR asks a factual question better answered from the live internet than from stale built-in knowledge. This covers two cases: (a) anything CURRENT or time-sensitive (news, release dates, upcoming seasons or products, prices, scores, weather), and (b) a factual question about a SPECIFIC real-world entity — a person, company, product, place, organization, or a creative work such as a show, anime, movie, game, or book ("what do you know about Black Clover", "who is the CEO of X", "tell me about the Framework laptop"). Jarvis looks these up rather than guessing, promising, or reciting possibly-outdated training data.
BROWSE — asks Jarvis to DO something on a live website by driving a browser: play or watch a video ("play jane by the long faces on youtube", "watch the new trailer on youtube", "open youtube and play some lofi"); sign in to a site and then navigate or open something in it ("sign in to github and open my oldest repo", "log into my account and download the invoice"); operate an interactive web app; or fill in and submit a web form ("apply to the first 3 python jobs on weworkremotely"). This is ACTING on a live site — distinct from WEB, which only LOOKS UP information. Jarvis never asks the user for a password: if a site needs signing in, it opens the sign-in page for the user and continues after — so "sign in to X and ..." is BROWSE, never a request for credentials.
CHAT — anything else: casual conversation; OPINION, reasoning, or general/timeless concepts Jarvis can reason about ("what do you think of vector databases", "explain recursion", "how does TCP work"); help writing or debugging code; questions about the user's own life or about Jarvis itself; sharing information about their life; talking ABOUT the user's own past or hypothetical actions; an answer to an earlier question; or a request none of these tools can do (reminders — handled elsewhere).

Judge the INTENT, not the vocabulary:
- "I sent him the files yesterday" or "my desktop is such a mess" is CHAT (mentioning files while talking), while "get rid of the txt files in that folder" is TASK even though it names no tool.
- "I emailed him yesterday" or "my inbox is out of control" is CHAT, while "email jamil about dinner" is EMAIL even though it names no tool.
- An instruction to SEND is EMAIL even when the text to send reads like a statement or is written on someone's behalf: "email i221538@nu.edu.pk that the report is done", "send Ali a mail saying I'll be late", and "email him that this is Furi writing on behalf of my master" are all EMAIL, not CHAT.
- "my calendar is packed this week" is CHAT, while "put a meeting with jamil on my calendar tomorrow at 3" is CALENDAR.
- "what do you think of vector databases?" is CHAT (answerable from knowledge), while "search the web for the latest LangGraph release" or "look up who won the match today" or "open https://example.com and summarize it" is WEB.
- "play jane by the long faces on youtube", "watch the new severance trailer on youtube", or "open youtube and play some lofi" is BROWSE (act on a live site), while "what's the most-viewed youtube video" or "who owns youtube" is WEB (just look it up).
- "sign in to github and open my oldest repo", "log into linkedin and open my messages", or "apply to the first 3 python jobs on weworkremotely" is BROWSE (act on a live site — signing in, navigating, or submitting a form), while "what is github" or "who founded linkedin" is WEB (just look it up).
- A question about something CURRENT is WEB even when it never says "search": "when is the new season of Black Clover coming out?" or "what's the latest iPhone price?" needs up-to-date information — never answer it from stale knowledge or promise to look it up later.
- A factual question about a SPECIFIC real-world thing is WEB even when it isn't time-sensitive and never says "search": "what do you know about Black Clover", "who is Grigori Perelman", "tell me about the Framework laptop" — look them up for an accurate, current answer rather than reciting possibly-stale training data. But a question of OPINION, REASONING, or a general/timeless concept is CHAT: "what do you think of Black Clover", "how does anime production work", "what is recursion".
- A question about JARVIS'S OWN actions is TASK, not CHAT — Jarvis answers it from its action record, never from memory: "what was the name of the folder you created?", "did you delete anything today?", "who created the jarvis_test folder?" (Jarvis may have) are all TASK; "I deleted a bunch of files yesterday" is CHAT (the user talking about their own actions).
Any wording that asks for one of those actions now — or asks about actions Jarvis itself performed — gets its action label; anything else is CHAT.

{context_block}USER MESSAGE:
{message}

One word (TASK, EMAIL, CALENDAR, WEB, BROWSE, or CHAT):"""

# The recognized action labels. All three feed the SAME planner and the same
# approval gates — there is one execution path. The label buys recall +
# telemetry and is the clean seam for future per-domain handlers.
_ACTION_LABELS = ("TASK", "EMAIL", "CALENDAR", "WEB", "BROWSE")

# Shown to the classifier when the conversation has earlier turns. A message
# is part of a conversation, not an island: "its in my downloads folder" after
# a failed delete is the user steering that task, not small talk (live bug
# 2026-07-10 — it fell open to chat, whose LLM promised the deletion and then
# fabricated "the task has been initiated").
_CLASSIFY_CONTEXT_TEMPLATE = """RECENT CONVERSATION (context only — the user message below is the NEXT message in it):
{context}

A short follow-up that continues an action being discussed in that conversation — supplying a detail it was missing ("its in my downloads folder"), correcting it, or telling Jarvis to go ahead with it ("send it", "yes do that", "play it") — gets that action's label (TASK, EMAIL, CALENDAR, WEB, or BROWSE): "send it" after an email was being discussed is EMAIL; "play it" after a song on YouTube was being discussed is BROWSE. A message merely commenting on a finished action ("thanks, that worked") is CHAT.

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
            # NOT a tiny cap: on thinking models (gemini-2.5-*) reasoning
            # tokens count against max_tokens, so 8 produced ZERO output
            # (finish_reason=MAX_TOKENS) and EVERY message fell open to chat —
            # Jarvis stopped doing tasks entirely on Gemini (found 2026-07-13).
            # The parser only reads the first word; temp-0 keeps replies short.
            max_tokens=512,
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

    # Built before the gate: the follow-up check reads it, and the classifier
    # below judges the message IN its conversation either way. Pure string
    # work — no LLM, no DB.
    conversation = conversation_context(request)

    # The gate fires on the message's own words; a short follow-up steering an
    # action under discussion ("send it") borrows its object from the
    # conversation instead. Either way the classifier makes the real call.
    if not looks_like_task(goal) and not is_action_followup(goal, conversation):
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

    # Multi-class routing (Phase 5, Part 5): TASK / EMAIL / CALENDAR / CHAT.
    # CHAT fails open to Phase 2. The three action labels all route to the SAME
    # planner below — the tool registry already contains the file, email, and
    # calendar tools, so the planner picks the right ones from the goal. The
    # label buys recall + telemetry and is the documented insertion point for
    # future per-domain handlers (do not add a dispatcher until one is needed).
    #
    # The classifier round trip and the planner's memory context ("one brain",
    # Phase 3.5) run CONCURRENTLY: the classifier never touches the request's
    # DB session and the context build makes no LLM call, so exactly one of
    # them uses each contended resource — gather is safe, and the memory build
    # (embeds + queries) is hidden inside the classifier's network wait. On a
    # CHAT label the memory string is discarded; that wasted work is local and
    # cheap, while the saved wall time is paid on every action turn. Neither
    # coroutine raises by contract (classify fails to CHAT, memory to "").
    effective_goal = cleaned_goal if background else goal
    label, memory = await asyncio.gather(
        _classify_message(provider, effective_goal, conversation),
        planner_memory_context(db, effective_goal),
    )
    if label == "CHAT":
        return None

    logger.info(f"Chat message routed to agent planner [{label}]: '{goal[:80]}'")

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
    persist_user: bool = True,
) -> StreamingResponse:
    return StreamingResponse(
        plan_run_events(
            user_text, conversation, memory, run, session_id, db, provider,
            persist_user=persist_user,
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
            await persist_message_best_effort(
                db, session_id, "assistant", assistant_text,
                model=provider.model_name, what="task response",
            )

    return event_generator()


async def rescue_web_turn(
    goal: str,
    request: ChatRequest,
    session_id: str,
    db: AsyncSession,
    provider: LLMProvider,
):
    """Re-run a chat turn as a plan after the chat model itself admitted the
    answer needs a web lookup (chat.py's `_DEAD_END_OFFER_RE`).

    Deliberately does NOT re-classify. Routing already had its chance at this
    message and got it wrong — that failure is the entire reason this path
    exists, and asking the same classifier the same question a second time
    would just buy the same answer. The model's own "ask me to search" IS the
    label, and it is a better one: it was produced with the whole
    conversation, the memory context, and its own knowledge in view.

    Everything downstream is the ordinary task path — same planner, same
    registry, same structural approval gate. `web_search` and `read_webpage`
    are READ tools, so a rescued turn cannot write anything; and if the
    planner drafts a write step anyway, it pauses for approval exactly as it
    would have on the front door. A rescue widens recall, never authority."""
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


# ================================================================ rendering

# Moved to app/agents/rendering.py in Phase 4 Part 5 (the background task
# runner renders the SAME words); the alias keeps this module's public shape.
_deterministic_text = deterministic_plan_text


# Moved to app/agents/summary.py (2026-07-12) so the agent HTTP endpoints —
# /api/agent/choose (clicked options) and /approve — can render the SAME
# completion words this streaming path does; the alias keeps this module's
# public shape for the tests that patch it here.
_summarize_completed = stream_completed_summary
