"""
Jarvis OS — The approval contract, spoken (2026-08-03)

*(Tier 2, item 8 of `suhhestionsfromclaude.txt` — "approval needs a screen")*

WHAT WAS ALREADY TRUE, AND IS NOT REBUILT HERE
----------------------------------------------
`rendering.deterministic_plan_text` has always been a single, LLM-FREE,
code-derived string, and `task_router.plan_run_events` yields it as an ordinary
text delta right after the plan chunk — so `voiceOutput.onDelta` receives it and
an INLINE plan's approval contract is already spoken today. The gap was never
that the text did not exist.

The gap was that it is written FOR EYES. It is a numbered list of paths, one per
line, and `sanitize_for_speech` reduces each path to its basename — so a
destructive delete reads aloud as a run of bare filenames with no shape:

    "1. Delete 3 files from the phase3test folder (DESTRUCTIVE)
        → delete 3 file(s) → moved to trash (~/.jarvis/trash)
       C:\\Users\\DELL\\Desktop\\phase3test\\a.txt   … and so on"

A person cannot hold that. What they can hold is the COUNT, the KIND of change,
and WHERE it lands. So this is a second rendering of the SAME facts — the step's
tool, its parameters and its code-derived `action_detail` — not a second source
of truth, and like its visual twin it involves NO LLM.

⚠️ COVERAGE IS ENFORCED, NOT ASSUMED. `test_every_non_read_tool_has_a_spoken_form`
walks the REGISTRY: every non-READ tool must have an entry here or be listed in
`SPOKEN_FALLBACK` with a reason. A new destructive tool that nobody taught to
speak would otherwise be approved aloud as "step 1" — the
`test_every_path_param_is_covered_or_exempt` discipline, which found a real hole
on its first run.
"""
from __future__ import annotations

import os
import re
from typing import Any, Callable

from app.agents.schemas import AgentPlan, PermissionLevel, PlanStatus, StepStatus

# Speech is linear and unskimmable, so a spoken contract that lists twenty
# filenames is worse than one that says "twenty files". Past this many, names
# give way to a count.
MAX_SPOKEN_NAMES = 3


def _base(path: Any) -> str:
    """A path as a person would say it. `sanitize_for_speech` would do this at
    the TTS boundary anyway, but doing it here means the spoken contract IS the
    record — what was said aloud is what this function returned."""
    text = str(path or "").strip().rstrip("\\/")
    return os.path.basename(text) or text


def _names(values: Any) -> str:
    """Name a few, count the rest. Never read a long list aloud."""
    items = [v for v in (values if isinstance(values, list) else [values]) if v]
    if not items:
        return "nothing"
    if len(items) == 1:
        return _base(items[0])
    if len(items) <= MAX_SPOKEN_NAMES:
        spoken = [_base(i) for i in items]
        return ", ".join(spoken[:-1]) + f" and {spoken[-1]}"
    return f"{len(items)} files"


def _where(path: Any) -> str:
    parent = os.path.dirname(str(path or "").rstrip("\\/"))
    name = os.path.basename(parent)
    return f" in {name}" if name else ""


# One spoken form per non-READ tool. Each reads the SAME parameters the visual
# contract renders — a different sentence about identical facts.
SPOKEN_STEPS: dict[str, Callable[[dict], str]] = {
    # --- files -------------------------------------------------------------
    "create_file": lambda p: f"create a file called {_base(p.get('path'))}{_where(p.get('path'))}",
    "create_folder": lambda p: f"create a folder called {_base(p.get('path'))}",
    "move_file": lambda p: (
        f"move {_base(p.get('source'))} into {_base(p.get('destination'))}"
    ),
    "move_files": lambda p: (
        f"move {_names(p.get('sources'))} into {_base(p.get('destination'))}"
    ),
    "rename_file": lambda p: (
        f"rename {_base(p.get('path'))} to {_base(p.get('new_name') or p.get('new_path'))}"
    ),
    "delete_file": lambda p: (
        f"delete {_base(p.get('path'))}{_where(p.get('path'))} — it goes to the trash"
    ),
    "delete_files": lambda p: (
        f"delete {_names(p.get('paths') or p.get('sources'))} — they go to the trash"
    ),
    # --- terminal ----------------------------------------------------------
    "run_command": lambda p: f"run the command {p.get('command') or 'nothing'}",
    "execute_script": lambda p: f"run the script {_base(p.get('script_path'))}",
    # --- email -------------------------------------------------------------
    "send_email": lambda p: (
        f"send an email to {_recipients(p)} about {p.get('subject') or 'no subject'}"
    ),
    "reply_email": lambda p: (
        f"reply to that email — the address comes from the message itself, "
        f"not from me"
    ),
    "create_email_draft": lambda p: (
        f"save a draft to {_recipients(p)} about {p.get('subject') or 'no subject'}"
        f" — it is not sent"
    ),
    # --- calendar ----------------------------------------------------------
    "create_event": lambda p: (
        f"put {p.get('summary') or 'an event'} in your calendar"
        + (f" on {p.get('start')}" if p.get("start") else "")
    ),
    "update_event": lambda p: "change an event in your calendar",
    "delete_event": lambda p: "delete an event from your calendar",
    # --- browser -----------------------------------------------------------
    "browse_commit": lambda p: "submit a form on that website",
}

