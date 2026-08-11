"""
A bare "open <site>" must DRIVE the browser, not fetch the page (2026-08-11).

THE INCIDENT. `open junaidjamshed.com` came back as "Done — 1 step(s)
completed" followed by the site's entire homepage text — the country picker, the
sale countdown, SIGN IN / TRACKING INFO / GIFTING, the nav menu twice over. The
verbosity is the symptom; the harm is that NOTHING WAS EVER OPENED. There is no
browse trace for that run because `browse` never ran:

    13:34:32  Bare navigation routed in code [BROWSE]: 'open junaidjamshed.com'
    13:34:35  Chat message routed to Browser agent [BROWSE/DELEGATE]
    13:34:51  Plan drafted: 1 step(s)
    13:35:02  Tool executed: read_webpage(url='https://junaidjamshed.com') → ok

Four layers, each individually reasonable:
  1. routing was RIGHT and maximally certain — `_is_bare_navigation` decided
     BROWSE in code, zero LLM calls, because the classifier does not agree with
     itself on this message shape;
  2. plan RULE 20 said "read_webpage is the DEFAULT way to open a URL", and the
     user said "open";
  3. the browser agent's catalog carries read_webpage/web_search/browse_page;
  4. `_browse_downgrade_violation` — the right-shaped guard — is gated on
     `is_browse_task`, seeded only by a submit/sign-in/checkout VERB, which a
     bare "open <site>" has none of. It returned None on its first line.

WHY THESE TESTS DRIVE `_generate_steps` AND NOT THE HELPER ALONE. Asserting
`_browse_substitution` returns a string proves the predicate works and says
nothing about whether the planner ever consults it — the recorded failure mode
of this codebase (2026-07-17: a fan-out feature that had never fired under 1,578
green tests; 2026-08-03/04: two falsifications that came back green because the
test called the function directly). `_generate_steps` is the real method holding
the reject chain, and it is the tightest level that does not launch a Chromium
the suite refuses to give it.
"""
import pytest

import app.tools  # noqa: F401 — registers the real tools, so the catalog is real
from app.agents import planner as P
from app.agents.agent_registry import agent_for_label, agent_for_key
from app.agents.planner import AgentPlanner
from app.agents.rendering import _fmt_browse
from app.agents.schemas import AgentPlan, PlanStep
from app.core import plan_trace
from app.core.base_tool import PermissionLevel

from tests.test_agent_planner import FakeProvider, plan_json, step

# The message that produced the wall of text. Frozen.
_GOAL = "open junaidjamshed.com"
_SITE = "https://junaidjamshed.com"

BROWSER = agent_for_label("BROWSE").key   # "browser"
RESEARCH = agent_for_label("WEB").key     # "research"
GENERAL = agent_for_key(None).key         # "general"


# --------------------------------------------------------------------- harness

def _step(tool: str, level=PermissionLevel.READ, **params) -> PlanStep:
    return PlanStep(
        description=f"{tool} step",
        tool=tool,
        parameters=params,
        permission_level=level,
        requires_approval=level is not PermissionLevel.READ,
    )


def _read_webpage_draft() -> str:
    """What the model actually drafted live."""
    return plan_json([step("Open the junaidjamshed.com homepage",
                           "read_webpage", url=_SITE)])


def _browse_draft() -> str:
    return plan_json([step("Open junaidjamshed.com in the browser", "browse",
                           goal="Open the junaidjamshed.com homepage",
                           start_url=_SITE,
                           allowed_origins=["junaidjamshed.com"])])


async def _generate(provider, agent_key: str, *, goal: str = _GOAL):
    """Run the REAL _generate_steps for one agent. Returns (steps, error).

    `db=None` is safe and deliberate: _generate_steps is one LLM call plus the
    reject chain — it opens no session. Passing a real one would only add a
    fixture between the test and the thing being measured."""
    planner = AgentPlanner(None, provider, agent=agent_for_key(agent_key))
    plan = AgentPlan(goal=goal, agent_key=agent_key)
    result = await planner._generate_steps(
        "PROMPT", allow_empty=False, goal=goal, plan=plan,
        # What the real callers pass (planner.py `_browse_grounding`): without it
        # the ORIGIN guard fires first on every browse step and the test would be
        # measuring that instead.
        browse_origins=P._browse_grounding(plan, ""),
    )
    return result[0], result[3]


# ------------------------------------------------------------- the incident

@pytest.mark.asyncio
async def test_the_incident_a_read_webpage_only_browse_plan_is_rejected():
    """The live draft, through the real planner: a browser-agent plan whose only
    step is read_webpage is refused, and the retry lands on `browse`."""
    provider = FakeProvider([_read_webpage_draft(), _browse_draft()])
    steps, error = await _generate(provider, BROWSER)

    assert error is None
    assert [s.tool for s in steps] == ["browse"]
    # It did not merely accept the second draft by luck — the first was rejected,
    # which is what cost the extra call.
    assert provider.calls == 2
    assert steps[0].parameters["start_url"] == _SITE


