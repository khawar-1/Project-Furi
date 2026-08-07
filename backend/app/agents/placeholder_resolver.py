"""
Jarvis OS — Deterministic Placeholder Resolution (planner hardening, 2026-07-10)

"PENDING: ..." placeholders are the planner's DESIGNED data-flow mechanism
(plan rule 3): a step that consumes paths an earlier step will discover
carries a placeholder instead of a guessed path. Until now, REACHING one at
execution time was treated as a step FAILURE that spent an LLM replan round
to fill it in — the mechanism working exactly as designed burned replan
budget and provider quota, showed the user spurious red "failed" steps, and
let a plan pause for approval on a step reading "PENDING: .txt file paths".
Live incident 2026-07-10: two placeholder "failures" → two replans (one of
which re-ran an already-completed search) → the second replan call hit the
provider's daily rate limit → the whole plan FAILED, even though every path
it needed was already sitting in a completed step's results.

This module fills placeholders IN CODE from the plan's own completed step
results — the same data the revise LLM would have been shown:

- A single-target parameter (delete_file/read_file/rename_file path,
  move_file source, execute_script script_path) EXPANDS into one concrete
  step per file the most recent path-producing step found — rule 4 ("one
  file per step") made concrete. Each expanded step is a fresh PlanStep with
  a fresh signature, so every write/destructive action the user approves
  names its exact real path. A search/list that found NOTHING expands to
  zero steps — honestly "nothing to do", never a failure.
- ...UNLESS there are many, in which case move_file/delete_file become ONE
  move_files/delete_files step carrying the explicit list (2026-07-29). One
  step per file cannot express bulk work AT ALL: with MAX_PLAN_STEPS=30 an
  85-PDF move blew the cap, this module returned None, the step failed with
  "still contain unresolved 'PENDING:' placeholders", and the plan reported
  "Done — 2 step(s) completed" having moved nothing. The batch step keeps
  every property the per-file form had — fresh signature, the real paths in
  its parameters, the same structural approval gate — and removes the
  ceiling. A bulk MUTATION additionally defaults to the searched folder's OWN
  files (partition_by_depth) and refuses a source search that hit its result
  cap, because acting on a knowingly partial set and reporting success is the
  same defect wearing different clothes.
- A folder parameter (search_files directory, list_directory path,
  run_command working_directory) substitutes when the completed results pin
  exactly ONE candidate: a single folder whose name appears in the
  placeholder text, or the only folder found. Several plausible folders stay
  unresolved — code never picks between targets.
- A recipient parameter (send_email / create_email_draft "to", Phase 5
  Part 3) substitutes the email a completed lookup_contact step RESOLVED —
  the designed "email Jamil" data flow (lookup first, "PENDING: Jamil's
  email address" in the send step). Only when the results pin exactly ONE
  address: a resolved contact whose name appears in the placeholder text,
  or the only resolved contact at all. The substituted step gets a fresh
  signature and a regenerated action_detail, so the approval card names the
  real address. Addresses come EXCLUSIVELY from lookup_contact outputs here
  — never from read email content — which keeps the planner's recipient-
  grounding rule true on the code path too.

⚠️ ONE PART OF THIS MODULE IS NOT ABOUT PLACEHOLDERS AT ALL. The bulk-mutation
scope rules — `mutation_scope`, and `scope_concrete_list` / `apply_list_scope`
on top of it — apply to a batch file list HOWEVER it was authored, including
one a revise/refine round wrote out concretely with no `PENDING:` anywhere.
They live here because they belong with `partition_by_depth` and the pool
logic, but `planner._execute_node` calls them on every pass, independently of
whether a placeholder was ever present. That independence IS the fix: from
2026-07-29 to 2026-08-03 they were reachable only through `resolve()`, and
scripts/plan_bench.py reproduced the consequence 5/5 runs.

Resolution is CONSERVATIVE: anything ambiguous returns None and the existing
LLM replan path takes over (unchanged behavior). Extension tokens inside a
placeholder ("PENDING: .txt file paths") filter the candidate files — except
when the goal explicitly asks for ALL files and never names that extension:
the goal's own words outrank a filter the model invented (the same
goal-fidelity rule the planner's scope guard enforces on drafted steps —
long-term memory is data, and data must never narrow the user's request).
"""
import os
import re
from pathlib import PurePath
from typing import Any, NamedTuple, Optional

from loguru import logger

from app.agents.schemas import AgentPlan, PlanStep, StepStatus
from app.core.base_tool import PermissionLevel

_PLACEHOLDER_MARK = "PENDING:"

# Single-file parameters: the step is a per-file template that expands into
# one concrete step per found file (plan rule 4).
_FILE_PARAMS = {
    "delete_file": "path",
    "read_file": "path",
    "rename_file": "path",
    "move_file": "source",
    "execute_script": "script_path",
}

# Bulk file work is ONE step, not N (2026-07-29). A per-file template whose
# pool is larger than this becomes a single batch step carrying the explicit
# list. The threshold is a product decision with a name, deliberately NOT
# `max_new`: keying the plan's SHAPE on a cap that moves with plan length made
# 27 files render as 27 approval rows and 29 as one, on a boundary nobody
# chose. Below it, per-file steps keep their individual narration ticks.
BATCH_EXPAND_MAX = 8

# Per-file template → (batch tool, its list parameter). rename_file has NO
# batch form by design (every rename needs its own new_name) and read_file has
# none either (85 file bodies would blow every rendering cap).
_BATCH_TOOLS = {
    "move_file": ("move_files", "sources"),
    "delete_file": ("delete_files", "paths"),
}

# The batch tools' own list parameters — for when the LLM drafts move_files
# directly with a PENDING placeholder instead of the singular form, and (since
# 2026-08-03) for scoping a list the model wrote out concretely.
_LIST_PARAMS = {"move_files": "sources", "delete_files": "paths"}

# A mutating tool that takes a list of strings but NOT a list of files. Written
# down rather than implied, so `test_every_bulk_list_param_is_covered_or_exempt`
# can walk the registry and fail when a new one belongs to neither map — the
# shape that caught `read_file.path` on its first run in folder_resolver.
_EXEMPT_LIST_PARAMS = {("browse_commit", "allowed_origins")}

