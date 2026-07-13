"""
Jarvis OS — Same-Named Folder Disambiguation (planner hardening, 2026-07-12)

"A well-known folder name is not a location — it is a location on EVERY drive."

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
and the placeholder resolver: BEFORE a read step scopes itself to the DEFAULT
home copy of a well-known folder the user named WITHOUT a drive, probe the
drives for duplicates.
  - two or more real folders with that name  → pause AWAITING_CHOICE and let
    the user pick the real one (the same question/option machinery every other
    "several matches" case uses; the options are verified-existing paths).
  - exactly one, and it is NOT the home copy the step was heading for → fix the
    step's directory in code (the home copy the model guessed doesn't exist).
  - zero, or only the home copy → leave the step untouched.

Grounding: we only disambiguate a folder the USER named by a bare name. If the
goal (or the user's later answer) carries an explicit drive/path qualifier
("D:\\Downloads", "the downloads on d drive"), the user was specific.

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

# The folders that reliably exist under HOME and are also, on many machines,
# duplicated at a drive root (D:\Downloads) or on a second data drive. Kept
# deliberately small — this is exactly the set whose home location the system
# treats as canonical (mirrors question_gate._STOPWORDS' well-known folders).
WELL_KNOWN_FOLDERS = frozenset({
    "downloads", "desktop", "documents", "pictures", "videos", "music",
})

# The read tools whose scope is a single folder parameter. The multi-root
# search_files "directories" form is deliberately excluded: if the model
# listed explicit roots it was being specific, not defaulting to home.
_DIR_KEY = {"search_files": "directory", "list_directory": "path"}

# An explicit drive/path reference anywhere in the user's own words: a
# drive-rooted path (C:\, D:/), or a spoken "d drive" / "drive d". Its presence
# means the user already chose a location — the guard must not override it.
_DRIVE_QUALIFIER_RE = re.compile(
    r"[A-Za-z]:[\\/]|\b[A-Za-z]\s*drive\b|\bdrive\s*[A-Za-z]\b", re.IGNORECASE
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


def _user_was_explicit(goal: str, user_answers) -> bool:
    corpus = " ".join([goal or ""] + [str(a) for a in (user_answers or [])])
    return bool(_DRIVE_QUALIFIER_RE.search(corpus))


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
    """The guard's verdict on one directory-scoped step.
    action: "ask"        — pause AWAITING_CHOICE; `paths` are the real options
            "substitute" — set the step's `key` to paths[0] in code"""

    action: str
    key: str
    name: str
    paths: list[str]


def detect(step, goal: str, user_answers) -> Optional[FolderResolution]:
    """Whether a read step is about to search the DEFAULT home copy of a
    well-known folder the user named without a drive, while other copies exist.
    Best-effort: returns None (no action) on anything uncertain or on error."""
    try:
        key = _DIR_KEY.get(step.tool)
        if key is None:
            return None
        # The multi-root form is an explicit choice — leave it alone.
        if step.tool == "search_files":
            dirs = step.parameters.get("directories")
            if isinstance(dirs, list) and dirs:
                return None
        raw = str(step.parameters.get(key) or "").strip()
        if not raw or _PLACEHOLDER_MARK in raw.upper():
            return None

        resolved = _resolve_against_home(raw)
        name = resolved.name
        if name.lower() not in WELL_KNOWN_FOLDERS:
            return None
        # The user's own words pin a CONCRETE existing copy of this folder
        # (a clicked option, a typed full path): enforce it in code. Trusting
        # the revise LLM to fill the picked option failed live 2026-07-12 —
        # it kept the home copy and the stood-down guard let the wrong
        # search run. A step already targeting the chosen copy is correct.
        chosen = _explicit_folder_choice(name, goal, user_answers)
        if chosen is not None:
            if _normkey(chosen) != _normkey(resolved):
                return FolderResolution("substitute", key, name.lower(), [chosen])
            return None
        # Only intervene when the step is heading for the home copy — a step
        # already scoped to a specific drive is the model being correct.
        if _normkey(resolved.parent) != _normkey(_home()):
            return None
        # Ground in the user's own words: they must have named the folder, and
        # must NOT have qualified it with a drive (a vague qualifier like
        # "the one on d drive" — no concrete path — still stands down).
        if _user_was_explicit(goal, user_answers):
            return None
        if not _named_in_words(name, goal, user_answers):
            return None

        paths = find_duplicate_folders(name)
        if len(paths) >= 2:
            return FolderResolution("ask", key, name.lower(), paths)
        if len(paths) == 1 and _normkey(paths[0]) != _normkey(resolved):
            return FolderResolution("substitute", key, name.lower(), paths)
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
