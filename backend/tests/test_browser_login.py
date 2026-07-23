"""
Phase 14.4 — in-loop sign-in walls.

When a browse loop lands on a login page it must NOT type credentials (it has
none and stores none): it stops, the tool opens a user-driven sign-in window,
and the plan PAUSES on a clarifying question (AWAITING_CHOICE). Answering
'continue' re-runs the browse authenticated. These pin that wiring end to end:

  - the planner turns the browse tool's login signal into an AWAITING_CHOICE
    pause (not a replan), leaving the step to be re-run;
  - resuming (answer) re-plans and re-runs the browse;
  - the BrowseTool surfaces the wall as a structured result AND opens the
    sign-in window, handling no credential itself.
"""
import pytest

import app.tools  # noqa: F401 — registers the real tools
from app.agents import browser_loop, planner as planner_mod
from app.agents.browser_loop import BrowseOutcome
from app.agents.planner import AgentPlanner
from app.agents.schemas import PlanStatus, StepStatus
from app.core import browser_runtime, browser_session
from app.core.base_tool import PermissionLevel, ToolResult

from tests.test_agent_planner import FakeProvider, plan_json, step


def _login_result() -> ToolResult:
    return ToolResult(
        success=False,
        output={
            "login_required": True,
            "login_site": "accounts.google.com",
            "login_url": "https://accounts.google.com/",
            "login_window_opened": True,
        },
        error="Sign-in required at accounts.google.com.",
        permission_level=PermissionLevel.READ,
    )


def _challenge_result() -> ToolResult:
    return ToolResult(
        success=False,
        output={
            "challenge_required": True,
            "challenge_kind": "Cloudflare",
            "challenge_site": "shop.test",
            "challenge_url": "https://shop.test/",
            "challenge_window_opened": True,
        },
        error="A Cloudflare verification at shop.test needs to be completed.",
        permission_level=PermissionLevel.READ,
    )


def _success_result() -> ToolResult:
    return ToolResult(
        success=True,
        output={
            "url": "https://youtube.com/watch?v=x",
            "title": "Jane",
            "rendered": "playing",
            "goal_reached": True,
            "playing": True,
        },
        error="",
        permission_level=PermissionLevel.READ,
    )


def _browse_step() -> dict:
    return step(
        "Play it", "browse",
        goal="play jane on youtube",
        start_url="https://youtube.com",
        allowed_origins=["youtube.com"],
        keep_open=True,
    )


# ------------------------------------------------------------- planner pause
async def test_a_browse_login_wall_pauses_the_plan(db_session, monkeypatch):
    """The tool's login signal → AWAITING_CHOICE, not a failed/replanned step.
    The browse step is left PENDING with no terminal result so the resume can
    re-run it fresh (the same path a clarifying question already uses)."""
    calls = {"n": 0}

    async def fake_exec(tool, params, db, session_id=None, approved=False):
        calls["n"] += 1
        assert tool == "browse"
        return _login_result()

    monkeypatch.setattr(planner_mod, "execute_tool", fake_exec)
    provider = FakeProvider([plan_json([_browse_step()])])

    plan = await AgentPlanner(db_session, provider, session_id="s-login").start(
        "play jane by the long faces on youtube"
    )

    assert plan.status == PlanStatus.AWAITING_CHOICE
    assert plan.question is not None
    assert "sign in" in plan.question.text.lower()
    # Two options (2026-07-23): sign in yourself, OR continue as a guest — many
    # sites are usable without an account and a modal can read as a wall.
    assert plan.question.options == [
        "I've signed in — continue",
        "Continue without signing in",
    ]
    assert calls["n"] == 1
    # Nothing was completed; the browse step is not left in a terminal state.
    assert all(s.status != StepStatus.COMPLETED for s in plan.steps)