def _mutates(tool: str) -> bool:
    """Does this template CHANGE the filesystem? Two rules ride on the answer:
    a truncated source search is refused (acting on a knowingly partial set and
    reporting success is the defect this module exists to remove), and the pool
    defaults to the searched folder's OWN files rather than everything nested
    beneath it.

    ⚠️ READ FROM THE REGISTRY, never a hand-kept name list. This was a literal
    list — `{"move_file", "delete_file", "rename_file"}` — and it silently
    omitted the PLURAL tools, so both rules switched off for exactly the shape
    the same round's rule 4 had just started telling the planner to draft.
    Live 2026-07-30: "move all the pdf files from downloads" drafted
    `move_files(sources="PENDING: …")`, `_fill_list` ran a pool with no
    partition, and all 85 PDFs moved — including 8 out of subfolders, one from
    inside a source repo's `frontend/src/Assets`. The guard applied to the
    shape the planner used BEFORE the change and not to the one it encourages
    AFTER, which is why the end-to-end test (drafted the singular form) passed
    while the real run did not. The registry already owns permission levels —
    the same reason `_batch_step` reads them there instead of copying them.

    Unknown tool → treat as a mutation: the strict rules are the safe default.

    Promoted to `registry.mutates` on 2026-08-01, when folder_resolver became
    the second caller — one fact, one home, for exactly the reason above.
    """
    from app.tools.registry import mutates  # runtime import — no cycle

    return mutates(tool)

# The user asking for depth explicitly. Their own words outrank the top-level
# default, exactly as `extension_grounded` lets the goal outrank a filter.
_RECURSIVE_CUE_RE = re.compile(
    r"\b(?:sub[- ]?folders?|sub[- ]?director(?:y|ies)|recursive(?:ly)?|nested|"
    r"everywhere|all the way down|includ\w*\s+sub\w+)\b",
    re.IGNORECASE,
)

# Single-folder parameters: substituted only when the completed results
# identify exactly one candidate.
_DIR_PARAMS = {
    "search_files": "directory",
    "list_directory": "path",
    "run_command": "working_directory",
}

# ⚠️ open_folder is SEPARATE from _DIR_PARAMS, and the difference is the point.
# Live incident 2026-08-06: "open folder 'fomi'" drafted exactly the flow the
# plan rules ask for — search_files, then open_folder("PENDING: full path of
# the folder named 'fomi' found by the search") — and `resolve()` had NO branch
# for the tool, so it fell through to `return None`, the step FAILED on
# "unresolved 'PENDING:' placeholders", and the LLM replan path took over. The
# audit puts the cost at **93 seconds of planning** for one folder: a burned
# replan round, a clarifying question the user had to answer, and an approval
# pause on the replacement step. The tool was added to six maps that day; this
# was the seventh and it was missed, which is the defect class this file has
# now recorded four times ("a second copy of a list is a hole").
#
# It cannot simply join _DIR_PARAMS, because open_folder ALSO accepts a FILE
# path ("show me where my resume lives" → open its containing folder, plan
# rule 9). Folder-only substitution would leave that flow dead-ending exactly
# as the incident did. See _substitute_open_target.
_OPEN_TARGET_PARAMS = {"open_folder": "path"}

# Recipient parameters: substituted only from a lookup_contact result that
# pins exactly one address (Phase 5 Part 3). cc placeholders stay on the LLM
# path — conservative, like everything here.
_EMAIL_TO_PARAMS = {
    "send_email": "to",
    "create_email_draft": "to",
}

# Event-id parameters: substituted only from a calendar read (list_events /
# find_events) that pins exactly one event (Phase 5 Part 4). Mirror of the
# recipient path — ids come EXCLUSIVELY from read-step results, so the
# planner's event-id grounding rule holds on the code path too.
_EVENT_ID_PARAMS = {
    "update_event": "event_id",
    "delete_event": "event_id",
}
_CALENDAR_READ_TOOLS = ("list_events", "find_events")

# Home entity ids: the designed flow is list_devices → set_device_state(
# entity_id="PENDING: the kitchen lights"). Without this branch every such plan
# burns an LLM replan on the mechanism working as designed (the round-14
# incident, and its email/calendar repeats). Ids come EXCLUSIVELY from home read
# results, so the planner's entity-id grounding rule holds on the code path too.
_ENTITY_ID_PARAMS = {
    "set_device_state": "entity_id",
    "run_scene": "entity_id",
    "set_climate": "entity_id",
}
_HOME_READ_TOOLS = ("list_devices", "get_device_state")

# The desktop twin: a PENDING window handle fills from this plan's own
# list_windows results. `title` rides along for close_window — see
# _substitute_window_handle for why it must.
_WINDOW_HANDLE_PARAMS = {
    "focus_window": "handle",
    "close_window": "handle",
}
_DESKTOP_READ_TOOLS = ("list_windows",)

# URL parameters: a read step whose url is a per-page template ("PENDING: the
# three job listing URLs from the search results") expands into one concrete
# step per result URL from the most recent completed web_search — the web
# mirror of _FILE_PARAMS (2026-07-19: the WWR run died at the replan cap on
# exactly this designed flow, "Step parameters still contain unresolved
# 'PENDING:' placeholders"). URLs come EXCLUSIVELY from web_search's own ranked
# results (RRF order), so the read targets stay grounded in a search the plan
# ran — never in free text. Both tools are strictly READ.
_URL_PARAMS = {
    "read_webpage": "url",
    "browse_page": "url",
}

# "the first three …" / "3 job listings" — the count the placeholder itself
# asks for. Only small counts; no number found = expand every result (the
# _expand_files ALL-found rule), still capped by max_new.
_COUNT_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_COUNT_RE = re.compile(r"\b(one|two|three|four|five|six|seven|eight|nine|ten|[1-9]\d?)\b", re.IGNORECASE)

# An explicitly-universal file request: "all (the) files", "every file",
# "all file/folders", "everything". When the goal says this, no extension
# filter the user never mentioned may narrow it. Shared with the planner's
# scope guard — one definition of "the user said ALL".
UNIVERSAL_FILES_RE = re.compile(
    r"\b(?:all|every)\s+(?:the\s+|my\s+)?(?:files?|file(?:s)?[/\\]folders?)\b"
    r"|\beverything\b",
    re.IGNORECASE,
)

# An extension token: ".txt", ".docx" — a dot, a letter, then up to 4 more
# word chars. Real extensions only; ".jarvis" and version numbers don't match.
EXT_TOKEN_RE = re.compile(r"\.([A-Za-z][A-Za-z0-9]{0,4})\b")


