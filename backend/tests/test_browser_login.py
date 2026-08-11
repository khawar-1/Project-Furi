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
def _captcha_tool_harness(monkeypatch, *, release_ok: bool = True):
    """The BrowseTool driven to a Cloudflare interstitial, with every window
    operation recorded. Returns (opened, created, run) — `run` executes the tool.

    The fake session models the two things that matter here: whether it was
    CLOSED, and whether it was RELEASED to the user. A fake that could not tell
    those apart would pass either way."""
    opened: list[str] = []
    created: list = []

    class FakeBrowseSession:
        def __init__(self):
            self.closed = False
            self.released = False
            self.tab_reused = False

        async def goto(self, url):
            pass

        async def release_to_user(self):
            self.released = release_ok
            return release_ok

        async def close(self):
            self.closed = True

        async def release_after_run(self):
            pass

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

    async def run():
        return await BrowseTool().execute(
            goal="open the shop",
            start_url="https://shop.test",
            allowed_origins=["shop.test"],
        )

    return opened, created, run


async def test_a_captcha_hands_over_the_tab_instead_of_closing_the_browser(monkeypatch):
    """THE 2026-08-03 INCIDENT, frozen. A CAPTCHA must not demolish the browser.

    Live: eBay showed a CAPTCHA while junaidjamshed.com was open in another tab.
    The tool closed its session and called open_login_window, whose first act is
    _window.close_all() — so BOTH tabs went, a clean window reopened eBay alone,
    and the resume closed that and launched a third window.

    The check is on a page already on screen, so it is handed to the user there:
    nothing closed, no separate window opened, and the pause text says where to
    look rather than claiming a window was opened."""
    opened, created, run = _captcha_tool_harness(monkeypatch)

    result = await run()

    assert result.success is False
    assert result.output["challenge_required"] is True
    assert result.output["challenge_kind"] == "Cloudflare"
    assert result.output["challenge_site"] == "shop.test"
    # The incident, in three assertions.
    assert opened == []                              # no separate window
    assert created and created[0].closed is False    # the tab is still there
    assert created[0].released is True               # and it is the user's now
    assert result.output["challenge_in_place"] is True
    assert result.output["window_open"] is True
    # The user is told where the check actually is.
    assert "already on your screen" in result.error
    assert "I've opened the page" not in result.error


async def test_a_site_that_challenges_again_escalates_to_the_clean_window(monkeypatch):
    """The clean window is not deleted, it is EARNED (2026-08-03).

    Turnstile and Google fingerprint the automated browser and re-issue the check
    however often a human solves it (live 2026-07-19). So a second challenge from
    the same site, while the in-place hand-over is still fresh, escalates to the
    separate clean window — paying the tabs only once the cheap path has been
    tried and observed to fail."""
    opened, created, run = _captcha_tool_harness(monkeypatch)

    first = await run()
    assert first.output["challenge_in_place"] is True
    assert opened == []

    second = await run()

    assert second.output["challenge_in_place"] is False
    assert opened == ["https://shop.test/"]          # user-driven window at the challenge
    assert created[-1].closed is True                # agent session freed for the profile
    assert "I've opened the page" in second.error


async def test_a_failed_hand_over_falls_back_to_the_clean_window(monkeypatch):
    """release_to_user() returning False must not strand the user with a check
    nobody can reach: the old destructive path is still the fallback, because a
    window that costs tabs beats no window at all."""
    opened, created, run = _captcha_tool_harness(monkeypatch, release_ok=False)

    result = await run()

    assert result.output["challenge_in_place"] is False
    assert opened == ["https://shop.test/"]
    assert created[-1].closed is True
    assert "I've opened the page" in result.error


