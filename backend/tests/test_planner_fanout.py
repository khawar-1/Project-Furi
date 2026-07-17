"""
Query fan-out, driven through the PLANNER (2026-07-17).

THE GAP THIS CLOSES. Every fan-out test written before today lives in
test_browser_tools.py and calls _web_search().execute(queries=[...]) directly.
That proves the TOOL fans out. It says nothing about whether the PLANNER ever
asks it to — and it never did: the feature shipped, 1,578 tests were green, and
it had not once fired in a live plan. A test suite that cannot see a whole
feature failing is the actual defect; these tests drive the real graph, so they
can.

Sibling of test_evidence_resolver.py — same shape, same borrowed FakeProvider.
That module fixed rule 16's "are the snippets enough?" clause (an unbound
predicate that needed a later evaluation TIME). This one fixes its "is the
question ambiguous?" clause — a predicate perfectly bound at draft time, which
needed an independent EVALUATOR, because the pass that wrote the query was the
one certifying it.
"""
import json

import pytest

import app.tools  # noqa: F401 — registers the real tools
from app.agents import reading_enumerator
from app.agents.planner import AgentPlanner
from app.agents.schemas import PlanStatus, StepStatus
from app.tools import browser_tools

from tests.test_agent_planner import FakeProvider, plan_json, step

# The question that produced the wrong answer live, twice. Frozen.
_GOAL = "which teams have qualified for fifa finals 2026"

_TWO_READINGS = [
    "which teams are playing the 2026 World Cup final",
    "which teams qualified for the 2026 World Cup tournament",
]


# --------------------------------------------------------------- harness

def _web_row(url: str) -> dict:
    # Substantive by default: thin rows would make evidence_resolver splice a
    # read_webpage and turn a fan-out assertion into an escalation one.
    return {"title": "T", "url": url, "snippet": "sn", "content": "x" * 900,
            "truncated": False}


def _fake_search(searched: list):
    def _search(query: str, max_results: int):
        searched.append(query)
        return [_web_row(f"https://ex.com/{len(searched)}")]
    return _search


def _stub_readings(monkeypatch, readings, *, raises=None) -> dict:
    """Override the autouse _hermetic_reading_enumerator stub with a scripted
    enumeration, and count how often the planner asks for one."""
    calls = {"n": 0}

    async def _enumerate(goal, provider, now=None):
        calls["n"] += 1
        if raises is not None:
            raise raises
        return list(readings)

    monkeypatch.setattr(reading_enumerator, "enumerate_readings", _enumerate)
    return calls


async def _run(db_session, monkeypatch, draft_params: dict, readings, *,
               raises=None, goal: str = _GOAL):
    searched: list = []
    monkeypatch.setattr(browser_tools, "SEARCH_PROVIDER_FACTORY", _fake_search(searched))
    calls = _stub_readings(monkeypatch, readings, raises=raises)
    provider = FakeProvider([plan_json([step("Search", "web_search", **draft_params)])])
    plan = await AgentPlanner(db_session, provider, session_id="s-fan").start(goal)
    return plan, searched, calls


# ------------------------------------------------------------ the incident

async def test_a_single_query_web_search_is_fanned_out_by_code(db_session, monkeypatch):
    """The live failure, end to end. The draft commits to the qualification
    reading — defensible English, and exactly what it did on 2026-07-17 — and
    CODE widens it, because an independent enumeration says the question means
    two things. The model is not asked to reconsider and cannot decline."""
    plan, searched, _ = await _run(
        db_session, monkeypatch, {"query": "fifa 2026 qualified teams"}, _TWO_READINGS
    )

    assert plan.status == PlanStatus.COMPLETED
    search = plan.steps[0]
    assert search.parameters["queries"] == _TWO_READINGS
    assert search.auto_fanout is True
    # Not merely recorded on the step — both readings actually reached the tool.
    assert sorted(searched) == sorted(_TWO_READINGS)
    # The model's own reading survives for audit: it is the only evidence of
    # whether plan rule 16 ever does anything on its own.
    assert search.parameters["query"] == "fifa 2026 qualified teams"


