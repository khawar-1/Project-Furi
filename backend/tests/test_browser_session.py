"""
Phase 14 Part 1 — the browser safety surface.

These tests pin the guarantee the whole feature rests on: in READ mode the
session cannot mutate anything. If any test here goes green while the rule it
names is broken, `browse` is no longer a READ tool and the approval gate has
been bypassed rather than satisfied — so they are written to fail loudly.
"""
import pytest

from app.core import browser_session
from app.core.browser_session import (
    BrowserBlocked,
    BrowserSession,
    InterceptStats,
    _normalize_origin,
)


# --------------------------------------------------------------- fake driver
class FakeRequest:
    def __init__(self, url, method="GET", navigation=False, frame="main"):
        self.url = url
        self.method = method
        self._navigation = navigation
        self.frame = frame

    def is_navigation_request(self):
        return self._navigation


class FakeRoute:
    """Records the verdict. Playwright demands exactly one of continue_/abort
    per request — 'neither' hangs the page's JS, which is precisely the failure
    mode that killed the original hold-the-POST design."""

    def __init__(self, request):
        self._request = request
        self.verdict = None

    @property
    def request(self):
        return self._request

    async def continue_(self):
        assert self.verdict is None, "route answered twice"
        self.verdict = "continue"

    async def abort(self):
        assert self.verdict is None, "route answered twice"
        self.verdict = "abort"


class FakePage:
    def __init__(self, url="about:blank"):
        self.url = url
        self.main_frame = "main"
        self.routes = []
        self.goto_calls = []

    async def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    async def goto(self, url, **kwargs):
        self.goto_calls.append(url)
        self.url = url

    async def wait_for_load_state(self, *a, **kw):
        pass


class FakeBrowser:
    def __init__(self, page=None):
        self.page = page or FakePage()
        self.closed = False

    async def new_page(self):
        return self.page

    async def close(self):
        self.closed = True


@pytest.fixture
def fake_browser(monkeypatch):
    browser = FakeBrowser()
    monkeypatch.setattr(browser_session, "BROWSER_FACTORY", lambda: browser)
    return browser


async def _session(allowlist={"example.com"}, browser=None):
    return await BrowserSession.open(allowlist)


async def _verdict(session, **request_kwargs):
    route = FakeRoute(FakeRequest(**request_kwargs))
    await session._intercept(route)
    return route.verdict


# ------------------------------------------------------ RULE 1: non-GET dies
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "post", "TRACE"])
async def test_every_mutating_method_is_aborted(fake_browser, method):
    """THE guarantee. If this fails, browse can submit forms."""
    session = await _session()
    assert await _verdict(session, url="https://example.com/api", method=method) == "abort"
    assert session.stats.blocked_mutations == 1


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
async def test_safe_methods_pass(fake_browser, method):
    session = await _session()
    assert await _verdict(session, url="https://example.com/x", method=method) == "continue"
    assert session.stats.blocked_mutations == 0


async def test_a_cross_origin_subresource_post_is_aborted(fake_browser):
    """The load-bearing case: an SPA submits via a background fetch, not a form
    POST. A rule that only covered navigations would leave LinkedIn's apply
    button wide open while looking correct."""
    session = await _session()
    verdict = await _verdict(
        session, url="https://api.other.com/v2/apply", method="POST", navigation=False
    )
    assert verdict == "abort"


async def test_blocked_mutations_are_reported_not_swallowed(fake_browser):
    """An aborted POST is a BREAKAGE, not a mutation — a page that half-works
    for reasons nobody surfaced is the confusing outcome."""
    session = await _session()
    await _verdict(session, url="https://example.com/log", method="POST")
    reported = session.stats.as_dict()
    assert reported["blocked_mutations"] == 1
    assert "POST https://example.com/log" in reported["mutation_urls"]


