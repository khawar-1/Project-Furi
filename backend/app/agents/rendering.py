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
from typing import Optional

from app.agents.schemas import AgentPlan, PlanStatus, StepStatus

_PERMISSION_TAGS = {"read": "read", "write": "WRITE", "destructive": "DESTRUCTIVE"}

# Sized so that a "show me the files" answer is never cut mid-list: the
# tools themselves cap results (SEARCH_MAX_RESULTS=100), and 100 grouped
# names fit comfortably here. The old 700/2000 caps cut real answers —
# live bug 2026-07-10: a 52-match search rendered ~11 matches.
_STEP_RESULT_CAP = 3500    # chars of one step's rendered output (default)
_MAX_NAMES = 120           # names listed before "… and N more"

# Per-tool caps. Web steps carry PROSE, not name lists: a fetched page is the
# evidence a factual answer rests on, so clipping it to the default starves the
# answer. Live bug 2026-07-16: read_webpage fetches PAGE_MAX_CHARS=20_000 and
# _render_step clipped every step to 3500 — 82% of every page the tool went and
# got was thrown away before any consumer saw it.
#
# web_search is sized to fit a FULL result set: FANOUT_MERGED_MAX(8) ×
# CONTENT_MAX_CHARS(1200) + per-row title/url/fence overhead ≈ 10500. It was
# 6000 for one live run, which sat just under the then-5-row set and silently
# clipped the fifth result — the same "the last one loses" defect as the
# block-level cap, one level down.
#
# THE INVARIANT: keep this ABOVE FANOUT_MERGED_MAX × CONTENT_MAX_CHARS if
# either constant moves. Raised 7000 → 11000 when web_search learned to fan a
# question out over several readings (2026-07-17): the merged set is now up to
# 8 rows rather than 5, and leaving the cap at 7000 would have clipped the
# extra evidence away — turning the fan-out into a more expensive way to starve
# the record, which is the exact defect (content starvation) the whole web
# hardening round exists to prevent.
# browse_page renders an element list (dom_observe._ELEMENT_BUDGET = 6000) plus
# page prose (_PAGE_TEXT_BUDGET = 4000) plus a URL/TITLE head. Clipping it here
# would silently eat the element list's tail — the actionable half — which is the
# 5-wide trap exactly: a budget widened upstream and not downstream STARVES the
# record. test_dom_observe.py asserts this cap stays above what dom_observe can
# emit, so moving one number without the other fails loudly.
_STEP_RESULT_CAPS = {
    "web_search": 11000,
    "read_webpage": 12000,
    # browse renders the SAME dom_observe observation browse_page does (element
    # budget + prose budget + head), so it needs the same headroom — clipping it
    # eats the element list's tail (the 5-wide trap). test_dom_observe pins both.
    "browse_page": 11000,
    "browse": 11000,
}

# Chars of the whole results block. Raised 8000 → 20000 together with the
# per-tool caps above: web evidence is bulkier than a file listing, and the
# fair-share allocator below (not arrival order) now decides who gets what.
_RESULTS_TOTAL_CAP = 20000
_MIN_STEP_SHARE = 800      # a step is never starved to nothing


def _step_cap(tool: str) -> int:
    return _STEP_RESULT_CAPS.get(tool, _STEP_RESULT_CAP)


def _clip(text: str, cap: int) -> str:
    """Mark a cut — but do NOT call it "truncated".

    In this codebase `truncated` is a FACT ABOUT THE WORLD that tools report
    on their own results: the search hit its cap, there is more out there we
    did not fetch. A render clip is something else entirely — OUR display
    budget running out over a result we hold in full. Using one word for both
    let the record lie: on 2026-07-29 a complete 85-file search was clipped
    here, the revise LLM read "(truncated)", and told the user "the search
    returned a truncated list. I can see these 8 files" — then asked whether
    to search again and the plan died on it. Real source truncation is still
    reported, separately and only when true, by _fmt_search_files."""
    return text if len(text) <= cap else text[:cap] + "… (clipped for length)"


def fair_shares(rendered: list[str], total: int = _RESULTS_TOTAL_CAP) -> list[int]:
    """Public seam over `_fair_shares` for callers with their own budget (the
    planner's revise prompt already carries the tool catalog, memory,
    conversation and 22 rules, so it cannot afford the full results cap).
    The default reproduces `_fair_shares` exactly."""
    return _fair_shares(rendered, total)


def render_step_result(step) -> Optional[str]:
    """Public seam over `_render_step`: one step's real output as readable
    text, or None when there is nothing to show. Used by the planner so the
    revise LLM reads the SAME code-authored rendering the summary LLM does."""
    return _render_step(step)


