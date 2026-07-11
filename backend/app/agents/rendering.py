"""
Jarvis OS — Deterministic plan rendering (Phase 3, shared since Phase 4 Part 5)

The plain-language texts for plan outcomes live HERE, in the agents package,
so both the chat task router (inline plans) and the background task runner
render the exact same words — and neither ever lets an LLM paraphrase what
the user is approving or spin a failure. Moved out of app/api/task_router.py
when Part 5 needed them from a background context (an api→agents import from
the runner would be circular).
"""
import json
from pathlib import PurePath

from app.agents.schemas import AgentPlan, PlanStatus, StepStatus

_PERMISSION_TAGS = {"read": "read", "write": "WRITE", "destructive": "DESTRUCTIVE"}

# Sized so that a "show me the files" answer is never cut mid-list: the
# tools themselves cap results (SEARCH_MAX_RESULTS=100), and 100 grouped
# names fit comfortably here. The old 700/2000 caps cut real answers —
# live bug 2026-07-10: a 52-match search rendered ~11 matches.
_STEP_RESULT_CAP = 3500    # chars of one step's rendered output
_RESULTS_TOTAL_CAP = 8000  # chars of the whole results block
_MAX_NAMES = 120           # names listed before "… and N more"


def _clip(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[:cap] + "… (truncated)"


def _names(names: list[str], limit: int = _MAX_NAMES) -> str:
    """Comma-join, clipped by ITEM so a name is never cut in half."""
    shown = ", ".join(names[:limit])
    extra = len(names) - limit
    return shown if extra <= 0 else f"{shown} … and {extra} more"


def _fence(text: str) -> str:
    """Wrap raw content (file text, command output) in a markdown code fence
    so the chat renderer never interprets it as markup. A longer fence is
    used when the content itself contains one."""
    marker = "````" if "```" in text else "```"
    return f"{marker}\n{text}\n{marker}"


# Per-tool renderings of a completed step's real output — code-derived from
# each tool's own result dict, never an LLM's retelling. Shapes match the
# tools' _ok payloads exactly (file_tools / terminal_tools / memory_tools).
# The output is MARKDOWN (the chat renders assistant text with ReactMarkdown):
# bullet lines for name lists, backticked paths, fenced raw content.

def _fmt_list_directory(output: dict) -> str:
    entries = output.get("entries") or []
    path = output.get("path", "?")
    if not entries:
        return f"`{path}` is empty."
    files = [str(e.get("name", "?")) for e in entries if e.get("type") == "file"]
    folders = [str(e.get("name", "?")) for e in entries if e.get("type") != "file"]
    lines = [f"`{path}` contains {len(folders)} folder(s) and {len(files)} file(s)."]
    if folders:
        lines.append(f"- Folders: {_names(folders)}")
    if files:
        lines.append(f"- Files: {_names(files)}")
    if output.get("truncated"):
        lines.append("- (listing truncated — more entries exist)")
    return "\n".join(lines)


def _fmt_search_files(output: dict) -> str:
    matches = output.get("matches") or []
    if not matches:
        return "No matches found."
    # Group by parent folder — repeating "D:\Downloads\" 52 times buries the
    # names the user actually asked for.
    order: list[str] = []
    groups: dict[str, list[str]] = {}
    for m in matches:
        p = PurePath(str(m.get("path", "?")))
        parent = str(p.parent)
        name = p.name or str(p)
        if m.get("type") == "folder":
            name += " (folder)"
        if parent not in groups:
            groups[parent] = []
            order.append(parent)
        groups[parent].append(name)
    lines = [f"Found {len(matches)} match(es):"]
    for parent in order:
        lines.append(f"- In `{parent}`: {_names(groups[parent])}")
    if output.get("truncated"):
        lines.append("- (more exist — the list was truncated)")
    return "\n".join(lines)


def _fmt_read_file(output: dict) -> str:
    content = str(output.get("content") or "").strip()
    path = output.get("path", "?")
    if not content:
        return f"`{path}` is empty."
    return f"Contents of `{path}`:\n{_fence(content)}"


def _fmt_shell(output: dict) -> str:
    stdout = str(output.get("stdout") or "").strip()
    return f"Output:\n{_fence(stdout)}" if stdout else "The command produced no output."


def _fmt_recall_memory(output: dict) -> str:
    rows = [str(m.get("content", "")) for m in output.get("memories") or []]
    rows += [
        f"{e.get('title', '?')} — {e.get('summary', '')}"
        for e in output.get("episodes") or []
    ]
    rows = [r for r in rows if r.strip()]
    if not rows:
        return "No saved memories matched."
    return "From memory: " + " | ".join(rows)


def _fmt_search_emails(output: dict) -> str:
    emails = output.get("emails") or []
    if not emails:
        return "No emails matched."
    lines = [f"Found {len(emails)} email(s):"]
    for e in emails:
        subject = str(e.get("subject") or "(no subject)")
        unread = " · unread" if e.get("unread") else ""
        snippet = str(e.get("snippet") or "").strip()
        lines.append(
            f"- **{subject}** — from {e.get('from') or '?'} "
            f"({e.get('date') or '?'}{unread})"
            + (f": {snippet}" if snippet else "")
        )
    if output.get("truncated"):
        lines.append("- (more exist — the list was truncated)")
    return "\n".join(lines)


def _fmt_read_email(output: dict) -> str:
    subject = str(output.get("subject") or "(no subject)")
    head = (
        f"Email **{subject}** — from {output.get('from') or '?'} "
        f"to {output.get('to') or '?'} ({output.get('date') or '?'}):"
    )
    body = str(output.get("body") or "").strip()
    if not body:
        return head + "\n(The email has no readable text body.)"
    return head + "\n" + _fence(body)


def _fmt_read_thread(output: dict) -> str:
    messages = output.get("messages") or []
    subject = str(output.get("subject") or "(no subject)")
    if not messages:
        return f"Thread **{subject}** is empty."
    blocks = [f"Thread **{subject}** — {output.get('count', len(messages))} message(s):"]
    for m in messages:
        body = str(m.get("body") or "").strip()
        head = f"From {m.get('from') or '?'} ({m.get('date') or '?'}):"
        blocks.append(head + ("\n" + _fence(body) if body else ""))
    if output.get("truncated"):
        blocks.append("(older messages not shown — the thread was truncated)")
    return "\n\n".join(blocks)


def _fmt_lookup_contact(output: dict) -> str:
    status = output.get("status")
    if status == "resolved":
        contact = output.get("contact") or {}
        name = contact.get("name", "?")
        rel = contact.get("relationship")
        return f"Contact found: {name}" + (f" ({rel})" if rel else "")
    if status == "ambiguous":
        candidates = ", ".join(str(c) for c in output.get("candidates") or [])
        return f"Several contacts match '{output.get('name', '?')}': {candidates}"
    return f"No saved contact matches '{output.get('name', '?')}'."


_RESULT_FORMATTERS = {
    "list_directory": _fmt_list_directory,
    "search_files": _fmt_search_files,
    "read_file": _fmt_read_file,
    "run_command": _fmt_shell,
    "execute_script": _fmt_shell,
    "recall_memory": _fmt_recall_memory,
    "lookup_contact": _fmt_lookup_contact,
    "search_emails": _fmt_search_emails,
    "read_email": _fmt_read_email,
    "read_thread": _fmt_read_thread,
}


def _render_step(step) -> str | None:
    """One completed step's real output as readable text, or None when there
    is nothing to show. Shared by the deterministic completion text and the
    inline summary LLM's input — both always see the same rendering."""
    if step.status != StepStatus.COMPLETED:
        return None
    output = step.result.output if step.result else None
    formatter = _RESULT_FORMATTERS.get(step.tool)
    if formatter is not None and isinstance(output, dict):
        return _clip(formatter(output), _STEP_RESULT_CAP)
    if step.requires_approval:
        # Write/destructive tools: the approved description IS the record
        # of what happened; their outputs are bookkeeping (trash paths).
        return f"Done: {step.description}"
    if output is not None:
        return _clip(json.dumps(output, default=str), _STEP_RESULT_CAP)
    return None


def completed_results_text(plan: AgentPlan) -> str:
    """Deterministic rendering of what the completed steps actually produced.
    Background completions never get an LLM summary, so this block IS the
    answer — without it, "how many files are in phase3test" finished as
    "Done — 1 step(s) completed." with the answer nowhere (live bug,
    2026-07-09). Also the inline flow's fallback when the summary LLM fails."""
    blocks: list[str] = []
    total = 0
    for step in plan.steps:
        rendered = _render_step(step)
        if rendered is None:
            continue
        if total + len(rendered) > _RESULTS_TOTAL_CAP:
            blocks.append("… more results not shown — see the Activity timeline.")
            break
        blocks.append(rendered)
        total += len(rendered)
    return "\n\n".join(blocks)


def steps_for_summary(plan: AgentPlan) -> str:
    """The completed steps rendered for the inline summary LLM's prompt.
    The LLM NEVER sees raw JSON — it can only re-present text a code
    formatter already made readable. Live bug 2026-07-10: the summary prompt
    carried json.dumps of the step output cut at 2000 chars, so the LLM
    pasted escaped JSON into the chat showing ~11 of 52 search matches and
    called the complete list "truncated"."""
    blocks: list[str] = []
    total = 0
    for step in plan.steps:
        rendered = _render_step(step)
        if rendered is None:
            continue
        block = f"ACTION: {step.description}\nRESULT: {rendered}"
        if total + len(block) > _RESULTS_TOTAL_CAP:
            blocks.append("(further step results omitted)")
            break
        blocks.append(block)
        total += len(block)
    return "\n\n".join(blocks)


def serialize_plan_for_api(plan: AgentPlan) -> dict:
    """The serialized AgentPlan every endpoint and push event carries, plus
    the requires_approval convenience flag so the frontend never
    string-compares the status enum."""
    data = plan.model_dump(mode="json")
    data["requires_approval"] = plan.status == PlanStatus.AWAITING_APPROVAL
    return data


def deterministic_plan_text(plan: AgentPlan) -> str:
    """Plain-language text for every non-completed outcome. No LLM involved
    for approvals and failures: what the user approves or is told about a
    failure is never paraphrased. A clarifying question is quoted VERBATIM
    (it is LLM-authored, but answering it executes nothing — anything the
    answer leads to still pauses for deterministic approval)."""
    if plan.status == PlanStatus.AWAITING_CHOICE and plan.question is not None:
        lines = [plan.question.text.strip()]
        if plan.question.options:
            lines.append("")
            lines.extend(
                f"{i}. {opt}" for i, opt in enumerate(plan.question.options, 1)
            )
        lines.append("")
        lines.append(
            "Nothing has been done yet — pick an option or answer in your own words."
        )
        return "\n".join(lines)
    if plan.status == PlanStatus.AWAITING_APPROVAL:
        lines = []
        for i, s in enumerate(plan.steps, 1):
            if s.status != StepStatus.PENDING:
                continue
            tag = _PERMISSION_TAGS.get(s.permission_level.value, s.permission_level.value)
            lines.append(f"{i}. {s.description} ({tag})")
            if s.action_detail:  # code-derived — exactly what will run
                lines.append(f"   → {s.action_detail}")
        done_note = ""
        completed = plan.completed_steps()
        if completed:
            done_note = (
                f"I've already gathered what I need ({len(completed)} read-only "
                f"step(s) done). "
            )
        return (
            f"{done_note}Here's what I'm about to do:\n\n" + "\n".join(lines) +
            "\n\nNothing has been changed yet — this needs your approval first."
        )
    if plan.status == PlanStatus.FAILED:
        base = plan.message or "The plan could not be completed."
        completed = plan.completed_steps()
        if completed:
            done = "; ".join(s.description for s in completed[:5])
            return (
                f"I couldn't finish that. {base}\n\n"
                f"Steps that did complete before the failure: {done}."
            )
        return f"I couldn't do that. {base}"
    if plan.status == PlanStatus.CANCELLED:
        return plan.message or "Cancelled by the user — nothing further was executed."
    # COMPLETED (or any unexpected status): the text must CARRY the results —
    # in the background flow this deterministic message is the only channel
    # the answer can arrive by ("Done — 1 step(s) completed." told the user
    # nothing about the files they asked for, live bug 2026-07-09).
    base = plan.message or f"Done — {len(plan.completed_steps())} step(s) completed."
    results = completed_results_text(plan)
    return f"{base}\n\n{results}" if results else base
