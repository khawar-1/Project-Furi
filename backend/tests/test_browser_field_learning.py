"""
Field learning + the optional sign-in offer + the browse hand-off budget
(2026-07-19).

Three user-reported gaps, fixed here:
  - a form value not in the profile was asked for but NEVER remembered — now the
    answer is derived into a clean profile key and SAVED (`_save_fill_answer`);
  - a site that OFFERS sign in / sign up was silently applied to as a guest — now
    the user is asked (sign in / sign up / apply as guest), and the choice is
    resolved in code (`_auth_offer_choice` / `_handle_auth_offer_answer`);
  - the MAX_QUESTIONS=3 clarification cap would fail an application at the third
    field — structural browse hand-offs now use their own budget
    (`_pause_on_browse_handoff` → browse_handoffs).

Hermetic: the graph is stubbed so `answer()` never launches a real browser (the
conftest refuses that anyway); the browser hand-off helpers are stubbed to no-ops.
"""
import pytest

from app.agents import planner as P
from app.agents.planner import (
    _MAX_BROWSE_HANDOFFS,
    AgentPlanner,
    _auth_offer_choice,
    _auth_offer_question,
    _fill_wall_question,
)
from app.agents.schemas import AgentPlan, PlanQuestion, PlanStatus, PlanStep
from app.core import autofill
from app.core.base_tool import PermissionLevel

from tests.test_agent_planner import FakeProvider


# ------------------------------------------------------------------ harness
class _FakeGraph:
    """Stands in for the compiled LangGraph so answer()'s resume never runs the
    real execute node (which would launch a browser). Returns the plan as-is."""

    def __init__(self):
        self.invoked = 0

    async def ainvoke(self, state):
        self.invoked += 1
        return {"plan": state["plan"]}


def _planner(db) -> AgentPlanner:
    p = AgentPlanner(db, FakeProvider([]), session_id="s-fl")
    p._graph = _FakeGraph()
    return p


def _commit_step() -> PlanStep:
    return PlanStep(
        description="apply to the job",
        tool="browse_commit",
        parameters={"goal": "apply", "start_url": "https://jobs.test/apply",
                    "allowed_origins": ["jobs.test"]},
        permission_level=PermissionLevel.DESTRUCTIVE,
        requires_approval=True,
    )


def _paused_plan(**over) -> AgentPlan:
    plan = AgentPlan(goal="apply to the job", steps=[_commit_step()])
    plan.status = PlanStatus.AWAITING_CHOICE
    plan.question = PlanQuestion(text="q", options=[])
    for k, v in over.items():
        setattr(plan, k, v)
    return plan


# --------------------------------------------------------- pure question text
def test_fill_wall_question_promises_to_remember():
    q = _fill_wall_question("ctl00$ContentPlaceHolder1$txtEmail")
    assert "txtEmail" in q.text
    assert "save" in q.text.lower() and q.options == []


def test_auth_offer_question_shows_only_offered_options():
    both = _auth_offer_question(
        {"auth_offer_site": "jobs.test", "auth_offer_signin": True, "auth_offer_signup": True}
    )
    assert both.kind == "auth_offer"
    assert both.options == ["Sign in", "Sign up", "Apply as guest"]

    signin_only = _auth_offer_question(
        {"auth_offer_site": "jobs.test", "auth_offer_signin": True, "auth_offer_signup": False}
    )
    assert signin_only.options == ["Sign in", "Apply as guest"]


def test_auth_offer_choice_defaults_to_guest():
    assert _auth_offer_choice("Sign in") == "signin"
    assert _auth_offer_choice("I'll sign up") == "signup"
    assert _auth_offer_choice("Apply as guest") == "guest"
    assert _auth_offer_choice("no thanks") == "guest"
    assert _auth_offer_choice("") == "guest"          # fail-safe: never sign in on noise
    assert _auth_offer_choice("register please") == "signup"


# ------------------------------------------------- the browse hand-off budget
def test_browse_handoff_pause_uses_its_own_budget_not_max_questions():
    plan = AgentPlan(goal="g")
    P.AgentPlanner._pause_on_browse_handoff(plan, PlanQuestion(text="need a value"))
    assert plan.status == PlanStatus.AWAITING_CHOICE
    assert plan.browse_handoffs == 1
    assert plan.questions_asked == 0     # NOT counted against the clarification cap
    assert _MAX_BROWSE_HANDOFFS > P.MAX_QUESTIONS   # a real application needs many


# --------------------------------------------------------- field learning
async def test_a_fill_answer_is_saved_to_the_profile(db_session):
    p = _planner(db_session)
    await p._save_fill_answer("ctl00$ContentPlaceHolder1$txtEmail", "me@example.com")

    row = await autofill.get_field(db_session, "email")
    assert row is not None and row.value == "me@example.com" and row.label == "Email"


async def test_a_continue_answer_is_not_saved(db_session):
    p = _planner(db_session)
    # The user added it in Settings themselves and said 'continue' — nothing to learn.
    await p._save_fill_answer("txtPhone", "continue")
    assert await autofill.get_field(db_session, "phone") is None


async def test_answer_learns_the_field_then_resumes(db_session):
    """End to end through answer(): a plan paused for a missing field, the user
    supplies it → it is saved AND the plan resumes (the graph is invoked)."""
    p = _planner(db_session)
    plan = _paused_plan(pending_fill_field="applicant[first_name]")

    out = await p.answer(plan, "Khawar")

    assert plan.pending_fill_field is None          # consumed
    assert p._graph.invoked == 1                     # resumed
    row = await autofill.get_field(db_session, "first_name")
    assert row is not None and row.value == "Khawar"


# --------------------------------------------------- optional sign-in offer
async def test_guest_choice_resolves_the_page_and_resumes(db_session):
    p = _planner(db_session)
    plan = _paused_plan(
        pending_auth_offer="jobs.test",
        pending_auth_url="https://jobs.test/apply",
    )

    out = await p.answer(plan, "Apply as guest")

    assert plan.pending_auth_offer is None
    # The page is recorded so the loop never re-asks it.
    assert "https://jobs.test/apply" in plan.auth_resolved_urls
    assert p._graph.invoked == 1                      # resumed the browse as a guest


async def test_sign_in_choice_hands_off_and_re_pauses(db_session, monkeypatch):
    p = _planner(db_session)
    # Keep it hermetic: the sign-in hand-off would open a real window / discard a
    # held session — both no-ops here.
    async def _noop(*a, **k):
        return True

    monkeypatch.setattr(p, "_discard_discovery_hold", _noop)
    monkeypatch.setattr(p, "_open_commit_login", _noop)

    plan = _paused_plan(
        pending_auth_offer="jobs.test",
        pending_auth_url="https://jobs.test/apply",
    )

    out = await p.answer(plan, "Sign in")

    # It re-paused on the credential hand-off (the user signs in themselves),
    # did NOT resume the browse yet, and marked the page resolved.
    assert out.status == PlanStatus.AWAITING_CHOICE
    assert out.question is not None and out.question.kind == "login"
    assert p._graph.invoked == 0
    assert "https://jobs.test/apply" in plan.auth_resolved_urls
