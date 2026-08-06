"""
Jarvis OS — Same-Named Folder Disambiguation (planner hardening, 2026-07-12)

"A folder name is not a location — it is a location on EVERY drive."

Live incident: "find all PDF files in downloads, then tell me how many there
are and what the largest one is called" — the user meant D:\\Downloads, but the
plan searched C:\\Users\\DELL\\Downloads (the home copy), found no PDFs, and
failed. Three separate places quietly assumed "downloads" has exactly ONE
location, under HOME:
  - file_tools._resolve_path anchors a bare "downloads" to Path.home().
  - Plan rule 3 ("bare name → search first") treats the well-known folders as
    already-known, so the model passes the home path without searching.
  - question_gate._STOPWORDS lists downloads/desktop/documents as names whose
    "locations are never genuinely unknown", switching the disambiguation
    machinery OFF for exactly these words.

So even though the planner is TOLD the available drives (C:\\, D:\\), nothing
made it consider that a second "Downloads" might live on another one.

This module is the structural fix, in the mold of the pre-flight path guard
and the placeholder resolver: BEFORE a step operates INSIDE the DEFAULT home
copy of a folder the user named WITHOUT a drive, probe the drives for
duplicates.
  - two or more real folders with that name  → pause AWAITING_CHOICE and let
    the user pick the real one (the same question/option machinery every other
    "several matches" case uses; the options are verified-existing paths).
  - exactly one, and it is NOT the home copy the step was heading for → fix the
    step's path in code (the home copy the model guessed doesn't exist).
  - zero, or only the home copy → leave the step untouched.

⚠️ 2026-08-01 — THE GUARD WAS READ-ONLY, AND THE COST LANDED ON THE WRITE SIDE.
"move all pdf files from 'pdfff2' to downloads" moved 85 PDFs into
C:\\Users\\DELL\\Downloads; the user meant D:\\Downloads and was never asked.
`detect()` WAS called on that `move_files` step and returned None on its first
line, because the tool map held only search_files and list_directory. Every
other precondition was already satisfied — the goal named "downloads" bare, no
drive qualifier, the drafted path's parent was home, and D:\\Downloads existed.
The guard would have asked; it was simply never consulted for a write. Which
made the coverage exactly backwards: present on the branch where being wrong
finds nothing, absent from the branch where being wrong relocates 85 files.

Two things changed that day, and they are the same lesson twice:

  1. `_FOLDER_PARAMS` covers THE FOLDER A STEP OPERATES INSIDE — a move's
     destination and a create's parent, not just a read's scope — and
     `_EXEMPT_PATH_PARAMS` states the ones deliberately left out, with a test
     that fails when a new file tool matches neither. See 2026-07-30's
     `registry.mutates`: "when you add a preferred form of an operation, every
     predicate that keys on the old form's NAME is now a hole."

  2. The hardcoded six-name list stopped being the gate. It never carried the
     meaning — `_normkey(container.parent) == _normkey(_home())` does, and says
     it exactly: this path is what you get when a bare name the user said is
     anchored under home. `find_duplicate_folders` only ever reports folders
     that ACTUALLY EXIST, so dropping the list cannot invent an ambiguity, only
     surface one that is really there ("move these to projects" with
     C:\\Users\\DELL\\projects and D:\\projects both present was silently wrong
     in precisely the same way).

Grounding: we only disambiguate a folder the USER named by a bare name. If the
goal (or the user's later answer) carries an explicit drive/path qualifier FOR
THAT FOLDER ("D:\\Downloads", "the downloads on d drive"), the user was
specific. "For that folder" is load-bearing and was learned the hard way — see
_user_was_explicit: a qualifier attached to a DIFFERENT folder (an answer of
"C:\\Users\\DELL\\Desktop" to an earlier question) used to stand this guard
down for "downloads" too, and the plan then searched the wrong Downloads and
reported it empty.

When that qualifier is a CONCRETE existing path for this same folder name (a
clicked option is exactly that), the choice is ENFORCED in code: a step still
heading for a different same-named copy gets the user's path substituted.
Standing down and trusting the revise LLM to fill the picked option was the
original design — live failure 2026-07-12 (verification run): the user picked
D:\\Downloads, the revise round kept C:\\Users\\DELL\\Downloads, the stood-down
guard let it run, and Jarvis reported "no PDF files" from the wrong folder.
A vaguer qualifier ("the one on d drive") still just stands the guard down.
Either way the answer→re-plan loop terminates: a substituted (or obeyed) step
targets the chosen path, so the next detect() pass returns no action.

The probe is a handful of os.path.isdir() checks (home\\<Name> plus <drive>\\<Name>
for each drive) — no filesystem walk, no scan cap, essentially free. Everything
is best-effort: any failure returns "no action", never a broken plan.
"""
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from loguru import logger

