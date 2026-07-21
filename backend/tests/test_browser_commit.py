"""
Phase 14.5 — COMMIT mode (submit ONE approved web form, and nothing else).

Two halves are pinned here:

  - the PLANNER flow: a browse_commit step discovers the form (READ), the plan
    PAUSES for signature approval with the code-read form contract rendered into
    action_detail AND baked into the signature, and approving runs exactly one
    submit; an ungrounded site is refused before discovery, a sign-in wall pauses
    for manual login;
  - the SUBMIT orchestration (browser_commit.perform): it verifies the held form
    is unchanged, arms the interceptor, submits, and fails closed on a mismatch
    or an expired session.

The interceptor's one-shot arming — the security core — is pinned in
test_browser_session.py.
"""
import pytest

import app.tools  # noqa: F401 — registers the real tools
from app.agents import browser_commit, browser_loop, planner as planner_mod
from app.agents.planner import AgentPlanner
from app.agents.schemas import PlanStatus, StepStatus
from app.core import browser_runtime, browser_session, dom_observe
from app.core.base_tool import PermissionLevel, ToolResult
from app.core.browser_session import BrowserSession, _commit_fingerprint

from tests.test_agent_planner import FakeProvider, plan_json, step
from tests.test_browser_loop import FakeProvider as LoopProvider, ScriptedPage, _el, _page


_STATE = {
    "url": "https://example.com/comment",
    "method": "POST",
    "fields": [{"name": "comment", "value": "hello world"}],
}


def _commit_step() -> dict:
    return step(
        "Post the comment",
        "browse_commit",
        goal="post 'hello world' as a comment on example.com",
        start_url="https://example.com/post",
        allowed_origins=["example.com"],
    )


def _record_exec(calls: list):
    async def fake_exec(tool, params, db, session_id=None, approved=False):
        calls.append({"tool": tool, "approved": approved, "params": dict(params)})
        return ToolResult(
            success=True,
            output={"submitted": True, "message": "Submitted the approved form."},
            permission_level=PermissionLevel.DESTRUCTIVE,
        )

    return fake_exec


# --------------------------------------------------------------- planner flow
async def test_a_browse_commit_step_discovers_the_form_then_pauses_for_approval(
    db_session, monkeypatch
):
    """DISCOVER runs (READ), the code-read contract lands in action_detail AND the
    signature, and NOTHING executes — the plan waits for approval."""

    async def fake_discover(params, session_id=None, **kwargs):
        return browser_commit.CommitDiscovery(state=dict(_STATE))

    calls: list = []
    monkeypatch.setattr(browser_commit, "discover", fake_discover)
    monkeypatch.setattr(planner_mod, "execute_tool", _record_exec(calls))

    provider = FakeProvider([plan_json([_commit_step()])])
    plan = await AgentPlanner(db_session, provider, session_id="s-commit").start(
        "post 'hello world' as a comment on example.com"
    )

    assert plan.status == PlanStatus.AWAITING_APPROVAL
    s = plan.steps[0]
    # The card shows the exact contract — method, URL, and the real field value.
    assert s.action_detail is not None
    assert "POST https://example.com/comment" in s.action_detail
    assert "hello world" in s.action_detail
    # The approval binds to the discovered values (they are in the signature).
    assert browser_commit.COMMIT_PARAM in s.parameters
    assert "hello world" in s.signature()
    # Nothing ran — no submit before approval.
    assert calls == []


async def test_a_form_value_not_in_the_profile_pauses_to_ask(db_session, monkeypatch):
    """15.2: a form value the loop could not ground in the profile or the user's
    words makes DISCOVERY return fill_required — the plan PAUSES on a clarifying
    question naming the field, and NOTHING is submitted (never a guessed value)."""

    async def fake_discover(params, session_id=None, **kwargs):
        return browser_commit.CommitDiscovery(
            fill_required=True, fill_field="Phone number", error="need a phone number"
        )

    calls: list = []
    monkeypatch.setattr(browser_commit, "discover", fake_discover)
    monkeypatch.setattr(planner_mod, "execute_tool", _record_exec(calls))

    provider = FakeProvider([plan_json([_commit_step()])])
    plan = await AgentPlanner(db_session, provider, session_id="s-fill").start(
        "post 'hello world' as a comment on example.com"
    )

    assert plan.status == PlanStatus.AWAITING_CHOICE
    assert plan.question is not None and "Phone number" in plan.question.text
    assert calls == []  # nothing submitted while a value is unknown


async def test_a_fill_pause_at_the_handoff_budget_discards_the_held_window(
    db_session, monkeypatch
):
    """When the hand-off budget is exhausted, the fill pause becomes a step
    failure — but discover() already HELD the live part-filled session for the
    pause that never happens. The failure path must discard that hold, or a
    real Chromium window leaks until the next discovery replaces it."""

    class _Held:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    held = _Held()

    async def fake_discover(params, session_id=None, **kwargs):
        await browser_session.hold_discovery(
            held, meta={"reason": "fill", "goal": "post a comment"}
        )
        return browser_commit.CommitDiscovery(
            fill_required=True, fill_field="Phone number", error="need a phone number"
        )

    calls: list = []
    monkeypatch.setattr(browser_commit, "discover", fake_discover)
    monkeypatch.setattr(planner_mod, "execute_tool", _record_exec(calls))
    monkeypatch.setattr(planner_mod, "_MAX_BROWSE_HANDOFFS", 0)

    provider = FakeProvider(
        [plan_json([_commit_step()]), plan_json([_commit_step()]), plan_json([_commit_step()])]
    )
    plan = await AgentPlanner(db_session, provider, session_id="s-fill-cap").start(
        "post 'hello world' as a comment on example.com"
    )

    assert plan.status == PlanStatus.FAILED
    assert held.closed, "the held discovery session leaked on the budget-exhausted failure"
    assert browser_session.pending_discovery() is None
    assert calls == []


async def test_approving_the_form_runs_exactly_one_submit(db_session, monkeypatch):
    """On approval the same step re-enters — discovery is NOT repeated — and runs
    the one submit, approved, carrying the exact approved field values."""

    async def fake_discover(params, session_id=None, **kwargs):
        return browser_commit.CommitDiscovery(state=dict(_STATE))

    calls: list = []
    monkeypatch.setattr(browser_commit, "discover", fake_discover)
    monkeypatch.setattr(planner_mod, "execute_tool", _record_exec(calls))

    provider = FakeProvider([plan_json([_commit_step()])])
    planner = AgentPlanner(db_session, provider, session_id="s-commit2")
    plan = await planner.start("post 'hello world' as a comment on example.com")
    assert plan.status == PlanStatus.AWAITING_APPROVAL

    resumed = await planner.resume(plan, approved=True)

    assert resumed.status == PlanStatus.COMPLETED
    assert len(calls) == 1
    assert calls[0]["tool"] == "browse_commit"
    assert calls[0]["approved"] is True
    approved = calls[0]["params"][browser_commit.COMMIT_PARAM]
    assert approved["fields"][0]["value"] == "hello world"
    assert approved["url"] == "https://example.com/comment"


async def test_cancelling_the_approval_submits_nothing(db_session, monkeypatch):
    async def fake_discover(params, session_id=None, **kwargs):
        return browser_commit.CommitDiscovery(state=dict(_STATE))

    calls: list = []
    monkeypatch.setattr(browser_commit, "discover", fake_discover)
    monkeypatch.setattr(planner_mod, "execute_tool", _record_exec(calls))

    provider = FakeProvider([plan_json([_commit_step()])])
    planner = AgentPlanner(db_session, provider, session_id="s-commit3")
    plan = await planner.start("post 'hello world' as a comment on example.com")
    resumed = await planner.resume(plan, approved=False)

    assert resumed.status == PlanStatus.CANCELLED
    assert calls == []  # a declined submit runs nothing


