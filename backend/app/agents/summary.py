"""
Jarvis OS — Completed-plan summary (the ONE LLM voice for plan outcomes)

Extracted from task_router.py (2026-07-12) so the agent HTTP endpoints can
render the SAME completion words the typed-chat path streams — before this,
an inline plan answered by a clicked option (/api/agent/choose) or resumed by
the Approve button finished silently: the summary machinery existed only in
the chat SSE path (live bug: "find all PDF files in downloads … tell me how
many" → folder question → click → "Completed — 1 step ran." and no answer).

Deliberately NOT in rendering.py: rendering stays LLM-free so "no LLM call
ever happens in the runner" remains auditable from imports alone. The summary
LLM only ever sees code-rendered readable step results (steps_for_summary),
never raw JSON, and falls back to the deterministic completion text — an LLM
outage degrades to a complete, honest answer, never to silence.
"""
import re
from datetime import datetime

from loguru import logger

from app.agents.evidence_resolver import WEB_THIN_CONTENT_CHARS, _needs_more
from app.agents.rendering import deterministic_plan_text, steps_for_summary
from app.agents.schemas import AgentPlan, StepStatus
from app.providers.base import LLMMessage, LLMProvider

# ------------------------------------------------------- fabrication guard
# The empty-record guard below asks "does the record have ANYTHING?" — a
# question with a certain answer. This guard has to ask a different one:
# "does the OUTPUT rest on the RECORD?"
#
# Live incident 2026-07-16: asked which teams were going to the FIFA finals,
# the record held a 300-char fragment of FIFA's qualified-teams page, cut
# mid-word exactly where the list began. The model emitted 112 country bullets
# — India, Brunei, Timor-Leste, Seychelles, Lesotho… — for a 48-team
# tournament, and credited them to FIFA. SUMMARY_PROMPT already said "never
# invent" and "Copy names EXACTLY". This defect is the proof of the
# prompts-are-not-guarantees doctrine, not an exception to it.
#
# What is NOT done here, deliberately: gating on "is the record incomplete?"
# and falling back. Incompleteness is a JUDGEMENT about whether the answer is
# present — "who is the president of Pakistan" has a short record whose 200
# chars contain the complete correct answer, and such a gate would replace a
# good answer with a step dump. A guard cannot inherit certainty from a
# predicate that lacks it. So an open evidence gap only selects the
# verification MODE; the VERDICT always comes from a substring test against the
# record — the same shape as _recipient_violation / _scope_violation, which
# reject output tokens not traceable to a grounding corpus.
_FABRICATION_MIN_BULLETS = 8    # below this a list is an answer, not an enumeration
_FABRICATION_UNGROUNDED = 0.6   # share of bullets absent from the record
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)\s*$", re.MULTILINE)
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'\-]{2,}")
# Words that carry no grounding signal — a bullet made only of these is skipped
# rather than judged.
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "that", "this", "are", "was", "were",
    "has", "have", "had", "not", "but", "all", "any", "its", "their", "they",
})


def _step_output(step) -> dict:
    output = step.result.output if step.result else None
    return output if isinstance(output, dict) else {}


def _read_is_substantive(step) -> bool:
    """A read_webpage that actually came back with a page worth answering from."""
    return (
        step.tool == "read_webpage"
        and step.status == StepStatus.COMPLETED
        and len(str(_step_output(step).get("content") or "")) >= WEB_THIN_CONTENT_CHARS
    )