# Non-READ tools that deliberately fall back to the step's own description.
# Empty today — every one has a spoken form. The set exists so that adding a
# tool is a DECISION (add a form, or write it here and say why) rather than a
# silent gap the coverage test would otherwise have to allow.
SPOKEN_FALLBACK: frozenset[str] = frozenset()


def _recipients(params: dict) -> str:
    to = params.get("to")
    items = to if isinstance(to, list) else [to]
    items = [str(i) for i in items if i]
    if not items:
        return "nobody"
    if len(items) == 1:
        return items[0]
    return f"{len(items)} people"


def spoken_step(step: Any) -> str:
    """One step, in words. Falls back to the step's own description — which is
    LLM-authored, so it is the LAST resort and never the first."""
    render = SPOKEN_STEPS.get(step.tool)
    if render is not None:
        try:
            text = render(step.parameters or {})
            if text:
                return text
        except Exception:  # pragma: no cover — a bad param must not mute the card
            pass
    return (step.description or step.tool).strip()


def spoken_plan_text(plan: AgentPlan) -> str:
    """The approval contract, shaped for the ear.

    Returns "" for anything that is not an approval pause — the caller then has
    nothing to speak, which is the honest outcome for a plan that is not asking
    for consent."""
    if plan.status != PlanStatus.AWAITING_APPROVAL:
        return ""
    pending = [s for s in plan.steps if s.status == StepStatus.PENDING]
    if not pending:
        return ""

    destructive = [s for s in pending if s.permission_level.value == "destructive"]
    parts: list[str] = []
    if len(pending) == 1:
        parts.append(f"I'm about to {spoken_step(pending[0])}.")
    else:
        parts.append(f"I'm about to do {len(pending)} things.")
        parts.extend(f"{i}. {spoken_step(s)}." for i, s in enumerate(pending, 1))

    if destructive:
        # Say the word. A spoken contract that does not name the risk is the
        # one thing worse than no spoken contract at all.
        parts.append(
            "That includes something destructive."
            if len(destructive) == 1
            else f"{len(destructive)} of those are destructive."
        )
    parts.append("Nothing has changed yet.")
    # ⚠️ THE CONTRACT MUST TEACH ITS OWN PHRASE. `is_spoken_approval` below
    # deliberately refuses a bare "yes", so a user who is never told what to say
    # is left guessing — which is the exact "magic word" dead end the 2026-07-17
    # round was written to kill.
    parts.append('Say "approve" to go ahead, or "cancel" to drop it.')
    return " ".join(parts)


# --------------------------------------------------------------- spoken consent

# ⚠️ ITS OWN WORD SET. THREE now exist and they must not be merged:
#
#   planner._CARRY_ON_RE      "carry on" at a PAUSE — and it accepts "never
#                             mind" / "nvm", which at an approval card mean the
#                             OPPOSITE. Reusing it for consent would read a
#                             request to DROP a delete as permission to RUN it.
#                             That contradiction is measured and frozen in
#                             test_the_carry_on_word_set_would_have_flipped_a_delete_on.
#   task_router._is_typed_approval   used ONLY to REFUSE and nudge to the card.
#   this                      the only one that can GRANT, and the narrowest.
#
# ⚠️ AND IT IS NARROWER THAN THE TYPED ONE ON PURPOSE. A bare "yes"/"ok"/"sure"
# does NOT approve here, though it does satisfy `_is_typed_approval`. The
# channel is the reason: a typed "yes" was at least aimed at the card, while a
# spoken one may be ambient — said to someone in the room, or to the television,
# while the mic is open. Requiring an explicit approval verb makes consent an
# ACT rather than a coincidence, and the spoken contract ends by naming it.
_SPOKEN_APPROVE_RE = re.compile(
    r"^\W*(?:(?:yes|yeah|yep|ok|okay|sure)[\s,]+)?"
    r"(?:approve[d]?|approve\s*it|confirm(?:ed|\s*it)?|"
    r"go\s*ahead|do\s*it|send\s*it|permission\s*granted)\b",
    re.IGNORECASE,
)
# Filler that may trail consent without turning it into an instruction.
_SPOKEN_NOISE = frozenset({
    "please", "now", "jarvis", "thanks", "thank", "you", "sir", "it", "that",
    "then", "and", "just", "go", "ahead", "on", "with", "the", "task", "all",
    "of", "them", "yes", "ok", "okay", "sure", "do", "this", "for", "me",
})


def is_spoken_approval(utterance: str) -> bool:
    """True when a SPOKEN utterance is unambiguous consent.

    Whole-utterance by construction, the `_is_typed_approval` rule: "approve"
    is consent, "approve but not the second one" has substantive words left and
    is a STEER. Fails CLOSED — anything unrecognised is not consent, and the
    caller re-offers the contract rather than guessing."""
    text = (utterance or "").strip()
    match = _SPOKEN_APPROVE_RE.match(text)
    if match is None:
        return False
    rest = re.findall(r"[\w'-]+", text[match.end():].lower())
    return not [w for w in rest if w not in _SPOKEN_NOISE]


def plan_needs_screen(plan: AgentPlan, level: str) -> bool:
    """Whether this plan may NOT be approved by voice at the configured level.

    `level` is `VoiceConfig.spoken_approval`: "off" (never), "write" (WRITE
    steps only — anything DESTRUCTIVE still needs eyes on the card), or "all"."""
    if level == "all":
        return False
    if level != "write":
        return True
    return any(
        s.permission_level == PermissionLevel.DESTRUCTIVE
        for s in plan.steps
        if s.status == StepStatus.PENDING
    )
