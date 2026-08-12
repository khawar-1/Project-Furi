"""
Furi OS — Question Self-Resolution Gate (planner hardening, 2026-07-10)

"Never ask the user something Furi can answer with its own tools."

Live incident: asked to "list the desktop and delete all the files from
phase3test", the draft paused on "What is the full path of the phase3test
folder?" — an options-free question the user rightly called Furi's own
job. Round 9 policed invented OPTIONS (_validated_question); nothing policed
the QUESTION. Prompt rule 3 ("a bare name means search-first") had been
ignored live four separate times, so — like the approval gate, the pre-flight
path guard, and option verification — this is structural, in code:

Before any options-free clarifying question is allowed to pause a plan, the
gate runs a REAL filesystem search (the registered search_files tool — read
level, audited in ActivityLog like every tool call) for each name the
question and the goal share:

  - something found, first attempt  → the question is REJECTED and the retry
    feedback hands the model the verified path(s): plan with them, don't ask.
  - something found, second attempt → the question goes through but carries
    the verified paths as clickable options — the user picks a truth instead
    of doing Furi's research.
  - nothing found / nothing to look for → the question passes unchanged
    (an honest question beats a guess — ask-don't-guess still stands).

The gate only ever performs READ searches; its results only feed planning.
Any write/destructive step a resolved question leads to still pauses at the
signature-based approval gate. A false-positive candidate name costs one
bounded search (SEARCH_MAX_SCANNED caps the walk) and at worst one LLM
retry — never a wrong action.

Questions that offer SEVERAL options are untouched: they are the legitimate
rule-11 class (ambiguous dates, several real matches), and their path-like
options were already verified by _validated_question. A question with
exactly ONE option is different (live run 2026-07-10: the draft guessed two
paths for phase3test, option verification dropped the invented one, and the
surviving single-option "what is the full path?" reached the user): when a
code search confirms that lone option is the ONLY match for the goal's name
on this computer, the question answers itself — it is rejected on the first
attempt with the answer in the feedback. A single option the search cannot
confirm (plain text, a date, one of several real matches) still passes:
auto-answering an unverified guess would be guessing.
"""
import re
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.schemas import PlanQuestion
from app.tools.registry import execute_tool

# Test injection (memory_tools.SESSION_FACTORY idiom): roots the verification
# search runs over. None → search_files' own default, the user's home.
SEARCH_ROOTS: Optional[list[str]] = None

MAX_NAMES = 3    # names verified per question — a question is about one thing
MAX_OPTIONS = 6  # verified paths offered as clickable options
_MIN_TOKEN_LEN = 3

# A string written as a concrete filesystem path: drive-rooted, UNC, or
# ~-rooted. Bare names ("phase3test") and plain text ("March 4") are not
# paths. Shared with the planner's option verification.
PATH_LIKE_RE = re.compile(r"^(?:[a-zA-Z]:[\\/]|\\\\|~(?:[\\/]|$))")