async def test_continue_without_login_resumes_as_a_guest(db_session, monkeypatch):
    """Choosing 'Continue without signing in' resumes the SAME browse step with
    skip_login_wall stamped, so the loop ignores the wall and proceeds as a guest
    — no re-draft, and the step carries the flag into run_browse."""
    seq = [_login_result(), _success_result()]
    seen: list[str] = []
    skip_flags: list[bool] = []

    async def fake_exec(tool, params, db, session_id=None, approved=False):
        seen.append(tool)
        skip_flags.append(bool(params.get("skip_login_wall")))
        return seq.pop(0)

    monkeypatch.setattr(planner_mod, "execute_tool", fake_exec)
    provider = FakeProvider([plan_json([_browse_step()])])
    planner = AgentPlanner(db_session, provider, session_id="s-guest")

    plan = await planner.start("play jane by the long faces on youtube")
    assert plan.status == PlanStatus.AWAITING_CHOICE

    resumed = await planner.answer(plan, "Continue without signing in")

    assert resumed.status == PlanStatus.COMPLETED
    assert seen == ["browse", "browse"]         # ran once (wall), then again (guest)
    assert skip_flags == [False, True]          # the resume carries the skip flag
    assert resumed.skip_login_wall is True


async def test_resume_after_sign_in_reruns_the_browse(db_session, monkeypatch):
    """Answering 'continue' re-plans and re-runs the browse — this time it
    succeeds (the persistent profile now holds the cookie)."""
    seq = [_login_result(), _success_result()]
    seen: list[str] = []

    async def fake_exec(tool, params, db, session_id=None, approved=False):
        seen.append(tool)
        return seq.pop(0)

    monkeypatch.setattr(planner_mod, "execute_tool", fake_exec)
    # Draft, then the post-answer revise re-emits the browse step.
    provider = FakeProvider([plan_json([_browse_step()]), plan_json([_browse_step()])])
    planner = AgentPlanner(db_session, provider, session_id="s-login2")

    plan = await planner.start("play jane by the long faces on youtube")
    assert plan.status == PlanStatus.AWAITING_CHOICE

    resumed = await planner.answer(plan, "continue")

    assert resumed.status == PlanStatus.COMPLETED
    assert seen == ["browse", "browse"]  # ran once (wall), then again (signed in)


async def test_a_non_browse_failure_is_not_treated_as_a_login_wall(db_session, monkeypatch):
    """The signal is narrow: only the browse tool's explicit login_required flag.
    A different tool returning a dict never pauses on a sign-in question."""

    async def fake_exec(tool, params, db, session_id=None, approved=False):
        return ToolResult(
            success=False, output={"login_required": True}, error="boom",
            permission_level=PermissionLevel.READ,
        )

    monkeypatch.setattr(planner_mod, "execute_tool", fake_exec)
    # read_file is not the browse tool — a login-shaped output must be ignored.
    provider = FakeProvider([
        plan_json([step("Read", "read_file", path="C:/nope.txt")]),
        plan_json([], reason="cannot read a missing file"),
    ])
    plan = await AgentPlanner(db_session, provider, session_id="s-nl").start("read the file")
    assert plan.status != PlanStatus.AWAITING_CHOICE


# --------------------------------------------------- CAPTCHA / challenge (15.4)
async def test_a_browse_captcha_pauses_the_plan(db_session, monkeypatch):
    """The tool's challenge signal → AWAITING_CHOICE, tagged kind='captcha', never
    a failed/replanned step. The browse step is left PENDING with no terminal
    result so the resume re-runs it once the user has completed the check."""
    calls = {"n": 0}

    async def fake_exec(tool, params, db, session_id=None, approved=False):
        calls["n"] += 1
        assert tool == "browse"
        return _challenge_result()

    monkeypatch.setattr(planner_mod, "execute_tool", fake_exec)
    provider = FakeProvider([plan_json([_browse_step()])])

    plan = await AgentPlanner(db_session, provider, session_id="s-cap").start(
        "play jane by the long faces on youtube"
    )

    assert plan.status == PlanStatus.AWAITING_CHOICE
    assert plan.question is not None
    assert plan.question.kind == "captcha"
    assert "cloudflare" in plan.question.text.lower()
    assert plan.question.options == ["I've completed it — continue"]
    assert calls["n"] == 1
    assert all(s.status != StepStatus.COMPLETED for s in plan.steps)


