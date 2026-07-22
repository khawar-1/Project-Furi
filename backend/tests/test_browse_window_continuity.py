"""
Persistent agent browser window — session continuity (2026-07-21).

The live defects these pin: every `browse` step of one plan launched its OWN
Chrome and closed it when the step ended — step 2 relaunched at start_url and
RE-DID step 1's navigation (books.toscrape: the book was opened twice, the
open/close cycle was the screen flicker), a fresh session's `back` had no
history, and the window closed the instant a task finished so the user never
saw the result (the LinkedIn compose report).

The fix: a "browse" held-session slot. A finished browse run HOLDS its live
session (success or clean loop failure); the next browse run TAKES and reuses
it — same page, real history, no relaunch — re-scoping the allowlist and only
navigating to start_url when the current page is off the new task's sites.
Walls/challenges still close the session (the profile must be freed for the
hand-off window); exceptions still close via the finally.
"""
import pytest

import app.tools  # noqa: F401 — registers the real tools
from app.agents import browser_loop
from app.agents.browser_loop import BrowseOutcome
from app.core import browser_runtime, browser_session


# --------------------------------------------------------------- fake pieces
class FakePage:
    def __init__(self, url="https://books.toscrape.com/catalogue/travel_2/"):
        self.url = url
        self.evaluate_calls = 0

    async def evaluate(self, expression, *args):
        self.evaluate_calls += 1
        return 1


class DeadPage(FakePage):
    async def evaluate(self, expression, *args):
        raise RuntimeError("Target page, context or browser has been closed")


class FakeSession:
    """The shape BrowseTool touches on a session: page, allowlist,
    origin_allowed, goto, close, per-run state."""

    def __init__(self, page=None, allowlist=None):
        self.page = page or FakePage()
        self.allowlist = set(allowlist or {"books.toscrape.com"})
        self.browse_history = ["stale entry from an earlier run"]
        self.last_redirect_offsite = {"host": "stale.example"}
        self.goto_calls = []
        self.closed = False

    def origin_allowed(self, host):
        if not host:
            return False
        host = host.strip().lower().rstrip(".")
        return any(
            host == origin or host.endswith("." + origin)
            for origin in self.allowlist
        )

    async def goto(self, url):
        self.goto_calls.append(url)
        self.page.url = url

    async def close(self):
        self.closed = True


def _success_outcome(url="https://books.toscrape.com/x", title="Books"):
    return BrowseOutcome(
        success=True,
        actions_taken=2,
        final={"url": url, "title": title, "rendered": "the page", "page_text": "£45.17"},
        done_reason="done",
    )


def _stuck_outcome():
    return BrowseOutcome(
        success=False,
        actions_taken=5,
        final={"url": "https://books.toscrape.com/y", "title": "Stuck"},
        error="the page didn't respond to that action after several tries",
    )


@pytest.fixture
def wired(monkeypatch):
    """Wire BrowseTool's seams to fakes: session opening, the loop, the runtime
    marshal, and the provider — the test_browser_login pattern."""
    from app.core.browser_session import BrowserSession

    state = {"opened": [], "outcome": _success_outcome(), "run_sessions": []}

    async def fake_session_open(allowlist):
        s = FakeSession(page=FakePage(url="about:blank"), allowlist=allowlist)
        state["opened"].append(s)
        return s

    async def fake_run_browse(session, goal, provider, **kw):
        state["run_sessions"].append(session)
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
        "goal": "click the top book and read its price",
        "start_url": "https://books.toscrape.com",
        "allowed_origins": ["books.toscrape.com"],
    }
    params.update(kwargs)
    return await BrowseTool().execute(**params)