async def test_a_browse_commit_to_an_unnamed_site_is_refused_before_discovery(
    db_session, monkeypatch
):
    """Origin grounding applies to browse_commit with a write behind it: a step
    targeting a site the user never named is rejected by the planner, and
    discovery never runs."""

    async def boom(params, session_id=None):  # pragma: no cover - must not run
        raise AssertionError("discovery ran for an ungrounded browse_commit step")

    monkeypatch.setattr(browser_commit, "discover", boom)

    bad = step(
        "Post it",
        "browse_commit",
        goal="post a comment on example.com",
        start_url="https://attacker.com/x",
        allowed_origins=["attacker.com"],
    )
    provider = FakeProvider([plan_json([bad]), plan_json([bad])])
    plan = await AgentPlanner(db_session, provider, session_id="s-bad").start(
        "post a comment on example.com"
    )
    assert plan.status == PlanStatus.FAILED


async def test_a_sign_in_wall_during_discovery_pauses_for_manual_login(
    db_session, monkeypatch
):
    async def fake_discover(params, session_id=None, **kwargs):
        return browser_commit.CommitDiscovery(
            login_required=True,
            login_site="accounts.google.com",
            error="sign-in required at accounts.google.com",
        )

    async def fake_open(site):
        return True

    monkeypatch.setattr(browser_commit, "discover", fake_discover)
    monkeypatch.setattr(
        planner_mod.AgentPlanner, "_open_commit_login", staticmethod(fake_open)
    )

    provider = FakeProvider([plan_json([_commit_step()])])
    plan = await AgentPlanner(db_session, provider, session_id="s-login").start(
        "post 'hello world' as a comment on example.com"
    )
    assert plan.status == PlanStatus.AWAITING_CHOICE
    assert plan.question.kind == "login"
    assert "sign in" in plan.question.text.lower()


async def test_a_challenge_pause_spends_the_handoff_budget_not_the_question_budget(
    db_session, monkeypatch
):
    """Unified budget (refactor): a CAPTCHA hand-off is a structural browse
    hand-off like fill/login/origin — it counts against browse_handoffs and
    leaves the scarce MAX_QUESTIONS clarification budget untouched. It used to
    burn questions_asked (and ignore the hand-off cap entirely), so a couple of
    challenges could rob a plan of its ability to ask anything else."""

    async def fake_discover(params, session_id=None, **kwargs):
        return browser_commit.CommitDiscovery(
            challenge_required=True,
            challenge_kind="reCAPTCHA",
            challenge_site="example.com",
            challenge_mode="interstitial",
            error="a reCAPTCHA verification at example.com must be completed first",
        )

    async def fake_open(site):
        return True

    monkeypatch.setattr(browser_commit, "discover", fake_discover)
    monkeypatch.setattr(
        planner_mod.AgentPlanner, "_open_commit_login", staticmethod(fake_open)
    )

    provider = FakeProvider([plan_json([_commit_step()])])
    plan = await AgentPlanner(db_session, provider, session_id="s-chal-budget").start(
        "post 'hello world' as a comment on example.com"
    )
    assert plan.status == PlanStatus.AWAITING_CHOICE
    assert plan.question.kind == "captcha"
    assert plan.browse_handoffs == 1
    assert plan.questions_asked == 0


async def test_an_embedded_challenge_at_the_handoff_cap_discards_the_held_window(
    db_session, monkeypatch
):
    """A challenge arriving with the hand-off budget spent fails honestly — and
    must release the embedded-challenge hold, or the filled form's window leaks
    (the same leak class as the fill-at-cap case)."""

    class _Held:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

        def disarm_challenge_traffic(self):
            pass

    held = _Held()

    async def fake_discover(params, session_id=None, **kwargs):
        await browser_session.hold_challenge(
            held,
            meta={"kind": "Cloudflare", "site": "example.com", "url": "", "goal": "g"},
        )
        return browser_commit.CommitDiscovery(
            challenge_required=True,
            challenge_kind="Cloudflare",
            challenge_site="example.com",
            challenge_mode="embedded",
            error="a Cloudflare verification at example.com must be completed",
        )

    monkeypatch.setattr(browser_commit, "discover", fake_discover)
    monkeypatch.setattr(planner_mod, "_MAX_BROWSE_HANDOFFS", 0)

    provider = FakeProvider([plan_json([_commit_step()])])
    plan = await AgentPlanner(db_session, provider, session_id="s-chal-cap").start(
        "post 'hello world' as a comment on example.com"
    )
    assert plan.status == PlanStatus.FAILED
    assert held.closed, "the held challenge session leaked on the at-cap give-up"
    assert browser_session.pending_challenge() is None


async def test_a_signup_wall_during_discovery_pauses_for_manual_signup(
    db_session, monkeypatch
):
    """A signup/account-creation form hands off the SAME way as a login wall,
    tagged kind='signup' so the pause text says the USER creates the account."""
    async def fake_discover(params, session_id=None, **kwargs):
        return browser_commit.CommitDiscovery(
            login_required=True,
            login_site="example.com",
            wall_kind="signup",
            error="account sign-up required at example.com",
        )

    async def fake_open(site):
        return True

    monkeypatch.setattr(browser_commit, "discover", fake_discover)
    monkeypatch.setattr(
        planner_mod.AgentPlanner, "_open_commit_login", staticmethod(fake_open)
    )

    provider = FakeProvider([plan_json([_commit_step()])])
    plan = await AgentPlanner(db_session, provider, session_id="s-signup").start(
        "create an account on example.com"
    )
    assert plan.status == PlanStatus.AWAITING_CHOICE
    assert plan.question.kind == "signup"
    assert "account" in plan.question.text.lower()


async def test_a_captcha_during_discovery_pauses_for_manual_completion(
    db_session, monkeypatch
):
    """A CAPTCHA on the way to the form hands off the SAME way as a login wall
    (15.4), tagged kind='captcha' — the USER completes the check, Jarvis never
    solves it — and the step is left PENDING to re-discover after."""
    async def fake_discover(params, session_id=None, **kwargs):
        return browser_commit.CommitDiscovery(
            challenge_required=True,
            challenge_kind="Cloudflare",
            challenge_site="example.com",
            error="a Cloudflare verification at example.com must be completed first",
        )

    async def fake_open(site):
        return True

    monkeypatch.setattr(browser_commit, "discover", fake_discover)
    monkeypatch.setattr(
        planner_mod.AgentPlanner, "_open_commit_login", staticmethod(fake_open)
    )

    provider = FakeProvider([plan_json([_commit_step()])])
    plan = await AgentPlanner(db_session, provider, session_id="s-captcha").start(
        "post 'hello world' as a comment on example.com"
    )
    assert plan.status == PlanStatus.AWAITING_CHOICE
    assert plan.question.kind == "captcha"
    assert "cloudflare" in plan.question.text.lower()
    assert all(s.status != StepStatus.COMPLETED for s in plan.steps)