# ----------------------------------------------------------- RULE 2: SSRF
async def test_private_hosts_are_aborted(fake_browser, monkeypatch):
    monkeypatch.setattr(browser_session, "_host_is_blocked", lambda h: h == "localhost")
    browser_session.reset_host_cache()
    session = await _session(allowlist={"localhost"})
    assert await _verdict(session, url="http://localhost:8000/api/tasks") == "abort"
    assert session.stats.blocked_hosts == 1


async def test_ssrf_verdicts_are_cached_per_host(fake_browser, monkeypatch):
    """The interceptor sees every image on a page; an uncached getaddrinfo per
    request would stall the loop and re-resolve one CDN host hundreds of times."""
    calls = []

    def _count(host):
        calls.append(host)
        return False

    monkeypatch.setattr(browser_session, "_host_is_blocked", _count)
    browser_session.reset_host_cache()
    session = await _session(allowlist={"example.com"})
    for _ in range(5):
        await _verdict(session, url="https://cdn.example.com/img.png")
    assert calls == ["cdn.example.com"]


# ------------------------------------------------------- RULE 3: allowlist
async def test_navigation_off_the_allowlist_is_aborted(fake_browser):
    """The exfiltration bound: injected text saying 'go to attacker.com/?data=x'
    is a navigation, and it is refused."""
    session = await _session(allowlist={"example.com"})
    verdict = await _verdict(
        session, url="https://attacker.com/?data=secret", navigation=True
    )
    assert verdict == "abort"
    assert session.stats.blocked_navigations == 1


async def test_cross_origin_get_subresources_are_allowed(fake_browser):
    """DELIBERATE, DOCUMENTED HOLE. YouTube serves video from googlevideo.com
    and thumbnails from ytimg.com — gating subresources by origin means the page
    never renders and the feature does not exist. This test exists so that
    'loosen the allowlist' is never proposed as a fix for a broken page: it is
    already this loose, on purpose, and the mutation rule is what holds."""
    session = await _session(allowlist={"youtube.com"})
    verdict = await _verdict(
        session, url="https://rr3---sn-x.googlevideo.com/videoplayback?x=1", navigation=False
    )
    assert verdict == "continue"


async def test_subframe_navigation_is_not_origin_gated(fake_browser):
    """Consent dialogs and embeds are iframes; gating them breaks the page."""
    session = await _session(allowlist={"example.com"})
    verdict = await _verdict(
        session, url="https://consent.vendor.com/frame", navigation=True, frame="iframe-7"
    )
    assert verdict == "continue"


async def test_a_lookalike_domain_does_not_match(fake_browser):
    """'evil-youtube.com' must not satisfy an allowlist of 'youtube.com'. The
    dot is the whole rule."""
    session = await _session(allowlist={"youtube.com"})
    assert session.origin_allowed("www.youtube.com") is True
    assert session.origin_allowed("m.youtube.com") is True
    assert session.origin_allowed("youtube.com") is True
    assert session.origin_allowed("evil-youtube.com") is False
    assert session.origin_allowed("youtube.com.attacker.net") is False
    assert session.origin_allowed(None) is False


async def test_an_empty_allowlist_permits_nothing(fake_browser):
    """Fail closed: a session that was never told where it may go, may go
    nowhere."""
    session = await _session(allowlist=set())
    assert session.origin_allowed("example.com") is False


# ------------------------------------------------------------- interceptor
async def test_the_interceptor_never_leaves_a_route_unanswered(fake_browser):
    """An exception escaping a route handler hangs the page's own JavaScript.
    Fail closed — an unrendered page beats an unguarded one."""
    session = await _session()

    class BadRoute(FakeRoute):
        @property
        def request(self):
            raise RuntimeError("driver blew up")

    bad = BadRoute(FakeRequest("https://example.com"))
    await session._intercept(bad)
    assert bad.verdict == "abort"


async def test_browser_internal_schemes_pass(fake_browser):
    session = await _session()
    assert await _verdict(session, url="about:blank", navigation=True) == "continue"
    assert await _verdict(session, url="data:text/html,<p>x", navigation=True) == "continue"