# ------------------------------------------------------------ hold at the end
async def test_a_successful_browse_holds_the_window_open(wired):
    """Success without keep_open (no media): the session is HELD, not closed —
    the user sees the result and the next run reuses it."""
    result = await _browse()
    assert result.success is True
    assert result.output["window_open"] is True
    session = wired["opened"][0]
    assert session.closed is False
    meta = browser_session.active_browse_window()
    assert meta is not None and meta["url"] == "https://books.toscrape.com/x"


async def test_a_stuck_browse_also_holds_the_window(wired):
    """A clean loop failure (no wall) leaves the window open too — the user
    asked to SEE where it got stuck; the failure text says so."""
    wired["outcome"] = _stuck_outcome()
    result = await _browse()
    assert result.success is False
    assert "window is still open" in (result.error or "")
    assert wired["opened"][0].closed is False
    assert browser_session.active_browse_window() is not None


async def test_an_exception_still_closes_the_session(wired):
    """A crash mid-loop is a half-broken window — the finally closes it and
    nothing is held."""
    wired["outcome"] = RuntimeError("boom")
    result = await _browse()
    assert result.success is False
    assert wired["opened"][0].closed is True
    assert browser_session.active_browse_window() is None


async def test_media_keep_open_wins_over_the_browse_hold(wired, monkeypatch):
    """A play goal with keep_open goes to the MEDIA slot (playback mode), never
    the browse slot — the media lifecycle is unchanged."""
    session_holder = {}

    async def fake_open(allowlist):
        s = FakeSession(page=FakePage(url="about:blank"), allowlist=allowlist)
        s.playback = False
        s.played = False

        async def enter_playback_mode():
            s.playback = True

        async def ensure_playing():
            s.played = True

        s.enter_playback_mode = enter_playback_mode
        s.ensure_playing = ensure_playing
        session_holder["s"] = s
        return s

    from app.core.browser_session import BrowserSession

    monkeypatch.setattr(BrowserSession, "open", fake_open)
    result = await _browse(keep_open=True)
    assert result.success is True
    assert result.output["playing"] is True
    assert result.output["window_open"] is False
    assert browser_session.active_media() is not None
    assert browser_session.active_browse_window() is None
    assert session_holder["s"].closed is False


async def test_media_keep_open_hands_off_to_a_clean_window_in_production(
    wired, monkeypatch
):
    """When clean_media_enabled (production, or an injected launcher), a play goal
    CLOSES the automation session and hands the final URL to open_media_window (a
    normal ad-blocked window) — it does NOT play in place, and does NOT hold the
    browse window."""
    calls = {}

    async def fake_open_media(url, *, title=""):
        calls["url"] = url
        calls["title"] = title
        return True

    monkeypatch.setattr(browser_session, "clean_media_enabled", lambda: True)
    monkeypatch.setattr(browser_session, "open_media_window", fake_open_media)

    result = await _browse(keep_open=True)

    assert result.success is True
    assert result.output["playing"] is True
    assert result.output["handoff"] == "clean_window"
    assert calls["url"] == "https://books.toscrape.com/x"
    # The automation session was closed (its lock freed for the clean window), and
    # nothing is held in the in-place media or browse slots.
    assert wired["opened"][0].closed is True
    assert browser_session.active_media() is None
    assert browser_session.active_browse_window() is None


async def test_media_handoff_that_cannot_open_a_window_reports_not_playing(
    wired, monkeypatch
):
    """The clean window failed to open (no system browser) → playing False and
    handoff 'none', so the summary tells the user to open the link themselves."""
    monkeypatch.setattr(browser_session, "clean_media_enabled", lambda: True)

    async def fake_open_media(url, *, title=""):
        return False

    monkeypatch.setattr(browser_session, "open_media_window", fake_open_media)

    result = await _browse(keep_open=True)

    assert result.success is True
    assert result.output["playing"] is False
    assert result.output["handoff"] == "none"
    assert wired["opened"][0].closed is True