async def test_the_likeliest_reading_is_stamped_for_the_summary(db_session, monkeypatch):
    """Retrieving both readings is only half the job — SOMETHING has to decide
    which one gets answered, and live it was the summary model, mid-prose, with
    "qualified" anchoring it from the goal. It lost.

    Element 0 of the enumeration is that verdict, made by the call whose only job
    is the question, and it rides the plan to summary.py."""
    plan, _, _ = await _run(
        db_session, monkeypatch, {"query": "fifa 2026 qualified teams"}, _TWO_READINGS
    )

    assert plan.primary_reading == _TWO_READINGS[0]
    assert "final" in plan.primary_reading  # the reading the user actually meant


async def test_an_unambiguous_question_stamps_no_reading(db_session, monkeypatch):
    """No ambiguity, no verdict: the summary is left entirely alone. "" is the
    signal for both "one reading" and "we could not tell" — there is one safe
    response to not knowing, and it is to add nothing."""
    plan, _, _ = await _run(
        db_session, monkeypatch, {"query": "who is the president of pakistan"}, [],
        goal="who is the president of pakistan",
    )

    assert plan.primary_reading == ""


async def test_a_stamped_reading_survives_serialization(db_session, monkeypatch):
    """The summary runs long after a background task's plan left memory — it is
    rehydrated from the parked payload. An excluded field would drop the verdict
    on exactly the turns that take longest, so this one is serialized."""
    plan, _, _ = await _run(
        db_session, monkeypatch, {"query": "fifa 2026 qualified teams"}, _TWO_READINGS
    )

    from app.agents.schemas import AgentPlan

    restored = AgentPlan.model_validate(json.loads(plan.model_dump_json()))
    assert restored.primary_reading == _TWO_READINGS[0]


async def test_an_unambiguous_question_stays_a_single_query(db_session, monkeypatch):
    """The control, and the cost ceiling. "who is the president of pakistan" has
    one reading, so nothing is widened and the turn still costs 1x search
    credits. A fan-out that fires on everything is just a slower, pricier
    search — and this is the assertion that keeps it honest."""
    plan, searched, calls = await _run(
        db_session, monkeypatch, {"query": "who is the president of pakistan"}, [],
        goal="who is the president of pakistan",
    )

    assert plan.status == PlanStatus.COMPLETED
    assert "queries" not in plan.steps[0].parameters
    assert plan.steps[0].auto_fanout is False
    assert searched == ["who is the president of pakistan"]
    assert calls["n"] == 1  # asked once, told "one reading", left it alone


async def test_a_model_authored_fanout_is_ranked_but_not_rewritten(db_session, monkeypatch):
    """A draft that already fanned out needs no WIDENING — but it still needs a
    RANKING, and that is the turn that failed live.

    2026-07-17: the model fanned out by itself, both readings were retrieved, and
    the answer still led with the 48-team qualification list two days before the
    final. Retrieval was never the problem there; deciding which reading to
    ANSWER was. So the enumeration runs on every web turn (this test used to
    assert the opposite — that a model-authored fan-out costs zero calls — and
    that saving is exactly what left this turn with no verdict to carry).

    Its queries are left EXACTLY as the model wrote them (RRF does not care about
    order, and second-guessing a rule that worked buys nothing); only
    primary_reading is added."""
    plan, searched, calls = await _run(
        db_session, monkeypatch, {"queries": _TWO_READINGS}, list(_TWO_READINGS),
    )

    assert plan.status == PlanStatus.COMPLETED
    assert plan.steps[0].parameters["queries"] == _TWO_READINGS  # untouched
    assert plan.steps[0].auto_fanout is False  # the model did this, not us
    assert calls["n"] == 1  # asked once — for the ranking, not the widening
    assert plan.primary_reading == _TWO_READINGS[0]
    assert sorted(searched) == sorted(_TWO_READINGS)


# -------------------------------------------------------- never break a plan

async def test_enumerator_failure_never_breaks_planning(db_session, monkeypatch):
    """Best-effort, the _load_folder_signal rule. A 429 on the enumeration
    degrades to the pre-fan-out behaviour — one query, a real answer — never to
    a failed turn. The signal is an improvement, not a dependency."""
    plan, searched, _ = await _run(
        db_session, monkeypatch, {"query": "fifa 2026 qualified teams"}, [],
        raises=RuntimeError("429 rate limited"),
    )

    assert plan.status == PlanStatus.COMPLETED
    assert plan.steps[0].status == StepStatus.COMPLETED
    assert "queries" not in plan.steps[0].parameters
    assert searched == ["fifa 2026 qualified teams"]


