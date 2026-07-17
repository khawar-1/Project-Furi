"""
Evidence escalation — thin web_search results are enriched in CODE.

Live bug 2026-07-16: "…which teams are going to fifa finals 2026" searched, got
FIFA's qualified-teams page, kept 300 chars of it (cut mid-word exactly where
the list began), never fetched the page — and the summary LLM invented 112
countries. Planner rule 16 already told the model to follow up with
read_webpage; it could never fire, because the planner drafts every step before
any snippet exists and a SUCCESSFUL step never re-enters revise.

Covered here:
  - the escalate() decision matrix (incomplete vs complete evidence, top url,
    idempotence, caps, never-recursive);
  - end-to-end through the planner: a thin search splices a read_webpage that
    runs, with ZERO extra LLM calls (the whole thesis) and zero replan budget;
  - an escalated step's failure is NON-FATAL (it must never hand the plan to
    the replan loop — that is the rejected "thin = FAILED" design sneaking in
    through the back door);
  - approval signatures are unaffected;
  - a cancel between steps still beats a spliced step.
"""
from typing import List

import pytest

import app.tools  # noqa: F401 — registers the real tools
from app.agents import evidence_resolver
from app.agents.evidence_resolver import (
    MAX_WEB_ESCALATIONS,
    WEB_THIN_CONTENT_CHARS,
    escalate,
)
from app.agents.planner import AgentPlanner, MAX_PLAN_STEPS
from app.agents.schemas import AgentPlan, PlanStatus, PlanStep, StepStatus
from app.core.base_tool import PermissionLevel, ToolResult
from app.tools import browser_tools

from tests.test_agent_planner import FakeProvider


# --------------------------------------------------------------- builders

def _row(url: str = "https://ex.com/a", content: str = "") -> dict:
    return {"title": "T", "url": url, "snippet": "sn", "content": content,
            "truncated": False}


def _search_step(rows: List[dict], status=StepStatus.COMPLETED) -> PlanStep:
    return PlanStep(
        description="search", tool="web_search", parameters={"query": "q"},
        permission_level=PermissionLevel.READ, requires_approval=False,
        status=status,
        result=ToolResult(
            success=True, output={"query": "q", "results": rows, "count": len(rows)},
            permission_level=PermissionLevel.READ,
        ),
    )


def _plan(*steps: PlanStep) -> AgentPlan:
    return AgentPlan(goal="who is in the final", steps=list(steps))


# ---------------------------------------------------------- the decision

def test_thin_results_escalate_to_the_top_url():
    plan = _plan(_search_step([_row("https://fifa.com/x", "tiny")]))
    step = escalate(plan, 0, MAX_PLAN_STEPS)
    assert step is not None
    assert step.tool == "read_webpage"
    assert step.parameters == {"url": "https://fifa.com/x"}
    assert step.auto_escalated is True
    # Permission comes from the registry, never asserted by the resolver.
    assert step.permission_level == PermissionLevel.READ
    assert step.requires_approval is False


def test_substantive_results_do_not_escalate():
    """The evidence already answers — spending a fetch would be pure latency."""
    plan = _plan(_search_step([_row(content="x" * (WEB_THIN_CONTENT_CHARS + 1))]))
    assert escalate(plan, 0, MAX_PLAN_STEPS) is None


def test_completeness_is_decided_by_the_best_row_not_the_first():
    """One complete substantive result is enough evidence, wherever it ranks."""
    plan = _plan(_search_step([
        _row("https://a.com", "tiny"),
        _row("https://b.com", "y" * (WEB_THIN_CONTENT_CHARS + 1)),
    ]))
    assert escalate(plan, 0, MAX_PLAN_STEPS) is None