def _login_tool_harness(monkeypatch, *, release_ok: bool = True):
    """The BrowseTool driven to a genuine sign-in wall, with every window
    operation recorded. The twin of _captcha_tool_harness, and for the same
    reason: the fake models whether the session was CLOSED and whether it was
    RELEASED, because a fake that could not tell those apart would pass whichever
    way the code went."""
    opened: list[str] = []
    created: list = []

    class FakeBrowseSession:
        def __init__(self):
            self.closed = False
            self.released = False
            self.tab_reused = False

        async def goto(self, url):
            pass

        async def release_to_user(self):
            self.released = release_ok
            return release_ok

        async def close(self):
            self.closed = True

        async def release_after_run(self):
            pass

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

    async def fake_close_result():
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
    monkeypatch.setattr(browser_session, "close_result_window", fake_close_result)
    monkeypatch.setattr(browser_runtime, "run_browser", fake_run_browser)
    monkeypatch.setattr("app.providers.factory.build_provider", lambda: FakeProv())

    from app.tools.browser_agent_tools import BrowseTool

    async def run():
        return await BrowseTool().execute(
            goal="play jane on youtube",
            start_url="https://youtube.com",
            allowed_origins=["youtube.com"],
            keep_open=True,
        )

    return opened, created, run


async def test_a_sign_in_wall_hands_over_the_tab_instead_of_closing_the_browser(
    monkeypatch,
):
    """THE 2026-08-08 INCIDENT, frozen — and it is the 2026-08-03 CAPTCHA fix in
    the branch that round did not reach.

    Live: a false wall on anikoto closed EVERY tab (open_login_window's first act
    is _window.close_all()) and reopened a normal window on the wrong episode,
    which the user reasonably read as "it played episode 1 when I asked for 4".

    A wall is on a page already on screen, so it is handed to the user there:
    nothing closed, no separate window opened, and the pause text says where to
    look rather than claiming a window was opened."""
    opened, created, run = _login_tool_harness(monkeypatch)

    result = await run()

    assert result.success is False
    assert result.output["login_required"] is True
    assert result.output["login_site"] == "accounts.google.com"
    # The incident, in three assertions.
    assert opened == []                              # no separate window
    assert created and created[0].closed is False    # the tab is still there
    assert created[0].released is True               # and it is the user's now
    assert result.output["login_in_place"] is True
    assert result.output["window_open"] is True
    # The user is told where the sign-in page actually is.
    assert "already on your screen" in result.error
    assert "I've opened a sign-in window" not in result.error


async def test_a_site_that_walls_again_escalates_to_the_clean_window(monkeypatch):
    """The clean window is not deleted, it is EARNED — the challenge branch's
    rule, and it is true of a wall for the same reason: a site that fingerprints
    the automated browser can re-issue the wall however often a human signs in."""
    opened, created, run = _login_tool_harness(monkeypatch)

    first = await run()
    assert first.output["login_in_place"] is True
    assert opened == []

    second = await run()

    assert second.output["login_in_place"] is False
    assert opened == ["https://accounts.google.com/"]  # user-driven window at the wall
    assert created[-1].closed is True                  # agent session freed for the profile
    assert "I've opened a sign-in window" in second.error


async def test_a_failed_login_hand_over_falls_back_to_the_clean_window(monkeypatch):
    """release_to_user() returning False must not strand the user with a sign-in
    page nobody can reach: the old destructive path is still the fallback,
    because a window that costs tabs beats no window at all."""
    opened, created, run = _login_tool_harness(monkeypatch, release_ok=False)

    result = await run()

    assert result.output["login_in_place"] is False
    assert opened == ["https://accounts.google.com/"]
    assert created[-1].closed is True
    assert "I've opened a sign-in window" in result.error


