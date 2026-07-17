"""
Summary anti-fabrication guard.

Live bug 2026-07-16: asked "which teams are going to fifa finals 2026", the
record held a 300-char fragment of FIFA's qualified-teams page — cut mid-word
exactly where the list began, so ZERO team names were in it. The summary LLM
emitted 112 country bullets (India, Brunei, Timor-Leste, Seychelles…) for a
48-team tournament and credited them to FIFA. SUMMARY_PROMPT already said
"never invent" and "Copy names EXACTLY".

The guard does NOT gate on "is the record incomplete?" — that is a judgement
about whether the answer is present, and "who is the president of pakistan" has
a short record containing the complete correct answer. An open evidence gap only
selects the verification MODE; the verdict is a substring test of the OUTPUT
against the RECORD.

Covered:
  - the incident: ungrounded enumeration → deterministic text;
  - the false-positive cases that must NOT fire (short answers, grounded
    lists, non-web plans);
  - verification mode is entered on ANY web step with an UNFILLED evidence gap
    (the incident was MIXED: two substantive questions and one starved one) —
    and NOT when the escalation already filled it;
  - normal turns still stream delta-by-delta (Phase 7 speech untouched);
  - the 2026-07-13 empty-record guard still fires.
"""
from typing import AsyncIterator, List, Optional

import pytest

from app.agents.summary import (
    _is_fabricated_enumeration,
    _plan_has_unresolved_web_gap,
    stream_completed_summary,
)
from app.agents.schemas import AgentPlan, PlanStatus, PlanStep, StepStatus
from app.core.base_tool import PermissionLevel, ToolResult
from app.providers.base import EmbeddingResponse, LLMMessage, LLMProvider, LLMResponse