def _plan_has_unresolved_web_gap(plan: AgentPlan) -> bool:
    """True when ANY completed web search's evidence is known-incomplete AND
    nothing went and filled the gap.

    ANY, not "the whole record is incomplete": the incident turn was MIXED —
    two substantive questions and one starved one. A plan-level "is the record
    thin?" test answers *no* to that turn and would never fire on the very
    incident it was written for.

    "Nothing filled the gap" is what keeps verification RARE. The
    evidence_resolver splices its read_webpage directly after the search it
    enriches, so a search followed by a substantive read is resolved — the page
    is in the record and the model has no hole to invent into. Only a gap that
    is still open at summary time (the escalation failed, or was capped, or the
    source is genuinely thin) buys the buffering."""
    for i, step in enumerate(plan.steps):
        if step.status != StepStatus.COMPLETED:
            continue
        if step.tool == "read_webpage" and not step.auto_escalated:
            # A page the PLAN chose to read: if it came back with nothing much,
            # that is an open gap of its own.
            if not _read_is_substantive(step):
                return True
            continue
        if step.tool != "web_search":
            continue
        rows = _step_output(step).get("results")
        rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
        if not rows or not _needs_more(rows):
            continue
        # Incomplete — was it filled by the read spliced right after it?
        following = plan.steps[i + 1] if i + 1 < len(plan.steps) else None
        if following is not None and following.auto_escalated and _read_is_substantive(following):
            continue
        return True
    return False


def _is_fabricated_enumeration(text: str, rendered: str) -> bool:
    """True when the summary enumerates many items the record never mentions.

    Two gates, both of which must hold — the caller has already established
    that a web step left an open evidence gap:
      1. it is a long list (>= _FABRICATION_MIN_BULLETS), not a short answer;
      2. and most bullets' significant words appear NOWHERE in the record.
    A 40-name file listing passes (the names are in the record). A two-line
    answer passes (gate 1). 112 invented countries do not."""
    bullets = [b for b in _BULLET_RE.findall(text) if b.strip()]
    if len(bullets) < _FABRICATION_MIN_BULLETS:
        return False
    haystack = rendered.lower()
    judged = ungrounded = 0
    for bullet in bullets:
        tokens = [
            t for t in _TOKEN_RE.findall(bullet)
            if t.lower() not in _STOPWORDS
        ]
        if not tokens:
            continue
        judged += 1
        if not any(t.lower() in haystack for t in tokens):
            ungrounded += 1
    if judged < _FABRICATION_MIN_BULLETS:
        return False
    return (ungrounded / judged) > _FABRICATION_UNGROUNDED

SUMMARY_PROMPT = """You are Jarvis, the user's personal AI — composed, precise, quietly capable. You just finished executing a task for them. Report the outcome in that register: you may address the user as "sir" once (an opening or closing beat, e.g. "All done, sir."), and a single touch of understated dry wit is acceptable when the outcome is good news — but the persona NEVER changes the facts you report or adds anything beyond them.

CURRENT DATE/TIME: {now}

THE USER ASKED:
{goal}

WHAT WAS DONE AND WHAT IT FOUND (already rendered as readable text — this is the COMPLETE record):
{steps}

Write the reply to the user:
- Start with one short first-person sentence saying what was done.
- When the user asked to SEE data (file/folder names, file contents, command output), present ALL of it from the results above: names as a markdown bullet list (you may group folders and files), file contents and command output in a fenced code block. Never summarize the data away.
- When the user asked a QUESTION that web results answer, ANSWER IT: lead with the answer in a sentence or two and name the source once. Do NOT walk through the results one by one, quote each source, or describe what each page "states" — the results are evidence for you to read, not a report to recite. Answer each of several questions in its own short section.
- If the question could reasonably be read more than one way and the results answer more than one of those readings, lead with the reading the user most likely meant — use the current date above and what they said to judge it — and then add ONE short line offering the other (e.g. "If you meant X instead, say the word."). Never silently answer only the less likely reading, and never make the user re-ask to get the obvious one.
- Copy names, paths, numbers, and contents EXACTLY as written above — never invent, drop, round, or embellish anything.
- Only call a list truncated if the results above literally say so — otherwise it is complete.
- When a result says its content was cut off, SAY SO and report only what is actually there — never present a cut-off list as if it were the whole of it, and never fill the gap from your own knowledge.
- Never output JSON, curly braces, or escaped backslashes; do not mention tools, steps, or plans."""