def extension_grounded(ext: str, corpus: str) -> bool:
    """True when the user's own words mention this extension ("txt" appears
    as a standalone token in the goal/conversation/answers)."""
    return bool(
        re.search(rf"(?i)(?<![a-z0-9]){re.escape(ext)}(?![a-z0-9])", corpus or "")
    )


def paths_from_step(step: PlanStep) -> tuple[list[str], list[str]]:
    """(file paths, folder paths) a COMPLETED step's real output contributed.
    This is the ground truth expansion draws from — never LLM text."""
    if step.status != StepStatus.COMPLETED or step.result is None:
        return [], []
    output = step.result.output
    if not isinstance(output, dict):
        return [], []
    files: list[str] = []
    folders: list[str] = []
    if step.tool == "search_files":
        for m in output.get("matches") or []:
            if isinstance(m, dict) and m.get("path"):
                bucket = folders if m.get("type") == "folder" else files
                bucket.append(str(m["path"]))
    elif step.tool == "list_directory":
        base = str(output.get("path") or "")
        if base:
            for e in output.get("entries") or []:
                if isinstance(e, dict) and e.get("name"):
                    full = str(PurePath(base) / str(e["name"]))
                    bucket = folders if e.get("type") == "directory" else files
                    bucket.append(full)
    elif step.tool == "create_file" and output.get("created"):
        files.append(str(output["created"]))
    return files, folders


def _string_placeholder_keys(parameters: dict[str, Any]) -> list[str]:
    return [
        k
        for k, v in parameters.items()
        if isinstance(v, str) and _PLACEHOLDER_MARK in v.upper()
    ]


def _nested_placeholder(value: Any) -> bool:
    """A placeholder buried in a list/dict parameter — too ambiguous to
    resolve in code."""
    if isinstance(value, dict):
        return any(_nested_placeholder(v) for v in value.values())
    if isinstance(value, list):
        return any(
            (isinstance(v, str) and _PLACEHOLDER_MARK in v.upper())
            or _nested_placeholder(v)
            for v in value
        )
    return False


def _describe(tool: str, params: dict[str, Any], path: str, original: str) -> str:
    """Deterministic per-file description for an expanded step — code-derived
    like action_detail, so the user reads exactly what will happen."""
    name = PurePath(path).name
    parent = str(PurePath(path).parent)
    if tool == "delete_file":
        return f"Delete {name} from {parent}"
    if tool == "read_file":
        return f"Read {name}"
    if tool == "rename_file":
        return f"Rename {name} to '{params.get('new_name')}'"
    if tool == "move_file":
        return f"Move {name} to {params.get('destination')}"
    if tool == "execute_script":
        return f"Run the script {name}"
    return f"{original} — {name}"


def _concrete_step(
    template: PlanStep, key: str, path: str, description: Optional[str] = None
) -> PlanStep:
    """A fresh PlanStep cloned from the template with the real path filled
    in. Fresh id and therefore a fresh signature: an expanded write or
    destructive step is NEVER covered by an approval given to the
    placeholder form — the user approves the exact path or nothing."""
    from app.agents.planner import _step_action_detail  # runtime import — no cycle

    parameters = {**template.parameters, key: path}
    return PlanStep(
        description=description
        or _describe(template.tool, parameters, path, template.description),
        tool=template.tool,
        parameters=parameters,
        permission_level=template.permission_level,
        requires_approval=template.requires_approval,
        action_detail=_step_action_detail(template.tool, parameters),
    )


def _searched_roots(step: PlanStep) -> list[str]:
    """The folders a completed search actually looked in — its own output, not
    a guess. list_directory reports one `path`; search_files reports every root."""
    output = step.result.output if step.result else None
    if not isinstance(output, dict):
        return []
    if step.tool == "search_files":
        return [str(r) for r in (output.get("searched_in") or []) if r]
    if step.tool == "list_directory" and output.get("path"):
        return [str(output["path"])]
    return []


