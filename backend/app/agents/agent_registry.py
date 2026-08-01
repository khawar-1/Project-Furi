"""
Jarvis OS — Domain agent registry (the "boss + specialized agents" model)

Jarvis is the BOSS: it classifies a task's domain and hands it to the matching
agent. Every agent is the SAME proven ``AgentPlanner`` engine — it shares the
approval gate (``registry.execute_tool``), the grounding/recipient/event-id/
upload locks, the path guards, and the background ``task_runner``. An agent is a
*specialization*, not a new engine: a FOCUSED TOOL SUBSET plus a one-line
PERSONA the planner prompt carries.

The specialization is enforced structurally but at ZERO execution cost: the
subset only filters the tool catalog the planner shows the LLM (``_tools_json``).
The model can only draft steps from tools it was shown, so an agent stays in its
lane WITHOUT touching ``execute_tool`` — no execution change, no regression
surface. The approval gate remains the real guard on top.

"Tools not starved" (the cross-domain decision): every agent also carries the
SHARED READ tools, so a primary-domain agent can still CHAIN a legitimate
cross-domain read — the email agent keeps ``search_files``/``read_file`` for
"find the file about X and email it to me", one worker, one plan, no
decomposition. ``general`` (the fallback for an unknown/blank label) sees ALL
tools — exactly the pre-agent behavior, so nothing regresses.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class AgentSpec:
    """A domain agent = a focused tool surface + a planner persona. ``tools=None``
    means "every registered tool" (the general/cross-domain fallback)."""

    key: str
    display_name: str
    label: Optional[str]  # the classifier label this agent serves (None = general)
    tools: Optional[frozenset[str]]  # None = all tools (no filtering)
    persona: str


# Read tools every agent gets, so cross-domain chaining is never starved. All
# strictly READ (safe to share widely): memory/contact/audit recall + file reads.
_SHARED_READS: frozenset[str] = frozenset({
    "recall_memory",
    "lookup_contact",
    "recall_actions",
    "search_files",
    "read_file",
    "list_directory",
    "semantic_file_search",
})


def _spec(key: str, name: str, label: Optional[str], extra: set[str], persona: str) -> AgentSpec:
    return AgentSpec(
        key=key,
        display_name=name,
        label=label,
        tools=frozenset(extra) | _SHARED_READS,
        persona=persona,
    )


AGENTS: dict[str, AgentSpec] = {
    "file": _spec(
        "file", "File agent", "TASK",
        {"move_file", "move_files", "rename_file", "create_folder", "create_file",
         "delete_file", "delete_files", "run_command", "execute_script"},
        "You are Jarvis's file & system agent: you work with the user's files, "
        "folders, and terminal. Stay within file/system work; use your read "
        "tools to locate things before you change them.",
    ),
    "email": _spec(
        "email", "Email agent", "EMAIL",
        {"search_emails", "read_email", "read_thread",
         "create_email_draft", "send_email", "reply_email"},
        "You are Jarvis's email agent: you search, read, draft, send, and reply "
        "to the user's Gmail. You may read the user's files when a task needs "
        "you to reference or attach one.",
    ),
    "calendar": _spec(
        "calendar", "Calendar agent", "CALENDAR",
        {"list_events", "find_events", "create_event", "update_event", "delete_event"},
        "You are Jarvis's calendar agent: you look at and change the user's "
        "Google Calendar events.",
    ),
    "research": _spec(
        "research", "Research agent", "WEB",
        {"web_search", "read_webpage", "browse_page"},
        "You are Jarvis's research agent: you search the web and read pages to "
        "look up online information. You only READ the web — you never act on a "
        "live site or submit anything.",
    ),
    "browser": _spec(
        "browser", "Browser agent", "BROWSE",
        {"browse", "browse_commit", "stop_media", "browse_page",
         "web_search", "read_webpage"},
        "You are Jarvis's browser agent: you drive a real browser to ACT on the "
        "live sites the user named — play/watch media, sign in and navigate, "
        "fill and submit forms. Only visit sites the user named.",
    ),
    # Cross-domain / unknown fallback: the full registry (pre-agent behavior).
    "general": AgentSpec(
        key="general", display_name="Jarvis", label=None, tools=None,
        persona="",
    ),
}

GENERAL = AGENTS["general"]

_LABEL_TO_KEY: dict[str, str] = {
    spec.label: key for key, spec in AGENTS.items() if spec.label is not None
}


def agent_for_label(label: Optional[str]) -> AgentSpec:
    """The agent that serves a classifier label. An unknown/None label (and the
    CHAT non-action label) falls to ``general`` — never an error."""
    if not label:
        return GENERAL
    return AGENTS.get(_LABEL_TO_KEY.get(label.strip().upper(), "general"), GENERAL)


def agent_for_key(key: Optional[str]) -> AgentSpec:
    """Rebuild an agent from a persisted ``AgentPlan.agent_key`` (resume path).
    An unknown/None key falls to ``general``."""
    if not key:
        return GENERAL
    return AGENTS.get(key, GENERAL)