async def test_off_site_apply_pauses_then_resumes_on_yes(db_session, monkeypatch):
    """Off-site hand-off (2026-07-18): discovery would leave the named site for a
    page-derived origin → PAUSE and ask. On 'yes' the origin is approved
    (plan.approved_origins), injected into the re-drafted step's allowlist, and
    the resumed discovery reaches the form. Jarvis leaves the site only on the
    user's explicit go-ahead."""
    seen = {"n": 0}

    async def fake_discover(params, session_id=None, **kwargs):
        seen["n"] += 1
        if seen["n"] == 1:
            return browser_commit.CommitDiscovery(
                origin_approval_required=True,
                origin_candidate="greenhouse.io",
                error="needs your approval to visit greenhouse.io",
            )
        # The resume must have injected the approved origin into the allowlist.
        assert "greenhouse.io" in (params.get("allowed_origins") or [])
        return browser_commit.CommitDiscovery(state=dict(_STATE))

    calls: list = []
    monkeypatch.setattr(browser_commit, "discover", fake_discover)
    monkeypatch.setattr(planner_mod, "execute_tool", _record_exec(calls))

    # draft + reflect (browse_commit is a WRITE plan, so reflect runs) + the
    # post-answer revise round.
    provider = FakeProvider([plan_json([_commit_step()])] * 3)
    planner = AgentPlanner(db_session, provider, session_id="s-offsite")
    plan = await planner.start("post 'hello world' as a comment on example.com")

    assert plan.status == PlanStatus.AWAITING_CHOICE
    assert plan.question.kind == "origin_approval"
    assert "greenhouse.io" in plan.question.text
    assert plan.pending_origin_approval == "greenhouse.io"

    resumed = await planner.answer(plan, "Yes — continue to greenhouse.io")

    assert "greenhouse.io" in resumed.approved_origins
    assert resumed.pending_origin_approval is None
    assert resumed.status == PlanStatus.AWAITING_APPROVAL   # reached the form
    assert seen["n"] == 2                                    # discovery re-ran


async def test_an_approved_origin_resumes_at_the_approved_url(db_session, monkeypatch):
    """2026-07-19, the WWR resume-blind incident: after the user's 'yes' the
    revised browse restarted at the ORIGINAL start_url, wandered the homepage
    and died on the stuck-limit — the approved destination had been thrown
    away. Now the pause records the exact URL (plan.pending_origin_url) and a
    'yes' stamps it into the paused step IN CODE and re-enters execute directly:
    the resumed discovery opens the page the user approved, and the resume
    costs ZERO extra LLM calls."""
    seen = {"start_urls": []}

    async def fake_discover(params, session_id=None, **kwargs):
        seen["start_urls"].append(params.get("start_url"))
        if len(seen["start_urls"]) == 1:
            return browser_commit.CommitDiscovery(
                origin_approval_required=True,
                origin_candidate="greenhouse.io",
                origin_url="https://boards.greenhouse.io/acme/jobs/123/apply",
                error="needs your approval to visit greenhouse.io",
            )
        return browser_commit.CommitDiscovery(state=dict(_STATE))

    calls: list = []
    monkeypatch.setattr(browser_commit, "discover", fake_discover)
    monkeypatch.setattr(planner_mod, "execute_tool", _record_exec(calls))

    provider = FakeProvider([plan_json([_commit_step()])] * 2)  # draft + reflect ONLY
    planner = AgentPlanner(db_session, provider, session_id="s-offsite-resume")
    plan = await planner.start("post 'hello world' as a comment on example.com")

    assert plan.status == PlanStatus.AWAITING_CHOICE
    assert plan.pending_origin_url == "https://boards.greenhouse.io/acme/jobs/123/apply"
    llm_before = provider.calls

    resumed = await planner.answer(plan, "Yes — continue to greenhouse.io")

    # The resumed discovery opened the page the user approved, not the old start.
    assert seen["start_urls"][1] == "https://boards.greenhouse.io/acme/jobs/123/apply"
    assert provider.calls == llm_before          # deterministic — no revise call
    assert resumed.status == PlanStatus.AWAITING_APPROVAL
    assert resumed.pending_origin_url is None
    assert "greenhouse.io" in resumed.approved_origins


async def test_a_cross_origin_pending_url_is_never_stamped(db_session, monkeypatch):
    """Fail-closed: a recorded URL whose host does not match the origin the user
    actually approved is ignored — the ordinary revise path runs instead, and the
    step keeps its original start_url."""
    seen = {"start_urls": []}

    async def fake_discover(params, session_id=None, **kwargs):
        seen["start_urls"].append(params.get("start_url"))
        if len(seen["start_urls"]) == 1:
            return browser_commit.CommitDiscovery(
                origin_approval_required=True,
                origin_candidate="greenhouse.io",
                origin_url="https://evil.example.net/phish",
                error="needs your approval to visit greenhouse.io",
            )
        return browser_commit.CommitDiscovery(state=dict(_STATE))

    calls: list = []
    monkeypatch.setattr(browser_commit, "discover", fake_discover)
    monkeypatch.setattr(planner_mod, "execute_tool", _record_exec(calls))

    # draft + reflect + the post-answer revise round (the fallback path).
    provider = FakeProvider([plan_json([_commit_step()])] * 3)
    planner = AgentPlanner(db_session, provider, session_id="s-offsite-cross")
    plan = await planner.start("post 'hello world' as a comment on example.com")
    assert plan.status == PlanStatus.AWAITING_CHOICE

    resumed = await planner.answer(plan, "Yes — continue to greenhouse.io")

    assert seen["start_urls"][1] != "https://evil.example.net/phish"
    assert resumed.status == PlanStatus.AWAITING_APPROVAL


async def test_off_site_apply_declined_stops_without_visiting(db_session, monkeypatch):
    """Fail-closed: a non-affirmative answer to the origin-approval means DON'T
    leave the named site — the plan stops (CANCELLED), the origin is NOT approved,
    and discovery never re-runs. Jarvis never follows a page-derived site on a
    'no'."""
    seen = {"n": 0}

    async def fake_discover(params, session_id=None, **kwargs):
        seen["n"] += 1
        return browser_commit.CommitDiscovery(
            origin_approval_required=True,
            origin_candidate="greenhouse.io",
            error="needs your approval to visit greenhouse.io",
        )

    calls: list = []
    monkeypatch.setattr(browser_commit, "discover", fake_discover)
    monkeypatch.setattr(planner_mod, "execute_tool", _record_exec(calls))

    provider = FakeProvider([plan_json([_commit_step()])])
    planner = AgentPlanner(db_session, provider, session_id="s-offsite-no")
    plan = await planner.start("post 'hello world' as a comment on example.com")
    assert plan.status == PlanStatus.AWAITING_CHOICE

    resumed = await planner.answer(plan, "no, stay on example.com")

    assert resumed.status == PlanStatus.CANCELLED
    assert "greenhouse.io" not in resumed.approved_origins
    assert seen["n"] == 1     # discovery did NOT re-run
    assert calls == []        # nothing submitted


async def test_a_discovery_failure_replans_not_submits(db_session, monkeypatch):
    """A discovery that cannot reach a submittable form fails the step into the
    replan loop — it never pauses for approval on nothing."""

    async def fake_discover(params, session_id=None, **kwargs):
        return browser_commit.CommitDiscovery(error="I couldn't find a form to submit.")

    calls: list = []
    monkeypatch.setattr(browser_commit, "discover", fake_discover)
    monkeypatch.setattr(planner_mod, "execute_tool", _record_exec(calls))

    # Draft the commit step; the revise round gives up honestly.
    provider = FakeProvider(
        [plan_json([_commit_step()]), plan_json([], reason="no form to submit")]
    )
    plan = await AgentPlanner(db_session, provider, session_id="s-nofrm").start(
        "post 'hello world' as a comment on example.com"
    )
    assert plan.status != PlanStatus.AWAITING_APPROVAL
    assert calls == []  # nothing was ever submitted