_TOKEN_RE = re.compile(r"[A-Za-z0-9._-]+")
_QUOTED_RE = re.compile(r"['\"‘’“”]([^'\"‘’“”]{2,120})['\"‘’“”]")
# A concrete path ANYWHERE in a text — stripped whole before tokenizing, so
# "delete C:\data\x" never yields "data" as a name to hunt for.
_PATH_SPAN_RE = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\|~[\\/])[^\s'\"?!,;]*")

# Words that can never be the NAME of the thing a question is about: English
# filler, question words, generic filesystem nouns, action verbs, and the
# well-known user folders (their locations are never genuinely unknown).
# Tuning this list is low-stakes by design: a false candidate costs one
# bounded read-only search, never a wrong action.
_STOPWORDS = frozenset("""
    the and are was were will would should could can may might must have has
    had does did doing done for from with without into onto about above below
    under over between during after before again then than that this these
    those there here where which what whose when whom how why who all any
    each every some more most other another such only own same very just also
    not but nor off out too you your yours yourself please furi jarvis want wants
    wanted need needs needed know knows knew tell tells told give gives gave
    mean means meant like exact exactly correct right wrong full complete
    entire whole located location locations place places stored sitting
    file files folder folders directory directories subfolder subfolders
    path paths name names named filename filenames extension extensions
    drive drives disk disks computer laptop machine system user users home
    item items thing things stuff one ones two three first second last new
    old current desktop documents downloads pictures videos music temp tmp
    cache trash recycle
    list lists listed listing delete deletes deleted deleting remove removes
    removed removing create creates created creating make makes made making
    move moves moved moving rename renames renamed renaming read reads
    reading open opens opened opening find finds found finding search
    searches searched searching locate locates located locating run runs
    running execute executes executed executing show shows showed showing
    get gets got put puts copy copies copied check checks checked clean
    cleans cleaned clear clears cleared add adds added set sets remind
    reminds reminded reminder reminders background task tasks provide
    provides provided specify specifies specified contents inside within
""".split())


def _tokens(text: str) -> list[str]:
    """Name-like tokens: lowercased, stopwords/short/numeric out, concrete
    paths stripped whole first. Dots, dashes and underscores stay inside a
    token so file names ("notes.txt", "my-project") survive as single
    candidates."""
    out: list[str] = []
    for raw in _TOKEN_RE.findall(_PATH_SPAN_RE.sub(" ", text or "")):
        token = raw.strip("._-").lower()
        if len(token) < _MIN_TOKEN_LEN or token in _STOPWORDS or token.isdigit():
            continue
        out.append(token)
    return out


def extract_shared_names(goal: str, question_text: str) -> list[str]:
    """The names a clarifying question is asking about — deterministically:
    strings the question shares with the GOAL (the user's own words ground
    the candidate; the LLM cannot steer the gate toward things the user
    never mentioned). Quoted spans first (they survive spaces: 'project
    notes'), then single shared tokens not already covered by a span."""
    goal_lower = (goal or "").lower()
    names: list[str] = []
    for span in _QUOTED_RE.findall(question_text or ""):
        cleaned = span.strip().strip("._-")
        lowered = cleaned.lower()
        if (
            len(lowered) >= _MIN_TOKEN_LEN
            and lowered in goal_lower
            and lowered not in _STOPWORDS
            and not lowered.isdigit()
            and not PATH_LIKE_RE.match(cleaned)
            and lowered not in names
        ):
            names.append(lowered)
    goal_tokens = set(_tokens(goal))
    for token in _tokens(question_text):
        if token in goal_tokens and token not in names and not any(
            token in existing for existing in names
        ):
            names.append(token)
    return names[:MAX_NAMES]


async def locate_name(
    name: str, db: AsyncSession, session_id: Optional[str]
) -> list[str]:
    """Verified paths matching a name, via the REAL registered search_files
    tool (read level — no approval needed; audited in ActivityLog, so the
    Timeline shows Furi looked for itself). Exact-name matches are
    preferred over substring hits ("phase3test" beats "phase3test_old.txt").
    Best-effort: any failure returns [] — verification must never break
    planning."""
    params: dict = {"query": name, "include_folders": True}
    if SEARCH_ROOTS:
        params["directories"] = list(SEARCH_ROOTS)
    try:
        result = await execute_tool("search_files", params, db, session_id=session_id)
    except Exception as e:  # pragma: no cover — execute_tool already shields
        logger.warning(f"Question gate search for '{name}' crashed: {e}")
        return []
    if not result.success or not isinstance(result.output, dict):
        return []
    paths = [
        str(m.get("path"))
        for m in result.output.get("matches") or []
        if isinstance(m, dict) and m.get("path")
    ]
    exact = [p for p in paths if PurePath(p).name.lower() == name.lower()]
    return (exact or paths)[:MAX_OPTIONS]


@dataclass
class Resolution:
    """The gate's verdict on one clarifying question.
    action: "pass"   — let the question through unchanged
            "reject" — retry the LLM call; `feedback` carries the found paths
            "answer" — let the question through WITH `options` (verified)"""

    action: str
    feedback: Optional[str] = None
    options: list[str] = field(default_factory=list)


async def self_resolve(
    question: PlanQuestion,
    goal: str,
    attempt: int,
    db: AsyncSession,
    session_id: Optional[str],
    cache: dict[str, list[str]],
) -> Resolution:
    """Try to answer a clarifying question before the user ever sees it.
    `cache` is per-planning-call (name → found paths) so the retry after a
    rejection never repeats the same filesystem walk."""
    if len(question.options) > 1:
        return Resolution("pass")  # legitimate rule-11 choice — not this class
    names = extract_shared_names(goal, question.text)
    if not names:
        return Resolution("pass")
    if question.options:
        # Exactly ONE option: a question with a single possible answer
        # answers itself — but only when a real search confirms the option
        # is the ONLY match for the goal's name (anything less would be
        # auto-answering a guess).
        if attempt != 1:
            return Resolution("pass")
        single = question.options[0].strip().strip("'\"")
        for name in names:
            if name not in cache:
                cache[name] = await locate_name(name, db, session_id)
            if len(cache[name]) == 1 and cache[name][0].lower() == single.lower():
                logger.info(
                    f"Question gate: rejected a single-option question — "
                    f"'{single}' is the only match for '{name}', so it IS "
                    "the answer"
                )
                return Resolution(
                    "reject",
                    feedback=(
                        "your question offers exactly ONE option, and a "
                        f"search verified that '{single}' is the only match "
                        f"for '{name}' on this computer — a question with a "
                        "single possible answer answers itself. Use that "
                        "exact path and return the full plan steps now. Only "
                        "ask a question when the user must choose between "
                        "SEVERAL real candidates or provide information no "
                        "search can supply."
                    ),
                )
        return Resolution("pass")
    found: dict[str, list[str]] = {}
    for name in names:
        if name not in cache:
            cache[name] = await locate_name(name, db, session_id)
        if cache[name]:
            found[name] = cache[name]
    if not found:
        return Resolution("pass")  # genuinely not findable — an honest question
    options: list[str] = []
    for paths in found.values():
        for path in paths:
            if path not in options:
                options.append(path)
    options = options[:MAX_OPTIONS]
    if attempt == 1:
        listing = "; ".join(
            f"'{name}' → " + ", ".join(paths) for name, paths in found.items()
        )
        logger.info(
            f"Question gate: rejected an options-free question — search found {listing}"
        )
        return Resolution(
            "reject",
            feedback=(
                "your question asks the user for something a filesystem search "
                f"can answer. A search was already run FOR you and found: {listing}. "
                "These paths are verified to exist on this computer. NEVER ask "
                "the user where a file or folder is or for its full path. If "
                "that is what you needed, use these exact verified paths and "
                "return the full plan steps now. If several of them are "
                "plausible targets for ONE write/delete action, return the "
                "question again offering EXACTLY these real paths as options. "
                "Only re-ask a question that needs information no search can "
                "provide (a new name to use, a choice of wording, an ambiguous "
                "date)."
            ),
        )
    logger.info(
        f"Question gate: re-asked question gains {len(options)} verified option(s)"
    )
    return Resolution("answer", options=options)
