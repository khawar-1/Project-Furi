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
from app.browser import window as browser_window
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
        # Did this run OPEN the tab, or inherit one? Only the opener closes it
        # (2026-08-01). acquire_browse_tab sets it True on the reuse path; a
        # freshly opened session owns its window, so False is the default here
        # exactly as it is on the real BrowserSession.
        self.tab_reused = False
        self.playback = False

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
        # The real BrowserSession.close RELEASES its tab from the shared window
        # (2026-08-01). A fake that only flipped a flag would leave a closed
        # session in the tab registry, where the next browse would find and
        # "reuse" a dead tab.
        await browser_window.release_tab(self)

    async def release_after_run(self):
        """The real contract: a run closes only the tab it OPENED."""
        if not self.tab_reused:
            await self.close()

    async def resume_agent_control(self):
        self.playback = False
        return True


def _seed_tab(session, *, site, title="", url="", goal=""):
    """Put `session` in the shared window as an existing agent tab for `site` —
    what a previous browse run leaves behind."""
    browser_window.track_for_tests(session)
    session.tab_site = site
    browser_session.note_browse_tab(session, title=title, url=url, goal=goal)
    return session


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
        # The real BrowserSession.open registers the session as a TAB of the
        # shared window (2026-08-01), and a fake that skipped that would leave
        # the tab registry empty — making every assertion below vacuous.
        browser_window.track_for_tests(s)
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