from app.agents.schemas import PlanQuestion

_PLACEHOLDER_MARK = "PENDING:"

# ⚠️ NOT a gate. Until 2026-08-01 this list decided which folders were
# disambiguated at all, and that was wrong twice over: it made the guard blind
# to every name outside it, and it never carried the meaning anyway (the
# "anchored directly under home" test does — see the module docstring). Its
# ONLY remaining job is as a vocabulary for _user_was_explicit: recognising
# that a vague drive steer is aimed at a DIFFERENT folder ("the desktop one on
# d drive"). Named _COMMON_ rather than _WELL_KNOWN_ so it can never be
# mistaken for a gate again.
_COMMON_FOLDER_NAMES = frozenset({
    "downloads", "desktop", "documents", "pictures", "videos", "music",
})

# The folder each step operates INSIDE, by tool → (parameter, mode).
#   "self"   — the parameter names the folder itself (a read's scope, a move's
#              destination).
#   "parent" — the parameter names something INSIDE it, so the folder is the
#              parameter's parent (create_file "downloads/notes.txt",
#              create_folder "downloads/archive").
# The multi-root search_files "directories" form is deliberately excluded in
# detect(): if the model listed explicit roots it was being specific, not
# defaulting to home.
_FOLDER_PARAMS: dict[str, tuple[str, str]] = {
    "search_files": ("directory", "self"),
    "list_directory": ("path", "self"),
    "move_file": ("destination", "self"),
    "move_files": ("destination", "self"),
    "create_file": ("path", "parent"),
    "create_folder": ("path", "parent"),
    "run_command": ("working_directory", "self"),
    # COVERED, not exempt: "open downloads" is the LLM writing a bare name the
    # user spoke into a path anchored under HOME, with nothing on disk
    # consulted — the exact case this guard exists for. Mode "self" is right
    # for both shapes it accepts: a bare folder IS the container, while a file
    # path's parent is not HOME, so the guard correctly stands down there.
    "open_folder": ("path", "self"),
}

# Path parameters deliberately NOT disambiguated, and why. The split is by WHO
# AUTHORED THE PATH, and it is the whole justification for the asymmetry above:
#
#   covered  — the path is written by the LLM out of the user's words. A move's
#              destination and a create's location are chosen at draft time,
#              from a name the user spoke, with nothing on disk consulted. That
#              is precisely when "downloads" can mean two places.
#   exempt   — the path is a CONCRETE file an earlier READ step returned (rule
#              3's search-first, the placeholder resolver's expansion). Its
#              folder was already settled when that read ran — re-asking here
#              would interrogate the user about a choice they already made, on
#              every plan whose search legitimately ran in the home copy.
#
# Kept explicit rather than implicit so `test_every_path_param_is_covered_or_
# exempt` can enumerate the registry and fail loudly when a new file tool
# matches neither map — that test found `read_file.path` the first time it ran.
_EXEMPT_PATH_PARAMS: frozenset[tuple[str, str]] = frozenset({
    ("read_file", "path"),
    ("delete_file", "path"),
    ("rename_file", "path"),
    ("move_file", "source"),
    ("move_files", "sources"),
    ("delete_files", "paths"),
    ("execute_script", "script_path"),
})