@pytest.mark.asyncio
async def test_the_rejection_names_the_browser_and_is_audited():
    """The feedback tells the model what to do instead (never a bare refusal),
    and the rejection is recorded under its own guard name so a live miss is
    diagnosable from plan_traces — every sibling guard does both."""
    reject = P._browse_substitution([_step("read_webpage", url=_SITE)], BROWSER)

    assert reject is not None
    assert "browse" in reject
    assert "read_webpage" in reject
    # Its own name, distinct from the sibling it sits beside — otherwise a live
    # rejection is indistinguishable from a downgrade in the plan_traces table.
    assert plan_trace.GUARD_BROWSE_SUBSTITUTION == "browse_substitution"
    assert plan_trace.GUARD_BROWSE_SUBSTITUTION != plan_trace.GUARD_BROWSE_DOWNGRADE


# ------------------------------------------------- what it must NOT touch

@pytest.mark.asyncio
async def test_a_web_search_feeding_a_browse_is_not_a_substitution():
    """THE REGRESSION TWIN, and the reason this guard is narrow. A read-only web
    tool used ALONGSIDE the browser is a feeder, not a substitute —
    _collapse_browse_apply exists precisely to fold such a step in. Rejecting it
    would break a legitimate chain and cost a planning round for nothing."""
    draft = plan_json([
        step("Find the page", "web_search", query="junaidjamshed perfumes"),
        step("Open it", "browse", goal="Open the store",
             start_url=_SITE, allowed_origins=["junaidjamshed.com"]),
    ])
    provider = FakeProvider([draft])
    steps, error = await _generate(
        provider, BROWSER,
        # The site must be NAMED, or the ORIGIN guard rejects the browse step
        # first and this test measures that instead of the substitution guard.
        goal="find perfumes on junaidjamshed.com and open the first one",
    )

    assert error is None
    assert [s.tool for s in steps] == ["web_search", "browse"]
    assert provider.calls == 1  # accepted first time — no rejection round


def test_a_plan_with_no_read_web_tool_is_not_a_substitution():
    """"stop the music" routes BROWSE and plans a lone stop_media. Nothing was
    swapped in for the browser, so there is nothing to refuse."""
    assert P._browse_substitution([_step("stop_media")], BROWSER) is None


def test_a_research_agent_read_webpage_is_untouched():
    """A WEB lookup is the research agent's whole job — read_webpage is the RIGHT
    tool there. The guard keys on the router's verdict, so it never fires."""
    steps = [_step("read_webpage", url=_SITE)]
    assert P._browse_substitution(steps, RESEARCH) is None
    assert P._browse_substitution(steps, GENERAL) is None
    assert P._browse_substitution(steps, "") is None


def test_a_browse_commit_also_counts_as_driving_the_browser():
    """browse_commit drives the same live session; a plan built around it has not
    substituted anything away."""
    steps = [
        _step("read_webpage", url=_SITE),
        _step("browse_commit", PermissionLevel.DESTRUCTIVE, start_url=_SITE),
    ]
    assert P._browse_substitution(steps, BROWSER) is None


@pytest.mark.parametrize("tool", ["read_webpage", "browse_page", "web_search"])
def test_every_read_only_web_tool_is_caught(tool):
    """All three fetch content and none of them opens a window, so all three are
    the same mistake on this goal shape."""
    assert P._browse_substitution([_step(tool)], BROWSER) is not None


# ------------------------------------------------------- what the user sees

def test_the_browse_result_for_this_goal_is_one_line():
    """The end state. `browse` terminates on arrival (_destination_reached), and
    a destination-only outcome renders the head line ALONE — the page is not
    fenced. This is what replaces the 8,000-character homepage dump."""
    text = _fmt_browse({
        "title": "J. Junaid Jamshed Official Website",
        "url": "https://www.junaidjamshed.com/",
        "done_reason": "the site is open — that was the whole goal.",
        "destination_only": True,
        # Present in the tool output for the audit record, and must stay out of
        # the chat: this is the wall the user was shown.
        "page_text": "Select Your Country\nPakistan\n" + "nav item\n" * 400,
        "rendered": "[1] link 'WOMEN'\n" * 300,
    })

    assert len(text) < 300, text[:400]
    assert "Select Your Country" not in text
    assert "junaidjamshed.com" in text


# ------------------------------------------------------ the prompt contract

def test_rule_20_no_longer_claims_the_word_open():
    """The wording that steered the model. read_webpage owns READING a URL's
    content; it must not advertise itself as the way to "open" one."""
    assert "read_webpage is the DEFAULT way to open a URL" not in P._PLAN_RULES
    assert "read_webpage is the DEFAULT way to READ THE CONTENT of a URL" in P._PLAN_RULES


def test_rule_21_owns_the_bare_navigation_case():
    """…and the tool that DOES open a site says so, or rule 20's absence just
    leaves the case unowned."""
    rules = P._PLAN_RULES
    assert "Putting a site ON SCREEN is browse too" in rules
    assert "open junaidjamshed.com" in rules  # the incident, as the worked example