# ------------------------------------------------------------ tool-level gate
async def test_the_commit_tool_will_not_submit_without_an_approved_form():
    """browse_commit.execute is the SUBMIT phase only — with no approved contract
    (invoked out of sequence) it refuses, never submits on a guess."""
    from app.tools.browser_agent_tools import BrowseCommitTool

    result = await BrowseCommitTool().execute(goal="x", start_url="https://example.com")
    assert result.success is False
    assert "approved" in (result.error or "").lower()


async def test_execute_tool_refuses_browse_commit_without_approval(db_session):
    """The structural gate: browse_commit is DESTRUCTIVE, so execute_tool refuses
    it without approved=True — the submit can never run unapproved."""
    from app.tools.registry import execute_tool

    result = await execute_tool("browse_commit", {}, db_session, approved=False)
    assert result.success is False
    assert result.requires_approval is True


# ------------------------------------------------------ perform() orchestration
class StubCommitSession:
    """A held session as browser_commit.perform sees it — no real browser."""

    def __init__(self, *, verify=True, fired=True):
        self._verify = verify
        self._fired = fired
        self.armed = None
        self.submitted = False
        self.closed = False
        self.playback = False  # set iff enter_playback_mode is ever called
        self.page = object()
        self.stats = browser_session.InterceptStats()
        self.commits_done = 0  # multi-commit budget counter (15.1)

    async def enter_playback_mode(self, *, reload=True):
        # A kept-open result window must NEVER lift interception (unlike media) —
        # this flag lets a test prove perform() leaves the window read-only.
        self.playback = True

    async def verify_commit(self, approved):
        return self._verify

    def arm_commit(self, method, url):
        self.armed = (method, url)

    async def submit_commit(self):
        self.submitted = True

    async def settle(self):
        pass

    def commit_fired(self):
        return self._fired

    async def close(self):
        self.closed = True


async def _fake_observe(page):
    return dom_observe.Observation(
        observation_id="o",
        url="https://example.com/thanks",
        title="Thanks",
        elements=[],
        element_total=0,
        page_text="Posted!",
        text_truncated=False,
    )


@pytest.fixture
def _direct_browser_runtime(monkeypatch):
    async def fake_run_browser(coro, *, timeout=None):
        return await coro  # the stubs are loop-agnostic

    monkeypatch.setattr(browser_runtime, "run_browser", fake_run_browser)
    monkeypatch.setattr(dom_observe, "observe", _fake_observe)


async def test_perform_verifies_arms_submits_and_closes(_direct_browser_runtime):
    stub = StubCommitSession(verify=True, fired=True)
    await browser_session.hold_commit(stub, state=dict(_STATE))

    result = await browser_commit.perform(dict(_STATE))

    assert result["submitted"] is True
    assert stub.armed == ("POST", "https://example.com/comment")  # armed for the approved request
    assert stub.submitted is True
    assert stub.closed is True                       # one submit per session, then closed
    assert browser_session.pending_commit() is None  # registry emptied


async def test_perform_refuses_when_the_form_changed(_direct_browser_runtime):
    """Fail closed: if the held form no longer matches what was approved, nothing
    is armed and nothing is sent."""
    stub = StubCommitSession(verify=False)
    await browser_session.hold_commit(stub, state=dict(_STATE))

    result = await browser_commit.perform(dict(_STATE))

    assert result["submitted"] is False
    assert "changed" in result["error"].lower()
    assert stub.armed is None       # never armed the interceptor
    assert stub.closed is True


async def test_perform_reports_an_expired_session(_direct_browser_runtime):
    """A restart/timeout dropped the held session — the submit says so and sends
    nothing (never invents a submission)."""
    result = await browser_commit.perform(dict(_STATE))
    assert result["submitted"] is False
    assert "expired" in result["error"].lower()


async def test_perform_reports_a_submission_that_did_not_fire(_direct_browser_runtime):
    """The form was armed and submit() called, but no matching request went out
    (a JS handler swallowed it) — honest 'not sent', never a false success."""
    stub = StubCommitSession(verify=True, fired=False)
    await browser_session.hold_commit(stub, state=dict(_STATE))

    result = await browser_commit.perform(dict(_STATE))

    assert result["submitted"] is False
    assert "did not go through" in result["error"]
    assert stub.closed is True


async def test_perform_grounds_the_confirmation_in_the_server_response(_direct_browser_runtime):
    """14.6: the completion is grounded in what the site ACTUALLY returned (the
    observed page's visible prose), not the goal — the fix for the ungrounded
    'All done' that looked like a fabrication."""
    stub = StubCommitSession(verify=True, fired=True)
    await browser_session.hold_commit(stub, state=dict(_STATE))

    result = await browser_commit.perform(dict(_STATE))

    assert result["submitted"] is True
    assert result["response_text"] == "Posted!"   # from _fake_observe's page_text


async def test_perform_keep_open_leaves_the_window_read_only_and_registered(_direct_browser_runtime):
    """14.6: with keep_open the fired session is NOT closed — it is handed to the
    result-window registry so the user can see the response — and it stays
    READ-ONLY (enter_playback_mode is never called; the spent commit arm is what
    keeps it safe, not closing it)."""
    stub = StubCommitSession(verify=True, fired=True)
    await browser_session.hold_commit(stub, state=dict(_STATE))
    try:
        result = await browser_commit.perform(dict(_STATE), keep_open=True)

        assert result["window_open"] is True
        assert stub.closed is False             # handed off, not closed
        assert stub.playback is False           # interception never lifted — a viewer only
        active = browser_session.active_result_window()
        assert active is not None
        assert active["url"] == result["url"]
    finally:
        await browser_session.close_result_window()
    assert stub.closed is True                  # the registry closes it on demand
    assert browser_session.active_result_window() is None


async def test_perform_keep_open_still_closes_when_the_submit_did_not_fire(_direct_browser_runtime):
    """A submission that never went out must not leave a window lingering — only a
    REAL, fired submit earns the kept-open result window."""
    stub = StubCommitSession(verify=True, fired=False)
    await browser_session.hold_commit(stub, state=dict(_STATE))

    result = await browser_commit.perform(dict(_STATE), keep_open=True)

    assert result["submitted"] is False
    assert result["window_open"] is False
    assert stub.closed is True
    assert browser_session.active_result_window() is None


# ============================================================ 15.1 — multi-commit
# One browse goal may perform up to max_commits SEQUENTIAL approved submits ("apply
# to the first 3 jobs"). Each submit is its OWN signature, approval, and one-shot
# permit — never batched or replayed (the 14.5 guarantee repeated). Between submits
# the SAME live session is held across the pause and the loop RESUMES on it to reach
# the next form, which cannot exist until the previous one is submitted.