# A drive/path reference in the user's own words, in its two shapes. They are
# kept SEPARATE because they carry different amounts of information, and
# conflating them is what caused the 2026-07-30 incident (see
# _user_was_explicit): a CONCRETE path names one specific folder, while a
# SPOKEN drive reference is a bare steer that names none.
_DRIVE_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s\"',;]*")
_SPOKEN_DRIVE_RE = re.compile(
    r"\b[A-Za-z]\s*drive\b|\bdrive\s*[A-Za-z]\b", re.IGNORECASE
)

# Test-injection seams (question_gate.SEARCH_ROOTS idiom): None → the real
# machine. Tests point these at tmp dirs so the suite never probes real drives.
HOME: Optional[Path] = None
DRIVES: Optional[list[str]] = None


def _home() -> Path:
    return HOME if HOME is not None else Path.home()


def _drives() -> list[str]:
    if DRIVES is not None:
        return list(DRIVES)
    from app.agents.planner import _available_drives  # runtime import — no cycle

    return _available_drives()


def _normkey(path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _resolve_against_home(raw: str) -> Path:
    """Anchor a bare/relative name to the (injectable) home, mirroring
    file_tools._resolve_path but against _home() so tests are hermetic. No
    .resolve() — we only need where the path POINTS, not to touch the fs."""
    text = os.path.expandvars(raw.strip())
    if text.startswith("~"):
        text = str(_home()) + text[1:]
    path = Path(text)
    if not path.is_absolute():
        path = _home() / path
    return path


def find_duplicate_folders(name: str) -> list[str]:
    """Existing folders named `name` across the home directory and every drive
    root — deduplicated by OS-normalized path, home first. A pure stat probe of
    a few fixed candidate locations, never a walk."""
    candidates = [_home() / name]
    for drive in _drives():
        candidates.append(Path(drive) / name)
    seen: set[str] = set()
    out: list[str] = []
    for cand in candidates:
        try:
            if not cand.is_dir():
                continue
        except OSError:
            continue
        key = _normkey(cand)
        if key not in seen:
            seen.add(key)
            out.append(str(cand))
    return out


def _trim_path(raw: str) -> str:
    return raw.strip().rstrip("\\/").rstrip(".,;:!?)\"'")


def _well_known_named(text: str) -> set[str]:
    """The common folder names mentioned as bare words in `text`."""
    return {
        n for n in _COMMON_FOLDER_NAMES
        if re.search(rf"(?i)\b{re.escape(n)}\b", text)
    }


def _user_was_explicit(name: str, goal: str, user_answers) -> bool:
    """Whether the user's own words already pin a location FOR THIS FOLDER.

    ⚠️ This predicate is folder-SCOPED, and that scoping is the whole point.
    Live incident 2026-07-30: "create folder 'pdff' on dashboard and move all
    the pdf files from downloads in it" — the typo made the plan ask which
    folder was meant, the user clicked the verified option
    ``C:\\Users\\DELL\\Desktop``, and that answer landed in ``user_answers``.
    The old implementation joined goal+answers into ONE string and asked "is
    there a drive qualifier anywhere?"; the ``C:\\`` in the DESKTOP answer
    matched, the guard stood down for DOWNLOADS, the plan searched the empty
    home copy instead of D:\\Downloads, found 0 PDFs and reported "nothing to
    do" — with 85 PDFs sitting in D:\\Downloads.

    So an answer about one folder silently disarmed the disambiguation of a
    DIFFERENT one — and because every clarifying question whose options are
    verified paths puts a drive-qualified string into ``user_answers``, ANY
    path question in a plan disarmed this guard for the rest of that plan.
    ``_explicit_folder_choice`` (right above) was already folder-aware; this
    one was not, so the two disagreed about what "the user was explicit" meant
    and the blind one won by returning first.

    Each corpus entry is judged SEPARATELY — joining is what let one text's
    qualifier answer for another's folder. Within an entry:

    - a CONCRETE drive-rooted path stands the guard down only when it is a
      path to THIS folder (``D:\\Downloads`` for "downloads"); a path to
      something else (``C:\\Users\\DELL\\Desktop``, ``D:\\Projects``) says
      nothing about where "downloads" is;
    - a SPOKEN drive reference ("the one on d drive") names no folder at all,
      so it is taken as a steer for the folder in question — UNLESS that text
      names a different well-known folder ("the desktop one on d drive").

    Keeping the spoken case standing down is load-bearing for TERMINATION: it
    is what stops a vague answer from re-triggering the same question forever
    (the module docstring's "either way the answer→re-plan loop terminates").
    """
    lname = name.lower()
    for text in [goal or ""] + [str(a) for a in (user_answers or [])]:
        if not text.strip():
            continue
        vague = False
        for raw in _DRIVE_PATH_RE.findall(text):
            base = Path(_trim_path(raw)).name.lower()
            if base == lname:
                return True  # an explicit path to THIS folder
            if not base:
                vague = True  # a bare drive root ("D:\") names no folder
        if _SPOKEN_DRIVE_RE.search(text):
            vague = True
        if vague:
            named = _well_known_named(text)
            if not named or lname in named:
                return True
    return False


# A drive-rooted path embedded in a sentence — conservative (stops at
# whitespace/quotes), because free text gives no reliable path boundary. A
# clicked option arrives as the WHOLE answer, which is handled separately and
# may contain spaces.
_EMBEDDED_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s\"',;]+")


