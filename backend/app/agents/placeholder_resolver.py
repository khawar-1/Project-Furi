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

Resolution is CONSERVATIVE: anything ambiguous returns None and the existing
LLM replan path takes over (unchanged behavior). Extension tokens inside a
placeholder ("PENDING: .txt file paths") filter the candidate files — except
when the goal explicitly asks for ALL files and never names that extension:
the goal's own words outrank a filter the model invented (the same
goal-fidelity rule the planner's scope guard enforces on drafted steps —
long-term memory is data, and data must never narrow the user's request).
"""
import re
from pathlib import PurePath
from typing import Any, Optional

from loguru import logger

from app.agents.schemas import AgentPlan, PlanStep, StepStatus

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

# Single-folder parameters: substituted only when the completed results
# identify exactly one candidate.
_DIR_PARAMS = {
    "search_files": "directory",
    "list_directory": "path",
    "run_command": "working_directory",
}

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


def _expand_files(
    plan: AgentPlan,
    template: PlanStep,
    key: str,
    completed: list[PlanStep],
    max_new: int,
) -> Optional[list[PlanStep]]:
    source = next((s for s in reversed(completed) if paths_from_step(s)[0]), None)
    if source is None:
        # No completed step produced any file. If a search/list DID run and
        # found nothing, "each found file" is honestly zero steps — the plan
        # has nothing to do, which is an outcome, not a failure.
        if any(s.tool in ("search_files", "list_directory") for s in completed):
            return []
        return None  # nothing to draw from — the LLM replan path decides

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
            return None  # the filter matches nothing found — ambiguous, ask the LLM
        pool = filtered

    if len(pool) > max_new:
        return None  # would blow the plan-size cap — let the replan explain
    return [_concrete_step(template, key, path) for path in pool]


def _substitute_folder(
    template: PlanStep, key: str, completed: list[PlanStep]
) -> Optional[list[PlanStep]]:
    source = next((s for s in reversed(completed) if paths_from_step(s)[1]), None)
    if source is None:
        return None
    folders = paths_from_step(source)[1]
    placeholder_text = str(template.parameters.get(key) or "").lower()
    named = [f for f in folders if PurePath(f).name.lower() in placeholder_text]
    if len(named) == 1:
        pick = named[0]  # the placeholder names exactly one found folder
    elif len(folders) == 1:
        pick = folders[0]  # only one candidate exists at all
    else:
        return None  # several plausible folders — code never picks
    # Substitution, not expansion: the step is still the one the LLM
    # described — only its path became concrete.
    return [_concrete_step(template, key, pick, description=template.description)]


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


def resolve(plan: AgentPlan, index: int, max_new: int) -> Optional[list[PlanStep]]:
    """Replacement steps for plan.steps[index] (a step carrying a PENDING
    placeholder), derived purely from completed step results:
      [step, ...] — concrete step(s); splice them in and keep executing
      []          — the placeholder's source found nothing; nothing to do
      None        — not resolvable in code; the LLM replan path takes over
    Never raises: resolution is best-effort and must never break execution.
    """
    try:
        template = plan.steps[index]
        keys = _string_placeholder_keys(template.parameters)
        if len(keys) != 1 or _nested_placeholder(template.parameters):
            return None
        key = keys[0]
        completed = [
            s for s in plan.steps[:index] if s.status == StepStatus.COMPLETED
        ]
        if _FILE_PARAMS.get(template.tool) == key:
            return _expand_files(plan, template, key, completed, max_new)
        if _DIR_PARAMS.get(template.tool) == key:
            return _substitute_folder(template, key, completed)
        if _EMAIL_TO_PARAMS.get(template.tool) == key:
            return _substitute_recipient(template, key, completed)
        if _EVENT_ID_PARAMS.get(template.tool) == key:
            return _substitute_event_id(template, key, completed)
        return None
    except Exception as e:  # pragma: no cover — belt: never break the planner
        logger.warning(f"Placeholder resolution crashed (falling back to LLM): {e}")
        return None