async def test_perform_resumes_for_the_next_form_within_budget(
    _direct_browser_runtime, monkeypatch
):
    """A fired submit with budget remaining RESUMES the same held session to reach
    the next form, RE-HOLDS it (does not close), and reports next_commit_required
    so the planner can pause for a fresh, separate approval."""

    class _Provider:
        async def chat(self, *a, **k):
            return None

        async def __aexit__(self, *a):
            return None

    monkeypatch.setattr("app.providers.factory.build_provider", lambda: _Provider())

    next_state = {
        "url": "https://jobs.example.com/apply/2", "method": "POST",
        "fields": [{"name": "name", "value": "Sam"}],
    }

    async def fake_run_browse(session, goal, provider, *, commit=False, upload_path=None, **kwargs):
        assert commit is True            # the resume drives in commit mode
        return browser_loop.BrowseOutcome(
            success=True, actions_taken=2, commit_required=True,
            commit_state=dict(next_state),
        )

    monkeypatch.setattr(browser_loop, "run_browse", fake_run_browse)

    stub = StubCommitSession(verify=True, fired=True)
    await browser_session.hold_commit(stub, state=dict(_STATE))
    try:
        result = await browser_commit.perform(
            dict(_STATE), goal="apply to the first 3 jobs", max_commits=3
        )
        assert result["submitted"] is True
        assert result["commits_done"] == 1
        assert result["next_commit_required"] is True
        assert result["next_commit_state"]["url"].endswith("/apply/2")
        assert stub.closed is False                     # re-held, not closed
        assert browser_session.pending_commit() is not None  # back in the registry
    finally:
        await browser_session.discard_commit()
    assert stub.closed is True                          # the registry closes it


async def test_perform_stops_at_the_commit_budget(_direct_browser_runtime, monkeypatch):
    """The budget is the runaway backstop, enforced in code: once commits_done
    reaches max_commits, perform never resumes for another form — the loop is not
    even asked, so nothing can spin past the approved count."""
    resumed = {"n": 0}

    async def fake_run_browse(session, goal, provider, *, commit=False, upload_path=None, **kwargs):
        resumed["n"] += 1  # must never run — budget already spent
        return browser_loop.BrowseOutcome(success=True, actions_taken=1)

    monkeypatch.setattr(browser_loop, "run_browse", fake_run_browse)

    stub = StubCommitSession(verify=True, fired=True)
    stub.commits_done = 1  # this submit becomes the 2nd, == max_commits below
    await browser_session.hold_commit(stub, state=dict(_STATE))

    result = await browser_commit.perform(dict(_STATE), goal="apply to 2 jobs", max_commits=2)

    assert result["submitted"] is True
    assert result["commits_done"] == 2
    assert result.get("next_commit_required") is False
    assert resumed["n"] == 0        # never resumed for a 3rd form
    assert stub.closed is True      # final submit → closed


async def test_a_two_form_flow_pauses_twice_each_with_its_own_contract(
    db_session, monkeypatch
):
    """The whole multi-commit flow through the planner: discover form 1 → pause →
    approve → submit 1 + reach form 2 → pause AGAIN (a DIFFERENT contract and
    signature) → approve → submit 2 → completed. Two separate approvals, never
    one batched approval — the 14.5 guarantee repeated."""
    state1 = {"url": "https://jobs.example.com/apply/1", "method": "POST",
              "fields": [{"name": "name", "value": "Sam"}]}
    state2 = {"url": "https://jobs.example.com/apply/2", "method": "POST",
              "fields": [{"name": "name", "value": "Sam"}]}

    async def fake_discover(params, session_id=None, **kwargs):
        return browser_commit.CommitDiscovery(state=dict(state1))

    monkeypatch.setattr(browser_commit, "discover", fake_discover)

    calls: list = []

    async def fake_exec(tool, params, db, session_id=None, approved=False):
        calls.append({"approved": approved,
                      "commit": dict(params.get(browser_commit.COMMIT_PARAM) or {})})
        if len(calls) == 1:  # submit #1 reached another form
            out = {"submitted": True, "next_commit_required": True,
                   "next_commit_state": dict(state2), "commits_done": 1}
        else:                # submit #2 — no more forms
            out = {"submitted": True, "next_commit_required": False, "commits_done": 2}
        return ToolResult(success=True, output=out,
                          permission_level=PermissionLevel.DESTRUCTIVE)

    monkeypatch.setattr(planner_mod, "execute_tool", fake_exec)

    commit_step = step(
        "Apply to jobs", "browse_commit",
        goal="apply to the first 2 jobs on jobs.example.com",
        start_url="https://jobs.example.com",
        allowed_origins=["jobs.example.com"],
        max_commits=2,
    )
    provider = FakeProvider([plan_json([commit_step])])
    planner = AgentPlanner(db_session, provider, session_id="s-multi")

    plan = await planner.start("apply to the first 2 jobs on jobs.example.com")
    assert plan.status == PlanStatus.AWAITING_APPROVAL
    assert "apply/1" in plan.steps[0].action_detail
    sig1 = plan.steps[0].signature()

    plan = await planner.resume(plan, approved=True)      # approve form 1
    assert plan.status == PlanStatus.AWAITING_APPROVAL    # pauses AGAIN for form 2
    assert "apply/2" in plan.steps[0].action_detail
    sig2 = plan.steps[0].signature()
    assert sig1 != sig2                                   # a genuinely separate approval

    plan = await planner.resume(plan, approved=True)      # approve form 2
    assert plan.status == PlanStatus.COMPLETED

    assert len(calls) == 2
    assert all(c["approved"] for c in calls)              # each submit was approved
    assert calls[0]["commit"]["url"].endswith("/apply/1")
    assert calls[1]["commit"]["url"].endswith("/apply/2")


async def test_the_tool_clamps_max_commits_to_the_code_cap(monkeypatch):
    """The ceiling is structural: whatever number reaches the tool, it is clamped
    to [1, MAX_COMMITS_CAP] in code before perform ever sees it — a prompt can
    never request an unbounded run of approvals."""
    from app.tools.browser_agent_tools import MAX_COMMITS_CAP, BrowseCommitTool

    seen: dict = {}

    async def fake_perform(approved, *, goal="", max_commits=1, upload_path=None, keep_open=False, **kwargs):
        seen["max_commits"] = max_commits
        return {"submitted": True, "url": "", "title": "", "commits_done": 1}

    monkeypatch.setattr(browser_commit, "perform", fake_perform)

    await BrowseCommitTool().execute(
        goal="apply to a hundred jobs", start_url="https://x.example.com",
        max_commits=999, **{browser_commit.COMMIT_PARAM: dict(_STATE)},
    )
    assert seen["max_commits"] == MAX_COMMITS_CAP  # clamped down, not honored


# ================================================= 15.5 — legible flow + summary
# The multi-commit machinery exists (15.1); 15.5 makes the flow legible: each
# fired submit's server response is accumulated so the grounded completion quotes
# EVERY commit (not just the last), and cancelling a paused flow releases the held
# session so nothing lingers.