def _explicit_folder_choice(name: str, goal: str, user_answers) -> Optional[str]:
    """The ONE concrete folder the user's own words pin for `name`: a
    drive-rooted EXISTING directory whose basename is this well-known name.
    Answers are checked whole-string first (a clicked option IS the path,
    spaces and all), then both answers and goal are scanned for embedded
    paths. Several different matches → None: code never picks between them.
    Grounded exclusively in goal + user answers — memory, web, or email
    content can never plant a location here."""
    texts = [str(a) for a in (user_answers or [])] + [goal or ""]
    found: dict[str, str] = {}

    def consider(raw: str) -> None:
        cand = raw.strip().rstrip("\\/").rstrip(".,;:!?)\"'")
        if not cand:
            return
        path = Path(cand)
        try:
            if path.name.lower() != name.lower() or not path.is_dir():
                return
        except OSError:
            return
        found.setdefault(_normkey(path), str(path))

    for text in texts:
        whole = text.strip().strip("\"'")
        if re.match(r"^[A-Za-z]:[\\/]", whole):
            consider(whole)
        for token in _EMBEDDED_PATH_RE.findall(text):
            consider(token)
    if len(found) == 1:
        return next(iter(found.values()))
    return None


def _named_in_words(name: str, goal: str, user_answers) -> bool:
    corpus = " ".join([goal or ""] + [str(a) for a in (user_answers or [])])
    return re.search(rf"(?i)\b{re.escape(name)}\b", corpus) is not None


@dataclass
class FolderResolution:
    """The guard's verdict on one step whose work happens inside a folder.

    action:   "ask"        — pause AWAITING_CHOICE; `paths` are the real options
              "substitute" — assign `value` to the step's `key` in code
    key:      the step parameter to rewrite
    name:     the ambiguous folder's name, lowercased (for the question text)
    paths:    the real existing folders of that name — the question's options
    value:    what `key` becomes on a substitute. NOT paths[0]: in "parent"
              mode the parameter names something INSIDE the folder, so the
              basename has to be carried over (downloads/notes.txt →
              D:\\Downloads\\notes.txt). The planner assigns one string and
              stays ignorant of modes.
    mutating: does this step CHANGE anything? Decides which budget the ask is
              charged to — read from the registry, never a name list.
    """

    action: str
    key: str
    name: str
    paths: list[str]
    value: str = ""
    mutating: bool = False