# ---------------------------------------------------------------- navigation
async def test_goto_refuses_a_url_off_the_allowlist(fake_browser):
    session = await _session(allowlist={"example.com"})
    with pytest.raises(BrowserBlocked, match="not one of the sites"):
        await session.goto("https://attacker.com/steal")
    assert fake_browser.page.goto_calls == []


async def test_goto_refuses_a_private_address(fake_browser):
    session = await _session(allowlist={"localhost"})
    with pytest.raises(BrowserBlocked, match="local/private"):
        await session.goto("http://localhost:8000/api")


async def test_goto_rechecks_where_it_actually_landed(fake_browser):
    """A click is a navigation the guard never saw — the read_webpage 'redirect
    can land somewhere private' precedent, restated for the browser."""
    session = await _session(allowlist={"example.com"})

    async def _redirecting_goto(url, **kwargs):
        fake_browser.page.url = "https://attacker.com/landed"

    fake_browser.page.goto = _redirecting_goto
    with pytest.raises(BrowserBlocked, match="redirected"):
        await session.goto("https://example.com/start")


async def test_goto_returns_the_final_url_on_success(fake_browser):
    session = await _session(allowlist={"example.com"})
    final = await session.goto("https://example.com/page")
    assert final == "https://example.com/page"


async def test_a_failed_open_closes_the_browser(fake_browser, monkeypatch):
    """A leaked Chromium is a visible window the user cannot get rid of."""

    async def _boom():
        raise RuntimeError("no page for you")

    fake_browser.new_page = _boom
    with pytest.raises(RuntimeError):
        await BrowserSession.open({"example.com"})
    assert fake_browser.closed is True


# ------------------------------------------------------------------ origins
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://www.YouTube.com/results?q=x", "www.youtube.com"),
        ("YouTube.com", "youtube.com"),
        ("example.com/path", "example.com"),
        ("http://example.com.", "example.com"),
        ("", ""),
    ],
)
def test_origin_normalization(raw, expected):
    assert _normalize_origin(raw) == expected


def test_intercept_stats_clip_their_url_list():
    stats = InterceptStats()
    for i in range(30):
        stats.mutation_urls.append(f"POST https://x.com/{i}")
    assert len(stats.as_dict()["mutation_urls"]) == 10


# ----------------------------------------------------- one-time sign-in window
# The user-driven login window (Phase 14, brought forward 2026-07-17). It is a
# NORMAL browser — NO interceptor — because the user completes the sign-in POST
# by hand; the read-only guarantee is about the AGENT loop, which never touches
# this window. These pin: it launches, navigates, reports open/closed honestly,
# and never coexists with a media session (one profile, one live context).
async def test_open_login_window_launches_and_navigates(fake_browser):
    await browser_session.close_login_window()  # start clean
    assert browser_session.login_window_open() is False

    await browser_session.open_login_window("https://accounts.google.com/")
    assert browser_session.login_window_open() is True
    # No route() was installed — this window is user-driven, not intercepted.
    assert fake_browser.page.routes == []
    assert "https://accounts.google.com/" in fake_browser.page.goto_calls


async def test_close_login_window_is_idempotent(fake_browser):
    await browser_session.open_login_window()
    assert await browser_session.close_login_window() is True
    assert browser_session.login_window_open() is False
    # Closing nothing is not an error.
    assert await browser_session.close_login_window() is False


async def test_opening_login_stops_active_media(fake_browser):
    # One profile = one live persistent context: a sign-in window must close any
    # playing media session first, or the launch would deadlock on the lock.
    media_browser = FakeBrowser()
    session = BrowserSession(media_browser, media_browser.page, {"youtube.com"})
    await browser_session.register_media(session, title="song", url="https://youtube.com/watch")
    assert browser_session.active_media() is not None

    await browser_session.open_login_window()
    assert browser_session.active_media() is None      # media was stopped
    assert media_browser.closed is True                # and its window closed
    assert browser_session.login_window_open() is True