async def test_a_multi_commit_flow_records_each_server_response(db_session, monkeypatch):
    """Each fired submit's server response is accumulated on the step and folded
    into commit_history when the flow completes — so the grounded summary can quote
    every form's confirmation, not only the final one."""
    state1 = {"url": "https://jobs.example.com/apply/1", "method": "POST",
              "fields": [{"name": "name", "value": "Sam"}]}
    state2 = {"url": "https://jobs.example.com/apply/2", "method": "POST",
              "fields": [{"name": "name", "value": "Sam"}]}

    async def fake_discover(params, session_id=None, **kwargs):
        return browser_commit.CommitDiscovery(state=dict(state1))

    monkeypatch.setattr(browser_commit, "discover", fake_discover)

    calls: list = []

    async def fake_exec(tool, params, db, session_id=None, approved=False):
        calls.append(1)
        if len(calls) == 1:  # submit #1 fired and reached form #2
            out = {"submitted": True, "next_commit_required": True,
                   "next_commit_state": dict(state2), "commits_done": 1,
                   "url": "https://jobs.example.com/apply/1", "title": "Applied",
                   "response_text": "Application 1 received"}
        else:                # submit #2 fired, no more forms
            out = {"submitted": True, "next_commit_required": False, "commits_done": 2,
                   "url": "https://jobs.example.com/apply/2", "title": "Applied",
                   "response_text": "Application 2 received", "window_open": True}
        return ToolResult(success=True, output=out,
                          permission_level=PermissionLevel.DESTRUCTIVE)

    monkeypatch.setattr(planner_mod, "execute_tool", fake_exec)

    commit_step = step(
        "Apply to jobs", "browse_commit",
        goal="apply to the first 2 jobs on jobs.example.com",
        start_url="https://jobs.example.com",
        allowed_origins=["jobs.example.com"],
        max_commits=2,
    )
    provider = FakeProvider([plan_json([commit_step])])
    planner = AgentPlanner(db_session, provider, session_id="s-hist")

    plan = await planner.start("apply to the first 2 jobs on jobs.example.com")
    plan = await planner.resume(plan, approved=True)      # submit 1 → pause for form 2
    plan = await planner.resume(plan, approved=True)      # submit 2 → completed
    assert plan.status == PlanStatus.COMPLETED

    s = plan.steps[0]
    assert len(s.browse_commits) == 2                     # both submits recorded
    history = s.result.output["commit_history"]           # folded into the result
    assert [c["response_text"] for c in history] == [
        "Application 1 received", "Application 2 received",
    ]
    assert [c["url"] for c in history] == [
        "https://jobs.example.com/apply/1", "https://jobs.example.com/apply/2",
    ]


async def test_cancelling_a_paused_flow_releases_the_held_session(
    db_session, monkeypatch, _direct_browser_runtime
):
    """Stop-the-whole-flow (15.5): cancelling a paused browse_commit plan halts the
    flow AND discards the held browser session, so no discovered-but-unsubmitted
    window lingers. Nothing is submitted."""

    async def fake_discover(params, session_id=None, **kwargs):
        return browser_commit.CommitDiscovery(state=dict(_STATE))

    calls: list = []
    monkeypatch.setattr(browser_commit, "discover", fake_discover)
    monkeypatch.setattr(planner_mod, "execute_tool", _record_exec(calls))

    # A held session sits in the registry (as it would after a real discovery).
    stub = StubCommitSession(verify=True, fired=True)
    await browser_session.hold_commit(stub, state=dict(_STATE))

    provider = FakeProvider([plan_json([_commit_step()])])
    planner = AgentPlanner(db_session, provider, session_id="s-stopflow")
    plan = await planner.start("post 'hello world' as a comment on example.com")
    assert plan.status == PlanStatus.AWAITING_APPROVAL

    resumed = await planner.resume(plan, approved=False)

    assert resumed.status == PlanStatus.CANCELLED
    assert calls == []                                # nothing submitted
    assert stub.closed is True                        # the held window was released
    assert browser_session.pending_commit() is None   # registry emptied


# ============================================================ 14.6 — file upload
# The file is attached during the READ discovery (set_input_files sends nothing),
# folded into the approved commit contract, and only LEAVES on the approved
# submit. So upload-without-approval is impossible by construction, and the
# approval binds to the exact file.

# ------------------------------------------------- BrowserSession.upload_file
class _FileHandle:
    def __init__(self, name="attachment"):
        self._name = name
        self.files = None

    async def set_input_files(self, path):
        self.files = path

    async def get_attribute(self, attr):
        return self._name if attr == "name" else None


class _FilePage:
    def __init__(self, handle):
        self.handle = handle
        self.url = "https://example.com/form"

    async def query_selector(self, selector):
        return self.handle  # dom_observe.resolve returns this handle


def _file_obs() -> dom_observe.Observation:
    return dom_observe.Observation(
        observation_id="o", url="https://example.com/form", title="",
        elements=[dom_observe.Element(index=1, role="file", name="attachment")],
        element_total=1, page_text="", text_truncated=False,
    )


async def test_upload_file_sets_a_grounded_file_and_records_it(tmp_path):
    """A safe path: set_input_files is called with the RESOLVED path and the
    attachment is recorded so it can be folded into the approved contract."""
    from app.tools.file_tools import _resolve_path

    f = tmp_path / "resume.pdf"
    f.write_text("cv")
    handle = _FileHandle()
    session = BrowserSession(browser=object(), page=_FilePage(handle), allowlist={"example.com"})

    ok, note = await session.upload_file(_file_obs(), 1, str(f))

    resolved = str(_resolve_path(str(f)))
    assert ok is True
    assert handle.files == resolved
    assert session.uploads == [{"name": "attachment", "path": resolved}]


async def test_upload_file_refuses_an_unsafe_path_and_sets_nothing(tmp_path):
    """The defense-in-depth backstop: even though the planner already checked the
    path, the one place that touches the filesystem re-refuses a bad path — and
    set_input_files is NEVER called for it."""
    handle = _FileHandle()
    session = BrowserSession(browser=object(), page=_FilePage(handle), allowlist={"example.com"})

    ok, note = await session.upload_file(_file_obs(), 1, r"C:\definitely\missing.pdf")

    assert ok is False
    assert "does not exist" in note
    assert handle.files is None          # nothing was attached
    assert session.uploads == []


# --------------------------------------------- fingerprint / verify bind files
def test_commit_fingerprint_binds_the_attached_file():
    base = {"url": "https://x/s", "method": "POST", "fields": [{"name": "a", "value": "b"}]}
    with_file = {**base, "uploads": [{"name": "f", "path": "C:/x/resume.pdf"}]}
    other_file = {**base, "uploads": [{"name": "f", "path": "C:/x/OTHER.pdf"}]}
    assert _commit_fingerprint(with_file) == _commit_fingerprint(dict(with_file))
    assert _commit_fingerprint(with_file) != _commit_fingerprint(other_file)
    # Backwards compatible: a pre-14.6 state (no uploads key) == empty uploads.
    assert _commit_fingerprint(base) == _commit_fingerprint({**base, "uploads": []})


class _RereadPage:
    def __init__(self, form):
        self._form = form

    async def evaluate(self, js):
        return self._form


async def test_verify_commit_binds_the_file_and_fails_closed_on_a_swap():
    form = {"action": "https://example.com/upload", "method": "POST",
            "fields": [{"name": "note", "value": "hi"}], "has_password": False}
    session = BrowserSession(browser=object(), page=_RereadPage(form), allowlist={"example.com"})
    session.uploads = [{"name": "attachment", "path": "C:/x/resume.pdf"}]

    ok = {"url": "https://example.com/upload", "method": "POST",
          "fields": [{"name": "note", "value": "hi"}],
          "uploads": [{"name": "attachment", "path": "C:/x/resume.pdf"}]}
    assert await session.verify_commit(ok) is True

    swapped = {**ok, "uploads": [{"name": "attachment", "path": "C:/x/OTHER.pdf"}]}
    assert await session.verify_commit(swapped) is False


# ------------------------------------------------- the loop's upload action
class _FakeCommitSession:
    """A commit-mode session for the loop: it records upload_file calls and hands
    back a form contract on read_commit_target, so the loop can fold the attached
    file into commit_state."""

    def __init__(self, page, form):
        self.page = page
        self.stats = browser_session.InterceptStats()
        self.allowlist = {"example.com"}
        self.uploads: list = []
        self._form = form
        self.upload_calls: list = []

    async def settle(self):
        pass

    async def goto(self, url):
        self.page.navigate(url)

    async def upload_file(self, obs, index, path):
        self.upload_calls.append((index, path))
        self.uploads = [u for u in self.uploads if u.get("name") != "attachment"]
        self.uploads.append({"name": "attachment", "path": path})
        return True, ""

    async def read_commit_target(self, obs, index):
        return dict(self._form)