def test_truncated_content_escalates_even_though_it_is_long():
    """This test asserted the OPPOSITE until live verification (2026-07-16)
    falsified it. The original reasoning: `truncated` means we kept a full
    CONTENT_MAX_CHARS, which is "plenty", so truncated ≠ thin. The FIFA re-run
    disproved it — every source came back truncated at 1200 chars, nothing
    escalated, and the summary honestly reported "the data is cut off in each
    source, so I can only report what was actually retrieved" while the real
    answer sat two paragraphs further down the page. A cut BEFORE the data is a
    gap whatever its size; 1200 chars of preamble answers no better than 300."""
    row = _row(content="x" * (WEB_THIN_CONTENT_CHARS + 1))
    row["truncated"] = True
    step = escalate(_plan(_search_step([row])), 0, MAX_PLAN_STEPS)
    assert step is not None
    assert step.tool == "read_webpage"


def test_complete_substantive_content_does_not_escalate():
    """The one case that needs nothing: long AND whole. Escalating here would
    be pure latency."""
    row = _row(content="x" * (WEB_THIN_CONTENT_CHARS + 1))
    row["truncated"] = False
    assert escalate(_plan(_search_step([row])), 0, MAX_PLAN_STEPS) is None


def test_zero_rows_do_not_escalate():
    """Zero results is already a FAILED ToolResult upstream, which steers a
    reformulation — there is no url to read."""
    assert escalate(_plan(_search_step([])), 0, MAX_PLAN_STEPS) is None


def test_non_search_steps_never_escalate():
    """Never recursive: the read_webpage this module produces cannot itself
    escalate."""
    read = PlanStep(
        description="read", tool="read_webpage", parameters={"url": "https://a.com"},
        permission_level=PermissionLevel.READ, requires_approval=False,
        status=StepStatus.COMPLETED,
        result=ToolResult(success=True, output={"content": "hi"},
                          permission_level=PermissionLevel.READ),
    )
    assert escalate(_plan(read), 0, MAX_PLAN_STEPS) is None


def test_incomplete_step_never_escalates():
    plan = _plan(_search_step([_row(content="tiny")], status=StepStatus.PENDING))
    assert escalate(plan, 0, MAX_PLAN_STEPS) is None


def test_skips_a_url_the_plan_already_reads():
    """Idempotence, including trailing-slash variants of the same page."""
    read = PlanStep(
        description="read", tool="read_webpage",
        parameters={"url": "https://fifa.com/x/"},
        permission_level=PermissionLevel.READ, requires_approval=False,
    )
    plan = _plan(_search_step([_row("https://fifa.com/x", "tiny")]), read)
    assert escalate(plan, 0, MAX_PLAN_STEPS) is None


def test_respects_the_plan_step_cap():
    plan = _plan(_search_step([_row(content="tiny")]))
    assert escalate(plan, 0, max_steps=1) is None


def test_respects_the_per_plan_escalation_cap():
    """Bounds worst-case added latency."""
    plan = _plan(_search_step([_row(content="tiny")]))
    for i in range(MAX_WEB_ESCALATIONS):
        plan.steps.append(PlanStep(
            description=f"read {i}", tool="read_webpage",
            parameters={"url": f"https://old{i}.com"},
            permission_level=PermissionLevel.READ, requires_approval=False,
            auto_escalated=True,
        ))
    assert escalate(plan, 0, MAX_PLAN_STEPS) is None


def test_escalation_is_best_effort_and_never_raises():
    """Enrichment must never break a plan that already succeeded."""
    plan = _plan(_search_step([_row(content="tiny")]))
    plan.steps[0].result = ToolResult(
        success=True, output="not a dict", permission_level=PermissionLevel.READ,
    )
    assert escalate(plan, 0, MAX_PLAN_STEPS) is None
    assert escalate(plan, 99, MAX_PLAN_STEPS) is None  # index out of range


# ------------------------------------------------- end-to-end through the planner

def _fake_page(text: str):
    def _fetch(url: str):
        return browser_tools.FetchedPage(
            url=url, status_code=200, text=f"<html><body><p>{text}</p></body></html>",
            content_type="text/html",
        )
    return _fetch


async def _run(provider, db_session, rows, page_text="Argentina beat Brazil"):
    browser_tools.SEARCH_PROVIDER_FACTORY = lambda q, n: rows
    browser_tools.HTTP_FETCH_FACTORY = _fake_page(page_text)
    planner = AgentPlanner(db_session, provider, session_id="s1")
    return await planner.start("which teams are in the final")


