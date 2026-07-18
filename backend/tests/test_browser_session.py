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
        self.unroute_calls = []
        self.goto_calls = []
        self.reload_calls = 0
        # ensure_playing() calls page.evaluate; queue the dicts it should return in
        # order (default: a page with no media).
        self.evaluate_results = []
        self.evaluate_calls = 0

    async def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    async def unroute(self, pattern, handler=None):
        self.unroute_calls.append((pattern, handler))

    async def goto(self, url, **kwargs):
        self.goto_calls.append(url)
        self.url = url

    async def reload(self, **kwargs):
        self.reload_calls += 1

    async def evaluate(self, expression, *args):
        self.evaluate_calls += 1
        if self.evaluate_results:
            return self.evaluate_results.pop(0)
        return {"found": 0, "playing": 0}

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


# -------------------------------------------------- handoff (playback) lifts
#                                                     interception
# enter_playback_mode() is the keep_open media handoff: once the autonomous loop
# is done and the window is the user's own to watch, the interceptor is LIFTED
# ENTIRELY (unroute) so streaming runs at native network speed — keeping the
# per-request interceptor on a continuously-streaming video taxed every segment
# (round-trip + per-host DNS) and made the net crawl (user report 2026-07-18).
# This is the open_login_window posture: user-driven, no interception, sound
# because all three rules bound the AGENT LOOP, which never touches this window
# again. _read_only=False is a best-effort FALLBACK: if unroute fails the
# interceptor stays installed but stops aborting non-GET, so the player still
# works while Rules 2 & 3 keep guarding — these pin both halves.
async def test_playback_handoff_lifts_interception(fake_browser):
    """THE fix: the handoff unroutes the interceptor so the user-driven window
    runs at native speed (no per-request round-trip / DNS tax)."""
    session = await _session()
    assert session._read_only is True
    await session.enter_playback_mode(reload=False)
    assert session._read_only is False
    # The interceptor was removed — the window is user-driven, like the sign-in one.
    assert ("**/*", session._intercept) in fake_browser.page.unroute_calls


async def test_playback_fallback_flag_allows_non_get_when_intercept_still_runs(fake_browser):
    """The FALLBACK path: if unroute failed the interceptor is still installed,
    but _read_only=False means the same POST that was aborted before now passes —
    the player API can talk, so the video plays even in the degraded mode."""
    session = await _session()
    assert await _verdict(session, url="https://example.com/api", method="POST") == "abort"
    await session.enter_playback_mode(reload=False)
    assert await _verdict(session, url="https://example.com/api", method="POST") == "continue"


async def test_playback_fallback_still_guards_ssrf_and_allowlist(fake_browser, monkeypatch):
    """If unroute fails, the degraded path is still SAFE: with the interceptor
    installed, Rules 2 (SSRF) and 3 (allowlist) keep aborting even though Rule 1
    has relaxed — a slow window is acceptable, an unguarded one is not."""

    async def _unroute_boom(pattern, handler=None):
        raise RuntimeError("unroute not supported")

    fake_browser.page.unroute = _unroute_boom
    monkeypatch.setattr(browser_session, "_host_is_blocked", lambda h: h == "localhost")
    browser_session.reset_host_cache()
    session = await _session(allowlist={"example.com"})
    await session.enter_playback_mode(reload=False)  # unroute raises, caught

    # Rule 2 — a private host is still aborted (even for a GET).
    assert await _verdict(session, url="http://localhost:8000/api") == "abort"
    # Rule 3 — an off-allowlist main-frame navigation is still aborted.
    assert (
        await _verdict(session, url="https://attacker.com/?data=secret", navigation=True)
        == "abort"
    )


async def test_enter_playback_mode_reloads_the_page(fake_browser):
    """The reload is what makes an already-stuck player retry the POST it needs.
    A GET subresource still passes after the handoff (the video stream itself)."""
    session = await _session(allowlist={"youtube.com"})
    await session.enter_playback_mode()  # reload=True default
    assert fake_browser.page.reload_calls == 1
    assert (
        await _verdict(
            session, url="https://rr3.googlevideo.com/videoplayback", navigation=False
        )
        == "continue"
    )