async def test_a_single_reading_enumeration_widens_nothing(db_session, monkeypatch):
    """One reading back from the enumerator is not a fan-out. Guards the
    len < 2 gate at the planner boundary as well as inside the module."""
    plan, searched, _ = await _run(
        db_session, monkeypatch, {"query": "how tall is the eiffel tower"},
        ["how tall is the Eiffel Tower"], goal="how tall is the eiffel tower",
    )
    assert "queries" not in plan.steps[0].parameters
    assert searched == ["how tall is the eiffel tower"]


# ------------------------------------------------------------------ scope

async def test_the_enumeration_is_computed_once_per_run(db_session, monkeypatch):
    """Two searches in one plan must not cost two enumerations: the goal is
    fixed for the life of a plan, so its readings are too."""
    searched: list = []
    monkeypatch.setattr(browser_tools, "SEARCH_PROVIDER_FACTORY", _fake_search(searched))
    calls = _stub_readings(monkeypatch, _TWO_READINGS)
    provider = FakeProvider([plan_json([
        step("Search one", "web_search", query="a"),
        step("Search two", "web_search", query="b"),
    ])])

    plan = await AgentPlanner(db_session, provider, session_id="s-fan2").start(_GOAL)

    assert plan.status == PlanStatus.COMPLETED
    assert all(s.auto_fanout for s in plan.steps)
    assert calls["n"] == 1


async def test_fanout_never_touches_a_non_web_step(db_session, monkeypatch, tmp_path):
    """The splice is scoped to web_search. A file step in the same plan is not a
    search and must come out byte-identical."""
    (tmp_path / "a.txt").write_text("x")
    monkeypatch.setattr(browser_tools, "SEARCH_PROVIDER_FACTORY", _fake_search([]))
    _stub_readings(monkeypatch, _TWO_READINGS)
    provider = FakeProvider([plan_json([
        step("List the files", "list_directory", path=str(tmp_path)),
        step("Search", "web_search", query="fifa 2026 qualified teams"),
    ])])

    plan = await AgentPlanner(db_session, provider, session_id="s-fan3").start(_GOAL)

    listing, search = plan.steps
    assert listing.parameters == {"path": str(tmp_path)}
    assert listing.auto_fanout is False
    assert search.auto_fanout is True


async def test_fanout_splice_does_not_disturb_approval_signatures(
    db_session, monkeypatch
):
    """web_search is READ, so widening it can never move a step across the
    approval gate — but state the invariant, because the splice edits the very
    dict signature() is computed from. auto_fanout is excluded by construction
    (signature is [tool, parameters]), and a WRITE step in the same plan keeps
    its own signature: the user still approves exactly what they were shown."""
    monkeypatch.setattr(browser_tools, "SEARCH_PROVIDER_FACTORY", _fake_search([]))
    _stub_readings(monkeypatch, _TWO_READINGS)
    provider = FakeProvider([
        plan_json([
            step("Search", "web_search", query="fifa 2026 qualified teams"),
            step("Write it down", "create_file", path="notes.txt", content="x"),
        ]),
        '{"approved": true, "feedback": ""}',
    ])

    plan = await AgentPlanner(db_session, provider, session_id="s-fan4").start(_GOAL)

    assert plan.status == PlanStatus.AWAITING_APPROVAL
    search, write = plan.steps
    assert search.auto_fanout is True
    assert search.requires_approval is False  # READ, widened or not
    assert "auto_fanout" not in search.signature()
    assert _TWO_READINGS[0] in search.signature()  # identifies the real search
    assert write.requires_approval is True
    assert write.signature() == json.dumps(
        ["create_file", {"path": "notes.txt", "content": "x"}], sort_keys=True
    )


# ============ ranking from evidence — the SECOND decision, at the RIGHT time ==
# The draft-time order is a PRIOR; _rank_readings overwrites it once the search
# has said what the world is doing. These tests drive the real graph, because the
# two live bugs of 2026-07-17 were both in the wiring, not the module: ranking
# over the wrong strings, and ranking before the evidence existed.