_ONE_SEARCH = [
    '{"steps": [{"description": "search", "tool": "web_search", '
    '"parameters": {"query": "final teams"}}]}',
    '{"approved": true, "feedback": ""}',
]


async def test_thin_search_escalates_and_costs_no_llm_call(db_session):
    """THE THESIS: the url is already sitting in the completed step's own
    output, so there is nothing to decide — only to do. Zero LLM calls, zero
    replan budget. If this ever needs the model, the design has failed."""
    provider = FakeProvider(list(_ONE_SEARCH))
    plan = await _run(provider, db_session, [_row("https://fifa.com/x", "tiny")])
    calls_after_planning = provider.calls

    reads = [s for s in plan.steps if s.tool == "read_webpage"]
    assert len(reads) == 1
    assert reads[0].auto_escalated is True
    assert reads[0].status == StepStatus.COMPLETED
    assert "Argentina" in reads[0].result.output["content"]
    assert plan.status == PlanStatus.COMPLETED
    # ONE call for the whole turn: the draft. (reflect skips the LLM entirely
    # for an all-READ plan.) No revise round, so the escalation decided and
    # acted entirely in code — exactly the property that makes it affordable to
    # run on every thin search.
    assert calls_after_planning == 1


async def test_substantive_search_splices_nothing(db_session):
    provider = FakeProvider(list(_ONE_SEARCH))
    plan = await _run(
        provider, db_session, [_row(content="x" * (WEB_THIN_CONTENT_CHARS + 1))],
    )
    assert not [s for s in plan.steps if s.tool == "read_webpage"]
    assert plan.status == PlanStatus.COMPLETED


async def test_escalated_failure_is_non_fatal(db_session):
    """The back-door test. A spliced read that 403s/times out must NOT set
    pause='failed_step' — that would hand the plan to the replan loop over an
    opportunistic extra, re-importing every cost of the 'thin = FAILED' design
    this module exists to reject. The search keeps its evidence; the failed
    enrichment stays visible and audited."""
    def _boom(url: str):
        raise RuntimeError("403 paywall")

    provider = FakeProvider(list(_ONE_SEARCH))
    browser_tools.SEARCH_PROVIDER_FACTORY = lambda q, n: [_row("https://p.com", "tiny")]
    browser_tools.HTTP_FETCH_FACTORY = _boom
    planner = AgentPlanner(db_session, provider, session_id="s1")
    plan = await planner.start("which teams are in the final")

    assert plan.status == PlanStatus.COMPLETED      # not FAILED
    # The draft only — no revise round, so no replan budget was burned on an
    # enrichment the goal never depended on.
    assert provider.calls == 1
    search = next(s for s in plan.steps if s.tool == "web_search")
    assert search.status == StepStatus.COMPLETED    # evidence survives
    read = next(s for s in plan.steps if s.tool == "read_webpage")
    assert read.status == StepStatus.FAILED         # honest, never hidden


async def test_escalation_never_recurses(db_session):
    """A thin page fetched by an escalation must not trigger another one."""
    provider = FakeProvider(list(_ONE_SEARCH))
    plan = await _run(
        provider, db_session, [_row("https://fifa.com/x", "tiny")], page_text="brief",
    )
    assert len([s for s in plan.steps if s.tool == "read_webpage"]) == 1


async def test_escalated_step_does_not_disturb_approval_signatures(db_session):
    """auto_escalated is excluded from signature() by construction, so it can
    never change what a user approved."""
    a = PlanStep(description="d", tool="read_webpage", parameters={"url": "https://a.com"},
                 permission_level=PermissionLevel.READ, requires_approval=False)
    b = a.model_copy(update={"auto_escalated": True})
    assert a.signature() == b.signature()