def _now_text() -> str:
    """Local wall-clock, in the planner's format (planner.py's _context_block).

    The model WRITING the answer had no idea what day it was — the planner knew,
    the summary did not, and the gap cost a live answer. 2026-07-17: asked which
    teams had "qualified for the fifa worldcup final 2026" two days before the
    final, it reported the 48 teams that qualified for the tournament. Both
    readings are defensible English ("the World Cup Finals" IS the tournament in
    football usage) — but "the final is in two days" is exactly the signal that
    settles which one a person means, and it was the one thing not on the table.
    Cheap, and it disambiguates far more than sport: "the latest release",
    "this quarter", "who is the champion" all turn on today's date."""
    now = datetime.now()
    return f"{now.strftime('%Y-%m-%d %H:%M')} ({now.strftime('%A')})"


async def stream_completed_summary(provider: LLMProvider, plan: AgentPlan):
    """Stream a natural-language summary of a completed plan. Falls back to
    the deterministic text if the LLM stream fails before producing anything.
    The step results are handed over as code-rendered readable text
    (steps_for_summary), NEVER raw JSON — the LLM cannot paste JSON it never
    received (live display bug, 2026-07-10). A plan with NOTHING rendered to
    report (no completed step produced output — e.g. every step was a
    zero-match SKIP) never reaches the LLM at all: asking a model to 'report
    the outcome' of an empty record invites invention (live bug 2026-07-13:
    it fabricated file1/2/3.pdf for a 0-step plan).

    A record with an UNRESOLVED web evidence gap gets one extra safeguard: the
    stream is buffered and grounding-checked before delivery (verification
    mode) — an empty record invites invention, and so, it turns out, does a
    starved one (live bug 2026-07-16: 112 invented countries). Verification
    mode costs live streaming, so it is entered ONLY on a gap nothing filled;
    every other turn — including a web turn whose evidence was enriched by the
    evidence_resolver — streams delta-by-delta exactly as before, and Phase 7
    Part 4's sentence-by-sentence speech is untouched."""
    rendered = steps_for_summary(plan)
    if not rendered.strip():
        yield deterministic_plan_text(plan)
        return
    prompt = SUMMARY_PROMPT.format(goal=plan.goal, steps=rendered, now=_now_text())
    verify = _plan_has_unresolved_web_gap(plan)
    parts: list[str] = []
    produced = False
    try:
        async for delta in provider.stream_chat(
            messages=[LLMMessage(role="user", content=prompt)], temperature=0.3,
        ):
            produced = True
            if verify:
                parts.append(delta)   # buffer — nothing is delivered yet
            else:
                yield delta
    except Exception as e:
        logger.warning(f"Task summary stream failed: {e}")
    if not produced:
        yield deterministic_plan_text(plan)
        return
    if verify:
        text = "".join(parts)
        if _is_fabricated_enumeration(text, rendered):
            # The model enumerated items the record does not contain. Discard
            # the whole reply — the same degradation the empty-record guard
            # uses, and deterministic_plan_text still carries the COMPLETE real
            # record via completed_results_text. Less pretty; never invented.
            logger.warning(
                f"Summary rejected: enumerated items absent from the record "
                f"(plan {plan.id}) — falling back to the deterministic text"
            )
            yield deterministic_plan_text(plan)
            return
        yield text


async def completed_plan_text(provider: LLMProvider, plan: AgentPlan) -> str:
    """The non-streaming collector for the agent HTTP endpoints: the whole
    summary as one string. Any failure (or an empty stream) degrades to the
    deterministic completion text — a completed plan ALWAYS has an answer."""
    parts: list[str] = []
    try:
        async for delta in stream_completed_summary(provider, plan):
            parts.append(delta)
    except Exception as e:
        logger.warning(f"Collecting plan summary failed: {e}")
    text = "".join(parts).strip()
    return text or deterministic_plan_text(plan)