async def test_the_loop_attaches_the_file_then_folds_it_into_commit_state(tmp_path):
    """upload → submit: the loop attaches the pre-grounded file, and the returned
    commit_state carries it so the approval binds to it. The path is fixed
    (upload_path) — the model only chose which input."""
    f = tmp_path / "resume.pdf"
    f.write_text("cv")
    page = ScriptedPage([
        _page([_el(1, role="file", name="attachment"), _el(2, role="button", name="Upload")],
              url="https://example.com/form"),
    ])
    form = {"action": "https://example.com/upload", "method": "POST", "fields": [],
            "has_password": False}
    session = _FakeCommitSession(page, form)
    provider = LoopProvider(['{"action":"upload","index":1}', '{"action":"submit","index":2}'])

    outcome = await browser_loop.run_browse(
        session, "upload resume.pdf to example.com", provider,
        commit=True, upload_path=str(f),
    )

    assert outcome.commit_required is True
    assert session.upload_calls == [(1, str(f))]
    assert outcome.commit_state["uploads"] == [{"name": "attachment", "path": str(f)}]


async def test_the_loop_never_offers_upload_without_a_file():
    """Read-only mode (or a commit with no upload_path) can never attach a file:
    an 'upload' action with nothing behind it is a no-op the loop notes and moves
    past, and the fixed file is what makes this safe."""
    page = ScriptedPage([_page([_el(1, role="file", name="attachment")], url="https://example.com/form")])
    form = {"action": "https://example.com/upload", "method": "POST", "fields": [], "has_password": False}
    session = _FakeCommitSession(page, form)
    # commit=True but NO upload_path → the action is offered nowhere; even a stray
    # 'upload' does nothing (can_upload is False).
    provider = LoopProvider(['{"action":"upload","index":1}', '{"action":"done","reason":"stop"}'])

    outcome = await browser_loop.run_browse(session, "post a comment", provider, commit=True)

    assert session.upload_calls == []  # no file was ever attached


# ------------------------------------------------- planner end-to-end + gate
async def test_a_grounded_upload_discovers_then_pauses_showing_the_file(
    db_session, monkeypatch, tmp_path
):
    """The whole flow: a grounded, safe upload_path reaches discovery, the code-read
    contract (with the file) lands in action_detail AND the signature, and the plan
    PAUSES — nothing is sent before approval."""
    f = tmp_path / "resume.pdf"
    f.write_text("cv")
    upload_path = str(f)
    state = {
        "url": "https://example.com/upload", "method": "POST",
        "fields": [{"name": "note", "value": "here it is"}],
        "uploads": [{"name": "attachment", "path": upload_path}],
    }

    async def fake_discover(params, session_id=None, **kwargs):
        assert params.get("upload_path") == upload_path  # the grounded path reaches discovery
        return browser_commit.CommitDiscovery(state=dict(state))

    calls: list = []
    monkeypatch.setattr(browser_commit, "discover", fake_discover)
    monkeypatch.setattr(planner_mod, "execute_tool", _record_exec(calls))

    upload_step = step(
        "Upload the file", "browse_commit",
        goal=f"upload {f.name} to example.com",
        start_url="https://example.com/careers",
        allowed_origins=["example.com"],
        upload_path=upload_path,
    )
    provider = FakeProvider([plan_json([upload_step])])
    plan = await AgentPlanner(db_session, provider, session_id="s-upload").start(
        f"upload {f.name} to example.com"
    )

    assert plan.status == PlanStatus.AWAITING_APPROVAL
    s = plan.steps[0]
    assert "attach file:" in s.action_detail
    assert upload_path in s.action_detail        # the exact file on the card
    # The approval binds to the file: it is inside the signed contract (the path
    # is JSON-escaped in signature(), so match on the basename + the uploads key).
    assert '"uploads"' in s.signature()
    assert f.name in s.signature()
    assert calls == []                           # nothing sent before approval


async def test_an_ungrounded_upload_is_refused_before_discovery(
    db_session, monkeypatch, tmp_path
):
    """A real, safe file the user never named is refused by the planner gate —
    discovery never runs (a page can never name a file to exfiltrate)."""
    f = tmp_path / "secret.pdf"
    f.write_text("x")

    async def boom(params, session_id=None):  # pragma: no cover - must not run
        raise AssertionError("discovery ran for an ungrounded upload")

    monkeypatch.setattr(browser_commit, "discover", boom)

    bad = step(
        "Upload it", "browse_commit",
        goal="upload a file to example.com",   # never names secret.pdf
        start_url="https://example.com/x",
        allowed_origins=["example.com"],
        upload_path=str(f),
    )
    provider = FakeProvider([plan_json([bad]), plan_json([bad])])
    plan = await AgentPlanner(db_session, provider, session_id="s-uplbad").start(
        "upload a file to example.com"
    )
    assert plan.status == PlanStatus.FAILED


async def test_an_upload_submit_is_blocked_without_approval(db_session):
    """The structural proof the file never leaves unapproved: the only path that
    sends is the DESTRUCTIVE browse_commit submit, and execute_tool refuses it
    without approved=True."""
    from app.tools.registry import execute_tool

    result = await execute_tool(
        "browse_commit", {"upload_path": r"C:\x\resume.pdf"}, db_session, approved=False
    )
    assert result.success is False
    assert result.requires_approval is True


# ---------------------- embedded-challenge hold lifecycle (2026-07-19)
# An embedded widget's token is bound to the page render in the agent's own
# window — the separate hand-off window can never carry it (the "solved it,
# asked again" loop). discover() therefore HOLDS the live session across the
# pause (vendor traffic armed) and the resumed discovery re-attaches to it.
class _ChalSession:
    def __init__(self):
        self.closed = False
        self.armed = False
        self.browse_history: list = []
        self.goto_calls: list = []

    async def goto(self, url):
        self.goto_calls.append(url)

    def arm_challenge_traffic(self):
        self.armed = True

    def disarm_challenge_traffic(self):
        self.armed = False

    async def close(self):
        self.closed = True


_CHAL_PARAMS = {
    "goal": "apply to the job on example.com",
    "start_url": "https://example.com/jobs",
    "allowed_origins": ["example.com"],
}


@pytest.fixture
def _discover_rig(monkeypatch):
    """Drive the REAL discover() with its internals stubbed: run_browser is a
    passthrough, BrowserSession.open hands back a recording fake, and the SSRF
    host check never resolves DNS."""
    async def fake_run_browser(coro, *, timeout=None):
        return await coro

    monkeypatch.setattr(browser_runtime, "run_browser", fake_run_browser)
    monkeypatch.setattr(
        "app.tools.browser_tools._host_is_blocked", lambda h: False
    )

    opened: list[_ChalSession] = []

    async def fake_open(cls_or_allowlist, *a, **kw):
        session = _ChalSession()
        opened.append(session)
        return session

    monkeypatch.setattr(
        browser_session.BrowserSession, "open", classmethod(fake_open)
    )
    return opened


