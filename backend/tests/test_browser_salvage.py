"""
Partial-result salvage on a failed browse (2026-07-26).

THE LIVE DEFECT. Four browser tasks were given to Furi on 2026-07-26 and all
four failed. In one of them the loop reached eBay's real results page, ran
`extract`, and hit the action cap — so the listings the user asked it to compare
had actually been gathered. The user was told "it failed", full stop, because
every failure path funnelled through `_fail`, whose `output` is None.

`browser_agent_tools`' own comment on that path said "Report what it saw (the
final page) so the summary has something real, not silence" — and the code one
line below it threw the page away. The data was never missing; it was discarded
at the boundary.

What these tests pin:
  - a loop that fails after seeing a page returns that page (rendered,
    page_excerpt, extracted, url, title) with success=False;
  - a failure BEFORE any page was read still returns the uniform shape, so no
    consumer has to test for None first;
  - a Playwright navigation timeout is caught as a timeout and names the URL
    (its TimeoutError is not a builtin subclass, so `except TimeoutError`
    never saw it — the daraz.pk / ebay.com opaque error, live 2026-07-26);
  - success is never claimed by any of this. Salvage carries evidence; it does
    not soften the verdict.
"""
import asyncio

import pytest

import app.tools  # noqa: F401 — registers the real tools
from app.browser import loop as browser_loop
from app.browser import runtime as browser_runtime
from app.browser.loop import BrowseOutcome
from app.browser.session import BrowserSession


class FakePage:
    def __init__(self, url="https://www.ebay.com/sch/i.html?_nkw=racket"):
        self.url = url

    async def evaluate(self, expression, *args):
        return 1


class FakeSession:
    def __init__(self, allowlist=None):
        self.page = FakePage()
        self.allowlist = set(allowlist or {"ebay.com"})
        self.browse_history = []
        self.last_redirect_offsite = None
        self.closed = False
        # Only the run that OPENED a tab may close it (2026-08-01). A freshly
        # opened session owns its window, so False is the real default.
        self.tab_reused = False

    def origin_allowed(self, host):
        if not host:
            return False
        host = host.strip().lower().rstrip(".")
        return any(h == origin or h.endswith("." + origin)
                   for origin in self.allowlist for h in (host,))

    async def goto(self, url):
        self.page.url = url

    async def close(self):
        self.closed = True

    async def release_after_run(self):
        """The real contract: a run closes only the tab it OPENED."""
        if not self.tab_reused:
            await self.close()

    async def resume_agent_control(self):
        return True


# The eBay run, frozen: real listings extracted, then the action cap.
_EXTRACTED = [
    {"name": "Yonex Astrox 88D Pro", "price": "$94.00", "shipping": "free"},
    {"name": "Yonex Astrox 77 Pro", "price": "$88.50", "shipping": "$6.20"},
]


def _capped_outcome():
    return BrowseOutcome(
        success=False,
        actions_taken=25,
        final={
            "url": "https://www.ebay.com/sch/i.html?_nkw=racket&_udhi=100",
            "title": "Yonex Astrox Badminton Rackets for sale",
            "rendered": "ELEMENTS\n[1] link 'Yonex Astrox 88D Pro'",
            "page_text": "Yonex Astrox 88D Pro $94.00 Free shipping",
        },
        error="reached the 25-action limit without finishing",
        extracted=list(_EXTRACTED),
    )


@pytest.fixture
def wired(monkeypatch):
    state = {"outcome": _capped_outcome()}

    async def fake_session_open(allowlist):
        return FakeSession(allowlist)

    async def fake_run_browse(session, goal, provider, **kw):
        out = state["outcome"]
        if isinstance(out, Exception):
            raise out
        return out

    async def fake_run_browser(coro, *, timeout=None):
        return await coro

    class FakeProv:
        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(BrowserSession, "open", fake_session_open)
    monkeypatch.setattr(browser_loop, "run_browse", fake_run_browse)
    monkeypatch.setattr(browser_runtime, "run_browser", fake_run_browser)
    monkeypatch.setattr("app.providers.factory.build_provider", lambda: FakeProv())
    return state


async def _browse(**kwargs):
    from app.tools.browser_agent_tools import BrowseTool

    params = {
        "goal": "extract the top 4 listings and tell me which is cheapest",
        "start_url": "https://www.ebay.com",
        "allowed_origins": ["ebay.com"],
    }
    params.update(kwargs)
    return await BrowseTool().execute(**params)


# ------------------------------------------------- the incident, frozen
async def test_a_capped_run_still_returns_what_it_extracted(wired):
    """THE 2026-07-26 eBay run. The loop gathered the listings and hit the
    action cap; the user was told only "it failed". The rows must survive."""
    result = await _browse()

    assert result.success is False, "the goal was not reached — do not soften it"
    assert result.output is not None, "the evidence was discarded (the live bug)"
    assert result.output["extracted"] == _EXTRACTED
    assert "ebay.com" in result.output["url"]
    assert result.output["title"].startswith("Yonex Astrox")
    assert "Astrox 88D" in result.output["rendered"]
    assert "$94.00" in result.output["page_excerpt"]
    assert "25-action limit" in result.error