async def test_resume_after_a_captcha_reruns_the_browse(db_session, monkeypatch):
    """Answering 'continue' re-plans and re-runs the browse — this time it
    succeeds (the profile now holds the challenge-clearance cookie)."""
    seq = [_challenge_result(), _success_result()]
    seen: list[str] = []

    async def fake_exec(tool, params, db, session_id=None, approved=False):
        seen.append(tool)
        return seq.pop(0)

    monkeypatch.setattr(planner_mod, "execute_tool", fake_exec)
    provider = FakeProvider([plan_json([_browse_step()]), plan_json([_browse_step()])])
    planner = AgentPlanner(db_session, provider, session_id="s-cap2")

    plan = await planner.start("play jane by the long faces on youtube")
    assert plan.status == PlanStatus.AWAITING_CHOICE

    resumed = await planner.answer(plan, "continue")

    assert resumed.status == PlanStatus.COMPLETED
    assert seen == ["browse", "browse"]


async def test_a_re_issuing_challenge_stops_honestly_instead_of_looping(
    db_session, monkeypatch
):
    """Honest loop detection (2026-07-19): a challenge that keeps re-issuing after
    the user completes it — Cloudflare Turnstile fingerprinting the automated
    browser — must not pause forever. After _MAX_CHALLENGE_PAUSES hand-offs the
    plan FAILS honestly (never suggesting evasion) rather than trapping the user
    in an unwinnable loop."""

    async def fake_exec(tool, params, db, session_id=None, approved=False):
        return _challenge_result()  # the challenge never passes, however often solved

    monkeypatch.setattr(planner_mod, "execute_tool", fake_exec)
    provider = FakeProvider(
        [
            plan_json([_browse_step()]),  # draft
            plan_json([_browse_step()]),  # revise after hand-off 1
            plan_json([_browse_step()]),  # revise after hand-off 2
        ]
    )
    planner = AgentPlanner(db_session, provider, session_id="s-loop")

    plan = await planner.start("play jane by the long faces on youtube")
    assert plan.status == PlanStatus.AWAITING_CHOICE          # hand-off 1
    assert plan.challenge_attempts == 1

    plan = await planner.answer(plan, "continue")
    assert plan.status == PlanStatus.AWAITING_CHOICE          # hand-off 2
    assert plan.challenge_attempts == 2

    plan = await planner.answer(plan, "continue")
    # The third detection exceeds _MAX_CHALLENGE_PAUSES → honest STOP, not a pause.
    assert plan.status == PlanStatus.FAILED
    assert plan.challenge_attempts == 3
    message = (plan.message or "").lower()
    assert "evade" in message                                # never suggests evasion
    assert "bot protection" in message
    assert any(s.status == StepStatus.FAILED for s in plan.steps)