# ------------------------------------------------------------------ reuse
async def test_the_next_browse_reuses_the_held_window(wired):
    """A held live session is TAKEN and reused: no fresh launch, allowlist
    re-scoped, per-run state reset, and — already on an allowed site — NO goto,
    so the run continues exactly where the last one left off."""
    held = FakeSession(page=FakePage(url="https://books.toscrape.com/catalogue/travel_2/"))
    await browser_session.hold_browse_window(held, title="Travel", url=held.page.url)

    result = await _browse(goal="click the top book listed")
    assert result.success is True
    assert wired["opened"] == []                      # no fresh Chrome launch
    assert wired["run_sessions"] == [held]            # the loop ran on the held one
    assert held.goto_calls == []                      # continued in place
    assert held.browse_history == []                  # fresh transcript
    assert held.last_redirect_offsite is None
    assert held.allowlist == {"books.toscrape.com"}   # re-scoped, normalized
    # …and it was held AGAIN at the end for the next run / the user.
    assert browser_session.active_browse_window() is not None
    assert held.closed is False


async def test_reuse_navigates_when_the_held_page_is_off_the_new_sites(wired):
    """A held window sitting on site A, reused for a task on site B: same
    window (no relaunch — no flicker), but it navigates to the new start_url."""
    held = FakeSession(
        page=FakePage(url="https://www.linkedin.com/feed/"),
        allowlist={"linkedin.com"},
    )
    await browser_session.hold_browse_window(held, title="LinkedIn", url=held.page.url)

    result = await _browse()  # books.toscrape.com task
    assert result.success is True
    assert wired["opened"] == []
    assert held.goto_calls == ["https://books.toscrape.com"]


async def test_a_dead_held_window_falls_through_to_a_fresh_launch(wired):
    """The user closed Chrome by hand: the liveness probe fails, the corpse is
    closed, and a fresh session opens — the browse never fails over it."""
    held = FakeSession(page=DeadPage())
    await browser_session.hold_browse_window(held, title="old", url="https://x.test")

    result = await _browse()
    assert result.success is True
    assert held.closed is True
    assert len(wired["opened"]) == 1                  # fresh launch happened
    assert wired["run_sessions"] == [wired["opened"][0]]


async def test_page_excerpt_travels_in_the_output(wired):
    """The final page's prose rides the output EARLY (page_excerpt), so the
    1000-char audit-row clip keeps real facts — 'what was the price?' must be
    answerable from the record one turn later."""
    result = await _browse()
    assert result.output["page_excerpt"] == "£45.17"
    keys = list(result.output.keys())
    assert keys.index("page_excerpt") < keys.index("rendered")


# ------------------------------------------------- profile-exclusivity sweeps
async def test_opening_a_login_window_closes_the_held_browse_window(monkeypatch):
    """One profile = one live context: the sign-in hand-off must close the held
    agent window first (the opening-login-stops-media rule)."""
    fake = FakeSession()
    await browser_session.hold_browse_window(fake, title="t", url="https://x.test")

    class _LoginPage(FakePage):
        async def goto(self, url, **kwargs):
            self.url = url

    class _LoginBrowser:
        def __init__(self):
            self.page = _LoginPage(url="about:blank")
            self.closed = False

        async def new_page(self):
            return self.page

        async def close(self):
            self.closed = True

    monkeypatch.setattr(
        browser_session, "BROWSER_FACTORY", lambda: _LoginBrowser()
    )
    await browser_session.open_login_window("https://accounts.google.com/")
    assert fake.closed is True
    assert browser_session.active_browse_window() is None
    await browser_session.close_login_window()


async def test_shutdown_covers_the_browse_slot_by_construction():
    """close_all_held() iterates the registry table — the new slot cannot be
    forgotten by any aggregate teardown."""
    fake = FakeSession()
    await browser_session.hold_browse_window(fake, title="t", url="https://x.test")
    from app.browser import registry as browser_registry

    await browser_registry.close_all_held()
    assert fake.closed is True
    assert browser_session.active_browse_window() is None