async def test_the_error_prose_still_names_the_last_page(wired):
    """Salvage ADDS the output dict; it does not change the message the user
    reads. The prose contract from before this change is unchanged."""
    result = await _browse()
    assert "reached the 25-action limit" in result.error
    assert "Last page: https://www.ebay.com" in result.error


# ------------------------------------------- failures before any page was read
async def test_a_launch_failure_returns_the_uniform_shape(wired):
    """No page was ever read, so there is nothing to salvage — but the SHAPE is
    still uniform, so no consumer has to test for None before reading
    `extracted`. An empty list is a fact; None was a question."""
    wired["outcome"] = RuntimeError("chrome exploded")
    result = await _browse()

    assert result.success is False
    assert result.output is not None
    assert result.output["extracted"] == []
    assert result.output["rendered"] == ""
    assert result.output["goal_reached"] is False
    assert result.output["url"] == "https://www.ebay.com"
    assert "chrome exploded" in result.output["error"]


async def test_the_outer_timeout_returns_the_uniform_shape(wired):
    """The browser_runtime belt (asyncio.TimeoutError), distinct from a
    Playwright navigation timeout."""
    wired["outcome"] = asyncio.TimeoutError()
    result = await _browse()

    assert result.success is False
    assert result.output is not None
    assert result.output["extracted"] == []
    assert "timed out" in result.error


async def test_a_playwright_nav_timeout_is_caught_as_a_timeout(wired):
    """Playwright's TimeoutError derives from playwright.Error, NOT the builtin,
    so `except (TimeoutError, asyncio.TimeoutError)` never caught it and a nav
    timeout surfaced as an opaque generic failure with no URL in it. That is
    exactly what daraz.pk and ebay.com produced live on 2026-07-26."""
    from playwright.async_api import TimeoutError as PWTimeout

    assert not issubclass(PWTimeout, TimeoutError), (
        "if Playwright ever makes this a builtin subclass, the dedicated arm "
        "becomes redundant — but the ordering must still be checked"
    )

    wired["outcome"] = PWTimeout("Page.goto: Timeout 20000ms exceeded")
    result = await _browse()

    assert result.success is False
    assert "could not finish loading" in result.error
    assert "https://www.ebay.com" in result.error, "the failing URL must be named"
    assert result.output is not None
    assert "navigation timed out" in result.output["error"]


# ------------------------------------------------------- no false success
async def test_salvage_never_flips_the_verdict(wired):
    """The whole point is a HONEST partial: real evidence under a false
    success flag. A caller keying on `success` must be unaffected."""
    for outcome in (_capped_outcome(), RuntimeError("boom"), asyncio.TimeoutError()):
        wired["outcome"] = outcome
        result = await _browse()
        assert result.success is False
        assert result.output.get("goal_reached") is False


# -------------------------------- the opening navigation is a hand-off point
#
# THE LIVE DEFECT (2026-07-26): `_act` swallows BrowserBlocked mid-loop and turns
# a site-initiated redirect into an origin-approval PAUSE — but the OPENING goto
# sat outside the loop, so the same redirect just raised and killed the task.
# hangers.com.pk redirects to its host www.webx.pk; the run died on step one and
# the user was never asked a question they'd have answered in one word.
class _RedirectingSession(FakeSession):
    def __init__(self, exc, redirect=None):
        super().__init__({"hangers.com.pk"})
        self._exc = exc
        self.last_redirect_offsite = redirect

    async def goto(self, url):
        raise self._exc


@pytest.fixture
def redirecting(monkeypatch):
    """Wire BrowseTool so the opening navigation raises what a live redirect or
    an unreachable site raises."""
    state = {"session": None}

    async def fake_session_open(allowlist):
        return state["session"]

    async def fake_run_browse(session, goal, provider, **kw):
        raise AssertionError("the loop must never run — navigation failed first")

    async def fake_run_browser(coro, *, timeout=None):
        return await coro

    class FakeProv:
        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(BrowserSession, "open", fake_session_open)
    monkeypatch.setattr(browser_loop, "run_browse", fake_run_browse)
    monkeypatch.setattr(browser_runtime, "run_browser", fake_run_browser)
    monkeypatch.setattr("app.providers.factory.build_provider", lambda: FakeProv())
    return state