# ============================ per-reading escalation + failed-read retry (L2)
#
# Live 2026-07-17. Two defects, one shape — the plan reads ONE page:
#   (1) "which teams have qualified for fifa worldcup final 2026" is ambiguous;
#       once web_search fans it out over both readings, sufficiency judged over
#       the whole pile would call it satisfied the moment the TOP reading came
#       back whole, leaving the other interpretation — possibly the one meant —
#       with nothing but teasers.
#   (2) the reworded question escalated to ESPN's bracket, ESPN 403'd, and
#       escalation stopped there. The answer came from snippets alone (right,
#       by luck) while still citing "the bracket data from ESPN" — a page it
#       never read.

def _fan_row(url: str, found_by: list, content: str = "") -> dict:
    row = _row(url, content)
    row["found_by"] = found_by
    return row


def _fan_search_step(rows: List[dict], queries: list) -> PlanStep:
    step = _search_step(rows)
    step.result.output["queries"] = queries
    return step


def _read_step(url: str, status=StepStatus.COMPLETED) -> PlanStep:
    return PlanStep(
        description="read", tool="read_webpage", parameters={"url": url},
        permission_level=PermissionLevel.READ, requires_approval=False,
        status=status, auto_escalated=True,
    )


def test_a_reading_with_thin_evidence_escalates_even_when_another_is_whole():
    """Sufficiency is PER READING. A global test would see the whole 'final'
    page, call the record satisfied, and leave the 'qualified' reading
    unevidenced — the very question the user may have meant."""
    plan = _plan(_fan_search_step(
        [
            _fan_row("https://w.org/final", ["final q"], "x" * (WEB_THIN_CONTENT_CHARS + 1)),
            _fan_row("https://w.org/qualification", ["qualified q"], "tiny"),
        ],
        ["final q", "qualified q"],
    ))
    step = escalate(plan, 0, MAX_PLAN_STEPS)
    assert step is not None
    assert step.parameters == {"url": "https://w.org/qualification"}


def test_each_reading_gets_its_own_page_and_then_escalation_stops():
    """Idempotence across readings: escalate() serves one uncovered reading per
    call and returns None once they are all served — which is what lets the
    planner loop it without a termination argument of its own."""
    plan = _plan(_fan_search_step(
        [
            _fan_row("https://w.org/final", ["final q"], "tiny"),
            _fan_row("https://w.org/qualification", ["qualified q"], "tiny"),
        ],
        ["final q", "qualified q"],
    ))
    first = escalate(plan, 0, MAX_PLAN_STEPS)
    plan.steps.insert(1, first)
    second = escalate(plan, 0, MAX_PLAN_STEPS)
    assert second is not None
    plan.steps.insert(2, second)
    assert {first.parameters["url"], second.parameters["url"]} == {
        "https://w.org/final", "https://w.org/qualification",
    }
    assert escalate(plan, 0, MAX_PLAN_STEPS) is None, "both readings covered"


def test_a_reading_served_by_a_whole_page_is_never_escalated():
    plan = _plan(_fan_search_step(
        [_fan_row("https://a.com", ["q1", "q2"], "x" * (WEB_THIN_CONTENT_CHARS + 1))],
        ["q1", "q2"],
    ))
    assert escalate(plan, 0, MAX_PLAN_STEPS) is None


def test_a_failed_read_leaves_its_reading_uncovered():
    """A url we must not re-fetch and a reading we have actually covered are
    different questions. The FAILED step still carries the url, so a url-only
    check would call the reading handled — which is precisely how the ESPN 403
    ended escalation and left the turn on snippets."""
    plan = _plan(
        _fan_search_step(
            [
                _fan_row("https://espn.com/bracket", ["final q"], "tiny"),
                _fan_row("https://fifa.com/final", ["final q"], "tiny"),
            ],
            ["final q"],
        ),
        _read_step("https://espn.com/bracket", status=StepStatus.FAILED),
    )
    step = escalate(plan, 0, MAX_PLAN_STEPS)
    assert step is not None
    assert step.parameters == {"url": "https://fifa.com/final"}, "next candidate, not the 403"