async def test_enter_playback_mode_survives_a_reload_failure(fake_browser):
    """Best-effort: flipping the flag must not depend on the reload, and a reload
    that raises must never break the already-open window."""

    async def _boom(**kwargs):
        raise RuntimeError("page went away mid-reload")

    fake_browser.page.reload = _boom
    session = await _session()
    await session.enter_playback_mode()  # must not raise
    assert session._read_only is False


# --------------------------------------------------------- ensure_playing
# An automation-launched window opens media PAUSED (no user gesture), so a
# "play"/"watch" goal otherwise reaches the video and it just sits there (user
# report 2026-07-17). ensure_playing() presses play via the native HTML5 media
# API — generic across sites, never a per-site button. These pin: it polls until
# a media element reports playing, gives up cleanly, and never raises.
async def test_ensure_playing_starts_a_paused_media_element(fake_browser):
    session = await _session()
    # First tick: found but still paused (play() is async). Second tick: playing.
    fake_browser.page.evaluate_results = [
        {"found": 1, "playing": 0},
        {"found": 1, "playing": 1},
    ]
    ok = await session.ensure_playing(gap_seconds=0)
    assert ok is True
    assert fake_browser.page.evaluate_calls == 2  # stopped as soon as it played


async def test_ensure_playing_gives_up_after_its_attempts(fake_browser):
    """A page that never reports playback (no <video>, or a site that plays via
    Web Audio) is not an error — bounded, returns False."""
    session = await _session()
    fake_browser.page.evaluate_results = [{"found": 0, "playing": 0}] * 10
    ok = await session.ensure_playing(attempts=3, gap_seconds=0)
    assert ok is False
    assert fake_browser.page.evaluate_calls == 3  # bounded by attempts


async def test_ensure_playing_survives_an_evaluate_failure(fake_browser):
    """Best-effort: a page whose evaluate raises must never break the handoff."""

    async def _boom(expression, *args):
        raise RuntimeError("execution context was destroyed")

    fake_browser.page.evaluate = _boom
    session = await _session()
    assert await session.ensure_playing(attempts=2, gap_seconds=0) is False  # no raise


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


# --------------------------------------------------------------- COMMIT mode
# COMMIT (14.5) is the ONE approved way past Rule 1: arm_commit permits a SINGLE
# matching non-GET (a user-approved form submit), consumed the instant it fires
# (re-lock). These pin the security-critical properties the whole feature rests
# on — if any goes green while its rule is broken, the approval gate has been
# bypassed rather than satisfied.
async def test_an_unapproved_non_get_is_still_aborted_in_commit_capable_session(fake_browser):
    """With no arm set, the default guarantee is unchanged: a POST dies."""
    session = await _session(allowlist={"example.com"})
    assert session._armed_commit is None
    assert await _verdict(session, url="https://example.com/submit", method="POST") == "abort"
    assert session.commit_fired() is False


async def test_an_armed_commit_passes_exactly_once_then_relocks(fake_browser):
    """THE commit guarantee: the one approved request passes, and the permit is
    consumed in the same breath — a double-submit finds nothing armed."""
    session = await _session(allowlist={"example.com"})
    session.arm_commit("POST", "https://example.com/submit")
    # The one approved request goes through...
    assert await _verdict(session, url="https://example.com/submit", method="POST") == "continue"
    assert session.commit_fired() is True
    assert session.stats.allowed_commits == 1
    assert session._armed_commit is None              # re-locked
    # ...and a second identical request is aborted like any mutation.
    assert await _verdict(session, url="https://example.com/submit", method="POST") == "abort"
    assert session.stats.allowed_commits == 1         # not two


async def test_an_armed_commit_matches_only_the_exact_request(fake_browser):
    """The permit is for ONE request, named by method + URL. A different path or
    method is aborted AND does not consume the permit (fail closed, no leak)."""
    session = await _session(allowlist={"example.com"})
    session.arm_commit("POST", "https://example.com/submit")

    assert await _verdict(session, url="https://example.com/other", method="POST") == "abort"
    assert session._armed_commit is not None          # untouched by a non-match
    assert await _verdict(session, url="https://example.com/submit", method="PUT") == "abort"
    assert session._armed_commit is not None
    # The exact approved request still works afterwards.
    assert await _verdict(session, url="https://example.com/submit", method="POST") == "continue"