# A goal that genuinely ASKS for playback. The media hand-off lifts Rule 1 and
# presses .play(), so it is gated on the goal saying so (loop.goal_wants_playback)
# rather than on keep_open, which only ever meant "leave the window open".
_PLAY_GOAL = "play the audiobook sample for the top book"


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
    the browse slot — the media lifecycle is unchanged.

    ⚠️ This and the two tests below used to pass the module's DEFAULT goal,
    "click the top book and read its price" — not a play goal at all, in three
    tests whose own docstrings say "a play goal". They were asserting that
    keep_open ALONE hands a window to the media path, which is the defect the
    2026-08-01 add-to-cart incident is made of: a storefront was handed over
    with Rule 1 lifted and a banner video playing, and the next task reused that
    unguarded tab. Passing a real play goal is what makes them test their own
    docstring."""
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
    result = await _browse(keep_open=True, goal=_PLAY_GOAL)
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

    result = await _browse(keep_open=True, goal=_PLAY_GOAL)

    assert result.success is True
    assert result.output["playing"] is True
    assert result.output["handoff"] == "clean_window"
    assert calls["url"] == "https://books.toscrape.com/x"
    # The automation session was closed (its lock freed for the clean window), and
    # nothing is held in the in-place media or browse slots.
    assert wired["opened"][0].closed is True
    assert browser_session.active_media() is None
    assert browser_session.active_browse_window() is None


async def test_opening_a_site_with_keep_open_never_plays_anything(wired, monkeypatch):
    """⚠️ keep_open means "leave the window open", NOT "this is a media goal"
    (2026-08-01). The planner sets it for "open youtube" too — and the in-place
    branch lifts Rule 1 and calls ensure_playing(), which presses .play() on any
    <video>. A YouTube homepage is full of preview videos, so that is a SECOND
    route to the incident's symptom, made the common path by the multi-tab round
    (a clean window would close the user's other tabs).

    A run that finished purely by ARRIVING somewhere must fall through to the
    persistent-window branch instead: window open, nothing played, Rule 1 intact.
    """
    from app.core.browser_session import BrowserSession

    holder = {}
    clean = {"opened": False}

    async def fake_open(allowlist):
        s = FakeSession(page=FakePage(url="about:blank"), allowlist=allowlist)
        browser_window.track_for_tests(s)
        s.playback = False
        s.played = False

        async def enter_playback_mode():
            s.playback = True   # would LIFT Rule 1

        async def ensure_playing():
            s.played = True     # would press .play() on any <video>

        s.enter_playback_mode = enter_playback_mode
        s.ensure_playing = ensure_playing
        holder["s"] = s
        return s

    async def fake_open_media(url, *, title=""):
        clean["opened"] = True
        return True

    monkeypatch.setattr(BrowserSession, "open", fake_open)
    monkeypatch.setattr(browser_session, "open_media_window", fake_open_media)
    # The IN-PLACE branch is the dangerous one (it is what presses play) and the
    # multi-tab round made it the common one — so pin exactly that branch.
    monkeypatch.setattr(browser_session, "clean_media_enabled", lambda: False)

    wired["outcome"] = BrowseOutcome(
        success=True,
        actions_taken=0,
        final={"url": "https://www.youtube.com/", "title": "YouTube"},
        done_reason="YouTube is open — that was the whole goal.",
        destination_only=True,
    )

    result = await _browse(goal="Open the YouTube homepage", keep_open=True)

    assert result.success is True
    session = holder["s"]
    assert session.played is False, "nothing may be played on a navigation goal"
    assert session.playback is False, "Rule 1 must stay armed"
    assert clean["opened"] is False, "no media hand-off for a navigation goal"
    # It took the persistent-window branch — which is what keep_open asked for.
    assert result.output["window_open"] is True
    assert session.closed is False
    assert browser_session.active_media() is None
    assert browser_session.active_browse_window() is not None


async def test_a_real_media_goal_still_hands_off(wired, monkeypatch):
    """Regression: destination_only defaults False, so every existing play/watch
    path is untouched."""
    calls = {}

    async def fake_open_media(url, *, title=""):
        calls["url"] = url
        return True

    monkeypatch.setattr(browser_session, "clean_media_enabled", lambda: True)
    monkeypatch.setattr(browser_session, "open_media_window", fake_open_media)

    result = await _browse(goal="play jane by the long faces", keep_open=True)

    assert result.output["handoff"] == "clean_window"
    assert calls["url"] == "https://books.toscrape.com/x"


async def test_media_handoff_that_cannot_open_a_window_reports_not_playing(
    wired, monkeypatch
):
    """The clean window failed to open (no system browser) → playing False and
    handoff 'none', so the summary tells the user to open the link themselves."""
    monkeypatch.setattr(browser_session, "clean_media_enabled", lambda: True)

    async def fake_open_media(url, *, title=""):
        return False

    monkeypatch.setattr(browser_session, "open_media_window", fake_open_media)

    result = await _browse(keep_open=True, goal=_PLAY_GOAL)

    assert result.success is True
    assert result.output["playing"] is False
    assert result.output["handoff"] == "none"
    assert wired["opened"][0].closed is True


# ------------------------------------------------------------------ reuse
async def test_the_next_browse_reuses_the_held_window(wired):
    """The tab for THIS SITE is reused: no fresh launch, allowlist re-scoped,
    per-run state reset, and — already on an allowed site — NO goto, so the run
    continues exactly where the last one left off."""
    held = FakeSession(page=FakePage(url="https://books.toscrape.com/catalogue/travel_2/"))
    _seed_tab(held, site="toscrape.com", title="Travel", url=held.page.url)

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


async def test_a_tab_on_another_site_is_left_alone_and_a_new_one_opens(wired):
    """⚠️ THE BEHAVIOUR THE WHOLE CHANGE IS FOR. A LinkedIn tab is open and a
    books.toscrape task arrives. Pre-change the LinkedIn window was CLOSED (the
    whole context with it) and a new one launched — the flicker, and why only one
    browser task could be open. Now the LinkedIn tab stays and the new task gets
    a tab of its own."""
    other = FakeSession(
        page=FakePage(url="https://www.linkedin.com/feed/"),
        allowlist={"linkedin.com"},
    )
    _seed_tab(other, site="linkedin.com", title="LinkedIn", url=other.page.url)

    result = await _browse()  # books.toscrape.com task
    assert result.success is True
    assert other.closed is False, "the other site's tab must stay open"
    assert other.goto_calls == [], "and must not be navigated away"
    assert len(wired["opened"]) == 1, "the new site gets its own tab"
    assert wired["run_sessions"] == [wired["opened"][0]]
    assert browser_window.tab_count() == 2


async def test_a_dead_tab_falls_through_to_a_fresh_launch(wired):
    """The user closed the tab by hand: the liveness probe fails, the corpse is
    closed, and a fresh one opens — the browse never fails over it."""
    held = FakeSession(page=DeadPage())
    _seed_tab(held, site="toscrape.com", title="old", url="https://x.test")

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
async def test_opening_a_login_window_closes_every_tab(monkeypatch):
    """THE ONE CONSTRAINT MULTI-TAB DOES NOT REMOVE. A sign-in window is a
    separate Chrome process on the same user-data-dir, so it cannot coexist with
    the shared context however many tabs are in it — signing in closes them."""
    fake = FakeSession()
    other = FakeSession(page=FakePage(url="https://www.linkedin.com/feed/"))
    _seed_tab(fake, site="x.test", title="t", url="https://x.test")
    _seed_tab(other, site="linkedin.com", title="LinkedIn", url=other.page.url)

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
    assert other.closed is True, "EVERY tab goes, not just the most recent"
    assert browser_session.active_browse_window() is None
    assert browser_window.tab_count() == 0
    await browser_session.close_login_window()


async def test_shutdown_covers_every_tab_by_construction():
    """A clean shutdown must leave NO Chromium holding the ~/.jarvis/browser
    profile lock — the orphan that hangs the next run's launch.

    Closing every HELD session is no longer proof of that (2026-08-01): the
    shared context outlives individual sessions, so a tab that was never held in
    a registry slot — an ordinary finished browse — would keep the context, and
    the lock, alive past shutdown. window.close_all() covers every tab by
    construction, which is what makes the guarantee true again."""
    held = FakeSession()
    plain = FakeSession(page=FakePage(url="https://books.toscrape.com/"))
    _seed_tab(plain, site="toscrape.com", title="Books", url=plain.page.url)
    _seed_tab(held, site="x.test", title="t", url="https://x.test")
    await browser_session.register_media(held, title="t", url="https://x.test")

    await browser_session.shutdown_browser_windows()

    assert held.closed is True, "a held session still goes"
    assert plain.closed is True, "and so does a plain tab nobody held"
    assert browser_window.tab_count() == 0
    assert browser_session.active_browse_window() is None


async def test_the_junaidjamshed_goal_plays_nothing_even_though_it_reduces_to_nothing(
    wired, monkeypatch
):
    """⚠️ THE LIVE FAILURE OF THE PREVIOUS FIX, frozen (2026-08-01, 23:00:56).

    The destination_only gate shipped that morning and did not survive the
    evening. The planner wrote "Open the junaidjamshed.com homepage so it is
    visible in the browser." — the trailing clause defeats _extract_search_term,
    which returns None, so destination_only is FALSE and the negative gate opened.
    A storefront was handed to the user with the interceptor lifted and a banner
    video playing, and the NEXT task reused that unguarded tab to run an
    approved add-to-cart it could then not observe.

    The lesson is the gate's DIRECTION, not its wording: a negative test over an
    LLM-authored goal string fails OPEN, and lifting a safety guard has to
    require a reason. Note destination_only=False below — this test would not
    detect anything if it were True."""
    from app.core.browser_session import BrowserSession

    holder = {}

    async def fake_open(allowlist):
        s = FakeSession(page=FakePage(url="about:blank"), allowlist=allowlist)
        browser_window.track_for_tests(s)
        s.playback = False
        s.played = False

        async def enter_playback_mode():
            s.playback = True   # would LIFT Rule 1

        async def ensure_playing():
            s.played = True     # would press .play() on a banner video

        s.enter_playback_mode = enter_playback_mode
        s.ensure_playing = ensure_playing
        holder["s"] = s
        return s

    monkeypatch.setattr(BrowserSession, "open", fake_open)
    monkeypatch.setattr(browser_session, "clean_media_enabled", lambda: False)

    wired["outcome"] = BrowseOutcome(
        success=True,
        actions_taken=0,
        final={
            "url": "https://www.junaidjamshed.com/",
            "title": "J. Junaid Jamshed Official Website",
        },
        done_reason="The junaidjamshed.com homepage is already visible.",
        destination_only=False,   # exactly as the live run reported it
    )

    result = await _browse(
        goal="Open the junaidjamshed.com homepage so it is visible in the browser.",
        keep_open=True,
    )

    assert result.success is True
    session = holder["s"]
    assert session.played is False, "a storefront is not a media goal"
    assert session.playback is False, "Rule 1 must stay armed on this tab"
    assert browser_session.active_media() is None
    # keep_open still means what it says: the window stays open.
    assert result.output["window_open"] is True
    assert session.closed is False


# ---------------------------------------------- closing is the user's call
async def test_a_run_does_not_close_a_tab_it_inherited(wired, monkeypatch):
    """User report 2026-08-01: "it closed the tab, which it shouldn't have —
    that power should be to me." Even a run that BLOWS UP hands a borrowed
    window back; a broken-looking page the user can see beats one that
    vanished."""
    from app.core.browser_session import BrowserSession

    holder = {}

    async def fake_open(allowlist):
        s = FakeSession(page=FakePage(url="about:blank"), allowlist=allowlist)
        s.tab_reused = True          # acquire_browse_tab inherited it
        holder["s"] = s
        return s

    monkeypatch.setattr(BrowserSession, "open", fake_open)
    wired["outcome"] = RuntimeError("boom")

    result = await _browse()

    assert result.success is False
    assert holder["s"].closed is False, "an inherited tab is not ours to close"


# ------------------------------------------- a pause keeps the page it asks about
# THE INCIDENT (2026-08-02). The loop stopped before a world-acting gesture and
# the planner asked "I'm about to send … on www.junaidjamshed.com — say yes".
# One second BEFORE that question reached the user, this branch closed the tab:
# it called release_after_run(), which closes any tab THIS run opened, and a run
# that opened its own tab always has tab_reused False.
#
# The 2026-08-01 reasoning for keeping a BORROWED tab — "the user is about to be
# asked a question about the page they are looking at" — never depended on who
# opened it. It reached one pause branch of four by accident of where that round
# was working.
def _approval_outcome(url="https://www.junaidjamshed.com/", title="J. Junaid Jamshed"):
    return BrowseOutcome(
        success=False,
        actions_taken=0,
        final={"url": url, "title": title, "rendered": "the page", "page_text": "…"},
        action_approval_required=True,
        action_description='send "the Junaid Jamshed website"',
        action_site="www.junaidjamshed.com",
        action_fingerprint="d54ea0846b99486b",
    )


def _origin_outcome():
    return BrowseOutcome(
        success=False,
        actions_taken=1,
        final={"url": "https://books.toscrape.com/x", "title": "Books"},
        origin_approval_required=True,
        origin_candidate="ats.example.com",
        origin_url="https://ats.example.com/apply",
    )


async def test_the_incident_an_approval_pause_leaves_the_tab_open(wired):
    """THE INCIDENT, FROZEN. The page the user is being asked about must still
    be on screen when the question arrives."""
    wired["outcome"] = _approval_outcome()
    result = await _browse(goal="Open the Junaid Jamshed website", keep_open=True)

    assert result.output["action_approval_required"] is True
    session = wired["opened"][0]
    assert session.closed is False
    assert result.output["window_open"] is True
    # …and the window registry knows what it is showing, so the resumed run can
    # find it by site rather than navigating again.
    meta = browser_session.active_browse_window()
    assert meta is not None and meta["url"] == "https://www.junaidjamshed.com/"


async def test_an_origin_approval_pause_also_leaves_the_tab_open(wired):
    """Same question, same reason: the user is deciding about a page, and a page
    they cannot see is a worse decision."""
    wired["outcome"] = _origin_outcome()
    result = await _browse()

    assert result.output["origin_approval_required"] is True
    assert wired["opened"][0].closed is False
    assert result.output["window_open"] is True


async def test_an_approval_pause_keeps_a_borrowed_tab_too(wired):
    """The 2026-08-01 rule, unchanged: a tab we merely borrowed was never ours
    to close."""
    existing = _seed_tab(
        FakeSession(page=FakePage(url="https://www.junaidjamshed.com/")),
        site="junaidjamshed.com",
    )
    wired["outcome"] = _approval_outcome()
    await _browse(
        goal="Open the Junaid Jamshed website",
        start_url="https://www.junaidjamshed.com",
        allowed_origins=["junaidjamshed.com"],
    )

    assert existing.closed is False
    assert wired["opened"] == []  # it reused the tab rather than opening one


async def test_a_login_wall_still_closes_the_tab(wired, monkeypatch):
    """NOT changed, and deliberately: a sign-in window is a separate Chrome
    process on the same single profile, so the shared context genuinely has to
    go. Signing in closes the tabs."""
    from app.core import browser_session as bs

    async def fake_open_login(url):
        return True

    monkeypatch.setattr(bs, "open_login_window", fake_open_login)
    wired["outcome"] = BrowseOutcome(
        success=False,
        actions_taken=1,
        final={"url": "https://x.example/login", "title": "Sign in"},
        login_required=True,
        login_site="x.example",
        login_url="https://x.example/login",
    )
    result = await _browse()

    assert result.output["login_required"] is True
    assert wired["opened"][0].closed is True