def test_escalate_after_failed_read_finds_the_originating_search():
    plan = _plan(
        _search_step([_row("https://espn.com/bracket", "tiny"),
                      _row("https://fifa.com/final", "tiny")]),
        _read_step("https://espn.com/bracket", status=StepStatus.FAILED),
    )
    step = evidence_resolver.escalate_after_failed_read(plan, 1, MAX_PLAN_STEPS)
    assert step is not None
    assert step.parameters == {"url": "https://fifa.com/final"}
    assert step.auto_escalated is True


def test_escalate_after_failed_read_ignores_a_step_code_did_not_add():
    """A read the PLANNER drafted (a url the user gave) is the goal's own work.
    Its failure is real and belongs to the replan loop, not to enrichment."""
    plan = _plan(
        _search_step([_row("https://a.com", "tiny")]),
        PlanStep(
            description="read", tool="read_webpage", parameters={"url": "https://user.com"},
            permission_level=PermissionLevel.READ, requires_approval=False,
            status=StepStatus.FAILED,
        ),
    )
    assert evidence_resolver.escalate_after_failed_read(plan, 1, MAX_PLAN_STEPS) is None


def test_a_site_that_keeps_refusing_cannot_spin():
    """Every failed read counts toward MAX_WEB_ESCALATIONS, so the retry path is
    bounded by the same budget as any other escalation."""
    rows = [_row(f"https://s{i}.com", "tiny") for i in range(6)]
    plan = _plan(_search_step(rows))
    for i in range(MAX_WEB_ESCALATIONS):
        plan.steps.append(_read_step(f"https://s{i}.com", status=StepStatus.FAILED))
    assert escalate(plan, 0, MAX_PLAN_STEPS) is None


async def test_a_blocked_page_falls_through_to_the_next_end_to_end(db_session):
    """The ESPN incident, end to end: the top result 403s and the answer still
    rests on a real page — with no LLM call spent on the recovery."""
    fetched: list = []

    def _fetch(url: str):
        fetched.append(url)
        if "espn" in url:
            raise RuntimeError("403 Forbidden")
        return browser_tools.FetchedPage(
            url=url, status_code=200, content_type="text/html",
            text="<html><body><p>" + ("Spain v Argentina. " * 80) + "</p></body></html>",
        )

    provider = FakeProvider(list(_ONE_SEARCH))
    browser_tools.SEARCH_PROVIDER_FACTORY = lambda q, n: [
        _row("https://espn.com/bracket", "tiny"),
        _row("https://fifa.com/final", "tiny"),
    ]
    browser_tools.HTTP_FETCH_FACTORY = _fetch
    planner = AgentPlanner(db_session, provider, session_id="s1")
    plan = await planner.start("which teams are playing fifa final 2026")

    assert fetched == ["https://espn.com/bracket", "https://fifa.com/final"]
    reads = [s for s in plan.steps if s.tool == "read_webpage"]
    assert [s.status for s in reads] == [StepStatus.FAILED, StepStatus.COMPLETED]
    assert "Spain" in reads[1].result.output["content"], "the answer rests on a real page"
    assert plan.status == PlanStatus.COMPLETED, "a blocked page is not a failed turn"
    # ONE call for the whole turn — the draft — exactly as the no-403 path
    # costs. The recovery decided and acted entirely in code.
    assert provider.calls == 1


async def test_cancel_beats_a_spliced_step(db_session):
    """The cancel check sits at the top of the execute loop, so a spliced step
    re-enters through it like any other."""
    fired = {"n": 0}

    def _cancel_after_first():
        fired["n"] += 1
        return fired["n"] > 1   # let the search run, cancel before the read

    provider = FakeProvider(list(_ONE_SEARCH))
    browser_tools.SEARCH_PROVIDER_FACTORY = lambda q, n: [_row("https://x.com", "tiny")]
    browser_tools.HTTP_FETCH_FACTORY = _fake_page("never fetched")
    planner = AgentPlanner(
        db_session, provider, session_id="s1", cancel_check=_cancel_after_first,
    )
    plan = await planner.start("which teams are in the final")

    assert plan.status == PlanStatus.CANCELLED
    read = next(s for s in plan.steps if s.tool == "read_webpage")
    assert read.status == StepStatus.SKIPPED