async def test_commit_url_matching_ignores_fragment_and_trailing_slash(fake_browser):
    session = await _session(allowlist={"example.com"})
    session.arm_commit("POST", "https://example.com/submit")
    assert (
        await _verdict(session, url="https://example.com/submit/#done", method="POST")
        == "continue"
    )


async def test_an_approved_commit_still_obeys_the_ssrf_guard(fake_browser, monkeypatch):
    """Approval buys past Rule 1, never Rules 2 & 3: a matching request to a
    blocked host is STILL aborted (SSRF). Origin grounding should stop this
    upstream; this is the code-level backstop."""
    monkeypatch.setattr(browser_session, "_host_is_blocked", lambda h: h == "example.com")
    browser_session.reset_host_cache()
    session = await _session(allowlist={"example.com"})
    session.arm_commit("POST", "https://example.com/submit")
    assert await _verdict(session, url="https://example.com/submit", method="POST") == "abort"
    assert session.stats.blocked_hosts == 1


async def test_a_redirect_after_the_submit_is_re_guarded(fake_browser):
    """After the one approved POST, the session is re-locked: a response that
    redirects the top frame off-allowlist is aborted (Rule 3), and any further
    non-GET is aborted again (Rule 1) — the permit did not linger."""
    session = await _session(allowlist={"example.com"})
    session.arm_commit("POST", "https://example.com/submit")
    assert await _verdict(session, url="https://example.com/submit", method="POST") == "continue"
    # The submit's response redirects to another site — refused.
    assert (
        await _verdict(session, url="https://attacker.com/thanks", navigation=True)
        == "abort"
    )
    # And a further mutation is aborted again — re-locked, not "commit mode on".
    assert await _verdict(session, url="https://example.com/again", method="POST") == "abort"


def test_normalize_commit_url_canonicalizes_for_matching():
    from app.core.browser_session import _normalize_commit_url

    assert _normalize_commit_url("https://X.com/submit/#f") == "https://x.com/submit"
    assert _normalize_commit_url("https://x.com/a?b=1") == "https://x.com/a?b=1"
    assert _normalize_commit_url("https://x.com/a") != _normalize_commit_url("https://x.com/b")


def test_commit_fingerprint_binds_method_url_and_every_field():
    from app.core.browser_session import _commit_fingerprint

    a = {"url": "https://x.com/s", "method": "POST", "fields": [{"name": "t", "value": "hi"}]}
    # Same thing written the JS way (action/lowercase method) → identical fingerprint.
    b = {"action": "https://x.com/s", "method": "post", "fields": [{"name": "t", "value": "hi"}]}
    assert _commit_fingerprint(a) == _commit_fingerprint(b)
    # A changed field value breaks it — the approval binds to the exact values.
    c = {"url": "https://x.com/s", "method": "POST", "fields": [{"name": "t", "value": "BYE"}]}
    assert _commit_fingerprint(a) != _commit_fingerprint(c)


async def test_the_commit_registry_holds_and_hands_off_one_session(fake_browser):
    """The live filled session survives the approval pause in a one-slot registry
    (the media pattern). take_commit hands it off exactly once; a new hold closes
    the previous one; discard closes without submitting."""
    state = {"url": "https://example.com/s", "method": "POST", "fields": []}
    s1 = BrowserSession(FakeBrowser(), FakePage(), {"example.com"})
    await browser_session.hold_commit(s1, state=state)
    assert browser_session.pending_commit() == state

    taken = await browser_session.take_commit()
    assert taken is s1
    assert browser_session.pending_commit() is None      # handed off, slot empty
    assert await browser_session.take_commit() is None    # cannot be taken twice

    # A new discovery supersedes and CLOSES the previous held session.
    s2, s3 = BrowserSession(FakeBrowser(), FakePage(), set()), BrowserSession(FakeBrowser(), FakePage(), set())
    await browser_session.hold_commit(s2, state=state)
    await browser_session.hold_commit(s3, state=state)
    assert s2._browser.closed is True
    # discard closes the held one and clears the slot.
    assert await browser_session.discard_commit() is True
    assert s3._browser.closed is True
    assert await browser_session.discard_commit() is False