async def test_the_pause_text_points_at_the_tab_it_was_handed_over_on(monkeypatch):
    """WHAT THE USER READS must match what happened (2026-08-03).

    The pause text is the only instruction they get. Claiming "I've opened the
    page" about a check sitting on the tab already in front of them sends them
    hunting for a window that never appeared — the same class of defect as an
    approval card naming one site while acting on another."""
    from app.agents.planner import _challenge_wall_question

    in_place = _challenge_wall_question(
        {
            "challenge_site": "www.ebay.com",
            "challenge_kind": "CAPTCHA",
            "challenge_mode": "interstitial",
            "challenge_window_opened": True,
            "challenge_in_place": True,
        }
    )
    assert "already open in front of you" in in_place.text
    assert "I've opened the page" not in in_place.text
    assert in_place.kind == "captcha"

    # The escalated hand-off still says a window was opened, because one was.
    escalated = _challenge_wall_question(
        {
            "challenge_site": "www.ebay.com",
            "challenge_kind": "CAPTCHA",
            "challenge_mode": "interstitial",
            "challenge_window_opened": True,
            "challenge_in_place": False,
        }
    )
    assert "I've opened the page" in escalated.text


async def test_the_login_pause_text_points_at_the_tab_it_was_handed_over_on():
    """The login twin of the rule above (2026-08-08). Since the wall is now
    handed over IN PLACE, the sign-in page is normally on a tab the user is
    already looking at — and in the live incident, saying "I've opened a sign-in
    window" about it made the (wrong) episode that tab was showing read as
    Jarvis's answer rather than as the page it had stopped on."""
    from app.agents.planner import _login_wall_question

    in_place = _login_wall_question(
        {
            "login_site": "anikoto.cz",
            "login_window_opened": True,
            "login_in_place": True,
            "wall_kind": "login",
        }
    )
    assert "already on your screen" in in_place.text
    assert "i've opened a sign-in window" not in in_place.text.lower()
    # The guest path is always offered — many sites work without an account.
    assert "Continue without signing in" in in_place.options

    # The escalated hand-off still says a window was opened, because one was.
    escalated = _login_wall_question(
        {
            "login_site": "anikoto.cz",
            "login_window_opened": True,
            "login_in_place": False,
            "wall_kind": "login",
        }
    )
    assert "i've opened a sign-in window" in escalated.text.lower()

    # A sign-up wall says sign-up, in place or not.
    signup = _login_wall_question(
        {
            "login_site": "shop.test",
            "login_window_opened": True,
            "login_in_place": False,
            "wall_kind": "signup",
        }
    )
    assert "i've opened a sign-up window" in signup.text.lower()


def test_the_in_place_flag_survives_the_handoff_payload():
    """⚠️ THE PLUMBING THAT WAS MISSING. _challenge_wall_question has read
    `challenge_in_place` since 2026-08-03 and NOTHING ever passed it, so the
    pause text claimed a window had been opened even on the in-place path — the
    exact defect that round fixed one layer down. One field on HandoffPayload now
    carries it for every kind, so neither can drift again."""
    from app.browser import state as browse_state

    login = browse_state.handoff_from_flags(
        {
            "login_required": True,
            "login_site": "anikoto.cz",
            "login_url": "https://anikoto.cz/watch/x/ep-1",
            "login_in_place": True,
            "wall_kind": "login",
        }
    )
    assert login is not None
    assert login.reason is browse_state.Handoff.LOGIN
    assert login.in_place is True

    challenge = browse_state.handoff_from_flags(
        {
            "challenge_required": True,
            "challenge_site": "www.ebay.com",
            "challenge_kind": "CAPTCHA",
            "challenge_in_place": True,
        }
    )
    assert challenge is not None and challenge.in_place is True

    # And it survives a park/restore round trip, since the ask parks the plan.
    restored = browse_state.HandoffPayload.from_dict(login.to_dict())
    assert restored is not None and restored.in_place is True

    # A payload parked BEFORE this field existed still deserializes, defaulting
    # to "a window was opened" — the behaviour that predates the flag.
    old = dict(login.to_dict())
    old.pop("in_place")
    legacy = browse_state.HandoffPayload.from_dict(old)
    assert legacy is not None and legacy.in_place is False