def _fair_shares(rendered: list[str], total: int = _RESULTS_TOTAL_CAP) -> list[int]:
    """Split _RESULTS_TOTAL_CAP across N rendered step blocks so that POSITION
    NEVER DETERMINES SURVIVAL.

    Both result renderers used to accumulate in step order and `break` at the
    total cap, so the LAST step's results were the ones dropped. Live bug
    2026-07-16: a three-question turn ("black clover … president of pakistan …
    fifa finals") drafted three web searches, and the FIFA question was third —
    first-come-first-served meant the moment web evidence got bulky, the third
    question's record would be omitted entirely, handing the summary LLM
    literally nothing about the very thing it had to answer.

    Every step gets an equal share; steps needing less than their share release
    the remainder, which is redistributed (one pass) to the steps that want
    more. A cut is always MARKED by _clip — never silent."""
    n = len(rendered)
    if n == 0:
        return []
    share = max(total // n, _MIN_STEP_SHARE)
    shares = [share] * n
    # One redistribution pass: the under-budget steps hand their slack to the
    # over-budget ones, split evenly among them.
    hungry = [i for i, r in enumerate(rendered) if len(r) > share]
    if hungry:
        slack = sum(share - len(r) for r in rendered if len(r) < share)
        if slack > 0:
            bonus = slack // len(hungry)
            for i in hungry:
                shares[i] += bonus
    return shares


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


def _human_size(n: int | float) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"  # unreachable; keeps type checkers honest


def _sized_name(name: str, row: dict) -> str:
    """A file name with its human-readable size when the row carries one."""
    size = row.get("size_bytes")
    if isinstance(size, (int, float)):
        return f"{name} ({_human_size(size)})"
    return name


def _file_aggregates(rows: list[dict], name_of) -> str | None:
    """Deterministic aggregate line over FILE rows carrying size_bytes /
    modified. Plan RULE 8 promises that "how many / largest / smallest /
    newest / total size" questions are answered from the search/list results
    themselves — so the RENDERED record must carry the data. Live bug
    2026-07-12: names-only rendering meant neither the summary LLM nor the
    deterministic fallback could name the largest PDF without inventing it.
    Computed in code, never in the LLM's head (the search_files-filters
    philosophy: comparing 52 sizes is exactly what LLMs get wrong)."""
    if len(rows) < 2:
        return None  # a single file already shows its own size inline
    parts: list[str] = []
    sized = [r for r in rows if isinstance(r.get("size_bytes"), (int, float))]
    if sized:
        largest = max(sized, key=lambda r: r["size_bytes"])
        smallest = min(sized, key=lambda r: r["size_bytes"])
        parts.append(f"Largest: {name_of(largest)} ({_human_size(largest['size_bytes'])})")
        parts.append(f"Smallest: {name_of(smallest)} ({_human_size(smallest['size_bytes'])})")
    dated = [r for r in rows if r.get("modified")]
    if dated:
        # ISO timestamps compare correctly as strings; show the date part.
        newest = max(dated, key=lambda r: str(r["modified"]))
        parts.append(f"Newest: {name_of(newest)} (modified {str(newest['modified'])[:10]})")
    if sized:
        total = sum(r["size_bytes"] for r in sized)
        parts.append(f"Total: {_human_size(total)} across {len(sized)} file(s)")
    return " · ".join(parts) if parts else None


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
    file_rows = [e for e in entries if e.get("type") == "file"]
    files = [_sized_name(str(e.get("name", "?")), e) for e in file_rows]
    folders = [str(e.get("name", "?")) for e in entries if e.get("type") != "file"]
    lines = [f"`{path}` contains {len(folders)} folder(s) and {len(files)} file(s)."]
    # Aggregates first — the clip must never eat them (see _fmt_search_files).
    aggregates = _file_aggregates(file_rows, lambda e: str(e.get("name", "?")))
    if aggregates:
        lines.append(f"- {aggregates}")
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
        # A bare "No matches found." is undiagnosable: it does not say WHERE it
        # looked, and the searched roots are sitting right there in the tool's
        # own output. Live 2026-07-30 — "move all the pdf files from downloads"
        # searched the empty C:\Users\DELL\Downloads while 85 PDFs sat in
        # D:\Downloads; neither the user reading the outcome nor the revise LLM
        # planning the next step could see which Downloads had been searched,
        # so an obviously-wrong result read as a plain "you have no PDFs".
        roots = [str(r).strip() for r in (output.get("searched_in") or []) if str(r).strip()]
        if roots:
            return f"No matches found in {_names([f'`{r}`' for r in roots])}."
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
        else:
            name = _sized_name(name, m)
        if parent not in groups:
            groups[parent] = []
            order.append(parent)
        groups[parent].append(name)
    lines = [f"Found {len(matches)} match(es):"]
    # The aggregate line comes FIRST — a long listing can hit the per-step
    # character cap, and the clip must never eat the densest line (live
    # verify 2026-07-13: 80 sized names pushed the trailing footer into the
    # cap and 'Largest:' arrived half-cut as "AWS Cloud Que… (truncated)").
    file_rows = [m for m in matches if m.get("type") == "file"]
    aggregates = _file_aggregates(
        file_rows, lambda m: PurePath(str(m.get("path", "?"))).name or "?"
    )
    if aggregates:
        lines.append(f"- {aggregates}")
    for parent in order:
        lines.append(f"- In `{parent}`: {_names(groups[parent])}")
    if output.get("truncated"):
        lines.append("- (more exist — the list was truncated)")
    return "\n".join(lines)


def _fmt_semantic_file_search(output: dict) -> str:
    matches = output.get("matches") or []
    note = str(output.get("note") or "").strip()
    if not matches:
        return note or "No matching files or conversations found."

    files = [m for m in matches if m.get("type") != "conversation"]
    convos = [m for m in matches if m.get("type") == "conversation"]
    lines: list[str] = []

    if files:
        # Group by parent folder (the _fmt_search_files shape), but each file
        # also shows the matching content snippet in a fence.
        order: list[str] = []
        groups: dict[str, list[dict]] = {}
        for m in files:
            p = PurePath(str(m.get("path", "?")))
            parent = str(p.parent)
            if parent not in groups:
                groups[parent] = []
                order.append(parent)
            groups[parent].append(m)
        lines.append(f"Found {len(files)} matching file(s):")
        for parent in order:
            lines.append(f"- In `{parent}`:")
            for m in groups[parent]:
                name = PurePath(str(m.get("path", "?"))).name or "?"
                lines.append(f"  - **{name}**")
                snippet = str(m.get("snippet") or "").strip()
                if snippet:
                    lines.append(_fence(snippet))

    if convos:
        lines.append(f"Found {len(convos)} matching conversation message(s):")
        for m in convos:
            when = str(m.get("created") or "")[:10]  # YYYY-MM-DD
            role = str(m.get("role") or "message")
            head = f"- **{role}**" + (f" ({when})" if when else "") + ":"
            lines.append(head)
            snippet = str(m.get("snippet") or "").strip()
            if snippet:
                lines.append(_fence(snippet))

    if note:
        lines.append(f"- ({note})")
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


# recall_actions rows → one readable line each. Phrases are code-derived from
# the audited tool + parameters; for move/rename the RESULT's real final path
# (moved_to / renamed_to) beats the requested parameter (the file_intelligence
# rule — a move "destination" may be the folder the file landed INSIDE).
_ACTION_LINE_KEYS = {
    "create_folder": ("created folder", "path", None),
    "open_folder": ("opened folder", "path", None),
    "create_file": ("created file", "path", None),
    "delete_file": ("deleted", "path", None),
    "move_file": ("moved", "source", "destination"),
    "rename_file": ("renamed", "path", "new_name"),
    "send_email": ("sent an email to", "to", "subject"),
    "create_email_draft": ("drafted an email to", "to", "subject"),
    "reply_email": ("replied to email", "message_id", None),
    "create_event": ("created calendar event", "summary", None),
    "update_event": ("updated calendar event", "event_id", None),
    "delete_event": ("deleted calendar event", "event_id", None),
    "run_command": ("ran command", "command", None),
    "execute_script": ("ran script", "script_path", None),
}


def _action_line(row: dict) -> str:
    tool = str(row.get("tool") or "?")
    params = row.get("parameters") if isinstance(row.get("parameters"), dict) else {}
    result = row.get("result")
    result_data: dict = {}
    if isinstance(result, str) and result.startswith("{"):
        try:
            parsed = json.loads(result)
            if isinstance(parsed, dict):
                result_data = parsed
        except (ValueError, TypeError):
            pass

    # Local wall-clock time — the stored ISO string carries a UTC offset.
    when = ""
    time_str = str(row.get("time") or "")
    if time_str:
        try:
            from datetime import datetime
            when = datetime.fromisoformat(time_str).astimezone().strftime("%Y-%m-%d %H:%M")
        except ValueError:
            when = time_str

    if tool in ("move_files", "delete_files"):
        # A batch's params carry a long path LIST, which _render_action clips
        # into an unreadable call string. Answer plan rule 19 ("which files did
        # you move today?") with the counts the result actually reports.
        count = result_data.get(
            "moved_count" if tool == "move_files" else "deleted_count"
        )
        n = count if isinstance(count, int) else len(params.get(
            "sources" if tool == "move_files" else "paths"
        ) or [])
        if tool == "move_files":
            dest = str(result_data.get("destination") or params.get("destination") or "?")
            text = f"moved {n} file(s) → `{dest}`"
        else:
            text = f"deleted {n} file(s) (to the trash)"
        failed = result_data.get("failed_count")
        if isinstance(failed, int) and failed:
            text += f", {failed} failed"
    elif phrase := _ACTION_LINE_KEYS.get(tool):
        verb, first_key, second_key = phrase
        first = str(params.get(first_key) or "?")
        text = f"{verb} `{first}`"
        if tool == "move_file":
            dest = str(result_data.get("moved_to") or params.get("destination") or "?")
            text += f" → `{dest}`"
        elif tool == "rename_file":
            dest = str(result_data.get("renamed_to") or params.get("new_name") or "?")
            text += f" → `{dest}`"
        elif second_key and params.get(second_key):
            text += f" — {params.get(second_key)}"
    else:
        text = str(row.get("action") or tool)

    line = f"{when} — {text}" if when else text
    if row.get("success") is False:
        line += " (FAILED)"
    return line


def _fmt_recall_actions(output: dict) -> str:
    actions = output.get("actions") or []
    if not actions:
        return "No recorded actions matched — Jarvis has not performed any matching action."
    lines = [f"Jarvis performed {len(actions)} recorded action(s) (newest first):"]
    for row in actions:
        lines.append(f"- {_action_line(row)}")
    return "\n".join(lines)


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


def _fmt_calendar_events(output: dict) -> str:
    from app.tools.calendar_tools import format_event_when  # local: avoid import cycle

    events = output.get("events") or []
    if not events:
        return "No events found."
    lines = [f"Found {len(events)} event(s):"]
    for e in events[:_MAX_NAMES]:
        summary = str(e.get("summary") or "(no title)")
        when = format_event_when(e)
        loc = str(e.get("location") or "").strip()
        line = f"• {summary} — {when}" if when else f"• {summary}"
        if loc:
            line += f", {loc}"
        lines.append(line)
    extra = len(events) - _MAX_NAMES
    if extra > 0:
        lines.append(f"… and {extra} more")
    return "\n".join(lines)


def _fmt_windows(output: dict) -> str:
    """Open windows, grouped by the application that owns them — the way a
    person scans a taskbar. The handle is shown because a later step needs it
    and because it is what the approval card will name."""
    windows = output.get("windows") or []
    if not windows:
        return "No matching windows are open."

    by_app: dict[str, list[dict]] = {}
    for w in windows[:_MAX_NAMES]:
        by_app.setdefault(str(w.get("process") or "").strip() or "Unknown app", []).append(w)

    lines = [f"{len(windows)} window(s) open:"]
    for app in sorted(by_app):
        lines.append(f"\n{app}:")
        for w in by_app[app]:
            lines.append(f"• {w.get('title') or '(untitled)'}  (handle {w.get('handle')})")
    if len(windows) > _MAX_NAMES:
        lines.append(f"… and {len(windows) - _MAX_NAMES} more")
    return "\n".join(lines)


def _fmt_screenshot(output: dict) -> str:
    """A screenshot's PATH and size — never the image, and never a description
    of it. This tool saves a file; it does not look at one."""
    path = output.get("path") or "?"
    width, height = output.get("width"), output.get("height")
    size = f" ({width}x{height})" if width and height else ""
    return f"Screenshot saved to `{path}`{size}."


def _fmt_clipboard(output: dict) -> str:
    """The clipboard's text, fenced. Fenced because it is UNTRUSTED prose the
    user copied from somewhere — it may carry its own markdown, and it must
    never read as part of Jarvis's own answer."""
    text = str(output.get("text") or "")
    if not text.strip():
        return "The clipboard is empty, or holds something that is not text."
    head = f"Clipboard ({output.get('length', len(text))} characters):"
    if output.get("truncated"):
        head = f"Clipboard (first {len(text)} of {output.get('length')} characters):"
    return f"{head}\n```\n{text}\n```"


def _fmt_home_devices(output: dict) -> str:
    """Home devices, grouped by room — the way a person thinks about them.

    A hub with 200 entities would otherwise render as 200 flat bullets, and the
    answer to "what's on downstairs?" would be unreadable. Item-level clipping
    (never mid-name), and a real source truncation is reported separately from
    our display budget — the 2026-07-29 lesson that "(truncated)" must mean a
    fact about the world, not that we ran out of room."""
    devices = output.get("devices") or []
    if not devices:
        return "No matching devices found."

    by_area: dict[str, list[dict]] = {}
    for d in devices[:_MAX_NAMES]:
        by_area.setdefault(str(d.get("area") or "").strip() or "No room set", []).append(d)

    lines = [f"Found {len(devices)} device(s):"]
    for area in sorted(by_area):
        lines.append(f"\n{area}:")
        for d in by_area[area]:
            name = str(d.get("name") or d.get("entity_id") or "?")
            state = str(d.get("state") or "unknown")
            lines.append(f"• {name} — {state}  ({d.get('entity_id')})")
    extra = len(devices) - _MAX_NAMES
    if extra > 0:
        lines.append(f"… and {extra} more")
    if output.get("truncated"):
        lines.append("(the hub reported more devices than were returned)")
    return "\n".join(lines)


# A result's content is worth rendering as its own fenced block past this;
# below it, it is a teaser and belongs inline, exactly as before.
_WEB_CONTENT_INLINE_MAX = 300


def _fmt_web_search(output: dict) -> str:
    """Render search results — INCLUDING the fact that a result's content was
    cut, when it was.

    Live bug 2026-07-16: Tavily's extracted page content was cut to 300 chars
    with NO marker, so a fragment ending mid-word at "## FIFA World Cup 2026™
    qualified t" entered the record looking like the whole of FIFA's
    qualified-teams page. That is worse than thin — SUMMARY_PROMPT tells the
    model "only call a list truncated if the results above literally say so —
    otherwise it is complete", so the record's SILENCE actively certified the
    fragment as complete. The model filled the gap with 112 invented countries.
    A marked cut is a signpost; an unmarked one is a lie the prompt notarizes."""
    results = output.get("results") or []
    if not results:
        return "No web results found."
    lines: list[str] = []
    # When the question was ambiguous the planner searched each reading, and the
    # merged rows alone would hide that: the summary would see one pile of pages
    # and could not tell that the user's wording admitted two answers. Naming the
    # readings is what lets it lead with the likely one and offer the other
    # (SUMMARY_PROMPT's ambiguity clause) instead of silently picking.
    queries = [str(q) for q in (output.get("queries") or []) if str(q).strip()]
    if len(queries) > 1:
        lines.append(
            "The question could be read more than one way, so each reading was "
            "searched: " + "; ".join(f'"{q}"' for q in queries)
        )
    lines.append(f"Found {len(results)} web result(s):")
    for r in results[:_MAX_NAMES]:
        title = str(r.get("title") or r.get("url") or "(untitled)")
        url = str(r.get("url") or "")
        snippet = str(r.get("snippet") or "").strip()
        content = str(r.get("content") or "").strip()
        lines.append(f"- **{title}** — {url}" if url else f"- **{title}**")
        # Substantive content becomes its own fenced block: it is untrusted
        # prose that may carry markdown of its own (the FIFA page's own "##"
        # headers would otherwise be injected straight into the record and into
        # the user's chat). Fencing is this file's existing containment
        # convention — see _fmt_read_webpage / _fmt_semantic_file_search.
        if content and len(content) > _WEB_CONTENT_INLINE_MAX:
            lines.append(_fence(content))
            if r.get("truncated"):
                # States the FACT and nothing else. An earlier cut of this
                # marker also named the remedy ("read_webpage on <url> returns
                # the rest") on the _missing_target principle — a record whose
                # own text steers the next move. Live verification 2026-07-16
                # showed why that was wrong here: the summary LLM copied the
                # remedy verbatim into the user's answer, a dozen times,
                # leaking a tool name into prose that must never mention tools.
                # The steer is unnecessary now anyway — evidence_resolver goes
                # and reads the page in CODE, so the only job left for this
                # marker is honesty about what the record does NOT contain.
                lines.append("  (this content is a partial extract, not the whole page)")
        else:
            # Short result: unchanged rendering — the teaser inline on the bullet.
            inline = content or snippet
            if inline:
                lines[-1] += f": {inline}"
    return "\n".join(lines)


def _fmt_read_webpage(output: dict) -> str:
    title = str(output.get("title") or "").strip()
    url = str(output.get("url") or "")
    head = f"Web page **{title}** — {url}" if title else f"Web page {url}"
    content = str(output.get("content") or "").strip()
    if not content:
        return head + "\n(The page has no readable text.)"
    return head + "\n" + _fence(content)


def _fmt_browse_page(output: dict) -> str:
    """The rendered observation, fenced. Fenced because it is untrusted page
    prose carrying its own markdown (the _fmt_web_search lesson: a snippet that
    literally contained '##' headers), and because the element list is a
    structural listing that must survive as one block."""
    title = str(output.get("title") or "").strip()
    url = str(output.get("url") or "")
    head = f"Browser page **{title}** — {url}" if title else f"Browser page {url}"
    rendered = str(output.get("rendered") or "").strip()
    if not rendered:
        return head + "\n(The page rendered nothing readable.)"

    # An aborted mutation is a BREAKAGE, not a mutation — the guard worked. Say
    # so plainly: a page that misbehaved silently is the confusing outcome.
    blocked = output.get("blocked") or {}
    note = ""
    if isinstance(blocked, dict) and blocked.get("blocked_mutations"):
        note = (
            f"\n({blocked['blocked_mutations']} request(s) the page tried to send "
            f"were blocked — browsing is read-only.)"
        )
    return head + note + "\n" + _fence(rendered)


def _fmt_browse(output: dict) -> str:
    """A browse run's result: what it accomplished, then — only when the run
    actually READ something — the final page's PROSE, fenced (untrusted page text
    carrying its own markdown, the _fmt_web_search lesson). Leads with the outcome
    so a summary can answer 'did it play?' / 'did it get there?' without reading
    any page at all."""
    title = str(output.get("title") or "").strip()
    url = str(output.get("url") or "")
    where = f"**{title}** — {url}" if title else url
    handoff = str(output.get("handoff") or "")
    # A media/play outcome's head line IS the complete answer ("opened it, playing").
    # Its final page is a video player whose element list (hundreds of links — a
    # site's whole episode index / A-Z footer) is pure noise dumped into the chat
    # (live 2026-07-25: a "done" notification was a wall of 161 DOM elements). So
    # the page is rendered ONLY for the informational fallback below (a browse that
    # READ a fact — the book-price case relies on the excerpt surviving).
    show_page = False
    # A DESTINATION-ONLY browse is the same case as a media outcome and was not
    # covered: the goal asked only to BE somewhere, so nothing was read and there
    # is nothing to report but arriving. Live 2026-08-01: "open youtube" finished
    # correctly in 1s and then replied with the entire YouTube homepage — 108
    # element lines plus every video title on it, 8,609 characters. The arrival
    # terminator that made that goal finish is also the fact that says its page
    # has nothing to say.
    destination_only = bool(output.get("destination_only"))
    if output.get("playing") and handoff == "clean_window":
        # Handed off to a normal, ad-blocked (uBlock) window (2026-07-22). Honest
        # about the one trade-off: a non-automation window can't be told to press
        # play, so a custom player may need one click.
        head = (
            f"Opened it in a normal ad-free browser window: {where}. "
            "It should start on its own — if the player doesn't, just press play "
            "once (the window has an ad-blocker, so no pop-ups)."
        )
    elif output.get("playing"):
        head = f"Now playing in the browser: {where}"
    elif handoff == "none":
        # A watch/play goal that found the video but could not open the clean
        # window (no system browser found, or a profile-lock handoff exit).
        head = (
            f"I found it — {where} — but couldn't open a browser window to play "
            "it. Open that link yourself to watch."
        )
    else:
        reason = str(output.get("done_reason") or "").strip()
        head = f"Browsed to {where}" + (f" — {reason}" if reason else "")
        show_page = not destination_only

    blocked = output.get("blocked") or {}
    if isinstance(blocked, dict) and blocked.get("blocked_mutations"):
        head += (
            f"\n({blocked['blocked_mutations']} request(s) the page tried to send "
            f"were blocked — browsing is read-only.)"
        )

    # Structured records the loop gathered with `extract` (Skyvern/Atlas parity) —
    # the answer to a list/compare goal ("the 3 cheapest phones", "highest-rated").
    # Rendered before the page fence so a listing goal is answered from the real
    # gathered data, not the raw element dump.
    extracted_block = _fmt_extracted(output.get("extracted"))

    # The page's PROSE, never `rendered`. `rendered` is observe.render's output —
    # "the observation as the LLM sees it", i.e. the DECISION prompt's format,
    # led by a numbered listing of every clickable element on the page. That
    # listing is agent scaffolding: it exists so the loop can say "click 23". It
    # answers no user's question and grounds no summary, and it was the larger
    # half of the 8,609-character wall. `rendered` stays in the tool output for
    # the audit record; it is not a report. Falls back to it only when the page
    # yielded no prose at all, so a page that is genuinely all controls still
    # shows something.
    page = str(output.get("page_text") or "").strip()
    if not page:
        page = str(output.get("rendered") or "").strip()
    return (
        head
        + extracted_block
        + ("\n" + _fence(page) if (show_page and page) else "")
    )


# How much gathered data to surface in a browse summary. Bounded so a large scrape
# never floods the chat; the full set is in the tool output for a follow-up.
_EXTRACTED_MAX_ROWS = 25
_EXTRACTED_MAX_CHARS = 4000


def _fmt_extracted(records: object) -> str:
    """Render the `extract` records as a compact numbered list, or "" when there
    are none. Each record is a flat dict of copied page values (str/number) — the
    loop's _coerce_record guarantees the shape, so this only lays them out."""
    if not isinstance(records, list) or not records:
        return ""
    rows = []
    for i, rec in enumerate(records[:_EXTRACTED_MAX_ROWS], 1):
        if not isinstance(rec, dict):
            continue
        parts = ", ".join(f"{k}: {v}" for k, v in rec.items() if str(v).strip())
        if parts:
            rows.append(f"{i}. {parts}")
    if not rows:
        return ""
    body = "\n".join(rows)
    if len(body) > _EXTRACTED_MAX_CHARS:
        body = body[:_EXTRACTED_MAX_CHARS] + "\n…"
    more = len(records) - len(rows)
    tail = f"\n…and {more} more." if more > 0 else ""
    return f"\n\nGathered from the page ({len(records)} item(s)):\n" + body + tail


def _one_commit_block(commit: dict, *, label: str = "") -> str:
    """One submitted form's grounded confirmation — its destination, the site's
    title, and the fenced response prose. `label` prefixes it in a multi-commit
    flow ("Form 2 of 3"). The response prose is the site's own, so it is untrusted
    and fenced (the _fmt_web_search lesson)."""
    url = str(commit.get("url") or "")
    title = str(commit.get("title") or "").strip()
    head = (f"{label}: " if label else "") + "Submitted the form" + (
        f" to {url}" if url else ""
    )
    # "The site responded: X" is only true when the submission MOVED us — on an
    # AJAX submit that never navigates, X is the page we were already on, and
    # calling it a response is the same class of overclaim as reporting an
    # unconfirmed submit as a failure (2026-08-02).
    if title:
        head += (
            f". The site responded: **{title}**"
            if commit.get("page_changed")
            else f". The page stayed on **{title}**"
        )
    else:
        head += "."
    # When the site carried the submission on a different url than the form's
    # declared action (a `.js`/`.json` twin — the Shopify add-to-cart idiom), the
    # record says so. The user approved a contract; the honest confirmation names
    # the request that actually delivered it (2026-07-26).
    sent_to = str(commit.get("submitted_url") or "").strip()
    if sent_to:
        head += f" (Sent as {sent_to}.)"
    response = str(commit.get("response_text") or "").strip()
    if response:
        return head + "\nThe page shows:\n" + _fence(response)
    return head


def _fmt_browse_commit(output: dict) -> str:
    """A SUBMITTED form's result. Lead with the CONFIRMED facts — the submit fired
    and the site's own response — never a restated goal: browse_commit had no
    formatter before (2026-07-18), so a commit's completion fell to the generic
    path and the summary LLM turned it into an ungrounded 'All done'. This grounds
    it in what the SERVER returned. The response prose is the site's, so it is
    untrusted and fenced (the _fmt_web_search lesson).

    MULTI-COMMIT (15.5): a flow that submitted several forms carries a
    `commit_history` (one record per approved submit). Render ONE grounded block
    per commit — every server response, not just the last — so a "applied to 3
    jobs" flow quotes what each site actually said."""
    history = output.get("commit_history")
    if isinstance(history, list) and len(history) > 1:
        n = len(history)
        parts = [f"Submitted {n} forms — each one you approved separately:"]
        for i, commit in enumerate(history, 1):
            if isinstance(commit, dict):
                parts.append(_one_commit_block(commit, label=f"Form {i} of {n}"))
        if output.get("window_open"):
            parts.append(
                "(The last form's browser window is left open so you can see the "
                "result — close it from the status bar when done.)"
            )
        return "\n\n".join(parts)

    head = _one_commit_block(output)
    if output.get("window_open"):
        head += (
            "\n(The browser window is left open so you can see the result — "
            "close it from the status bar when done.)"
        )
    return head


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


def _fmt_batch_files(output: dict) -> str:
    """A bulk move/delete outcome, grounded in the tool's own per-file record.

    BOTH halves are always reported. A partial batch is the normal case (a
    name collision, a locked file), and "moved 84 of 85" with the one failure
    NAMED is the whole point — a bulk action that quietly did less than it
    said is the defect this feature was built to remove."""
    moved = output.get("moved")
    if isinstance(moved, list):
        done, verb, where = moved, "Moved", output.get("destination")
        names = [PurePath(str(m.get("moved_to") or "")).name for m in done]
        head = f"{verb} {len(done)} file(s)" + (f" into `{where}`" if where else "")
    else:
        done = output.get("deleted") or []
        names = [PurePath(str(d.get("deleted") or "")).name for d in done]
        head = f"Deleted {len(done)} file(s) (recoverable from the trash)"
    total = output.get("total_bytes")
    if isinstance(total, int) and total:
        head += f" — {_human_size(total)}"
    lines = [head]
    if names:
        lines.append(f"- {_names([n for n in names if n])}")
    failed = output.get("failed") or []
    if failed:
        lines.append(f"- {len(failed)} could NOT be done:")
        for item in failed[:_MAX_NAMES]:
            lines.append(
                f"  - `{item.get('path')}` — {item.get('error')}"
            )
        if len(failed) > _MAX_NAMES:
            lines.append(f"  - … and {len(failed) - _MAX_NAMES} more")
    return "\n".join(lines)


_RESULT_FORMATTERS = {
    "list_directory": _fmt_list_directory,
    "search_files": _fmt_search_files,
    "move_files": _fmt_batch_files,
    "delete_files": _fmt_batch_files,
    "semantic_file_search": _fmt_semantic_file_search,
    "read_file": _fmt_read_file,
    "run_command": _fmt_shell,
    "execute_script": _fmt_shell,
    "recall_memory": _fmt_recall_memory,
    "recall_actions": _fmt_recall_actions,
    "lookup_contact": _fmt_lookup_contact,
    "search_emails": _fmt_search_emails,
    "read_email": _fmt_read_email,
    "read_thread": _fmt_read_thread,
    "list_events": _fmt_calendar_events,
    "find_events": _fmt_calendar_events,
    "list_devices": _fmt_home_devices,
    "get_device_state": _fmt_home_devices,
    "list_windows": _fmt_windows,
    "take_screenshot": _fmt_screenshot,
    "read_clipboard": _fmt_clipboard,
    "web_search": _fmt_web_search,
    "read_webpage": _fmt_read_webpage,
    "browse_page": _fmt_browse_page,
    "browse": _fmt_browse,
    "browse_commit": _fmt_browse_commit,
}


def _render_step(step) -> str | None:
    """One step's real output as readable text, or None when there is nothing
    to show. Shared by the deterministic completion text and the inline summary
    LLM's input — both always see the same rendering.

    The cap is PER TOOL (_step_cap): web steps carry the prose a factual answer
    rests on, so the file-listing default would starve them.

    PARTIAL EVIDENCE (2026-07-26). A FAILED step used to render as nothing at
    all, which was right while every failure carried output=None. It stopped
    being right when the browser tools learned to salvage (browser_tools._partial):
    a browse that reached eBay's results, extracted the listings and then hit its
    action cap now HAS the answer on a failed step, and dropping it here would
    discard the evidence one layer above where it was rescued.

    So a failed step renders IFF it carries structured output a code formatter
    can read — and it is labelled, because the difference between "here is the
    answer" and "here is as far as I got" is the whole honesty of the report. A
    failed step with no output still renders nothing; the error prose already
    covers it."""
    partial = False
    if step.status != StepStatus.COMPLETED:
        if step.status != StepStatus.FAILED:
            return None
        output = step.result.output if step.result else None
        if not isinstance(output, dict) or step.tool not in _RESULT_FORMATTERS:
            return None
        partial = True
    cap = _step_cap(step.tool)
    output = step.result.output if step.result else None
    if partial:
        body = _clip(_RESULT_FORMATTERS[step.tool](output), cap)
        reason = str((step.result.error if step.result else "") or "").strip()
        head = "PARTIAL — this step did NOT finish"
        if reason:
            head += f" ({reason})"
        return f"{head}. What it got as far as:\n{body}"
    formatter = _RESULT_FORMATTERS.get(step.tool)
    if formatter is not None and isinstance(output, dict):
        return _clip(formatter(output), cap)
    if step.requires_approval:
        # Write/destructive tools: the approved description IS the record
        # of what happened; their outputs are bookkeeping (trash paths).
        return f"Done: {step.description}"
    if output is not None:
        return _clip(json.dumps(output, default=str), cap)
    return None


def completed_results_text(plan: AgentPlan) -> str:
    """Deterministic rendering of what the completed steps actually produced.
    Background completions never get an LLM summary, so this block IS the
    answer — without it, "how many files are in phase3test" finished as
    "Done — 1 step(s) completed." with the answer nowhere (live bug,
    2026-07-09). Also the inline flow's fallback when the summary LLM fails.

    Allocation is FAIR-SHARE, not arrival order (_fair_shares) — the last step's
    results can never be dropped to make room for the first's. Every step is
    therefore represented; a step that did not fit its share is clipped with a
    visible marker rather than omitted, and the block then points at the
    complete record."""
    rendered = [r for r in (_render_step(s) for s in plan.steps) if r is not None]
    shares = _fair_shares(rendered)
    blocks = [_clip(r, share) for r, share in zip(rendered, shares)]
    text = "\n\n".join(blocks)
    if any(len(b) > share for b, share in zip(rendered, shares)):
        # Something was cut. Nothing is silently MISSING (every step rendered,
        # every cut is marked), but the user still needs to know where the
        # untruncated record lives.
        text += "\n\n… some results were truncated — see the Activity timeline."
    return text


def steps_for_summary(plan: AgentPlan) -> str:
    """The completed steps rendered for the inline summary LLM's prompt.
    The LLM NEVER sees raw JSON — it can only re-present text a code
    formatter already made readable. Live bug 2026-07-10: the summary prompt
    carried json.dumps of the step output cut at 2000 chars, so the LLM
    pasted escaped JSON into the chat showing ~11 of 52 search matches and
    called the complete list "truncated".

    Allocation is FAIR-SHARE, not arrival order (_fair_shares). The old
    accumulate-and-break dropped the LAST steps — and a step whose RESULT is
    "(further step results omitted)" is precisely the empty record that invites
    invention (live bug 2026-07-16: the third of three questions)."""
    pairs = [
        (step, rendered)
        for step, rendered in ((s, _render_step(s)) for s in plan.steps)
        if rendered is not None
    ]
    shares = _fair_shares([r for _, r in pairs])
    return "\n\n".join(
        f"ACTION: {step.description}\nRESULT: {_clip(rendered, share)}"
        for (step, rendered), share in zip(pairs, shares)
    )


def serialize_plan_for_api(plan: AgentPlan) -> dict:
    """The serialized AgentPlan every endpoint and push event carries, plus
    the requires_approval convenience flag so the frontend never
    string-compares the status enum.

    An approval pause also carries its contract in SPOKEN form and the hash that
    binds it (2026-08-03). Added HERE, in the one serializer every surface goes
    through, so the inline plan chunk, the background `task` push and the phone
    surface all get it without a second copy — and so a client can only echo a
    hash it was actually given."""
    data = plan.model_dump(mode="json")
    data["requires_approval"] = plan.status == PlanStatus.AWAITING_APPROVAL
    if plan.status == PlanStatus.AWAITING_APPROVAL:
        from app.agents.spoken import spoken_plan_text

        data["spoken_contract"] = spoken_plan_text(plan)
        data["contract_hash"] = plan.contract_hash()
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
            # A failed plan must not throw away what its completed steps FOUND
            # (2026-07-21: the book's price sat in a completed browse step's
            # output and the failure text dropped it — "what was the price?"
            # was unanswerable one turn later). Same renderer the completion
            # path uses; already capped and item-clipped.
            results = completed_results_text(plan)
            text = (
                f"I couldn't finish that. {base}\n\n"
                f"Steps that did complete before the failure: {done}."
            )
            return f"{text}\n\n{results}" if results else text
        return f"I couldn't do that. {base}"
    if plan.status == PlanStatus.PAUSED:
        # Stopped by the user mid-run (2026-08-03). They are about to tell it
        # what to change, so the text must show WHERE IT GOT TO: the results so
        # far (same renderer every other path uses) and the steps still queued.
        # Without the remaining list "carry on" is a blind choice.
        base = plan.message or "Paused — nothing further was executed."
        parts = [base]
        results = completed_results_text(plan)
        if results:
            parts.append(results)
        pending = plan.pending_steps()
        if pending:
            lines = ["Still to run:"]
            for i, s in enumerate(pending, 1):
                tag = _PERMISSION_TAGS.get(
                    s.permission_level.value, s.permission_level.value
                )
                lines.append(f"{i}. {s.description} ({tag})")
            parts.append("\n".join(lines))
        return "\n\n".join(parts)
    if plan.status == PlanStatus.CANCELLED:
        return plan.message or "Cancelled by the user — nothing further was executed."
    # COMPLETED (or any unexpected status): the text must CARRY the results —
    # in the background flow this deterministic message is the only channel
    # the answer can arrive by ("Done — 1 step(s) completed." told the user
    # nothing about the files they asked for, live bug 2026-07-09).
    base = plan.message or f"Done — {len(plan.completed_steps())} step(s) completed."
    results = completed_results_text(plan)
    return f"{base}\n\n{results}" if results else base