# --------------------------------------------------------------- tool wiring
async def test_browse_tool_opens_the_window_and_signals_a_captcha(monkeypatch):
    """The BrowseTool closes its agent session, opens a USER-DRIVEN window at the
    challenge (solving nothing itself), and returns a STRUCTURED challenge result
    the planner can pause on."""
    opened: list[str] = []
    created: list = []

    class FakeBrowseSession:
        def __init__(self):
            self.closed = False

        async def goto(self, url):
            pass

        async def close(self):
            self.closed = True

    async def fake_session_open(allowlist):
        s = FakeBrowseSession()
        created.append(s)
        return s

    async def fake_run_browse(session, goal, provider, **kw):
        return BrowseOutcome(
            success=False, actions_taken=1,
            final={"url": "https://shop.test/", "title": "Just a moment...", "rendered": ""},
            error="a Cloudflare verification must be completed at shop.test",
            challenge_required=True, challenge_kind="Cloudflare",
            challenge_url="https://shop.test/", challenge_site="shop.test",
        )

    async def fake_open_login(url=browser_session.DEFAULT_LOGIN_URL):
        opened.append(url)

    async def fake_close_login():
        return False

    async def fake_close_result():
        return False

    async def fake_run_browser(coro, *, timeout=None):
        return await coro

    class FakeProv:
        async def __aexit__(self, *a):
            return False

    from app.core.browser_session import BrowserSession

    monkeypatch.setattr(BrowserSession, "open", fake_session_open)
    monkeypatch.setattr(browser_loop, "run_browse", fake_run_browse)
    monkeypatch.setattr(browser_session, "open_login_window", fake_open_login)
    monkeypatch.setattr(browser_session, "close_login_window", fake_close_login)
    monkeypatch.setattr(browser_session, "close_result_window", fake_close_result)
    monkeypatch.setattr(browser_runtime, "run_browser", fake_run_browser)
    monkeypatch.setattr("app.providers.factory.build_provider", lambda: FakeProv())

    from app.tools.browser_agent_tools import BrowseTool

    result = await BrowseTool().execute(
        goal="open the shop",
        start_url="https://shop.test",
        allowed_origins=["shop.test"],
    )

    assert result.success is False
    assert result.output["challenge_required"] is True
    assert result.output["challenge_kind"] == "Cloudflare"
    assert result.output["challenge_site"] == "shop.test"
    assert opened == ["https://shop.test/"]          # user-driven window at the challenge
    assert created and created[0].closed is True     # agent session freed


async def test_browse_tool_opens_the_sign_in_window_and_signals_login(monkeypatch):
    """The BrowseTool closes its agent session, opens a USER-DRIVEN sign-in
    window (handling no credential itself), and returns a STRUCTURED login
    result the planner can pause on."""
    opened: list[str] = []
    created: list = []

    class FakeBrowseSession:
        def __init__(self):
            self.closed = False

        async def goto(self, url):
            pass

        async def close(self):
            self.closed = True

    async def fake_session_open(allowlist):
        s = FakeBrowseSession()
        created.append(s)
        return s

    async def fake_run_browse(session, goal, provider, **kw):
        return BrowseOutcome(
            success=False, actions_taken=1,
            final={"url": "https://accounts.google.com/", "title": "Sign in", "rendered": ""},
            error="sign-in required at accounts.google.com",
            login_required=True, login_url="https://accounts.google.com/",
            login_site="accounts.google.com",
        )

    async def fake_open_login(url=browser_session.DEFAULT_LOGIN_URL):
        opened.append(url)

    async def fake_close_login():
        return False

    async def fake_run_browser(coro, *, timeout=None):
        return await coro  # run the coroutine on this loop — the fakes are loop-agnostic

    class FakeProv:
        async def __aexit__(self, *a):
            return False

    from app.core.browser_session import BrowserSession

    monkeypatch.setattr(BrowserSession, "open", fake_session_open)
    monkeypatch.setattr(browser_loop, "run_browse", fake_run_browse)
    monkeypatch.setattr(browser_session, "open_login_window", fake_open_login)
    monkeypatch.setattr(browser_session, "close_login_window", fake_close_login)
    monkeypatch.setattr(browser_runtime, "run_browser", fake_run_browser)
    monkeypatch.setattr("app.providers.factory.build_provider", lambda: FakeProv())

    from app.tools.browser_agent_tools import BrowseTool

    result = await BrowseTool().execute(
        goal="play jane on youtube",
        start_url="https://youtube.com",
        allowed_origins=["youtube.com"],
        keep_open=True,
    )

    assert result.success is False
    assert result.output["login_required"] is True
    assert result.output["login_site"] == "accounts.google.com"
    assert opened == ["https://accounts.google.com/"]       # sign-in window opened
    assert created and created[0].closed is True            # agent session freed
