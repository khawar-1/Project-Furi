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
from loguru import logger

from app.agents.rendering import deterministic_plan_text, steps_for_summary
from app.agents.schemas import AgentPlan
from app.providers.base import LLMMessage, LLMProvider

SUMMARY_PROMPT = """You are Jarvis, the user's personal AI. You just finished executing a task for them. Report the outcome.

THE USER ASKED:
{goal}

WHAT WAS DONE AND WHAT IT FOUND (already rendered as readable text — this is the COMPLETE record):
{steps}

Write the reply to the user:
- Start with one short first-person sentence saying what was done.
- When the user asked to SEE data (file/folder names, file contents, command output), present ALL of it from the results above: names as a markdown bullet list (you may group folders and files), file contents and command output in a fenced code block. Never summarize the data away.
- Copy names, paths, numbers, and contents EXACTLY as written above — never invent, drop, round, or embellish anything.
- Only call a list truncated if the results above literally say so — otherwise it is complete.
- Never output JSON, curly braces, or escaped backslashes; do not mention tools, steps, or plans."""


async def stream_completed_summary(provider: LLMProvider, plan: AgentPlan):
    """Stream a natural-language summary of a completed plan. Falls back to
    the deterministic text if the LLM stream fails before producing anything.
    The step results are handed over as code-rendered readable text
    (steps_for_summary), NEVER raw JSON — the LLM cannot paste JSON it never
    received (live display bug, 2026-07-10). A plan with NOTHING rendered to
    report (no completed step produced output — e.g. every step was a
    zero-match SKIP) never reaches the LLM at all: asking a model to 'report
    the outcome' of an empty record invites invention (live bug 2026-07-13:
    it fabricated file1/2/3.pdf for a 0-step plan)."""
    rendered = steps_for_summary(plan)
    if not rendered.strip():
        yield deterministic_plan_text(plan)
        return
    prompt = SUMMARY_PROMPT.format(goal=plan.goal, steps=rendered)
    produced = False
    try:
        async for delta in provider.stream_chat(
            messages=[LLMMessage(role="user", content=prompt)], temperature=0.3,
        ):
            produced = True
            yield delta
    except Exception as e:
        logger.warning(f"Task summary stream failed: {e}")
    if not produced:
        yield deterministic_plan_text(plan)


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