def _normkey(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def partition_by_depth(
    pool: list[str], roots: list[str]
) -> tuple[list[str], list[str]]:
    """(files directly inside a searched root, files nested deeper).

    search_files recurses to unlimited depth, which is right for FINDING and
    wrong as a default for MUTATING: of the 85 PDFs the 2026-07-29 run matched
    under D:\\Downloads, 8 lived in subfolders — one of them inside a source
    repo's `frontend/src/Assets`. Moving those out would have broken a project
    the user never mentioned. "The PDFs in Downloads" means the ones in
    Downloads; anything deeper is a separate, explicit ask.

    With no roots to compare against nothing is nested — the caller keeps the
    whole pool rather than inventing a boundary."""
    if not roots:
        return list(pool), []
    root_keys = {_normkey(r) for r in roots}
    top: list[str] = []
    nested: list[str] = []
    for p in pool:
        (top if _normkey(str(PurePath(p).parent)) in root_keys else nested).append(p)
    return top, nested


def _mutation_source(completed: list[PlanStep]) -> Optional[PlanStep]:
    """The completed step whose results a mutation's file list came from."""
    return next((s for s in reversed(completed) if paths_from_step(s)[0]), None)


def mutation_scope(
    plan: AgentPlan,
    tool: str,
    pool: list[str],
    source: Optional[PlanStep],
    grounding: str = "",
) -> tuple[Optional[list[str]], list[str]]:
    """The two rules that make a bulk file mutation safe, in ONE place.

    Returns (kept, deferred); `kept is None` means the source cannot be
    trusted to describe the whole set and the caller must refuse.

      1. A source search that hit its own result cap describes only PART of
         what is there. Acting on it moves or deletes a subset and reports
         success — silently, and on the destructive path.
      2. search_files recurses to unlimited depth, which is right for FINDING
         and wrong as a default for MUTATING. "The PDFs in Downloads" means
         the ones in Downloads; anything deeper is a separate, explicit ask,
         and the user's own words ("include subfolders") are what overrides it.

    ⚠️ THIS FUNCTION EXISTS BECAUSE THE RULES KEPT BEING REACHABLE FROM ONLY
    ONE SHAPE OF THE OPERATION. 2026-07-30 they keyed on a hand-listed set of
    SINGULAR tool names and the planner had just been told to draft the plural
    ones. 2026-08-03 scripts/plan_bench.py reproduced the identical OUTCOME
    5/5 runs from a completely different direction: they lived inside
    `_file_pool`, which only `resolve()` calls, so a revise round that wrote
    the CONCRETE list itself — no `PENDING:` placeholder anywhere, and the
    logs show `_revise_node … (refine)` and never "Placeholder resolved in
    code" — skipped both rules AND the "excluded N nested files" note. Third
    instance of one defect class. So the rules now take a POOL rather than a
    step, and both entry points (`_file_pool` for a placeholder, and
    `scope_concrete_list` for a list the model wrote) are thin callers.
    """
    if not _mutates(tool):
        return pool, []
    output = source.result.output if source is not None and source.result else None
    if isinstance(output, dict) and output.get("truncated"):
        return None, []
    corpus = " ".join(
        [plan.goal or "", grounding or "", " ".join(plan.user_answers or [])]
    )
    if _RECURSIVE_CUE_RE.search(corpus):
        return pool, []  # the user asked for the nested ones
    top, nested = partition_by_depth(
        pool, _searched_roots(source) if source is not None else []
    )
    # Narrowing to nothing is worse than the default: a search that returned
    # only nested files was scoped that way on purpose.
    return (top, nested) if top else (pool, [])


def _file_pool(
    plan: AgentPlan,
    template: PlanStep,
    key: str,
    completed: list[PlanStep],
    grounding: str = "",
) -> tuple[Optional[list[str]], list[str]]:
    """(paths the template should act on, paths deliberately left out).

    `None` means "not resolvable in code" — the caller falls back to the LLM
    replan path. `[]` means the source genuinely found nothing, which is an
    outcome rather than a failure. Shared by the per-file and batch branches so
    the goal-fidelity rules cannot drift between them."""
    source = _mutation_source(completed)
    if source is None:
        # No completed step produced any file. If a search/list DID run and
        # found nothing, "each found file" is honestly zero steps — the plan
        # has nothing to do, which is an outcome, not a failure.
        if any(s.tool in ("search_files", "list_directory") for s in completed):
            return [], []
        return None, []  # nothing to draw from — the LLM replan path decides

    pool = paths_from_step(source)[0]
    placeholder_text = str(template.parameters.get(key) or "")
    exts = {e.lower() for e in EXT_TOKEN_RE.findall(placeholder_text)}
    if exts and UNIVERSAL_FILES_RE.search(plan.goal or ""):
        # The goal says ALL files: an extension the user never said cannot
        # narrow the expansion (the model's filter came from memory or its
        # own assumption — data, not instructions).
        exts = {e for e in exts if extension_grounded(e, plan.goal)}
    if exts:
        filtered = [
            p for p in pool if PurePath(p).suffix.lstrip(".").lower() in exts
        ]
        if not filtered:
            return None, []  # the filter matches nothing found — ask the LLM
        pool = filtered

    return mutation_scope(plan, template.tool, pool, source, grounding)


def _human_bytes(total: int) -> str:
    value = float(total)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


def _total_bytes(paths: list[str]) -> int:
    total = 0
    for p in paths:
        try:
            total += os.path.getsize(p)
        except OSError:
            pass
    return total


def _describe_batch(
    tool: str, paths: list[str], destination: str, deferred: list[str]
) -> str:
    """The batch step's description — code-authored, like action_detail, so the
    LLM can never soften what is about to happen. It carries the counts, the
    size, and what was deliberately LEFT OUT, because `_step_action_detail`
    must stay a pure (tool, params) render and cannot touch the disk."""
    total = _total_bytes(paths)
    # Omit the size rather than claim "0 B" when nothing could be stat'd.
    what = f"{len(paths)} file(s)" + (f" ({_human_bytes(total)})" if total else "")
    if tool == "move_files":
        head = f"Move {what} into {destination}"
    else:
        head = f"Delete {what} (each backed up to the trash first)"
    if not deferred:
        return head
    folders = sorted({str(PurePath(p).parent) for p in deferred})
    shown = ", ".join(folders[:2]) + (
        f" and {len(folders) - 2} more" if len(folders) > 2 else ""
    )
    return (
        f"{head} — {len(deferred)} more sit inside subfolders ({shown}) and are "
        f"NOT included; ask me to add them if you want them too"
    )


def stamp_batch_contract(
    step: PlanStep,
    tool: str,
    list_key: str,
    paths: list[str],
    deferred: list[str],
) -> None:
    """Write a batch step's file list AND everything the user reads about it.

    ⚠️ The list, the description and the action_detail are ONE contract, and
    every one of them has to move together. `action_detail` is what binds the
    approval, but `description` is the sentence on the card, and 2026-08-01
    shipped a substitution that changed only the parameters — the card said
    "into C:\\Users\\DELL\\Downloads" above a step moving files to D:. Same
    class as the browse round one day later, where the card named one site
    while acting on another. So there is one function and both callers use it.

    Scalars keep their order and the LIST GOES LAST: `planner._missing_target`
    walks parameters.values() in insertion order, so a missing `destination`
    must be the path-like candidate it finds — not the first of 85 sources.
    """
    from app.agents.planner import _step_action_detail  # runtime import — no cycle

    scalars = {k: v for k, v in step.parameters.items() if k != list_key}
    scalars[list_key] = list(paths)
    step.parameters = scalars
    destination = str(step.parameters.get("destination") or "")
    step.description = _describe_batch(tool, paths, destination, deferred)
    step.action_detail = _step_action_detail(tool, step.parameters)


def _batch_step(
    template: PlanStep,
    tool: str,
    list_key: str,
    single_key: str,
    paths: list[str],
    deferred: list[str],
) -> PlanStep:
    """One step carrying the explicit file list, in place of N per-file steps.

    Fresh id ⇒ fresh signature, so an approval given to the PENDING form
    covers nothing — the same guarantee `_concrete_step` provides. The
    permission level comes from the REGISTRY, never copied from the template:
    `schemas.py` puts that trust boundary in the registry, and a copy would
    silently under-classify a batch tool the day one is reclassified."""
    from app.tools.registry import registry  # runtime import — no cycle

    spec = registry.get(tool)
    level = spec.permission_level if spec is not None else template.permission_level
    parameters: dict[str, Any] = {
        k: v for k, v in template.parameters.items() if k != single_key
    }
    step = PlanStep(
        description="",
        tool=tool,
        parameters=parameters,
        permission_level=level,
        requires_approval=level != PermissionLevel.READ,
    )
    # The list, the description and the action_detail are one contract —
    # written in one place so the two callers cannot drift.
    stamp_batch_contract(step, tool, list_key, paths, deferred)
    return step


def _expand_files(
    plan: AgentPlan,
    template: PlanStep,
    key: str,
    completed: list[PlanStep],
    max_new: int,
    grounding: str = "",
) -> Optional[list[PlanStep]]:
    pool, deferred = _file_pool(plan, template, key, completed, grounding)
    if not pool:
        return pool  # None → LLM path; [] → nothing to do (an outcome)

    # Batch when there are many, and ALSO whenever files were deliberately
    # left out: the exclusion needs one legible contract to be stated on, and
    # per-file steps have nowhere to say it.
    batch = _BATCH_TOOLS.get(template.tool)
    if batch is not None and (len(pool) > BATCH_EXPAND_MAX or deferred):
        return [_batch_step(template, batch[0], batch[1], key, pool, deferred)]

    if len(pool) > max_new:
        return None  # would blow the plan-size cap — let the replan explain
    return [_concrete_step(template, key, path) for path in pool]


def _fill_list(
    plan: AgentPlan,
    template: PlanStep,
    key: str,
    completed: list[PlanStep],
    grounding: str = "",
) -> Optional[list[PlanStep]]:
    """The LLM drafted the batch tool itself with a PENDING list. Same pool,
    same rules — only the shape of the step it lands in differs.

    "Same rules" was FALSE until 2026-07-30: the mutation rules keyed on a
    hand-listed set of SINGULAR tool names, so arriving here with `move_files`
    skipped both the top-level partition and the truncated-source refusal. It
    is true now because `_mutates` reads the registry (see its docstring), and
    it is the reason this path must never re-acquire a name list of its own."""
    pool, deferred = _file_pool(plan, template, key, completed, grounding)
    if not pool:
        return pool  # None → LLM path; [] → nothing to do (an outcome)
    return [_batch_step(template, template.tool, key, key, pool, deferred)]


def _pick_one(candidates: list[str], placeholder_text: str) -> Optional[str]:
    """The ONE path a placeholder identifies among `candidates`, or None when
    the results do not pin exactly one — code never picks between several.

    Takes a POOL rather than a step so the folder rule and the open-target rule
    below cannot drift apart (the 2026-08-03 `mutation_scope` refactor shape:
    one predicate, thin callers)."""
    named = [c for c in candidates if PurePath(c).name.lower() in placeholder_text]
    if len(named) == 1:
        return named[0]  # the placeholder names exactly one found path
    if len(candidates) == 1:
        return candidates[0]  # only one candidate exists at all
    return None


def _substitute_folder(
    template: PlanStep, key: str, completed: list[PlanStep]
) -> Optional[list[PlanStep]]:
    source = next((s for s in reversed(completed) if paths_from_step(s)[1]), None)
    if source is None:
        return None
    placeholder_text = str(template.parameters.get(key) or "").lower()
    pick = _pick_one(paths_from_step(source)[1], placeholder_text)
    if pick is None:
        return None
    # Substitution, not expansion: the step is still the one the LLM
    # described — only its path became concrete.
    return [_concrete_step(template, key, pick, description=template.description)]


def _substitute_open_target(
    template: PlanStep, key: str, completed: list[PlanStep]
) -> Optional[list[PlanStep]]:
    """The folder (or the file whose folder) a completed read pinned, for
    open_folder's single `path`.

    SUBSTITUTION, never expansion — deliberately not _expand_files. A search
    matching five files would become five steps there, i.e. five explorer
    windows for one "open it"; nobody means that. Several candidates yield
    None and the LLM replan path takes over, as everywhere else here.

    Folders are tried FIRST and the file pool is consulted only when NO
    completed step produced a folder at all. That ordering is what keeps the
    rule free of a folder-vs-file judgement call: "open the folder containing
    my resume" reaches the file branch precisely because the search found only
    the file, so there is nothing to choose between."""
    if (folder_step := _substitute_folder(template, key, completed)) is not None:
        return folder_step
    if any(paths_from_step(s)[1] for s in completed):
        return None  # a folder existed and did not pin one — do not guess a file
    source = next((s for s in reversed(completed) if paths_from_step(s)[0]), None)
    if source is None:
        return None
    placeholder_text = str(template.parameters.get(key) or "").lower()
    pick = _pick_one(paths_from_step(source)[0], placeholder_text)
    if pick is None:
        return None
    # The tool resolves a file to its parent itself (_folder_to_show), so the
    # concrete step stays honest about what the read actually found.
    return [_concrete_step(template, key, pick, description=template.description)]


def urls_from_step(step: PlanStep) -> list[tuple[str, str]]:
    """(url, title) pairs a COMPLETED web_search step's real output produced, in
    the tool's own ranked (RRF) order — the ground truth URL expansion draws
    from. Never LLM text, never page prose."""
    if (
        step.tool != "web_search"
        or step.status != StepStatus.COMPLETED
        or step.result is None
    ):
        return []
    output = step.result.output
    if not isinstance(output, dict):
        return []
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for r in output.get("results") or []:
        if isinstance(r, dict):
            url = str(r.get("url") or "").strip()
            if url and url not in seen:
                seen.add(url)
                pairs.append((url, str(r.get("title") or "").strip()))
    return pairs


def _requested_count(text: str) -> Optional[int]:
    """The small count the placeholder itself asks for ("the first three …",
    "3 job listings"), or None when it names none."""
    m = _COUNT_RE.search(text or "")
    if not m:
        return None
    token = m.group(1).lower()
    return _COUNT_WORDS.get(token) or int(token)


def _expand_urls(
    template: PlanStep,
    key: str,
    completed: list[PlanStep],
    max_new: int,
) -> Optional[list[PlanStep]]:
    """The web mirror of _expand_files: a read step whose url is a PENDING
    template expands into one concrete read per result URL from the most recent
    completed web_search — top-N when the placeholder names a count ("the first
    three"), every result otherwise. Both target tools are READ; the URLs come
    exclusively from the search the plan already ran."""
    source = next((s for s in reversed(completed) if urls_from_step(s)), None)
    if source is None:
        # A search DID run and produced no URLs → "read the found pages" is
        # honestly zero steps — an outcome, not a failure (the _expand_files
        # empty-search rule). No search at all → the LLM replan path decides.
        if any(s.tool == "web_search" for s in completed):
            return []
        return None

    pool = urls_from_step(source)
    count = _requested_count(
        f"{template.parameters.get(key) or ''} {template.description or ''}"
    )
    if count:
        pool = pool[:count]
    if len(pool) > max_new:
        return None  # would blow the plan-size cap — let the replan explain
    return [
        _concrete_step(
            template, key, url,
            description=f"Read {title}" if title else f"Read {url}",
        )
        for url, title in pool
    ]


def _resolved_lookup_email(step: PlanStep) -> Optional[tuple[str, str]]:
    """(contact name, email) when a completed lookup_contact step RESOLVED a
    contact that has an email on file. This is the ONLY source recipient
    substitution draws from — read email content never reaches it."""
    if step.tool != "lookup_contact" or step.status != StepStatus.COMPLETED:
        return None
    output = step.result.output if step.result else None
    if not isinstance(output, dict) or output.get("status") != "resolved":
        return None
    contact = output.get("contact") or {}
    email = str(contact.get("email") or "").strip()
    if not email:
        return None
    return str(contact.get("name") or ""), email


def _substitute_recipient(
    template: PlanStep, key: str, completed: list[PlanStep]
) -> Optional[list[PlanStep]]:
    """The email-address mirror of _substitute_folder: fill the recipient
    when the completed lookup_contact results pin exactly ONE address."""
    candidates: list[tuple[str, str]] = []
    for step in completed:
        resolved = _resolved_lookup_email(step)
        if resolved and resolved not in candidates:
            candidates.append(resolved)
    if not candidates:
        return None
    placeholder_text = str(template.parameters.get(key) or "").lower()
    named = [
        (name, email)
        for name, email in candidates
        # Word-boundary match so "Ali" never matches inside "email".
        if any(
            tok and re.search(rf"\b{re.escape(tok)}", placeholder_text)
            for tok in name.lower().split()
        )
    ]
    named_emails = {email for _, email in named}
    all_emails = {email for _, email in candidates}
    if len(named_emails) == 1:
        pick = next(iter(named_emails))  # the placeholder names exactly one contact
    elif len(all_emails) == 1:
        pick = next(iter(all_emails))  # only one resolved address exists at all
    else:
        return None  # several plausible recipients — code never picks
    # Substitution, not expansion: the step is still the one the LLM
    # described — only its recipient became concrete. Fresh signature +
    # regenerated action_detail: the user approves the real address.
    return [_concrete_step(template, key, pick, description=template.description)]


def _events_from_step(step: PlanStep) -> list[dict]:
    """Event rows a COMPLETED list_events/find_events step returned — the only
    source event-id substitution draws from (never LLM text)."""
    if (
        step.tool not in _CALENDAR_READ_TOOLS
        or step.status != StepStatus.COMPLETED
        or step.result is None
    ):
        return []
    output = step.result.output
    if not isinstance(output, dict):
        return []
    return [
        e for e in output.get("events") or []
        if isinstance(e, dict) and e.get("id")
    ]


def _event_summary_matches(event: dict, placeholder_text: str) -> bool:
    """True when a word from the event's summary appears in the placeholder
    text (word-boundary, like _substitute_recipient's name match)."""
    summary = str(event.get("summary") or "").lower()
    return any(
        tok and re.search(rf"\b{re.escape(tok)}", placeholder_text)
        for tok in summary.split()
    )


def _substitute_event_id(
    template: PlanStep, key: str, completed: list[PlanStep]
) -> Optional[list[PlanStep]]:
    """The calendar mirror of _substitute_recipient: fill the PENDING event_id
    when the completed calendar reads pin exactly ONE event — one whose
    summary words match the placeholder text, or the only event found.
    Several candidates ⇒ None, code never picks."""
    from app.tools.calendar_tools import format_event_when  # runtime — no cycle

    events: list[dict] = []
    seen: set[str] = set()
    for step in completed:
        for e in _events_from_step(step):
            eid = str(e["id"])
            if eid not in seen:
                seen.add(eid)
                events.append(e)
    if not events:
        return None
    placeholder_text = str(template.parameters.get(key) or "").lower()
    named = [e for e in events if _event_summary_matches(e, placeholder_text)]
    named_ids = {str(e["id"]) for e in named}
    if len(named_ids) == 1:
        pick = named[0]  # the placeholder names exactly one found event
    elif len(events) == 1:
        pick = events[0]  # only one candidate exists at all
    else:
        return None  # several plausible events — code never picks
    verb = "Delete" if template.tool == "delete_event" else "Update"
    when = format_event_when(pick)
    desc = (
        f"{verb} event '{pick.get('summary') or '(no title)'}'"
        + (f" — {when}" if when else "")
    )
    # Substitution, not expansion: the step is still the one the LLM described
    # — only its event_id became concrete. Fresh signature + regenerated
    # action_detail: the user approves the real event.
    return [_concrete_step(template, key, str(pick["id"]), description=desc)]


def _devices_from_step(step: PlanStep) -> list[dict]:
    """Device rows a COMPLETED list_devices/get_device_state step returned — the
    only source entity-id substitution draws from (never LLM text)."""
    if (
        step.tool not in _HOME_READ_TOOLS
        or step.status != StepStatus.COMPLETED
        or step.result is None
    ):
        return []
    output = step.result.output
    if not isinstance(output, dict):
        return []
    return [
        d for d in output.get("devices") or []
        if isinstance(d, dict) and d.get("entity_id")
    ]


def _device_name_matches(device: dict, placeholder_text: str) -> bool:
    """True when a word from the device's friendly name or its room appears in
    the placeholder text (word-boundary, like _substitute_recipient's name
    match). The room counts because "PENDING: the kitchen lights" names the area
    as often as the device."""
    words = f"{device.get('name') or ''} {device.get('area') or ''}".lower().split()
    return any(
        tok and re.search(rf"\b{re.escape(tok)}", placeholder_text)
        for tok in words
    )


def _substitute_entity_id(
    template: PlanStep, key: str, completed: list[PlanStep]
) -> Optional[list[PlanStep]]:
    """The home mirror of _substitute_event_id: fill the PENDING entity_id when
    the completed home reads pin exactly ONE device — one whose name or room
    matches the placeholder text, or the only device found. Several candidates
    ⇒ None, code never picks.

    ⚠️ The scene/climate tools additionally require a matching DOMAIN, so
    "PENDING: the goodnight scene" can never resolve to a light: a run_scene
    step only ever considers `scene.*` devices, and set_climate only
    `climate.*`. Without that filter the single-candidate branch could hand a
    scene tool a lamp, which the tool would then refuse — a confusing failure
    for a step that was resolvable."""
    from app.integrations.home_assistant import domain_of  # runtime — no cycle

    required_domain = {"run_scene": "scene", "set_climate": "climate"}.get(template.tool)

    devices: list[dict] = []
    seen: set[str] = set()
    for step in completed:
        for d in _devices_from_step(step):
            eid = str(d["entity_id"])
            if eid in seen:
                continue
            if required_domain and domain_of(eid) != required_domain:
                continue
            seen.add(eid)
            devices.append(d)
    if not devices:
        return None

    placeholder_text = str(template.parameters.get(key) or "").lower()
    named = [d for d in devices if _device_name_matches(d, placeholder_text)]
    named_ids = {str(d["entity_id"]) for d in named}
    if len(named_ids) == 1:
        pick = named[0]  # the placeholder names exactly one found device
    elif len(devices) == 1:
        pick = devices[0]  # only one candidate exists at all
    else:
        return None  # several plausible devices — code never picks

    label = str(pick.get("name") or pick["entity_id"])
    area = str(pick.get("area") or "").strip()
    where = f" in the {area}" if area else ""
    if template.tool == "run_scene":
        desc = f"Activate scene '{label}'"
    elif template.tool == "set_climate":
        desc = f"Set thermostat '{label}'{where}"
    else:
        desc = f"Set '{label}'{where} to {template.parameters.get('state') or '?'}"
    # Substitution, not expansion: the step is still the one the LLM described
    # — only its entity_id became concrete. Fresh signature + regenerated
    # action_detail: the user approves the real device.
    return [_concrete_step(template, key, str(pick["entity_id"]), description=desc)]


def _windows_from_step(step: PlanStep) -> list[dict]:
    """Window rows a COMPLETED list_windows step returned — the only source
    window-handle substitution draws from (never LLM text)."""
    if (
        step.tool not in _DESKTOP_READ_TOOLS
        or step.status != StepStatus.COMPLETED
        or step.result is None
    ):
        return []
    output = step.result.output
    if not isinstance(output, dict):
        return []
    return [
        w for w in output.get("windows") or []
        if isinstance(w, dict) and w.get("handle") is not None
    ]


def _window_matches(window: dict, placeholder_text: str) -> bool:
    """True when a word from the window's title or its application appears in
    the placeholder text — the _device_name_matches rule for windows.

    Short tokens are dropped: window titles are full of one- and two-character
    noise ("-", "|", "vs") that would match almost any placeholder and turn a
    genuine several-candidates case into a false single match."""
    words = f"{window.get('title') or ''} {window.get('process') or ''}".lower()
    return any(
        len(tok) >= 3 and re.search(rf"\b{re.escape(tok)}", placeholder_text)
        for tok in re.split(r"[^\w.]+", words)
    )


def _substitute_window_handle(
    template: PlanStep, key: str, completed: list[PlanStep]
) -> Optional[list[PlanStep]]:
    """The desktop mirror of _substitute_entity_id: fill the PENDING window
    handle when the completed list_windows steps pin exactly ONE window — one
    whose title or application matches the placeholder text, or the only window
    found. Several candidates ⇒ None, code never picks.

    ⚠️ `title` IS FILLED TOO, from the same row. close_window verifies the live
    window still matches the title it was approved for, so a resolved handle
    with an unresolved "PENDING: ..." title would fail that check every time —
    the step would be perfectly resolvable and still dead-end, which is exactly
    the spurious-failure class this module exists to remove."""
    windows: list[dict] = []
    seen: set[str] = set()
    for step in completed:
        for w in _windows_from_step(step):
            handle = str(w["handle"])
            if handle in seen:
                continue
            seen.add(handle)
            windows.append(w)
    if not windows:
        return None

    placeholder_text = str(template.parameters.get(key) or "").lower()
    named = [w for w in windows if _window_matches(w, placeholder_text)]
    named_handles = {str(w["handle"]) for w in named}
    if len(named_handles) == 1:
        pick = named[0]  # the placeholder names exactly one open window
    elif len(windows) == 1:
        pick = windows[0]  # only one candidate exists at all
    else:
        return None  # several plausible windows — code never picks

    title = str(pick.get("title") or "")
    process = str(pick.get("process") or "").strip()
    where = f" ({process})" if process else ""
    verb = "Close" if template.tool == "close_window" else "Bring"
    tail = "" if template.tool == "close_window" else " to the front"
    desc = f"{verb} the window '{title}'{where}{tail}"

    step = _concrete_step(template, key, str(pick["handle"]), description=desc)
    # Fill the title from the SAME row when it is still placeholder text — see
    # the docstring. A title the LLM wrote concretely is left alone: it is what
    # the user will see and verify, and overwriting it would let a substitution
    # silently change the contract.
    existing_title = str(step.parameters.get("title") or "")
    if title and (not existing_title or _PLACEHOLDER_MARK in existing_title.upper()):
        from app.agents.planner import _step_action_detail  # runtime — no cycle

        step.parameters["title"] = title
        step.action_detail = _step_action_detail(step.tool, step.parameters)
    return [step]


def _window_placeholder_key(template: PlanStep) -> Optional[str]:
    """The window tools' handle parameter when the step's placeholders name ONE
    window across two fields — `close_window(handle="PENDING: the notepad
    window", title="PENDING: its title")`.

    ⚠️ WITHOUT THIS THE FEATURE'S OWN INSTRUCTIONS ARE UNRESOLVABLE. close_window
    requires `title` (it verifies the live window still matches it, because
    handles get reused), and plan RULE 25 therefore tells the model to put a
    placeholder in BOTH — but `resolve()` refuses any step with more than one
    placeholder key, so the exact shape the rule asks for fell through to the
    LLM every time. Same class as the 2026-07-29 batch-list case: a recognized
    shape that the general veto could not see.

    The veto is bypassed for THIS shape only — handle must be placeholder text
    and no key OTHER than handle/title may be one — so a step with an unrelated
    ambiguous parameter stays ambiguous and still goes to the LLM."""
    key = _WINDOW_HANDLE_PARAMS.get(template.tool)
    if key is None:
        return None
    value = template.parameters.get(key)
    if not (isinstance(value, str) and _PLACEHOLDER_MARK in value.upper()):
        return None
    extra = set(_string_placeholder_keys(template.parameters)) - {key, "title"}
    if extra or _nested_placeholder(template.parameters):
        return None
    return key


def _placeholder_list_key(template: PlanStep) -> Optional[str]:
    """The batch tool's list parameter when it holds ONLY placeholder text —
    `move_files(sources=["PENDING: the pdf paths"])`.

    This shape is invisible to `_string_placeholder_keys` and vetoed by
    `_nested_placeholder`, which is why an LLM-drafted batch step would
    otherwise fail with the very error this module exists to prevent. The veto
    is bypassed for THIS recognized shape only: every element must be
    placeholder text, so a list mixing real paths with a placeholder stays
    ambiguous and still goes to the LLM."""
    key = _LIST_PARAMS.get(template.tool)
    if key is None:
        return None
    value = template.parameters.get(key)
    if isinstance(value, str) and _PLACEHOLDER_MARK in value.upper():
        return key
    if (
        isinstance(value, list)
        and value
        and all(
            isinstance(v, str) and _PLACEHOLDER_MARK in v.upper() for v in value
        )
    ):
        return key
    return None


class ListScope(NamedTuple):
    """The scope rules' verdict on a batch step's ALREADY-CONCRETE file list.

    `refuse` non-empty ⇒ fail the step with that reason. Otherwise `kept` is
    the list the step should carry and `deferred` is what was left out.
    """

    kept: list[str]
    deferred: list[str]
    refuse: str = ""


_TRUNCATED_SOURCE_ERROR = (
    "The search that produced this file list stopped at its result cap, so the "
    "list describes only part of what is there. Acting on a knowingly partial "
    "set and reporting success would be wrong. Narrow the search — a more "
    "specific folder, or a filter — so it returns everything, then act on that."
)


def scope_concrete_list(
    plan: AgentPlan, index: int, grounding: str = ""
) -> Optional[ListScope]:
    """Apply the bulk-mutation scope rules to a list the MODEL wrote itself.

    `resolve()` above only ever runs on a step still carrying a `PENDING:`
    placeholder. A revise/refine round that fills the concrete list instead
    reaches execution with no placeholder at all — so `mutation_scope` was
    never consulted, and scripts/plan_bench.py reproduced the consequence 5/5
    runs on 2026-08-03: "move all the pdf files in downloads into pdfs" moved
    all 14 matches including two from inside `downloads/project-src/assets`,
    with no partition, no truncated-source refusal, and no "excluded N nested
    files" note on the approval card.

    Returns None when there is nothing to say — not a batch mutation, no list,
    a list still holding placeholders (that is `resolve()`'s job), or a list
    the rules leave exactly as it is. Returning None on an unchanged list is
    what makes this safe to run on EVERY pass of the execute loop: the second
    pass must not re-stamp a description whose "N more sit inside subfolders"
    note it can no longer derive, because by then the nested files are gone
    from the list.

    Never raises — scope is best-effort, like every other pre-execution guard.
    """
    try:
        step = plan.steps[index]
        list_key = _LIST_PARAMS.get(step.tool)
        if list_key is None:
            return None
        current = step.parameters.get(list_key)
        if not isinstance(current, list) or not current:
            return None
        if not all(isinstance(p, str) and p for p in current):
            return None
        if any(_PLACEHOLDER_MARK in p.upper() for p in current):
            return None  # resolve() owns the placeholder shape
        completed = [
            s for s in plan.steps[:index] if s.status == StepStatus.COMPLETED
        ]
        kept, deferred = mutation_scope(
            plan, step.tool, list(current), _mutation_source(completed), grounding
        )
        if kept is None:
            return ListScope([], [], refuse=_TRUNCATED_SOURCE_ERROR)
        if not deferred and kept == list(current):
            return None
        return ListScope(kept, deferred)
    except Exception as e:  # pragma: no cover — belt: never break the planner
        logger.warning(f"Bulk-mutation scoping crashed (leaving the step as-is): {e}")
        return None


def apply_list_scope(step: PlanStep, scope: ListScope) -> None:
    """Narrow a batch step to the scoped list and refresh its whole contract."""
    list_key = _LIST_PARAMS[step.tool]
    stamp_batch_contract(step, step.tool, list_key, scope.kept, scope.deferred)


def resolve(
    plan: AgentPlan, index: int, max_new: int, grounding: str = ""
) -> Optional[list[PlanStep]]:
    """Replacement steps for plan.steps[index] (a step carrying a PENDING
    placeholder), derived purely from completed step results:
      [step, ...] — concrete step(s); splice them in and keep executing
      []          — the placeholder's source found nothing; nothing to do
      None        — not resolvable in code; the LLM replan path takes over
    Never raises: resolution is best-effort and must never break execution.

    `grounding` is the user's own words beyond the goal (conversation), read
    ONLY to honour an explicit "include subfolders" — never to widen scope on
    its own.
    """
    try:
        template = plan.steps[index]
        completed = [
            s for s in plan.steps[:index] if s.status == StepStatus.COMPLETED
        ]
        # A batch tool whose list parameter is pure placeholder text — checked
        # before the single-string rules, which cannot see this shape.
        list_key = _placeholder_list_key(template)
        if list_key is not None:
            return _fill_list(plan, template, list_key, completed, grounding)

        # A window named across handle AND title — one window, two fields.
        # Checked before the single-string rule, which cannot see this shape.
        window_key = _window_placeholder_key(template)
        if window_key is not None:
            return _substitute_window_handle(template, window_key, completed)

        keys = _string_placeholder_keys(template.parameters)
        if len(keys) != 1 or _nested_placeholder(template.parameters):
            return None
        key = keys[0]
        if _FILE_PARAMS.get(template.tool) == key:
            return _expand_files(plan, template, key, completed, max_new, grounding)
        if _DIR_PARAMS.get(template.tool) == key:
            return _substitute_folder(template, key, completed)
        if _OPEN_TARGET_PARAMS.get(template.tool) == key:
            return _substitute_open_target(template, key, completed)
        if _EMAIL_TO_PARAMS.get(template.tool) == key:
            return _substitute_recipient(template, key, completed)
        if _EVENT_ID_PARAMS.get(template.tool) == key:
            return _substitute_event_id(template, key, completed)
        if _ENTITY_ID_PARAMS.get(template.tool) == key:
            return _substitute_entity_id(template, key, completed)
        if _WINDOW_HANDLE_PARAMS.get(template.tool) == key:
            return _substitute_window_handle(template, key, completed)
        if _URL_PARAMS.get(template.tool) == key:
            return _expand_urls(template, key, completed, max_new)
        return None
    except Exception as e:  # pragma: no cover — belt: never break the planner
        logger.warning(f"Placeholder resolution crashed (falling back to LLM): {e}")
        return None