class ScriptedProvider(LLMProvider):
    """Streams a fixed text as several deltas (so delta-count is observable)."""

    def __init__(self, text: str, chunks: int = 4) -> None:
        size = max(1, len(text) // chunks)
        self._deltas = [text[i:i + size] for i in range(0, len(text), size)] or [""]

    @property
    def provider_name(self) -> str:
        return "scripted"

    @property
    def model_name(self) -> str:
        return "scripted-model"

    async def chat(self, messages, **kw) -> LLMResponse:  # pragma: no cover
        raise NotImplementedError

    async def stream_chat(self, messages, **kw) -> AsyncIterator[str]:
        for d in self._deltas:
            yield d

    async def embed(self, texts, **kw) -> EmbeddingResponse:  # pragma: no cover
        raise NotImplementedError


# --------------------------------------------------------------- builders

def _web_step(rows: List[dict]) -> PlanStep:
    return PlanStep(
        description="search the web", tool="web_search", parameters={"query": "q"},
        permission_level=PermissionLevel.READ, requires_approval=False,
        status=StepStatus.COMPLETED,
        result=ToolResult(
            success=True, output={"query": "q", "results": rows, "count": len(rows)},
            permission_level=PermissionLevel.READ,
        ),
    )


def _row(content: str, url: str = "https://fifa.com/x") -> dict:
    return {"title": "Qualified teams", "url": url, "snippet": content[:300],
            "content": content, "truncated": False}


def _listing_step(names: List[str]) -> PlanStep:
    return PlanStep(
        description="list the folder", tool="list_directory",
        parameters={"path": "C:\\x"},
        permission_level=PermissionLevel.READ, requires_approval=False,
        status=StepStatus.COMPLETED,
        result=ToolResult(
            success=True,
            output={"path": "C:\\x",
                    "entries": [{"name": n, "type": "file"} for n in names]},
            permission_level=PermissionLevel.READ,
        ),
    )


def _plan(*steps: PlanStep) -> AgentPlan:
    p = AgentPlan(goal="which teams are going to fifa finals 2026", steps=list(steps))
    p.status = PlanStatus.COMPLETED
    return p


# The fabrication, verbatim in shape: countries that appear nowhere in the record.
_FABRICATED = "\n".join(
    ["FIFA World Cup 2026 qualification is still ongoing. The qualified teams so far:"]
    + [f"- {c}" for c in [
        "Argentina", "Brazil", "Uruguay", "Ecuador", "Colombia", "Paraguay",
        "Chile", "Peru", "Venezuela", "Bolivia", "Germany", "England", "India",
        "Brunei", "Timor-Leste", "Seychelles", "Lesotho", "Eswatini", "Myanmar",
    ]]
)

# The starved record: a heading, cut exactly where the list would start.
_FIFA_FRAGMENT = (
    "# Qualified teams for the FIFA World Cup 2026. Look ahead to the global "
    "showpiece in Canada, Mexico and the United States with details on the "
    "teams who have booked their ticket to the tournament. ## **FIFA World "
    "Cup 2026 qualified t"
)


# ---------------------------------------------------- thin-evidence detection

def test_thin_web_evidence_is_a_gap():
    assert _plan_has_unresolved_web_gap(_plan(_web_step([_row(_FIFA_FRAGMENT)]))) is True


def test_substantive_complete_evidence_is_no_gap():
    assert _plan_has_unresolved_web_gap(_plan(_web_step([_row("x" * 2000)]))) is False


def test_truncated_evidence_is_a_gap():
    """Live verification 2026-07-16 overturned the first design here: a
    truncated row was treated as "plenty" because it carries a full content
    budget. It is not — 1200 chars of a page can be cut before the data just as
    300 was, and the FIFA re-run proved it (every source truncated, nothing
    escalated, the summary honestly reported a partial answer)."""
    row = _row("x" * 2000)
    row["truncated"] = True
    assert _plan_has_unresolved_web_gap(_plan(_web_step([row]))) is True


def test_any_thin_step_enters_verification_not_all():
    """THE INCIDENT'S EXACT SHAPE: two substantive questions and one starved
    one. A plan-level "is the record thin?" test answers *no* here and would
    never fire on the very bug it was written for."""
    plan = _plan(
        _web_step([_row("x" * 2000, "https://a.com")]),   # black clover — fine
        _web_step([_row("y" * 2000, "https://b.com")]),   # president — fine
        _web_step([_row(_FIFA_FRAGMENT)]),                # fifa — starved
    )
    assert _plan_has_unresolved_web_gap(plan) is True


def test_plan_without_web_steps_is_never_thin():
    """Zero blast radius outside web: a file plan can never enter verification."""
    assert _plan_has_unresolved_web_gap(_plan(_listing_step(["a.txt"]))) is False


# -------------------------------------------------------- the grounding test

def test_fabricated_enumeration_detected():
    assert _is_fabricated_enumeration(_FABRICATED, _FIFA_FRAGMENT) is True


def test_grounded_enumeration_passes():
    """The same shape of list, but the names ARE in the record."""
    record = "Found: Argentina, Brazil, Uruguay, Ecuador, Colombia, Paraguay, " \
             "Chile, Peru, Venezuela, Bolivia, Germany, England"
    text = "\n".join(f"- {c}" for c in [
        "Argentina", "Brazil", "Uruguay", "Ecuador", "Colombia", "Paraguay",
        "Chile", "Peru", "Venezuela", "Bolivia", "Germany", "England",
    ])
    assert _is_fabricated_enumeration(text, record) is False


def test_short_answer_is_never_a_fabrication():
    """The president-of-pakistan case: a thin record whose 200 chars contain the
    complete correct answer. Gate 1 (bullet count) must protect it."""
    assert _is_fabricated_enumeration(
        "- **President of Pakistan**: Asif Ali Zardari.", "Zardari felicitates…"
    ) is False


def test_file_listing_bullets_are_grounded():
    names = [f"file{i}.txt" for i in range(40)]
    text = "\n".join(f"- {n}" for n in names)
    assert _is_fabricated_enumeration(text, ", ".join(names)) is False


# ------------------------------------------------------------ the stream

async def test_fabricated_summary_is_rejected():
    """THE INCIDENT, FROZEN. Thin web evidence + an invented list → the LLM's
    text is discarded for the deterministic record."""
    plan = _plan(_web_step([_row(_FIFA_FRAGMENT)]))
    provider = ScriptedProvider(_FABRICATED)
    out = "".join([d async for d in stream_completed_summary(provider, plan)])
    assert "Brunei" not in out
    assert "Timor-Leste" not in out
    # The deterministic text carries the COMPLETE real record instead.
    assert "fifa.com" in out


async def test_grounded_summary_survives_verification():
    """Thin evidence alone must not reject: only thin evidence AND an
    ungrounded enumeration does."""
    plan = _plan(_web_step([_row(_FIFA_FRAGMENT)]))
    good = "The search found FIFA's qualified-teams page, sir, but its content was cut off."
    provider = ScriptedProvider(good)
    out = "".join([d async for d in stream_completed_summary(provider, plan)])
    assert out == good


async def test_substantive_record_streams_delta_by_delta():
    """Proves buffering is confined to verification mode — Phase 7 Part 4's
    sentence-by-sentence speech depends on getting deltas as they arrive, so a
    normal turn must never be buffered into one chunk."""
    plan = _plan(_web_step([_row("x" * 2000)]))
    provider = ScriptedProvider("All done, sir. " * 20, chunks=4)
    deltas = [d async for d in stream_completed_summary(provider, plan)]
    assert len(deltas) > 1


async def test_thin_record_is_buffered_into_one_chunk():
    """The cost, made explicit: verification mode necessarily delivers one
    chunk. Acceptable only because it is rare — Layer 1 keeps most records
    substantive and Layer 5 escalates the rest."""
    plan = _plan(_web_step([_row(_FIFA_FRAGMENT)]))
    provider = ScriptedProvider("A short grounded reply about FIFA.", chunks=4)
    deltas = [d async for d in stream_completed_summary(provider, plan)]
    assert len(deltas) == 1


async def test_empty_record_guard_still_fires():
    """The 2026-07-13 guard is untouched by the new one."""
    plan = _plan()
    provider = ScriptedProvider("should never be asked")
    out = "".join([d async for d in stream_completed_summary(provider, plan)])
    assert "should never be asked" not in out


def _escalated_read(content: str) -> PlanStep:
    return PlanStep(
        description="read the page", tool="read_webpage",
        parameters={"url": "https://fifa.com/x"},
        permission_level=PermissionLevel.READ, requires_approval=False,
        status=StepStatus.COMPLETED, auto_escalated=True,
        result=ToolResult(success=True, output={"url": "https://fifa.com/x",
                                                "title": "T", "content": content},
                          permission_level=PermissionLevel.READ),
    )


def test_a_filled_gap_needs_no_verification():
    """What keeps verification RARE — and keeps Phase 7's live speech on web
    turns. The escalation read the page, so the record has no hole to invent
    into and the stream need not be buffered."""
    plan = _plan(
        _web_step([_row(_FIFA_FRAGMENT)]),
        _escalated_read("Spain and Argentina will meet in the final. " * 40),
    )
    assert _plan_has_unresolved_web_gap(plan) is False


def test_a_failed_escalation_leaves_the_gap_open():
    """The residue case the guard exists for: the page could not be read, so
    the starved record still reaches the model."""
    read = _escalated_read("")
    read.status = StepStatus.FAILED
    plan = _plan(_web_step([_row(_FIFA_FRAGMENT)]), read)
    assert _plan_has_unresolved_web_gap(plan) is True


async def test_filled_gap_streams_live():
    plan = _plan(
        _web_step([_row(_FIFA_FRAGMENT)]),
        _escalated_read("Spain and Argentina will meet in the final. " * 40),
    )
    provider = ScriptedProvider("Spain and Argentina, sir. " * 10, chunks=4)
    deltas = [d async for d in stream_completed_summary(provider, plan)]
    assert len(deltas) > 1


# ============================ the summary's temporal context (L3, 2026-07-17)
#
# The model WRITING the answer did not know what day it was. The planner did
# (_context_block); SUMMARY_PROMPT.format() passed goal and steps only. Asked
# which teams had "qualified for the fifa worldcup final 2026" two days before
# the final, it answered with the 48 teams that qualified for the tournament.
# Both readings are defensible English — but "the final is in two days" is the
# signal that settles which one a person means, and it was the one thing not on
# the table.

class _CapturingProvider(ScriptedProvider):
    """Records the prompt the summary actually sends."""

    def __init__(self, text: str = "All done, sir.") -> None:
        super().__init__(text, chunks=2)
        self.prompts: List[str] = []

    async def stream_chat(self, messages, **kw):
        self.prompts.append(messages[0].content)
        async for d in super().stream_chat(messages, **kw):
            yield d


async def test_summary_prompt_carries_the_current_date():
    """Not a style point: it is what lets "the latest release", "this quarter",
    "who is the champion" and the FIFA case resolve to the reading a person
    means today."""
    from datetime import datetime

    provider = _CapturingProvider()
    plan = _plan(_web_step([_row("Spain and Argentina reach the final. " * 40)]))
    [d async for d in stream_completed_summary(provider, plan)]

    assert provider.prompts, "the summary reached the model"
    assert datetime.now().strftime("%Y-%m-%d") in provider.prompts[0]


async def test_summary_prompt_carries_the_ambiguity_clause():
    """An ambiguous question must never be answered by silently picking one
    reading — the user gets the likely answer AND is told the other exists."""
    provider = _CapturingProvider()
    plan = _plan(_web_step([_row("Spain and Argentina reach the final. " * 40)]))
    [d async for d in stream_completed_summary(provider, plan)]

    prompt = provider.prompts[0]
    assert "read more than one way" in prompt
    assert "most likely meant" in prompt


def test_summary_prompt_still_formats_with_every_placeholder():
    """A missing kwarg raises at the one call site, so the template and the
    call must not drift apart."""
    from app.agents.summary import SUMMARY_PROMPT

    rendered = SUMMARY_PROMPT.format(goal="g", steps="s", now="2026-07-17 09:00 (Friday)")
    assert "2026-07-17 09:00 (Friday)" in rendered
    for placeholder in ("{goal}", "{steps}", "{now}"):
        assert placeholder not in rendered