async def test_a_start_url_redirect_asks_instead_of_dying(redirecting):
    """THE hangers.com.pk RUN. The site redirected itself to www.webx.pk and the
    task failed outright. It must become the same origin-approval question the
    mid-loop path has always asked."""
    from app.browser.session import BrowserBlocked

    redirecting["session"] = _RedirectingSession(
        BrowserBlocked("The page redirected to 'www.webx.pk', which this task is "
                       "not allowed to visit."),
        redirect={"host": "www.webx.pk", "url": "https://www.webx.pk/"},
    )

    from app.tools.browser_agent_tools import BrowseTool

    result = await BrowseTool().execute(
        goal="open the mens trousers category and add a black trouser to cart",
        start_url="https://hangers.com.pk",
        allowed_origins=["hangers.com.pk"],
    )

    assert result.success is False
    assert result.output["origin_approval_required"] is True
    assert result.output["origin_candidate"] == "www.webx.pk"
    assert "approval" in result.error.lower()
    assert "webx.pk" in result.error


async def test_a_blocked_navigation_with_no_redirect_still_fails(redirecting):
    """The narrow gate: only a REDIRECT becomes a question. A refusal with no
    recorded redirect is a genuine policy block (the model aimed somewhere it
    was never allowed) and must not be laundered into an approval prompt."""
    from app.browser.session import BrowserBlocked

    redirecting["session"] = _RedirectingSession(
        BrowserBlocked("Refusing to open 'elsewhere.test'."), redirect=None
    )

    from app.tools.browser_agent_tools import BrowseTool

    result = await BrowseTool().execute(
        goal="do the thing",
        start_url="https://hangers.com.pk",
        allowed_origins=["hangers.com.pk"],
    )
    assert result.success is False
    assert (result.output or {}).get("origin_approval_required") is not True


async def test_an_unreachable_start_url_is_reported_honestly(redirecting):
    """THE outfitters.com RUN — a parked domain whose certificate fails. Say
    which site and why; never guess a neighbouring address, because an origin
    the user never named is outside the grounding corpus by construction."""
    from app.browser.session import BrowserUnreachable

    redirecting["session"] = _RedirectingSession(
        BrowserUnreachable("Couldn't load www.outfitters.com: its HTTPS "
                           "certificate isn't valid.")
    )

    from app.tools.browser_agent_tools import BrowseTool

    result = await BrowseTool().execute(
        goal="open mens trousers and add a black one to cart",
        start_url="https://www.outfitters.com",
        allowed_origins=["outfitters.com"],
    )

    assert result.success is False
    assert result.output["site_unreachable"] is True
    assert "certificate" in result.error
    assert "won't guess a different address" in result.error


# ------------------------------------------- the last mile: it reaches the words
#
# Salvaging into step.result is only half the job. `_render_step` dropped every
# non-COMPLETED step, so the rescued evidence would have died one layer above
# where it was rescued — invisible to both the summary LLM and the deterministic
# completion text, i.e. invisible to the user. These pin the whole path.
def _failed_browse_step():
    from app.agents.schemas import PlanStep, StepStatus
    from app.core.base_tool import PermissionLevel, ToolResult

    return PlanStep(
        description="Extract the top 4 listings and say which is cheapest",
        tool="browse",
        parameters={},
        permission_level=PermissionLevel.READ,
        requires_approval=False,
        status=StepStatus.FAILED,
        result=ToolResult(
            success=False,
            output={
                "url": "https://www.ebay.com/sch/i.html?_nkw=racket",
                "title": "Yonex Astrox Badminton Rackets for sale",
                "rendered": "ELEMENTS\n[1] link 'Yonex Astrox 88D Pro'",
                "page_excerpt": "Yonex Astrox 88D Pro $94.00",
                "extracted": list(_EXTRACTED),
                "goal_reached": False,
            },
            error="reached the 25-action limit without finishing",
            permission_level=PermissionLevel.READ,
        ),
    )


def test_a_failed_browse_step_renders_its_salvaged_evidence():
    from app.agents.rendering import _render_step

    rendered = _render_step(_failed_browse_step())

    assert rendered is not None, "the rescued evidence died at the rendering layer"
    assert "ebay.com" in rendered


def test_the_partial_rendering_says_it_did_not_finish():
    """Honesty is the point. Evidence from a failed step must never read as a
    completed result — the summary LLM is looking at this text."""
    from app.agents.rendering import _render_step

    rendered = _render_step(_failed_browse_step())

    assert "PARTIAL" in rendered
    assert "did NOT finish" in rendered
    assert "25-action limit" in rendered, "the reason it stopped belongs in the report"


def test_a_failed_step_with_no_output_still_renders_nothing():
    """The narrow gate: only a failed step carrying structured output a code
    formatter can read gets rendered. Everything else is unchanged — the error
    prose already covers it, and inventing a block for it would be noise."""
    from app.agents.rendering import _render_step
    from app.agents.schemas import PlanStep, StepStatus
    from app.core.base_tool import PermissionLevel, ToolResult

    step = PlanStep(
        description="read a file",
        tool="read_file",
        parameters={},
        permission_level=PermissionLevel.READ,
        requires_approval=False,
        status=StepStatus.FAILED,
        result=ToolResult(
            success=False, output=None, error="no such file",
            permission_level=PermissionLevel.READ,
        ),
    )
    assert _render_step(step) is None