def _rank_stub(monkeypatch, chooser) -> dict:
    """Override the autouse rank stub. `chooser` sees the readings the planner
    actually handed over — which is the thing under test."""
    seen: dict = {"n": 0, "readings": None, "rows": None}

    async def _rank(goal, readings, rows, provider, now=None):
        seen["n"] += 1
        seen["readings"] = list(readings)
        seen["rows"] = rows
        return chooser(readings)

    monkeypatch.setattr(reading_enumerator, "rank_readings", _rank)
    return seen


async def test_evidence_ranking_overrules_the_draft_time_order(db_session, monkeypatch):
    """The fourth FIFA round, frozen. The enumeration's order is a guess made
    before any result exists — live it put the qualification list first, two days
    before the final. Whatever the evidence says wins."""
    seen = _rank_stub(monkeypatch, lambda readings: readings[1])
    plan, _, _ = await _run(
        db_session, monkeypatch, {"query": "fifa 2026 qualified teams"}, _TWO_READINGS
    )

    assert plan.primary_reading == _TWO_READINGS[1]  # not the prior, [0]
    assert seen["n"] == 1


async def test_the_ranker_is_handed_the_queries_that_actually_ran(db_session, monkeypatch):
    """THE LIVE BUG, frozen. When the model fans out itself, nothing is spliced —
    so the enumeration's wording is NOT what produced the rows. Ranking over it
    left every row unattributable (`found_by` carries the executed query), the
    prompt grouped "(nothing)" under both readings, and the call judged blind.

    The executed queries are the ground truth: they fetched the evidence and they
    are what the summary answers from."""
    model_queries = ["model reading A", "model reading B"]
    seen = _rank_stub(monkeypatch, lambda readings: readings[0])
    plan, _, _ = await _run(
        db_session, monkeypatch, {"queries": model_queries}, list(_TWO_READINGS),
    )

    assert seen["readings"] == model_queries       # not _TWO_READINGS
    assert plan.primary_reading == "model reading A"


async def test_a_single_query_search_is_never_ranked(db_session, monkeypatch):
    """One search, nothing to choose between — and no call spent saying so."""
    seen = _rank_stub(monkeypatch, lambda readings: readings[0])
    plan, _, _ = await _run(
        db_session, monkeypatch, {"query": "who is the president of pakistan"}, [],
        goal="who is the president of pakistan",
    )

    assert seen["n"] == 0
    assert plan.primary_reading == ""


async def test_the_verdict_is_taken_once_per_plan(db_session, monkeypatch):
    """Two searches must not re-open a decision the first one's evidence settled:
    the goal is fixed for a plan's life, so the reading is too."""
    searched: list = []
    monkeypatch.setattr(browser_tools, "SEARCH_PROVIDER_FACTORY", _fake_search(searched))
    _stub_readings(monkeypatch, _TWO_READINGS)
    seen = _rank_stub(monkeypatch, lambda readings: readings[0])
    provider = FakeProvider([plan_json([
        step("Search one", "web_search", query="a"),
        step("Search two", "web_search", query="b"),
    ])])

    plan = await AgentPlanner(db_session, provider, session_id="s-rank").start(_GOAL)

    assert plan.status == PlanStatus.COMPLETED
    assert seen["n"] == 1


async def test_a_ranking_failure_leaves_the_prior_standing(db_session, monkeypatch):
    """Best-effort: "" is a no-op, not a wiped verdict. The plan still leads with
    something, which is why the prior is stamped at draft time at all."""
    _rank_stub(monkeypatch, lambda readings: "")
    plan, _, _ = await _run(
        db_session, monkeypatch, {"query": "fifa 2026 qualified teams"}, _TWO_READINGS
    )
    assert plan.primary_reading == _TWO_READINGS[0]


async def test_a_ranker_explosion_never_breaks_the_plan(db_session, monkeypatch):
    async def _boom(goal, readings, rows, provider, now=None):
        raise RuntimeError("429 rate limited")

    monkeypatch.setattr(reading_enumerator, "rank_readings", _boom)
    plan, _, _ = await _run(
        db_session, monkeypatch, {"query": "fifa 2026 qualified teams"}, _TWO_READINGS
    )

    assert plan.status == PlanStatus.COMPLETED
    assert plan.primary_reading == _TWO_READINGS[0]  # the prior survives