def _mutating(tool: str) -> bool:
    """Runtime import, the _drives() idiom — the registry owns this fact."""
    try:
        from app.tools.registry import mutates

        return mutates(tool)
    except Exception:  # pragma: no cover — unknown ⇒ the strict default
        return True


def detect(step, goal: str, user_answers) -> Optional[FolderResolution]:
    """Whether a step is about to operate inside the DEFAULT home copy of a
    folder the user named without a drive, while other copies exist.
    Best-effort: returns None (no action) on anything uncertain or on error."""
    try:
        entry = _FOLDER_PARAMS.get(step.tool)
        if entry is None:
            return None
        key, mode = entry
        # The multi-root form is an explicit choice — leave it alone.
        if step.tool == "search_files":
            dirs = step.parameters.get("directories")
            if isinstance(dirs, list) and dirs:
                return None
        raw = str(step.parameters.get(key) or "").strip()
        if not raw or _PLACEHOLDER_MARK in raw.upper():
            return None

        resolved = _resolve_against_home(raw)
        # The folder the work happens INSIDE — the parameter itself, or its
        # parent when the parameter names something within it.
        container = resolved if mode == "self" else resolved.parent
        name = container.name
        if not name:
            return None  # a drive root has no name to disambiguate

        def verdict(action: str, paths: list[str]) -> FolderResolution:
            # Rebuild the PARAMETER, not the container: in parent mode the
            # basename rides along (downloads/notes.txt → D:\Downloads\notes.txt).
            value = paths[0] if mode == "self" else str(Path(paths[0]) / resolved.name)
            return FolderResolution(
                action, key, name.lower(), paths, value, _mutating(step.tool)
            )

        # The user's own words pin a CONCRETE existing copy of this folder
        # (a clicked option, a typed full path): enforce it in code. Trusting
        # the revise LLM to fill the picked option failed live 2026-07-12 —
        # it kept the home copy and the stood-down guard let the wrong
        # search run. A step already targeting the chosen copy is correct.
        chosen = _explicit_folder_choice(name, goal, user_answers)
        if chosen is not None:
            if _normkey(chosen) != _normkey(container):
                return verdict("substitute", [chosen])
            return None
        # Only intervene when the step is heading for the home copy — a step
        # already scoped to a specific drive is the model being correct. This
        # test is also what replaced the old hardcoded name list: a path whose
        # parent is home is exactly what home-anchoring a bare name produces.
        if _normkey(container.parent) != _normkey(_home()):
            return None
        # Ground in the user's own words: they must have named the folder, and
        # must NOT have qualified it with a drive (a vague qualifier like
        # "the one on d drive" — no concrete path — still stands down).
        if _user_was_explicit(name, goal, user_answers):
            return None
        if not _named_in_words(name, goal, user_answers):
            return None

        paths = find_duplicate_folders(name)
        if len(paths) >= 2:
            return verdict("ask", paths)
        if len(paths) == 1 and _normkey(paths[0]) != _normkey(container):
            return verdict("substitute", paths)
        return None
    except Exception as e:  # pragma: no cover — never break the planner
        logger.warning(f"Folder disambiguation crashed (skipping): {e}")
        return None


def build_question(res: FolderResolution) -> PlanQuestion:
    """A clarifying question whose options are the verified duplicate folders —
    real existing paths, so they pass the same bar as _fallback_question's."""
    return PlanQuestion(
        text=(
            f"There are {len(res.paths)} folders named '{res.name}' on this "
            "computer — which one did you mean? Pick one, or answer in your "
            "own words."
        ),
        options=list(res.paths),
    )


def ambiguity_note(res: FolderResolution) -> str:
    """The last-resort disclosure, for when the hand-off budget is spent and the
    step is about to run on the guessed copy anyway. It is never a substitute
    for asking — it just refuses to let the approval card imply the choice was
    unambiguous when code knows it was not."""
    listed = "\n".join(f"    {p}" for p in res.paths)
    return (
        f"  NOTE: {len(res.paths)} folders named '{res.name}' exist and the "
        f"goal did not say which:\n{listed}"
    )