def _challenge_outcome(mode="embedded", kind="reCAPTCHA"):
    return browser_loop.BrowseOutcome(
        success=False, actions_taken=3,
        error=f"a {kind} verification must be completed at example.com",
        challenge_required=True, challenge_kind=kind,
        challenge_url="https://example.com/jobs/apply",
        challenge_site="example.com", challenge_mode=mode,
    )


async def test_discover_holds_the_session_on_an_embedded_challenge(
    _discover_rig, monkeypatch
):
    """The embedded pause: the live session is NOT closed — it is held in the
    challenge registry with the vendor carve-out ARMED, and the discovery
    reports mode 'embedded' so the planner sends the user to THIS window."""
    async def fake_run_browse(session, goal, provider, **kwargs):
        return _challenge_outcome()

    monkeypatch.setattr(browser_loop, "run_browse", fake_run_browse)
    try:
        discovery = await browser_commit.discover(dict(_CHAL_PARAMS))

        assert discovery.challenge_required is True
        assert discovery.challenge_mode == "embedded"
        session = _discover_rig[0]
        assert session.closed is False              # held, never closed
        assert session.armed is True                # the human's solve can complete
        pending = browser_session.pending_challenge()
        assert pending is not None
        assert pending["goal"] == _CHAL_PARAMS["goal"]
        # the loop context knows why it stopped when it resumes.
        assert any("verification" in line for line in session.browse_history)
    finally:
        await browser_session.discard_challenge()


async def test_discover_resumes_on_the_held_session_for_the_same_goal(
    _discover_rig, monkeypatch
):
    """The resume: the SAME held session is taken back (no new launch, no
    navigation — the solved token is bound to the page as it stands), the
    carve-out is disarmed, and the flow proceeds to the ordinary commit hold."""
    held = _ChalSession()
    held.armed = True
    await browser_session.hold_challenge(
        held, meta={"kind": "reCAPTCHA", "site": "example.com",
                    "url": "x", "goal": _CHAL_PARAMS["goal"]},
    )

    async def fake_run_browse(session, goal, provider, **kwargs):
        assert session is held                      # re-attached, not re-launched
        return browser_loop.BrowseOutcome(
            success=True, actions_taken=1, commit_required=True,
            commit_state={"url": "https://example.com/apply", "method": "POST",
                          "fields": [], "uploads": []},
        )

    monkeypatch.setattr(browser_loop, "run_browse", fake_run_browse)
    try:
        discovery = await browser_commit.discover(dict(_CHAL_PARAMS))

        assert discovery.state is not None
        assert _discover_rig == []                  # BrowserSession.open never ran
        assert held.goto_calls == []                # never navigated away
        assert held.armed is False                  # take_challenge disarmed it
        assert browser_session.pending_challenge() is None
        assert browser_session.pending_commit() is not None   # ordinary hold now
    finally:
        await browser_session.discard_commit()


async def test_discover_discards_a_stale_hold_from_another_goal(
    _discover_rig, monkeypatch
):
    """A held session belongs to ITS flow only: a discovery for a different goal
    discards it (wrong page, wrong allowlist) and launches fresh."""
    stale = _ChalSession()
    await browser_session.hold_challenge(
        stale, meta={"kind": "reCAPTCHA", "site": "other.test",
                     "url": "x", "goal": "a completely different goal"},
    )

    async def fake_run_browse(session, goal, provider, **kwargs):
        return browser_loop.BrowseOutcome(
            success=False, actions_taken=1, error="no form found",
        )

    monkeypatch.setattr(browser_loop, "run_browse", fake_run_browse)
    await browser_commit.discover(dict(_CHAL_PARAMS))

    assert stale.closed is True
    assert browser_session.pending_challenge() is None
    assert len(_discover_rig) == 1                  # a fresh session was opened


async def test_discover_interstitial_challenge_closes_and_never_holds(
    _discover_rig, monkeypatch
):
    """The interstitial path is UNCHANGED: the session closes (the clean-window
    hand-off works there — the clearance cookie lives in the shared profile) and
    nothing is held."""
    async def fake_run_browse(session, goal, provider, **kwargs):
        return _challenge_outcome(mode="interstitial", kind="Cloudflare")

    monkeypatch.setattr(browser_loop, "run_browse", fake_run_browse)
    discovery = await browser_commit.discover(dict(_CHAL_PARAMS))

    assert discovery.challenge_required is True
    assert discovery.challenge_mode == "interstitial"
    assert _discover_rig[0].closed is True
    assert _discover_rig[0].armed is False
    assert browser_session.pending_challenge() is None


# ----------------------- planner hand-off texts, mode-aware (2026-07-19)
async def test_an_embedded_challenge_pauses_into_the_agents_own_window(
    db_session, monkeypatch
):
    """mode 'embedded' → the pause tells the user to solve it in the Jarvis
    browser window that is ALREADY open (the held session) — and the separate
    clean hand-off window is NOT opened (it could never carry the token)."""
    async def fake_discover(params, session_id=None, **kwargs):
        return browser_commit.CommitDiscovery(
            challenge_required=True,
            challenge_kind="reCAPTCHA",
            challenge_site="example.com",
            challenge_mode="embedded",
            error="a reCAPTCHA verification at example.com must be completed "
                  "in the open browser window first",
        )

    opened: list = []

    async def fake_open(site):
        opened.append(site)
        return True

    monkeypatch.setattr(browser_commit, "discover", fake_discover)
    monkeypatch.setattr(
        planner_mod.AgentPlanner, "_open_commit_login", staticmethod(fake_open)
    )

    provider = FakeProvider([plan_json([_commit_step()])])
    plan = await AgentPlanner(db_session, provider, session_id="s-embed").start(
        "post 'hello world' as a comment on example.com"
    )

    assert plan.status == PlanStatus.AWAITING_CHOICE
    assert plan.question.kind == "captcha"
    assert "jarvis browser window" in plan.question.text.lower()
    assert opened == []                 # no separate window for an embedded widget


async def test_an_embedded_challenge_giveup_releases_the_held_session(
    db_session, monkeypatch
):
    """The honest-stop on an embedded challenge also DISCARDS the held session —
    a filled form must not linger in a window nobody will resume."""
    async def fake_discover(params, session_id=None, **kwargs):
        return browser_commit.CommitDiscovery(
            challenge_required=True, challenge_kind="reCAPTCHA",
            challenge_site="example.com", challenge_mode="embedded",
            error="a reCAPTCHA verification must be completed",
        )

    async def fake_run_browser(coro, *, timeout=None):
        return await coro

    monkeypatch.setattr(browser_commit, "discover", fake_discover)
    monkeypatch.setattr(browser_runtime, "run_browser", fake_run_browser)

    held = _ChalSession()
    await browser_session.hold_challenge(
        held, meta={"kind": "reCAPTCHA", "site": "example.com",
                    "url": "x", "goal": "whatever"},
    )

    # draft + reflect (a WRITE plan reflects) + the two post-answer revise rounds.
    provider = FakeProvider([plan_json([_commit_step()])] * 4)
    planner = AgentPlanner(db_session, provider, session_id="s-egive")
    plan = await planner.start("post 'hello world' as a comment on example.com")
    assert plan.status == PlanStatus.AWAITING_CHOICE       # hand-off 1
    plan = await planner.answer(plan, "continue")
    assert plan.status == PlanStatus.AWAITING_CHOICE       # hand-off 2
    plan = await planner.answer(plan, "continue")

    assert plan.status == PlanStatus.FAILED                # honest stop
    assert "evade" in (plan.message or "").lower()
    assert held.closed is True                             # the hold was released
    assert browser_session.pending_challenge() is None
