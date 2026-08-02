"""
Phase 14 Part 1 — the browser safety surface.

These tests pin the guarantee the whole feature rests on: in READ mode the
session cannot mutate anything. If any test here goes green while the rule it
names is broken, `browse` is no longer a READ tool and the approval gate has
been bypassed rather than satisfied — so they are written to fail loudly.
"""
import asyncio
import json
import time

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

    async def go_back(self, **kwargs):
        self.went_back = getattr(self, "went_back", 0) + 1


class FakeBrowser:
    """A persistent context. MINTS A DISTINCT PAGE PER new_page(), like the real
    one — a fake that handed out one shared page made every multi-tab assertion
    vacuous (two "tabs" were one object), which is exactly the test-shape
    blindness that has let real defects through this suite before.

    The FIRST new_page() still returns the page the test supplied, so every
    single-session test that reaches for `fake_browser.page` is unaffected."""

    def __init__(self, page=None):
        self.page = page or FakePage()
        self.pages = [self.page]
        self.closed = False
        self._minted = 0

    async def new_page(self):
        self._minted += 1
        if self._minted == 1:
            return self.page
        page = FakePage()
        self.pages.append(page)
        return page

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


# ------------------------- RULE 1: unapproved form NAVIGATION dies
# (Action-level policy, 2026-07-21: the page's own XHR/fetch traffic flows —
# blanket non-GET aborting broke every SPA. What Rule 1 refuses is the classic
# form-POST NAVIGATION without an armed commit permit; agent submits are gated
# by the gesture gate + the one-shot permit.)
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "post", "TRACE"])
async def test_every_unapproved_mutating_navigation_is_aborted(fake_browser, method):
    """The network backstop: a classic form-submit navigation with no armed
    commit permit dies. If this fails, an unapproved form POST can sail out."""
    session = await _session()
    assert (
        await _verdict(
            session, url="https://example.com/api", method=method, navigation=True
        )
        == "abort"
    )
    assert session.stats.blocked_mutations == 1


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
async def test_safe_methods_pass(fake_browser, method):
    session = await _session()
    assert await _verdict(session, url="https://example.com/x", method=method) == "continue"
    assert session.stats.blocked_mutations == 0


async def test_the_pages_own_xhr_posts_flow(fake_browser, monkeypatch):
    """THE capability half of the action-level trade: an SPA's background fetch
    (search, filters, lazy content, widget verification) is the page being a
    page — it flows, same-origin and cross-origin alike, still under the SSRF
    guard. Agent SUBMITS are gated by the gesture gate + commit permit, not by
    strangling the network."""
    monkeypatch.setattr(browser_session, "_host_is_blocked", lambda h: False)
    browser_session.reset_host_cache()
    session = await _session()
    assert (
        await _verdict(
            session, url="https://example.com/api/search", method="POST",
            navigation=False,
        )
        == "continue"
    )
    assert (
        await _verdict(
            session, url="https://api.other.com/v2/telemetry", method="POST",
            navigation=False,
        )
        == "continue"
    )
    assert session.stats.blocked_mutations == 0


async def test_a_flowing_xhr_post_is_still_ssrf_guarded(fake_browser, monkeypatch):
    """Opening the network to page traffic never opened it to the private
    ranges: a POST to a blocked host is aborted by Rule 2 exactly as a GET is."""
    monkeypatch.setattr(
        browser_session, "_host_is_blocked", lambda h: h == "internal.local"
    )
    browser_session.reset_host_cache()
    session = await _session()
    assert (
        await _verdict(
            session, url="http://internal.local/admin", method="POST",
            navigation=False,
        )
        == "abort"
    )
    assert session.stats.blocked_hosts == 1


async def test_blocked_mutations_are_reported_not_swallowed(fake_browser):
    """An aborted form navigation is a BREAKAGE, not a mutation — a page that
    half-works for reasons nobody surfaced is the confusing outcome."""
    session = await _session()
    await _verdict(
        session, url="https://example.com/log", method="POST", navigation=True
    )
    reported = session.stats.as_dict()
    assert reported["blocked_mutations"] == 1
    assert "POST https://example.com/log" in reported["mutation_urls"]


# ----------------------------------------------------- RULE 0: ad / tracker block
# The agent window cannot run uBlock (Chrome refuses --load-extension under CDP),
# so ad/tracker network blocking lives in the interceptor. It must ABORT known ad
# hosts (so a fake-play ad iframe never loads and can't be misclicked) and must
# NEVER touch content hosts.
@pytest.mark.parametrize(
    "host",
    [
        "googletagservices.com",
        "www.googletagservices.com",
        "pagead2.googlesyndication.com",
        "doubleclick.net",
        "exoclick.com",
        "cdn.exoclick.com",
        "popads.net",
        "taboola.com",
    ],
)
def test_is_ad_host_matches_ad_and_tracker_domains(host):
    assert browser_session._is_ad_host(host) is True


@pytest.mark.parametrize(
    "host",
    ["youtube.com", "example.com", "anilist.co", "notexoclick.com", "", None],
)
def test_is_ad_host_never_flags_content_hosts(host):
    assert browser_session._is_ad_host(host) is False


async def test_ad_requests_are_aborted_by_the_interceptor(fake_browser):
    """A request to a known ad host dies at Rule 0 — regardless of method — so
    the ad iframe/script never loads. This is what stops the loop misclicking a
    fake 'Play' button on an ad-heavy streaming site."""
    session = await _session()
    assert (
        await _verdict(session, url="https://cdn.exoclick.com/ad.js", method="GET")
        == "abort"
    )
    assert session.stats.blocked_ads == 1
    # counted separately from a real navigation/host block
    assert session.stats.blocked_navigations == 0
    assert session.stats.blocked_hosts == 0


async def test_ad_block_does_not_touch_content_requests(fake_browser, monkeypatch):
    """A first-party content GET on the allowlist still passes — the ad list must
    never starve a legitimate page."""
    monkeypatch.setattr(browser_session, "_host_is_blocked", lambda h: False)
    browser_session.reset_host_cache()
    session = await _session(allowlist={"example.com"})
    assert (
        await _verdict(session, url="https://example.com/player.js", method="GET")
        == "continue"
    )
    assert session.stats.blocked_ads == 0


async def test_blocked_ads_are_surfaced_in_stats(fake_browser):
    session = await _session()
    await _verdict(session, url="https://taboola.com/widget.js")
    assert session.stats.as_dict()["blocked_ads"] == 1


# ----------------------------------------------------------- RULE 2: SSRF
async def test_private_hosts_are_aborted(fake_browser, monkeypatch):
    # A private host that is NOT on the allowlist (the SSRF threat: a subresource
    # / redirect steering somewhere internal) is still fully checked and aborted.
    monkeypatch.setattr(browser_session, "_host_is_blocked", lambda h: h == "localhost")
    browser_session.reset_host_cache()
    session = await _session(allowlist={"example.com"})
    assert await _verdict(session, url="http://localhost:8000/api/tasks") == "abort"
    assert session.stats.blocked_hosts == 1


async def test_ssrf_verdicts_are_cached_per_host(fake_browser, monkeypatch):
    """The interceptor sees every image on a page; an uncached getaddrinfo per
    request would stall the loop and re-resolve one CDN host hundreds of times.
    Uses a THIRD-PARTY host — an allowlisted host now skips the check entirely
    (see test_ssrf_is_skipped_for_a_get_to_an_allowlisted_host)."""
    calls = []

    def _count(host):
        calls.append(host)
        return False

    monkeypatch.setattr(browser_session, "_host_is_blocked", _count)
    browser_session.reset_host_cache()
    session = await _session(allowlist={"example.com"})
    for _ in range(5):
        await _verdict(session, url="https://cdn.thirdparty.com/img.png")
    assert calls == ["cdn.thirdparty.com"]


# The 2026-07-19 speed round: a READ (GET/HEAD/OPTIONS) to an ALLOWLISTED host
# skips the per-request DNS SSRF lookup — first-party read subresources were the
# dominant per-request tax, goto()/_verify_landing already SSRF-check navigation
# there, and the third-party + commit paths keep the full check (below).
async def test_ssrf_is_skipped_for_a_get_to_an_allowlisted_host(fake_browser, monkeypatch):
    calls = []
    monkeypatch.setattr(
        browser_session, "_host_is_blocked", lambda h: calls.append(h) or False
    )
    browser_session.reset_host_cache()
    session = await _session(allowlist={"example.com"})
    # A subdomain of the allowlisted origin — first-party — is trusted, no lookup.
    assert await _verdict(session, url="https://cdn.example.com/img.png") == "continue"
    assert calls == []                       # DNS was never consulted
    assert session.stats.ssrf_checks == 0


async def test_ssrf_still_guards_a_non_get_even_to_an_allowlisted_host(fake_browser, monkeypatch):
    """The skip is READ-only: a NON-GET (here the one approved commit) to an
    allowlisted host is STILL SSRF-checked — approval never buys past Rule 2, and
    the perf skip never touches the request that leaves the machine."""
    monkeypatch.setattr(browser_session, "_host_is_blocked", lambda h: h == "example.com")
    browser_session.reset_host_cache()
    session = await _session(allowlist={"example.com"})
    session.arm_commit("POST", "https://example.com/submit")
    assert await _verdict(session, url="https://example.com/submit", method="POST") == "abort"
    assert session.stats.blocked_hosts == 1


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


# ------------------------------------ benign route races (2026-07-19 "offline")
# The false-'no internet': the interceptor's fail-closed `except` used to call
# route.abort() for ANY exception — including a BENIGN continue_() race on the
# MAIN document (Playwright's "Route is already handled" / "Target closed"),
# turning a good page load into a connection error. A benign race must be
# SWALLOWED (the request already resolved); only a GENUINE error fails closed.
class _BenignContinueRoute(FakeRoute):
    async def continue_(self):
        raise RuntimeError("Route is already handled!")


class _HardFailRoute(FakeRoute):
    async def continue_(self):
        raise RuntimeError("something genuinely broke")


async def test_a_benign_route_race_does_not_re_abort_a_good_request(fake_browser):
    session = await _session(allowlist={"example.com"})
    route = _BenignContinueRoute(FakeRequest("https://example.com/page", navigation=True))
    await session._intercept(route)
    assert route.verdict is None       # neither re-continued nor aborted


async def test_a_genuine_route_error_still_fails_closed(fake_browser):
    """A NON-benign error is still fail-closed-aborted — an unrendered page beats
    an unguarded one (the original guarantee, unchanged)."""
    session = await _session(allowlist={"example.com"})
    route = _HardFailRoute(FakeRequest("https://example.com/page", navigation=True))
    await session._intercept(route)
    assert route.verdict == "abort"


# ---------------------------------------------- measurement + adaptive settle
async def test_interceptor_counts_requests_and_ssrf_checks(fake_browser, monkeypatch):
    """Layer D: the per-session summary rests on these counters — total requests
    fielded, and how many actually cost a host lookup (allowlisted reads do not)."""
    monkeypatch.setattr(browser_session, "_host_is_blocked", lambda h: False)
    browser_session.reset_host_cache()
    session = await _session(allowlist={"example.com"})
    await _verdict(session, url="https://cdn.example.com/a.png")      # allowlisted GET → no lookup
    await _verdict(session, url="https://cdn.thirdparty.com/b.png")   # third-party GET → lookup
    d = session.stats.as_dict()
    assert d["total_requests"] == 2
    assert d["ssrf_checks"] == 1


async def test_settle_stops_early_on_a_stable_page(fake_browser):
    """Event-driven settle: the MutationObserver quiet-window (_QUIET_JS) is a
    SINGLE page.evaluate raced against networkidle, not the old polling loop —
    a stable page returns promptly on the first signal, no repeated sampling."""
    session = await _session()
    page = fake_browser.page
    await session.settle()
    assert page.evaluate_calls == 1     # one _QUIET_JS evaluate, never a poll loop
    assert session.stats.settle_seconds >= 0.0


async def test_settle_survives_an_evaluate_failure(fake_browser):
    """A page that navigated / closed mid-settle (the quiet evaluate raises) is
    not an error — networkidle wins the race and the quiet task's exception is
    drained, so settle returns cleanly and never raises."""

    async def _boom(expression, *args):
        raise RuntimeError("execution context was destroyed")

    session = await _session()
    fake_browser.page.evaluate = _boom
    await session.settle()  # must not raise


async def test_settle_is_bounded_when_the_page_never_quiets(fake_browser, monkeypatch):
    """A page whose quiet-window never fires AND never goes network-idle is bounded
    by the outer race deadline (SETTLE_HARD_CAP_SECONDS), not left hanging."""
    monkeypatch.setattr(browser_session, "SETTLE_HARD_CAP_SECONDS", 0.05)
    session = await _session()
    never = asyncio.Event()   # never set — both signals hang

    async def _hang(*a, **kw):
        await never.wait()

    fake_browser.page.evaluate = _hang
    fake_browser.page.wait_for_load_state = _hang
    started = time.monotonic()
    await session.settle()    # returns via the deadline, never hangs
    assert time.monotonic() - started < 1.0
    assert session.stats.settle_seconds >= 0.0


async def test_close_logs_a_summary_without_raising(fake_browser):
    """Layer D best-effort: the close summary must never get in the way of the
    teardown, and the browser is still closed."""
    session = await _session()
    await _verdict(session, url="https://example.com/x")
    await session.close()
    assert fake_browser.closed is True


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
    assert session.last_redirect_offsite is None


# ------------------------------------------- redirect landings (2026-07-19)
# The WWR-ad incident: an in-allowlist click-tracker URL 302'd to an origin the
# task may not visit. Playwright route handlers never re-fire on redirect hops,
# so _verify_landing is the ONLY judge — and a refused landing must be backed
# out (or the loop observes and acts on the off-limits page) and recorded (so
# the loop can offer the user the same origin-approval pause an off-site link
# gets — a job application's ATS is approvable, an ad is deniable).
async def test_a_refused_redirect_landing_is_backed_out_and_recorded(fake_browser):
    session = await _session(allowlist={"example.com"})

    async def _redirecting_goto(url, **kwargs):
        fake_browser.page.url = "https://ads.metana.io/opportunities?utm=x"

    fake_browser.page.goto = _redirecting_goto
    with pytest.raises(BrowserBlocked, match="redirected"):
        await session.goto("https://example.com/listing_ads/13/click")

    assert fake_browser.page.went_back == 1
    assert session.last_redirect_offsite == {
        "host": "ads.metana.io",
        "url": "https://ads.metana.io/opportunities?utm=x",
    }


async def test_a_redirect_to_a_blocked_host_backs_out_but_offers_no_approval(
    fake_browser, monkeypatch
):
    """An SSRF-blocked landing is never a candidate for user approval — the
    marker stays empty so the loop cannot offer to allowlist an internal host."""
    monkeypatch.setattr(browser_session, "_host_is_blocked", lambda h: h == "evil.internal")
    browser_session.reset_host_cache()
    session = await _session(allowlist={"example.com"})

    async def _redirecting_goto(url, **kwargs):
        fake_browser.page.url = "https://evil.internal/admin"

    fake_browser.page.goto = _redirecting_goto
    with pytest.raises(BrowserBlocked, match="blocked address"):
        await session.goto("https://example.com/x")

    assert fake_browser.page.went_back == 1
    assert session.last_redirect_offsite is None


async def test_a_failed_back_out_still_raises(fake_browser):
    """The retreat is best-effort; the refusal is not."""
    session = await _session(allowlist={"example.com"})

    async def _redirecting_goto(url, **kwargs):
        fake_browser.page.url = "https://elsewhere.com/x"

    async def _broken_back(**kwargs):
        raise RuntimeError("history is empty")

    fake_browser.page.goto = _redirecting_goto
    fake_browser.page.go_back = _broken_back
    with pytest.raises(BrowserBlocked, match="redirected"):
        await session.goto("https://example.com/x")


# --------------------------------------------- goto timeout retry (2026-07-19)
async def test_goto_retries_a_navigation_timeout_once(fake_browser):
    """Three live WWR runs each burned a whole browse (and a replan) on a
    transient first-load timeout that succeeded on the next attempt."""
    session = await _session(allowlist={"example.com"})
    calls = []

    async def _flaky_goto(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            raise TimeoutError("Page.goto: Timeout 20000ms exceeded.")
        fake_browser.page.url = url

    fake_browser.page.goto = _flaky_goto
    final = await session.goto("https://example.com/slow")
    assert final == "https://example.com/slow"
    assert len(calls) == 2


async def test_a_double_timeout_still_fails(fake_browser):
    """Bounded to exactly one retry — a dead site fails in two attempts.

    The FAILURE TYPE changed on 2026-07-26: a site that will not respond at all
    is now BrowserUnreachable, not a bare TimeoutError. The distinction is the
    point of the two-phase navigation — a SLOW page is no longer a failure of
    any kind (it commits, misses readiness, and gets observed anyway), so the
    only thing left that can raise here is a site that never answered."""
    from app.browser.session import BrowserUnreachable

    session = await _session(allowlist={"example.com"})
    calls = []

    async def _dead_goto(url, **kwargs):
        calls.append(url)
        raise TimeoutError("Page.goto: Timeout 20000ms exceeded.")

    fake_browser.page.goto = _dead_goto
    with pytest.raises(BrowserUnreachable) as excinfo:
        await session.goto("https://example.com/dead")
    assert len(calls) == 2
    assert "example.com" in str(excinfo.value), "name the site that failed"


# ------------------------------------------- two-phase navigation (2026-07-26)
#
# THE LIVE DEFECT: goto() waited for "domcontentloaded" and treated a timeout as
# fatal. daraz.pk and ebay.com each timed out twice at 20s and killed the whole
# browse — while the session stats showed 95 and 349 requests, i.e. the pages
# were loading fine and simply never fired DCL inside the budget. A slow page
# must not be a failure; the loop should look at what IS there.
async def test_a_page_that_never_reaches_readiness_is_still_navigated(fake_browser):
    """The daraz.pk case. The document commits and then keeps building forever.
    Old behaviour: raise, discard the page, fail the task. New: hand it to the
    loop anyway and count it."""
    session = await _session(allowlist={"example.com"})
    polls = {"n": 0}

    async def _never_ready(expression, *args):
        polls["n"] += 1
        # A growing page: node count keeps changing, so the stall exit never
        # fires either. The budget is the only way out.
        return {"ready": False, "acts": 1, "text": 10, "nodes": 100 + polls["n"]}

    async def _committed_goto(url, **kwargs):
        fake_browser.page.url = url

    fake_browser.page.goto = _committed_goto
    fake_browser.page.evaluate = _never_ready

    from app.browser import session as sess_mod

    monkey_budget = 0.6  # keep the test fast; the real budget is 15s
    original = sess_mod.READY_POLL_MS
    sess_mod.READY_POLL_MS = int(monkey_budget * 1000)
    try:
        final = await session.goto("https://example.com/heavy")
    finally:
        sess_mod.READY_POLL_MS = original

    assert final == "https://example.com/heavy", "the page must still be usable"
    assert session.stats.slow_navigations == 1, "and honestly marked as slow"
    assert polls["n"] > 1, "it really did poll"


async def test_a_finished_page_takes_the_readyState_fast_path(fake_browser):
    """readyState 'complete' is the platform's definitive "this page is done", so
    a page reporting it needs only to be holding still — it must not be made to
    prove a full second of stillness it demonstrated on the first poll.

    MEASURED (2026-07-26): example.com reports 'complete' at 0.66s while
    daraz.pk's results page stays 'interactive' until 8.8s, so the flag really
    does separate a trivial page from one still assembling itself."""
    session = await _session(allowlist={"example.com"})
    polls = {"n": 0}

    async def _complete(expression, *args):
        polls["n"] += 1
        return {"ready": True, "complete": True, "acts": 40, "text": 3000, "nodes": 900}

    async def _committed_goto(url, **kwargs):
        fake_browser.page.url = url

    fake_browser.page.goto = _committed_goto
    fake_browser.page.evaluate = _complete

    await session.goto("https://example.com/fast")
    assert polls["n"] <= 3, f"the fast path cost {polls['n']} polls"
    assert session.stats.slow_navigations == 0


async def test_a_substantive_page_must_also_hold_still(fake_browser):
    """THE daraz.pk MEASUREMENT, pinned. Substance alone fired at 2.31s when the
    page had 54 controls of nav chrome; the products arrived at 5.79s and the DOM
    went still at 6.36s. A page that is substantive but NOT 'complete' has to
    stop growing before it is observed — otherwise the loop is handed a header
    and asked to compare products that are not there yet."""
    session = await _session(allowlist={"example.com"})
    polls = {"n": 0}

    async def _growing_then_still(expression, *args):
        polls["n"] += 1
        # Grows for the first few polls, then holds.
        nodes = 300 * polls["n"] if polls["n"] <= 3 else 1000
        return {"ready": True, "complete": False, "acts": 20, "text": 900, "nodes": nodes}

    async def _committed_goto(url, **kwargs):
        fake_browser.page.url = url

    fake_browser.page.goto = _committed_goto
    fake_browser.page.evaluate = _growing_then_still

    await session.goto("https://example.com/spa")

    from app.browser import session as sess_mod

    assert polls["n"] >= 3 + sess_mod.READY_STABLE_POLLS, (
        f"returned after {polls['n']} polls — it did not wait for the DOM to settle"
    )
    assert session.stats.slow_navigations == 0


async def test_a_painted_but_quiet_spa_exits_on_the_growth_stall(fake_browser):
    """An SPA that painted and will never fire another event: `ready` stays
    false (thin prose) but the node count stops moving. Without this exit the
    poll would spend its whole budget on a page that is already finished."""
    session = await _session(allowlist={"example.com"})
    polls = {"n": 0}

    async def _stalled(expression, *args):
        polls["n"] += 1
        return {"ready": False, "acts": 2, "text": 12, "nodes": 500}  # never changes

    async def _committed_goto(url, **kwargs):
        fake_browser.page.url = url

    fake_browser.page.goto = _committed_goto
    fake_browser.page.evaluate = _stalled

    await session.goto("https://example.com/spa")
    assert session.stats.slow_navigations == 0, "a stalled page is DONE, not slow"
    assert polls["n"] <= 6, f"should exit on the stall, polled {polls['n']} times"


async def test_an_unrecognised_evaluate_result_is_treated_as_ready(fake_browser):
    """THE TEST-SHAPE RULE, and it is production-correct too. The suite's fake
    pages return canned dicts from evaluate(); without this every navigating
    test would block for the full poll budget. And in production, a result we
    cannot interpret means proceed and let the observation be judged — not burn
    the budget on a question nothing can answer."""
    session = await _session(allowlist={"example.com"})

    async def _canned(expression, *args):
        return {"found": 0, "playing": 0}  # the real fixture's shape

    async def _committed_goto(url, **kwargs):
        fake_browser.page.url = url

    fake_browser.page.goto = _committed_goto
    fake_browser.page.evaluate = _canned

    final = await session.goto("https://example.com/x")
    assert final == "https://example.com/x"
    assert session.stats.slow_navigations == 0


@pytest.mark.parametrize(
    "message, expected",
    [
        ("net::ERR_CERT_AUTHORITY_INVALID at https://www.outfitters.com/", "certificate"),
        ("net::ERR_NAME_NOT_RESOLVED", "resolve"),
        ("net::ERR_CONNECTION_REFUSED", "refused"),
        ("net::ERR_INTERNET_DISCONNECTED", "no internet"),
    ],
)
async def test_a_network_error_is_unreachable_and_never_retried(
    fake_browser, message, expected
):
    """The outfitters.com case (a parked domain with a bad certificate). These
    are NOT timeouts — retrying spends the budget twice for the same answer —
    and they are NOT a licence to guess a different domain."""
    from app.browser.session import BrowserUnreachable

    session = await _session(allowlist={"example.com"})
    calls = []

    async def _net_error_goto(url, **kwargs):
        calls.append(url)
        raise RuntimeError(message)

    fake_browser.page.goto = _net_error_goto
    with pytest.raises(BrowserUnreachable) as excinfo:
        await session.goto("https://example.com/dead")

    assert len(calls) == 1, "a network error is never retried"
    assert expected in str(excinfo.value).lower()
    assert "example.com" in str(excinfo.value)


async def test_the_landing_check_runs_after_the_readiness_poll(fake_browser):
    """PHASE C ordering. A client-side redirect (meta-refresh, location.replace)
    happens DURING the poll, so the landing check has to run after it — with the
    old domcontentloaded wait such a redirect got only a sliver of time. Here
    the page moves off-allowlist mid-poll and must still be caught."""
    from app.browser.session import BrowserBlocked

    session = await _session(allowlist={"example.com"})
    polls = {"n": 0}

    async def _redirecting(expression, *args):
        polls["n"] += 1
        if polls["n"] >= 2:
            fake_browser.page.url = "https://elsewhere.test/landed"
            return {"ready": True, "acts": 5, "text": 900, "nodes": 400}
        return {"ready": False, "acts": 0, "text": 0, "nodes": 10}

    async def _committed_goto(url, **kwargs):
        fake_browser.page.url = url

    fake_browser.page.goto = _committed_goto
    fake_browser.page.evaluate = _redirecting

    with pytest.raises(BrowserBlocked):
        await session.goto("https://example.com/redirects")
    assert session.last_redirect_offsite["host"] == "elsewhere.test"


async def test_a_non_timeout_navigation_error_is_never_retried(fake_browser):
    session = await _session(allowlist={"example.com"})
    calls = []

    async def _dns_dead_goto(url, **kwargs):
        calls.append(url)
        raise RuntimeError("net::ERR_NAME_NOT_RESOLVED")

    fake_browser.page.goto = _dns_dead_goto
    with pytest.raises(RuntimeError):
        await session.goto("https://example.com/x")
    assert len(calls) == 1


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
    but _read_only=False means the same form navigation that was aborted before
    now passes — the player works even in the degraded mode."""
    session = await _session()
    assert (
        await _verdict(
            session, url="https://example.com/api", method="POST", navigation=True
        )
        == "abort"
    )
    await session.enter_playback_mode(reload=False)
    assert (
        await _verdict(
            session, url="https://example.com/api", method="POST", navigation=True
        )
        == "continue"
    )


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


# ----------------------------------------------- clean (non-automation) hand-off
# 2026-07-19: the sign-in / CAPTCHA window is launched, in production, as a PLAIN
# Chrome SUBPROCESS — no CDP, no --enable-automation — so Cloudflare Turnstile /
# Google do not fingerprint it as a bot and the user's manual solve sticks (the
# clearance cookie lands in the profile the agent re-attaches to). The launcher
# is an injectable seam (CLEAN_BROWSER_LAUNCHER) so the suite never spawns a real
# process. These pin: it is PREFERRED over the automation window, is closable,
# and falls back to Playwright when no system browser is found.
class _FakeCleanProc:
    """A stand-in for the subprocess.Popen the real launcher returns. pid=None so
    _terminate_clean_proc takes the terminate() path (never runs taskkill)."""

    def __init__(self, url):
        self.url = url
        self.pid = None
        self.terminated = False

    def terminate(self):
        self.terminated = True


async def test_open_login_window_prefers_the_clean_subprocess(monkeypatch):
    """With a clean launcher available the hand-off uses it and NEVER touches the
    Playwright factory (the hermetic BROWSER_FACTORY refuser would raise if it
    did) — a genuinely un-automated window is what defeats the Turnstile loop."""
    await browser_session.close_login_window()
    launched: list[str] = []
    monkeypatch.setattr(
        browser_session, "CLEAN_BROWSER_LAUNCHER",
        lambda url: launched.append(url) or _FakeCleanProc(url),
    )

    await browser_session.open_login_window("https://shop.test/")

    assert launched == ["https://shop.test/"]
    assert browser_session.login_window_open() is True
    await browser_session.close_login_window()


async def test_clean_launcher_none_falls_back_to_playwright(fake_browser, monkeypatch):
    """No system browser (the launcher returns None) → the Playwright window still
    opens, so the hand-off is never lost on a box without Chrome/Edge."""
    await browser_session.close_login_window()
    monkeypatch.setattr(browser_session, "CLEAN_BROWSER_LAUNCHER", lambda url: None)

    await browser_session.open_login_window("https://accounts.google.com/")

    assert browser_session.login_window_open() is True
    # The fallback ran: the fake Playwright page navigated to the URL.
    assert "https://accounts.google.com/" in fake_browser.page.goto_calls


async def test_close_login_window_terminates_the_clean_proc(monkeypatch):
    """close_login_window closes the clean subprocess window (best-effort
    terminate), reports it, and is idempotent."""
    await browser_session.close_login_window()
    proc = _FakeCleanProc("x")
    monkeypatch.setattr(browser_session, "CLEAN_BROWSER_LAUNCHER", lambda url: proc)

    await browser_session.open_login_window("https://shop.test/")
    assert browser_session.login_window_open() is True

    assert await browser_session.close_login_window() is True
    assert proc.terminated is True
    assert browser_session.login_window_open() is False
    assert await browser_session.close_login_window() is False  # closing nothing


def test_clean_login_enabled_only_in_production():
    """The clean subprocess is used only when no BROWSER_FACTORY is injected
    (production) OR a launcher is explicitly injected. Under the hermetic suite
    BROWSER_FACTORY is a refuser and no launcher is set, so the clean path stays
    OFF and the suite never spawns a real Chrome."""
    assert browser_session.CLEAN_BROWSER_LAUNCHER is None
    assert browser_session.BROWSER_FACTORY is not None      # the hermetic refuser
    assert browser_session._clean_login_enabled() is False


# ------------------------------------ profile single-instance lock (2026-07-19)
# One profile, one live Chromium: a hand-off window launched right after another
# session closed can HAND OFF to the still-dying instance and exit with no visible
# window ("it said it opened a sign-in window but it didn't"). These pin the two
# guards: the clean subprocess is VERIFIED to stay alive (a fast exit → fall back
# to the window we control), and _settle_profile waits out the lock after a recent
# close (and pays nothing otherwise).
class _ExitedCleanProc:
    """A clean-window subprocess that handed off and exited immediately — poll()
    reports a return code straight away (the profile-lock race)."""

    def __init__(self, url):
        self.url = url
        self.pid = None

    def poll(self):
        return 0        # already gone

    def terminate(self):
        pass


class _LiveCleanProc(_FakeCleanProc):
    """A clean window that stayed up: poll() is None while the browser runs."""

    def poll(self):
        return None


async def test_a_handoff_exit_clean_window_falls_back_to_playwright(
    fake_browser, monkeypatch
):
    """The clean subprocess launched but exited at once (handed the URL to a
    Chromium already on the profile) — verification catches it and the hand-off
    falls back to the Playwright window we launch and control, so a real window
    always appears."""
    await browser_session.close_login_window()
    monkeypatch.setattr(browser_session, "_CLEAN_LOGIN_VERIFY_SECONDS", 0.4)
    monkeypatch.setattr(browser_session, "_PROFILE_SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(
        browser_session, "CLEAN_BROWSER_LAUNCHER", lambda url: _ExitedCleanProc(url)
    )

    await browser_session.open_login_window("https://accounts.google.com/")

    assert browser_session.login_window_open() is True
    # The Playwright fallback actually ran (the fake page navigated).
    assert "https://accounts.google.com/" in fake_browser.page.goto_calls
    await browser_session.close_login_window()


async def test_a_live_clean_window_is_kept(monkeypatch):
    """A clean subprocess that stays alive (poll() None) is used as-is — no
    fallback, the anti-fingerprint window is preserved."""
    await browser_session.close_login_window()
    monkeypatch.setattr(browser_session, "_CLEAN_LOGIN_VERIFY_SECONDS", 0.4)
    proc = _LiveCleanProc("x")
    monkeypatch.setattr(browser_session, "CLEAN_BROWSER_LAUNCHER", lambda url: proc)

    await browser_session.open_login_window("https://shop.test/")

    assert browser_session.login_window_open() is True
    await browser_session.close_login_window()


async def test_settle_profile_waits_after_a_recent_close(monkeypatch):
    import time as _time

    monkeypatch.setattr(browser_session, "_PROFILE_SETTLE_SECONDS", 0.3)
    browser_session._mark_profile_released()
    t0 = _time.monotonic()
    await browser_session._settle_profile()
    assert _time.monotonic() - t0 >= 0.25


async def test_settle_profile_is_a_noop_when_nothing_closed_recently(monkeypatch):
    import time as _time

    monkeypatch.setattr(browser_session, "_PROFILE_SETTLE_SECONDS", 0.3)
    monkeypatch.setattr(
        browser_session, "_profile_released_monotonic", _time.monotonic() - 10
    )
    t0 = _time.monotonic()
    await browser_session._settle_profile()
    assert _time.monotonic() - t0 < 0.1


# --------------------------------- clean-window media hand-off (2026-07-22)
# A watch/play goal hands the found video off to a NORMAL, user-driven window on
# the same profile (uBlock loaded, autoplay on) instead of playing in the
# automation window — so streaming/piracy-site ads are blocked by uBlock, which
# the code-side Rule 0 (off during playback) never could. The launcher is the
# injectable CLEAN_MEDIA_LAUNCHER seam so the suite never spawns a real Chrome, and
# under the hermetic fixture the whole hand-off is OFF (clean_media_enabled False)
# so the in-place playback path is what tests exercise unless one opts in.
async def test_clean_media_enabled_only_in_production():
    """Off under the hermetic suite (BROWSER_FACTORY is the refuser, no launcher
    injected) — so the media hand-off never spawns a real Chrome; ON when a
    launcher is injected or in production."""
    assert browser_session.CLEAN_MEDIA_LAUNCHER is None
    assert browser_session.BROWSER_FACTORY is not None
    assert browser_session.clean_media_enabled() is False


async def test_open_media_window_launches_clean_window(monkeypatch):
    """A live clean subprocess is adopted as THE media window: open returns True,
    and active_media()/active_media_window() report it (so the StatusBar lights up
    and stop_media covers it)."""
    await browser_session.stop_media_window()
    monkeypatch.setattr(browser_session, "_PROFILE_SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(browser_session, "_CLEAN_LOGIN_VERIFY_SECONDS", 0.3)
    launched: list[str] = []
    monkeypatch.setattr(
        browser_session, "CLEAN_MEDIA_LAUNCHER",
        lambda url: launched.append(url) or _LiveCleanProc(url),
    )

    opened = await browser_session.open_media_window(
        "https://anikoto.cz/watch/123", title="Ep 12"
    )

    assert opened is True
    assert launched == ["https://anikoto.cz/watch/123"]
    assert browser_session.active_media_window() == {
        "title": "Ep 12",
        "url": "https://anikoto.cz/watch/123",
    }
    # active_media() unifies both surfaces — the StatusBar/API see the clean window.
    assert browser_session.active_media() == {
        "title": "Ep 12",
        "url": "https://anikoto.cz/watch/123",
    }
    await browser_session.stop_media_window()


async def test_stop_media_closes_the_clean_media_window(monkeypatch):
    """stop_media() (the ONE stop entry) terminates the clean media window and is
    idempotent — every 'free the profile lock' site relies on this."""
    await browser_session.stop_media_window()
    monkeypatch.setattr(browser_session, "_PROFILE_SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(browser_session, "_CLEAN_LOGIN_VERIFY_SECONDS", 0.3)
    proc = _LiveCleanProc("x")
    monkeypatch.setattr(browser_session, "CLEAN_MEDIA_LAUNCHER", lambda url: proc)

    assert await browser_session.open_media_window("https://youtube.com/watch") is True
    assert await browser_session.stop_media() is True
    assert proc.terminated is True
    assert browser_session.active_media() is None
    assert await browser_session.stop_media() is False  # stopping nothing


async def test_open_media_window_reports_false_when_no_browser(monkeypatch):
    """No system browser (the launcher returns None) → open returns False and
    nothing is registered, so the caller reports 'couldn't open a window' honestly
    rather than a phantom playing state."""
    await browser_session.stop_media_window()
    monkeypatch.setattr(browser_session, "_PROFILE_SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(browser_session, "CLEAN_MEDIA_LAUNCHER", lambda url: None)

    assert await browser_session.open_media_window("https://x.test/") is False
    assert browser_session.active_media() is None


async def test_a_handoff_exit_media_window_reports_not_playing(monkeypatch):
    """The clean media subprocess exited at once (handed the URL to a Chromium
    already on the profile) — verification catches it and open returns False, so we
    never claim it is playing when no window came up."""
    await browser_session.stop_media_window()
    monkeypatch.setattr(browser_session, "_PROFILE_SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(browser_session, "_CLEAN_LOGIN_VERIFY_SECONDS", 0.4)
    monkeypatch.setattr(
        browser_session, "CLEAN_MEDIA_LAUNCHER", lambda url: _ExitedCleanProc(url)
    )

    assert await browser_session.open_media_window("https://x.test/") is False
    assert browser_session.active_media() is None


async def test_active_media_window_none_after_user_closes(monkeypatch):
    """If the user closes the clean window themselves, poll() reports it exited and
    active_media_window() returns None — the StatusBar drops the indicator without a
    stop call."""
    await browser_session.stop_media_window()
    monkeypatch.setattr(browser_session, "_PROFILE_SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(browser_session, "_CLEAN_LOGIN_VERIFY_SECONDS", 0.3)
    proc = _LiveCleanProc("x")
    monkeypatch.setattr(browser_session, "CLEAN_MEDIA_LAUNCHER", lambda url: proc)
    assert await browser_session.open_media_window("https://youtube.com/watch") is True
    assert browser_session.active_media_window() is not None

    proc.poll = lambda: 0  # the user closed the window
    assert browser_session.active_media_window() is None
    await browser_session.stop_media_window()


async def test_open_media_window_closes_a_prior_in_place_media_session(monkeypatch):
    """One profile = one live context: handing off to a clean window first closes a
    prior in-place BrowserSession media session (else two Chromiums fight the
    single-instance lock)."""
    await browser_session.stop_media_window()
    monkeypatch.setattr(browser_session, "_PROFILE_SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(browser_session, "_CLEAN_LOGIN_VERIFY_SECONDS", 0.3)
    media_browser = FakeBrowser()
    session = BrowserSession(media_browser, media_browser.page, {"youtube.com"})
    await browser_session.register_media(session, title="song", url="https://youtube.com/watch")
    monkeypatch.setattr(browser_session, "CLEAN_MEDIA_LAUNCHER", lambda url: _LiveCleanProc(url))

    assert await browser_session.open_media_window("https://anikoto.cz/watch") is True
    assert media_browser.closed is True  # the in-place session was closed
    await browser_session.stop_media_window()


def test_default_media_launcher_adds_the_autoplay_flag(monkeypatch):
    """The media launcher passes --autoplay-policy=no-user-gesture-required so a
    standard player starts on its own in the non-CDP window (which cannot be told to
    press play). The sign-in launcher does NOT get that flag."""
    import subprocess as _sp

    captured: dict[str, list] = {}

    class _FakePopen:
        def __init__(self, args, **kw):
            captured["args"] = args

    monkeypatch.setattr(browser_session, "_find_system_browser", lambda: "chrome.exe")
    monkeypatch.setattr(browser_session, "_harden_profile", lambda p: None)
    monkeypatch.setattr(_sp, "Popen", _FakePopen)

    browser_session._default_clean_media_launcher("https://youtube.com/watch")
    assert "--autoplay-policy=no-user-gesture-required" in captured["args"]
    assert captured["args"][-1] == "https://youtube.com/watch"

    browser_session._default_clean_launcher("https://accounts.google.com/")
    assert "--autoplay-policy=no-user-gesture-required" not in captured["args"]


# -------------------------------- orphaned-profile reclaim + launch (2026-07-20)
# A Chromium left holding ~/.jarvis/browser by a PRIOR backend (a sign-in window
# not closed on shutdown, a context leaked by a crash) hangs the next launch on
# the single-instance lock — the "kept on processing" incident. reclaim_orphaned_
# profile kills that orphan, and ONLY processes whose command line names that
# exact profile; the launch is bounded so a locked profile becomes a clean failure
# that a reclaim-and-retry self-heals, never an endless spinner.
class _FakeContext:
    def set_default_navigation_timeout(self, ms):
        self.nav = ms

    def on(self, event, cb):
        pass

    async def new_page(self):
        return FakePage()

    async def close(self):
        pass


class _FakeChromium:
    """launch_persistent_context replays a scripted outcome per call: 'hang' (never
    returns → the launch timeout fires), 'fail' (raises → dead channel), or 'ok'
    (a context)."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def launch_persistent_context(self, **kw):
        action = self.script[self.calls]
        self.calls += 1
        if action == "hang":
            await asyncio.sleep(30)     # the wait_for cap will time this out
        if action == "fail":
            raise RuntimeError("no browser on this channel")
        return _FakeContext()


class _FakePlaywright:
    def __init__(self, chromium):
        self.chromium = chromium
        self.stopped = False

    async def stop(self):
        self.stopped = True


def test_reaper_output_matches_only_the_jarvis_profile_command_line():
    """THE safety property: only a browser on the ~/.jarvis/browser profile is
    ever returned — the user's everyday Chrome (a different --user-data-dir) never
    is, no matter how many chrome.exe are running."""
    marker = f"--user-data-dir={browser_session.BROWSER_PROFILE_DIR}"
    everyday = "--user-data-dir=C:\\Users\\DELL\\AppData\\Local\\Google\\Chrome\\User Data"
    stdout = "\n".join([
        f"1111\tchrome.exe {marker} --new-window https://youtube.com",
        f"2222\tchrome.exe {everyday} --restore-last-session",  # the user's Chrome
        f"3333\tmsedge.exe {marker} --no-first-run",            # a Jarvis-profile Edge
        f"badpid\tchrome.exe {marker}",                         # malformed pid → skipped
    ])
    assert browser_session._parse_reaper_output(stdout, marker) == [1111, 3333]


def test_reaper_blank_marker_matches_nothing():
    marker = f"--user-data-dir={browser_session.BROWSER_PROFILE_DIR}"
    assert browser_session._parse_reaper_output(f"1\tchrome.exe {marker}", "") == []


def test_reclaim_kills_the_reaped_pids_and_marks_the_profile_released(monkeypatch):
    killed = []
    monkeypatch.setattr(browser_session, "_PROFILE_REAPER", lambda m: [7, 8])
    monkeypatch.setattr(
        browser_session, "_kill_pid_tree", lambda pid: (killed.append(pid), True)[1]
    )
    monkeypatch.setattr(browser_session, "_profile_released_monotonic", 0.0)

    assert browser_session.reclaim_orphaned_profile() == 2
    assert killed == [7, 8]
    # a kill frees the lock like a close() — the next launch must settle
    assert browser_session._profile_released_monotonic > 0.0


def test_reclaim_is_a_noop_when_there_is_no_orphan(monkeypatch):
    def _must_not_kill(pid):
        raise AssertionError("no orphan → nothing may be killed")

    monkeypatch.setattr(browser_session, "_PROFILE_REAPER", lambda m: [])
    monkeypatch.setattr(browser_session, "_kill_pid_tree", _must_not_kill)
    monkeypatch.setattr(browser_session, "_profile_released_monotonic", 0.0)

    assert browser_session.reclaim_orphaned_profile() == 0
    assert browser_session._profile_released_monotonic == 0.0   # nothing released


def test_reclaim_hands_the_reaper_the_exact_profile_marker(monkeypatch):
    seen = []
    monkeypatch.setattr(
        browser_session, "_PROFILE_REAPER", lambda m: (seen.append(m), [])[1]
    )
    browser_session.reclaim_orphaned_profile()
    assert seen == [f"--user-data-dir={browser_session.BROWSER_PROFILE_DIR}"]


async def test_launch_hang_self_heals_by_reclaiming_the_orphan(monkeypatch):
    """The headline fix: a launch that HANGS on the locked profile is timed out,
    the orphan is reclaimed, and a retry succeeds — the first browse after a
    restart heals itself instead of spinning forever."""
    killed = []
    monkeypatch.setattr(browser_session, "LAUNCH_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(browser_session, "_PROFILE_SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(browser_session, "_CHANNELS", (None,))
    monkeypatch.setattr(browser_session, "_harden_profile", lambda p: None)
    monkeypatch.setattr(browser_session, "_PROFILE_REAPER", lambda m: [999])
    monkeypatch.setattr(
        browser_session, "_kill_pid_tree", lambda pid: (killed.append(pid), True)[1]
    )
    chromium = _FakeChromium(["hang", "ok"])
    pw = _FakePlaywright(chromium)

    async def _fake_start():
        return pw

    monkeypatch.setattr(browser_session, "_PLAYWRIGHT_STARTER", _fake_start)
    browser_session.reset_playwright_driver()  # a fresh ensure per test

    result = await browser_session._default_browser_factory()

    assert isinstance(result, browser_session._RealBrowser)
    assert killed == [999]        # the orphan was reclaimed between the two passes
    assert chromium.calls == 2    # first launch hung/timed out, the retry succeeded
    assert pw.stopped is False    # a launched driver is kept, never stopped


async def test_launch_total_failure_with_no_orphan_raises_unavailable(monkeypatch):
    """Every channel fails and there is no orphan to reclaim → a clean
    BrowserUnavailable (the tool _fails). The SHARED driver is never stopped by a
    launch; its reference is dropped so the NEXT browse re-warms a fresh one."""
    monkeypatch.setattr(browser_session, "LAUNCH_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(browser_session, "_PROFILE_SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(browser_session, "_CHANNELS", (None,))
    monkeypatch.setattr(browser_session, "_harden_profile", lambda p: None)
    monkeypatch.setattr(browser_session, "_PROFILE_REAPER", lambda m: [])  # nothing to reclaim
    chromium = _FakeChromium(["fail"])
    pw = _FakePlaywright(chromium)

    async def _fake_start():
        return pw

    monkeypatch.setattr(browser_session, "_PLAYWRIGHT_STARTER", _fake_start)
    browser_session.reset_playwright_driver()  # a fresh ensure per test

    with pytest.raises(browser_session.BrowserUnavailable):
        await browser_session._default_browser_factory()
    assert chromium.calls == 1     # no orphan → no retry
    assert pw.stopped is False      # the shared driver is never stopped by a launch
    assert browser_session._shared_playwright is None  # dropped → next browse re-warms


async def test_launch_chain_stops_when_its_shared_budget_is_spent(monkeypatch):
    """2026-07-21: the chain's worst case (channels x attempts x per-attempt
    timeout) exceeded the OUTER browse belt, so the belt killed the browse with
    zero diagnostics. The chain now owns a shared budget: once it is spent, no
    further channel is attempted and the failure is an honest BrowserUnavailable
    naming the budget — never a silent outer-belt cancellation."""
    monkeypatch.setattr(browser_session, "LAUNCH_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(browser_session, "LAUNCH_CHAIN_BUDGET_SECONDS", 0.15)
    monkeypatch.setattr(browser_session, "_PROFILE_SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(browser_session, "_CHANNELS", (None, "chrome"))
    monkeypatch.setattr(browser_session, "_harden_profile", lambda p: None)
    monkeypatch.setattr(browser_session, "_PROFILE_REAPER", lambda m: [])
    chromium = _FakeChromium(["hang", "hang", "hang", "hang"])
    pw = _FakePlaywright(chromium)

    async def _fake_start():
        return pw

    monkeypatch.setattr(browser_session, "_PLAYWRIGHT_STARTER", _fake_start)
    browser_session.reset_playwright_driver()  # a fresh ensure per test

    with pytest.raises(browser_session.BrowserUnavailable, match="launch budget"):
        await browser_session._default_browser_factory()
    # the first attempt consumed the whole budget — the second channel was never
    # tried, so the chain can never outrun the outer belt again
    assert chromium.calls == 1
    assert pw.stopped is False      # the shared driver is never stopped by a launch
    assert browser_session._shared_playwright is None


async def test_a_cancelled_launch_reclaims_the_profile_but_keeps_the_shared_driver(monkeypatch):
    """The outer browse belt firing MID-LAUNCH must not leak a half-spawned
    profile-holding Chrome: a cancelled chain schedules a detached cleanup that
    reclaims the profile. The SHARED Node driver is deliberately NOT stopped — it
    is reused by the next browse, not owned by this launch."""
    reaped = []
    monkeypatch.setattr(browser_session, "LAUNCH_TIMEOUT_SECONDS", 30.0)
    monkeypatch.setattr(browser_session, "_CHANNELS", (None,))
    monkeypatch.setattr(browser_session, "_harden_profile", lambda p: None)
    monkeypatch.setattr(
        browser_session, "_PROFILE_REAPER", lambda m: (reaped.append(m), [])[1]
    )
    chromium = _FakeChromium(["hang"])
    pw = _FakePlaywright(chromium)

    async def _fake_start():
        return pw

    monkeypatch.setattr(browser_session, "_PLAYWRIGHT_STARTER", _fake_start)
    browser_session.reset_playwright_driver()  # a fresh ensure per test

    task = asyncio.ensure_future(browser_session._default_browser_factory())
    await asyncio.sleep(0.05)          # let it reach the hanging launch
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.1)           # the detached cleanup task runs
    assert pw.stopped is False         # the SHARED driver survives a cancelled launch
    assert reaped                       # and the profile was reclaimed


class _HeldFake:
    """A held session that records its close — shutdown must close EVERY slot."""

    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


async def test_shutdown_browser_windows_closes_every_held_slot(monkeypatch):
    """Every registry slot — INCLUDING commit and challenge, which the old
    hand-listed shutdown forgot (a backend stopping mid-approval leaked the
    profile-lock orphan its docstring promised to prevent) — plus the sign-in
    window. This test iterates the registry TABLE, so a newly added slot that
    shutdown misses fails here by construction."""
    from app.browser import registry as browser_registry

    held = {}
    for slot, reg in browser_registry.REGISTRIES.items():
        fake = _HeldFake()
        held[slot] = fake
        await reg.hold(fake, {"slot": slot})
    login_closed = []

    async def _fake_close_login():
        login_closed.append(True)
        return True

    monkeypatch.setattr(browser_session, "close_login_window", _fake_close_login)

    await browser_session.shutdown_browser_windows()

    for slot, fake in held.items():
        assert fake.closed, f"slot {slot!r} was not closed at shutdown"
        assert browser_registry.REGISTRIES[slot].peek() is None
    assert login_closed


async def test_shutdown_browser_windows_survives_one_close_failing(monkeypatch):
    from app.browser import registry as browser_registry

    class _Boom:
        async def close(self):
            raise RuntimeError("half-dead window")

    await browser_registry.REGISTRIES["media"].hold(_Boom(), {})
    survivor = _HeldFake()
    await browser_registry.REGISTRIES["challenge"].hold(survivor, {})
    login_closed = []

    async def _fake_close_login():
        login_closed.append(True)
        return True

    monkeypatch.setattr(browser_session, "close_login_window", _fake_close_login)

    await browser_session.shutdown_browser_windows()   # must not raise

    assert survivor.closed          # one failing close never blocks the rest
    assert login_closed


# --------------------------------------------------------------- COMMIT mode
# COMMIT (14.5) is the ONE approved way past Rule 1: arm_commit permits a SINGLE
# matching non-GET (a user-approved form submit), consumed the instant it fires
# (re-lock). These pin the security-critical properties the whole feature rests
# on — if any goes green while its rule is broken, the approval gate has been
# bypassed rather than satisfied.
async def test_an_unapproved_form_navigation_is_aborted_in_commit_capable_session(fake_browser):
    """With no arm set, the backstop is unchanged: a form-POST navigation dies
    and nothing records a fired commit."""
    session = await _session(allowlist={"example.com"})
    assert session._armed_commit is None
    assert (
        await _verdict(
            session, url="https://example.com/submit", method="POST", navigation=True
        )
        == "abort"
    )
    assert session.commit_fired() is False


async def test_an_armed_commit_passes_exactly_once_then_relocks(fake_browser):
    """THE commit guarantee: the one approved request passes, and the permit is
    consumed in the same breath — a double-submit finds nothing armed."""
    session = await _session(allowlist={"example.com"})
    session.arm_commit("POST", "https://example.com/submit")
    # The one approved request goes through (a classic form navigation)...
    assert (
        await _verdict(
            session, url="https://example.com/submit", method="POST", navigation=True
        )
        == "continue"
    )
    assert session.commit_fired() is True
    assert session.stats.allowed_commits == 1
    assert session._armed_commit is None              # re-locked
    # ...and a second identical submit navigation is aborted — re-locked.
    assert (
        await _verdict(
            session, url="https://example.com/submit", method="POST", navigation=True
        )
        == "abort"
    )
    assert session.stats.allowed_commits == 1         # not two


async def test_an_armed_commit_fires_on_the_spa_transport_too(fake_browser, monkeypatch):
    """An SPA submits via a background fetch to the action URL, not a form
    navigation. The permit matches on (method, url) whatever the transport, so
    commit_fired() records the real submission either way."""
    monkeypatch.setattr(browser_session, "_host_is_blocked", lambda h: False)
    browser_session.reset_host_cache()
    session = await _session(allowlist={"example.com"})
    session.arm_commit("POST", "https://example.com/submit")
    assert (
        await _verdict(
            session, url="https://example.com/submit", method="POST", navigation=False
        )
        == "continue"
    )
    assert session.commit_fired() is True
    assert session._armed_commit is None              # consumed, re-locked


async def test_an_armed_commit_matches_only_the_exact_request(fake_browser):
    """The permit is for ONE request, named by method + URL. A different path or
    method is aborted (as a form navigation) AND does not consume the permit
    (fail closed, no leak)."""
    session = await _session(allowlist={"example.com"})
    session.arm_commit("POST", "https://example.com/submit")

    assert (
        await _verdict(
            session, url="https://example.com/other", method="POST", navigation=True
        )
        == "abort"
    )
    assert session._armed_commit is not None          # untouched by a non-match
    assert (
        await _verdict(
            session, url="https://example.com/submit", method="PUT", navigation=True
        )
        == "abort"
    )
    assert session._armed_commit is not None
    # The exact approved request still works afterwards.
    assert (
        await _verdict(
            session, url="https://example.com/submit", method="POST", navigation=True
        )
        == "continue"
    )


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
    form navigation is aborted again (Rule 1) — the permit did not linger."""
    session = await _session(allowlist={"example.com"})
    session.arm_commit("POST", "https://example.com/submit")
    assert (
        await _verdict(
            session, url="https://example.com/submit", method="POST", navigation=True
        )
        == "continue"
    )
    # The submit's response redirects to another site — refused.
    assert (
        await _verdict(session, url="https://attacker.com/thanks", navigation=True)
        == "abort"
    )
    # And a further form navigation is aborted again — re-locked, never
    # "commit mode on".
    assert (
        await _verdict(
            session, url="https://example.com/again", method="POST", navigation=True
        )
        == "abort"
    )


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


# --------------------------- widget verification traffic (2026-07-21 policy)
# Under action-level safety a CAPTCHA widget's verification XHR is ordinary
# page traffic — it flows with no arming window (the old vendor carve-out and
# its arm/disarm lifecycle are gone). The no-touch guarantees are elsewhere and
# unchanged: the widget's elements are never stamped/listed, vision maps to
# nothing in its zone, and the loop refuses to act there.
async def test_widget_verification_posts_flow_without_any_arming(
    fake_browser, monkeypatch
):
    monkeypatch.setattr(browser_session, "_host_is_blocked", lambda h: False)
    browser_session.reset_host_cache()
    session = await _session()
    assert (
        await _verdict(
            session,
            url="https://www.google.com/recaptcha/api2/userverify",
            method="POST",
            navigation=False,
        )
        == "continue"
    )
    # The form's own submit NAVIGATION — the mutation approval exists to gate —
    # still aborts unarmed.
    assert (
        await _verdict(
            session, url="https://example.com/submit", method="POST", navigation=True
        )
        == "abort"
    )


# --------------------------------------- challenge hold registry (2026-07-19)
# An embedded widget's token is bound to the page render in the agent's own
# window — it cannot transfer from a separate hand-off window. The live session
# (form filled) is held here across the pause; the user ticks the box in that
# window; the resumed discovery takes the session back.
async def test_the_challenge_registry_holds_and_hands_off_one_session(fake_browser):
    meta = {"kind": "reCAPTCHA", "site": "example.com", "goal": "apply"}
    s1 = BrowserSession(FakeBrowser(), FakePage(), {"example.com"})
    await browser_session.hold_challenge(s1, meta=meta)
    assert browser_session.pending_challenge() == meta

    taken = await browser_session.take_challenge()
    assert taken is s1
    assert browser_session.pending_challenge() is None
    assert await browser_session.take_challenge() is None

    # one slot: a new hold closes the previous session; discard closes + clears.
    s2 = BrowserSession(FakeBrowser(), FakePage(), set())
    s3 = BrowserSession(FakeBrowser(), FakePage(), set())
    await browser_session.hold_challenge(s2, meta=meta)
    await browser_session.hold_challenge(s3, meta=meta)
    assert s2._browser.closed is True
    assert await browser_session.discard_challenge() is True
    assert s3._browser.closed is True
    assert await browser_session.discard_challenge() is False


# ------------------------------------- discovery hold registry (2026-07-19)
# A commit discovery that pauses to ask the user something (a missing form value,
# an optional sign-in offer, an off-site origin to approve) HOLDS its live,
# part-filled session here across the pause — before, the window closed the
# moment it asked ("filled two fields and then closed the chrome"). The resumed
# discovery takes it back and carries on from where it stopped.
async def test_the_discovery_registry_holds_and_hands_off_one_session(fake_browser):
    meta = {"goal": "apply to the job", "reason": "fill"}
    s1 = BrowserSession(FakeBrowser(), FakePage(), {"jobs.example.com"})
    await browser_session.hold_discovery(s1, meta=meta)
    assert browser_session.pending_discovery() == meta

    taken = await browser_session.take_discovery()
    assert taken is s1
    assert browser_session.pending_discovery() is None       # handed off, slot empty
    assert await browser_session.take_discovery() is None     # cannot be taken twice

    # One slot: a new hold closes the previous session; discard closes + clears.
    s2 = BrowserSession(FakeBrowser(), FakePage(), set())
    s3 = BrowserSession(FakeBrowser(), FakePage(), set())
    await browser_session.hold_discovery(s2, meta=meta)
    await browser_session.hold_discovery(s3, meta=meta)
    assert s2._browser.closed is True
    assert await browser_session.discard_discovery() is True
    assert s3._browser.closed is True
    assert await browser_session.discard_discovery() is False


# --------------------------------------------------- profile hardening (creds)
# SESSIONS, NOT CREDENTIALS: the ~/.jarvis/browser profile must never save or
# auto-fill a password (a saved credential auto-filling read as "the AI logged in
# itself" — user report 2026-07-18). These pin the hardening without a real
# browser: Preferences seeding, merge safety, idempotence, and — the load-bearing
# split — clearing saved credentials while leaving the session Cookies intact.
def test_harden_profile_disables_password_manager_and_autofill(tmp_path):
    browser_session._harden_profile(tmp_path)
    prefs = json.loads((tmp_path / "Default" / "Preferences").read_text(encoding="utf-8"))
    assert prefs["credentials_enable_service"] is False
    assert prefs["profile"]["password_manager_enabled"] is False
    assert prefs["autofill"]["profile_enabled"] is False
    assert prefs["autofill"]["credit_card_enabled"] is False


def test_harden_profile_merges_without_clobbering_existing_prefs(tmp_path):
    default = tmp_path / "Default"
    default.mkdir(parents=True)
    (default / "Preferences").write_text(
        json.dumps({"profile": {"exit_type": "Normal", "name": "me"}, "keep": 1}),
        encoding="utf-8",
    )
    browser_session._harden_profile(tmp_path)
    prefs = json.loads((default / "Preferences").read_text(encoding="utf-8"))
    assert prefs["profile"]["password_manager_enabled"] is False  # our key applied
    assert prefs["keep"] == 1                                     # unrelated key kept
    assert prefs["profile"]["exit_type"] == "Normal"              # nested key kept
    assert prefs["profile"]["name"] == "me"


def test_harden_profile_is_idempotent(tmp_path):
    browser_session._harden_profile(tmp_path)
    first = (tmp_path / "Default" / "Preferences").read_text(encoding="utf-8")
    browser_session._harden_profile(tmp_path)
    second = (tmp_path / "Default" / "Preferences").read_text(encoding="utf-8")
    assert first == second


def test_harden_profile_clears_saved_credentials_but_not_cookies(tmp_path):
    default = tmp_path / "Default"
    default.mkdir(parents=True)
    (default / "Login Data").write_text("saved-password-db", encoding="utf-8")
    (default / "Login Data For Account").write_text("saved", encoding="utf-8")
    (default / "Cookies").write_text("session-cookie", encoding="utf-8")
    browser_session._harden_profile(tmp_path)
    assert not (default / "Login Data").exists()
    assert not (default / "Login Data For Account").exists()
    # The session cookie is the "log in once, stay signed in" property — untouched.
    assert (default / "Cookies").read_text(encoding="utf-8") == "session-cookie"


def test_harden_profile_survives_a_corrupt_prefs_file(tmp_path):
    default = tmp_path / "Default"
    default.mkdir(parents=True)
    (default / "Preferences").write_text("{not valid json", encoding="utf-8")
    browser_session._harden_profile(tmp_path)  # must not raise
    prefs = json.loads((default / "Preferences").read_text(encoding="utf-8"))
    assert prefs["credentials_enable_service"] is False


# --------------------------------------------------- popup / new-tab following
# Many job boards (WeWorkRemotely, live 2026-07-18) open the application — or a
# CAPTCHA — in a NEW TAB. The loop only observes session.page, so an un-adopted
# popup is invisible. These pin the follow: a new tab is routed under the SAME
# interceptor (no new capability) and becomes the page the loop observes.
class FakeBrowserWithPages(FakeBrowser):
    """A FakeBrowser that records a context 'page' listener, so we can drive the
    popup event the real _RealBrowser.on_page would deliver."""

    def __init__(self, page=None):
        super().__init__(page)
        self.page_listener = None

    def on_page(self, callback):
        self.page_listener = callback


async def test_the_window_registers_one_popup_follower_for_the_whole_context(
    monkeypatch,
):
    """ONE listener for the window, not one per session (2026-08-01). With a
    context per session it made no difference; sharing one, a listener per
    session would have every session run the ownership decision on every new
    tab — and any divergence between them is a cross-tab bug. The decision lives
    in window._owner_of_new_page, once."""
    from app.browser import window as browser_window

    browser = FakeBrowserWithPages()
    monkeypatch.setattr(browser_session, "BROWSER_FACTORY", lambda: browser)
    await BrowserSession.open({"example.com"})
    assert browser.page_listener == browser_window._on_context_page


async def test_a_popup_is_adopted_under_the_same_interceptor(fake_browser):
    """The core follow: the new tab gets THIS session's read-only interceptor and
    becomes session.page — so the loop, which reads session.page, follows it."""
    session = await _session()
    original = session.page
    popup = FakePage(url="https://example.com/apply")
    await session._adopt_new_page(popup)
    assert session.page is popup
    assert session.page is not original
    # the SAME guard is installed on the popup (Rule 1/2/3 govern it too). Bound
    # methods compare by (__func__, __self__) — `is` on them is always False.
    assert any(
        getattr(h, "__func__", None) is BrowserSession._intercept and getattr(h, "__self__", None) is session
        for _, h in popup.routes
    )


async def test_the_context_page_event_schedules_adoption(fake_browser):
    """The sync context handler schedules the async adopt on the loop."""
    session = await _session()
    popup = FakePage(url="https://example.com/apply")
    session._on_new_page(popup)         # sync — Playwright dispatches it like this
    await asyncio.sleep(0)              # let the scheduled task run
    assert session.page is popup


async def test_an_adopted_popup_is_still_guarded(fake_browser):
    """Following a popup grants NO new capability: an unapproved form-POST
    navigation on the new tab is aborted exactly like on the original page."""
    session = await _session()
    popup = FakePage(url="https://example.com/apply")
    await session._adopt_new_page(popup)
    assert (
        await _verdict(
            session, url="https://example.com/apply", method="POST", navigation=True
        )
        == "abort"
    )
    assert session.stats.blocked_mutations == 1


class _PopupPage(FakePage):
    """A popup that reports an opener, the way Playwright's real one does."""

    def __init__(self, opener, url="https://ads.example.net/win"):
        super().__init__(url=url)
        self._opener = opener
        self.closed = False

    async def opener(self):
        return self._opener

    async def close(self):
        self.closed = True


async def test_an_unrelated_popup_is_closed_not_adopted(fake_browser):
    """An ad window (opened by a page no tab of ours owns) used to
    unconditionally become self.page — hijacking the loop mid-task. It is closed.

    The DECISION moved to window._owner_of_new_page (2026-08-01) — a session must
    not adopt a tab merely because it heard about it — so this drives the real
    dispatch path rather than calling _adopt_new_page directly."""
    from app.browser import window as browser_window

    session = await _session()
    original = session.page
    stranger = FakePage(url="https://example.com/other-tab")
    popup = _PopupPage(opener=stranger)

    await browser_window._dispatch_new_page(popup)

    assert popup.closed is True
    assert session.page is original          # the loop's page was never hijacked


async def test_a_popup_from_our_own_tab_is_adopted_by_that_tab(fake_browser):
    """The other half of the same decision: a popup our page opened is ours."""
    from app.browser import window as browser_window

    session = await _session()
    popup = _PopupPage(opener=session.page, url="https://example.com/apply")

    await browser_window._dispatch_new_page(popup)

    assert session.page is popup
    assert popup.closed is False


async def test_a_session_never_adopts_another_sessions_tab(fake_browser, monkeypatch):
    """⚠️ THE HAZARD A SHARED CONTEXT CREATES, frozen. `_adopt_new_page` closed a
    popup only when `opener is not None and opener is not self.page` — and a
    `context.new_page()` has a NULL opener, so it fell through and ADOPTED. One
    session would have taken the tab another had just opened for itself and
    closed its own page out from under a running task."""
    from app.browser import window as browser_window

    a = await _session()
    b = await BrowserSession.open({"other.com"})
    a_page, b_page = a.page, b.page

    # b's brand-new tab, announced to the window with no opener.
    await browser_window._dispatch_new_page(b_page)

    assert a.page is a_page, "A must not seize B's tab"
    assert b.page is b_page
    assert not getattr(a_page, "closed", False), "A's own page must survive"


async def test_a_tab_with_no_opener_goes_to_the_session_being_driven(fake_browser):
    """rel="noopener" and target=_blank genuinely produce a NULL opener, and that
    is the WeWorkRemotely case the popup follow exists for (2026-07-18). With
    several tabs open, such a tab belongs to whoever is acting — nobody else is
    clicking anything."""
    from app.browser import window as browser_window

    a = await _session()
    b = await BrowserSession.open({"other.com"})
    a_page = a.page
    browser_window.set_driving(b)
    try:
        orphan = FakePage(url="https://other.com/apply")   # no opener attribute
        await browser_window._dispatch_new_page(orphan)
    finally:
        browser_window.set_driving(None)

    assert b.page is orphan, "the driving session follows the new tab"
    assert a.page is a_page, "the idle tab is untouched"


async def test_a_tab_with_no_opener_and_no_driver_is_left_alone(fake_browser):
    """Rule 4: when we cannot tell whose it is, do nothing. Never close another
    tab on a guess."""
    from app.browser import window as browser_window

    a = await _session()
    b = await BrowserSession.open({"other.com"})
    a_page, b_page = a.page, b.page

    orphan = _PopupPage(opener=None, url="https://elsewhere.example/x")
    await browser_window._dispatch_new_page(orphan)

    assert a.page is a_page and b.page is b_page
    assert orphan.closed is False, "an unattributable tab is left alone, not closed"


async def test_adopting_a_tab_closes_the_superseded_one(fake_browser):
    """The loop drives ONE page; the tab it left behind is closed on adoption so
    tabs no longer accumulate for the life of the session."""
    session = await _session()

    class _ClosablePage(FakePage):
        def __init__(self, url):
            super().__init__(url=url)
            self.closed = False

        async def close(self):
            self.closed = True

    first = _ClosablePage("https://example.com/listing")
    session.page = first
    popup = FakePage(url="https://example.com/apply")   # no opener → our own tab
    await session._adopt_new_page(popup)

    assert session.page is popup
    assert first.closed is True


async def test_main_frame_check_is_scoped_to_the_requests_own_page(fake_browser):
    """A shared interceptor across adopted tabs must judge a navigation against the
    REQUEST'S OWN page main frame, not self.page's — else a popup's top-level
    navigation is mislabelled. (Regression guard for the popup-follow change.)"""
    session = await _session()  # session.page.main_frame == "main" (a different tab)

    class _Owner:
        def __init__(self, mf):
            self.main_frame = mf

    class _Frame:
        def __init__(self):
            self.page = None

    # A request whose frame IS its own page's main frame → a main-frame navigation.
    frame = _Frame()
    owner = _Owner(frame)
    frame.page = owner
    req = FakeRequest(url="https://x.com/", navigation=True, frame=frame)
    assert session._is_main_frame_navigation(req) is True

    # A SUBFRAME of that same page (frame != page.main_frame) → not main-frame,
    # even though self.page's crude "main" check is irrelevant here.
    sub = _Frame()
    sub.page = owner
    req2 = FakeRequest(url="https://x.com/", navigation=True, frame=sub)
    assert session._is_main_frame_navigation(req2) is False


# ------------------------------------------------- opt-in unpacked extensions
def test_no_extensions_dir_adds_no_flags(monkeypatch, tmp_path):
    """The default extension-free posture: an empty/absent BROWSER_EXTENSIONS_DIR
    adds ZERO launch flags, so the launch args are unchanged until the user drops
    an extension in — and the hermetic suite is never affected."""
    monkeypatch.setattr(browser_session, "BROWSER_EXTENSIONS_DIR", tmp_path / "browser_extensions")
    assert browser_session._extension_load_args() == []


def test_a_manifest_subdir_is_loaded(monkeypatch, tmp_path):
    ext_root = tmp_path / "browser_extensions"
    ublock = ext_root / "ublock-origin-lite"
    ublock.mkdir(parents=True)
    (ublock / "manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(browser_session, "BROWSER_EXTENSIONS_DIR", ext_root)

    args = browser_session._extension_load_args()
    resolved = str(ublock.resolve())
    # --load-extension ONLY — --disable-extensions-except would disable the
    # profile's own installed uBlock and block Web-Store installs (2026-07-22 fix).
    assert args == [f"--load-extension={resolved}"]
    assert not any(a.startswith("--disable-extensions-except") for a in args)


def test_a_subdir_without_a_manifest_is_ignored(monkeypatch, tmp_path):
    ext_root = tmp_path / "browser_extensions"
    (ext_root / "not-an-extension").mkdir(parents=True)  # no manifest.json
    (ext_root / "README.txt").parent.mkdir(exist_ok=True)  # a stray file, not a dir
    (ext_root / "README.txt").write_text("hi", encoding="utf-8")
    monkeypatch.setattr(browser_session, "BROWSER_EXTENSIONS_DIR", ext_root)
    assert browser_session._extension_load_args() == []


def test_two_extensions_are_comma_joined(monkeypatch, tmp_path):
    ext_root = tmp_path / "browser_extensions"
    a = ext_root / "a-ext"
    b = ext_root / "b-ext"
    for d in (a, b):
        d.mkdir(parents=True)
        (d / "manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(browser_session, "BROWSER_EXTENSIONS_DIR", ext_root)

    args = browser_session._extension_load_args()
    csv = f"{a.resolve()},{b.resolve()}"  # sorted() → 'a-ext' before 'b-ext'
    assert args == [f"--load-extension={csv}"]


def test_load_extension_switch_is_re_enabled_on_modern_chrome():
    """Chrome 137+ disabled --load-extension; without turning
    DisableLoadExtensionCommandLineSwitch off, a dropped-in uBlock never loads
    (the 2026-07-22 root cause). The shared disable-features set — reached by BOTH
    the agent window (_LAUNCH_ARGS) and the clean/media window — must carry it, and
    Chrome honors only ONE --disable-features so it must be a single string."""
    assert "DisableLoadExtensionCommandLineSwitch" in browser_session._DISABLE_FEATURES
    feature_flags = [a for a in browser_session._LAUNCH_ARGS if a.startswith("--disable-features=")]
    assert feature_flags == [f"--disable-features={browser_session._DISABLE_FEATURES}"]


def test_no_extension_flag_ever_disables_the_profiles_own_extensions(monkeypatch, tmp_path):
    """--disable-extensions-except is NEVER emitted — it disabled the profile's
    Web-Store uBlock and blocked new installs from taking effect (the double-symptom
    of the 2026-07-22 report)."""
    ext_root = tmp_path / "browser_extensions"
    ublock = ext_root / "ublock-origin-lite"
    ublock.mkdir(parents=True)
    (ublock / "manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(browser_session, "BROWSER_EXTENSIONS_DIR", ext_root)
    assert not any(
        a.startswith("--disable-extensions-except")
        for a in browser_session._extension_load_args()
    )


def test_extension_discovery_never_raises(monkeypatch):
    """A scan failure yields [] (the browser launches extension-free), never an
    exception that would break a launch — the _harden_profile discipline."""
    class _Boom:
        def mkdir(self, *a, **k):
            raise OSError("nope")

    monkeypatch.setattr(browser_session, "BROWSER_EXTENSIONS_DIR", _Boom())
    assert browser_session._extension_load_args() == []


# ------------------------------------------ SSRF host guard: single flight
async def test_concurrent_lookups_of_one_cold_host_resolve_it_once():
    """A page's first load fires many requests at the same handful of cold CDN
    hosts within milliseconds. The cache made the STEADY state cheap and left the
    stampede in place: every one of those misses spawned its own
    to_thread(getaddrinfo) before the first wrote its result."""
    import asyncio

    from app.browser import session as sess

    sess.reset_host_cache()
    calls = {"n": 0}

    def _slow_probe(host):
        calls["n"] += 1
        import time as _t
        _t.sleep(0.05)
        return False

    original = sess._host_is_blocked
    sess._host_is_blocked = _slow_probe
    try:
        results = await asyncio.gather(*[
            sess._host_blocked_cached("cdn.example.test") for _ in range(12)
        ])
    finally:
        sess._host_is_blocked = original
        sess.reset_host_cache()

    assert results == [False] * 12
    assert calls["n"] == 1, f"resolved {calls['n']} times for one host"


async def test_a_failing_lookup_is_not_cached_and_still_raises():
    """A resolution ERROR must not be memoised as an answer, and it must reach
    the caller's own guard — which already fails closed."""
    from app.browser import session as sess

    sess.reset_host_cache()

    def _boom(host):
        raise OSError("resolver down")

    original = sess._host_is_blocked
    sess._host_is_blocked = _boom
    try:
        with pytest.raises(OSError):
            await sess._host_blocked_cached("broken.example.test")
        assert "broken.example.test" not in sess._host_block_cache
        assert "broken.example.test" not in sess._host_block_inflight
    finally:
        sess._host_is_blocked = original
        sess.reset_host_cache()


# ====================== the AJAX submit — a form's action is not its endpoint
# 2026-07-26, junaidjamshed.com. Discovery read the product form correctly
# (POST /cart/add, 10 fields — that IS what the markup declares), the user
# approved, and the submit reported "Nothing was sent". The site's own theme
# does `fetchJSON("/cart/add.js", {method:"POST"})`, so the exact-match permit
# could NEVER fire on that site, and the failure message was built entirely on
# the absence of a match it could not have got.
#
# The split these pin: PERMISSION (_commit_allows) is untouched — it is the only
# thing that turns an abort into an allow. RECOGNITION (_is_commit_variant) is
# consulted solely for traffic Rule 1 was letting through anyway.
async def test_an_ajax_submit_to_the_actions_js_twin_is_recognised(fake_browser):
    """THE INCIDENT. Armed for the form's action; the page posts the `.js` twin
    (an in-page fetch — never a navigation). It must count as the approved
    submission and report the url that actually carried it."""
    session = await _session(allowlist={"example.com"})
    session.arm_commit("POST", "https://example.com/cart/add")

    assert (
        await _verdict(
            session, url="https://example.com/cart/add.js", method="POST",
            navigation=False,
        )
        == "continue"
    )
    assert session.commit_fired() is True
    assert session.commit_submitted_url() == "https://example.com/cart/add.js"
    assert session.stats.allowed_commits == 1
    assert session._armed_commit is None              # one-shot, re-locked


async def test_the_json_twin_is_recognised_too(fake_browser):
    session = await _session(allowlist={"example.com"})
    session.arm_commit("POST", "https://example.com/cart/add")
    await _verdict(
        session, url="https://example.com/cart/add.json", method="POST",
        navigation=False,
    )
    assert session.commit_fired() is True


async def test_variant_recognition_grants_no_new_permission(fake_browser):
    """The load-bearing property: recognition is RECORDING only. A main-frame
    NAVIGATION to the `.js` twin is still an unapproved form navigation — it dies
    exactly as before, and nothing is recorded as fired."""
    session = await _session(allowlist={"example.com"})
    session.arm_commit("POST", "https://example.com/cart/add")

    assert (
        await _verdict(
            session, url="https://example.com/cart/add.js", method="POST",
            navigation=True,
        )
        == "abort"
    )
    assert session.commit_fired() is False
    assert session.stats.allowed_commits == 0
    assert session._armed_commit is not None          # permit NOT consumed


@pytest.mark.parametrize(
    "url, why",
    [
        ("https://example.com/cart/addresses", "a longer path, not a suffix"),
        ("https://example.com/cart/add/confirm", "a different path"),
        ("https://example.com/cart/add.js.evil", "suffix must END the url"),
        ("https://example.com/cart/add?x=1", "query is inside the normalized form"),
        ("https://other.example/cart/add.js", "another host"),
        ("https://example.com/cart/add.php", "not a representation suffix"),
    ],
)
async def test_variant_recognition_is_narrow(fake_browser, url, why):
    """Nothing outside `armed + .js/.json` is ever taken for the user's approved
    submission — it is merely observed, so the failure can name it."""
    session = await _session(allowlist={"example.com", "other.example"})
    session.arm_commit("POST", "https://example.com/cart/add")
    await _verdict(session, url=url, method="POST", navigation=False)
    assert session.commit_fired() is False, why


async def test_a_different_method_is_not_the_approved_submission(fake_browser):
    session = await _session(allowlist={"example.com"})
    session.arm_commit("POST", "https://example.com/cart/add")
    await _verdict(
        session, url="https://example.com/cart/add.js", method="DELETE",
        navigation=False,
    )
    assert session.commit_fired() is False


async def test_same_origin_traffic_during_the_window_is_recorded_for_the_report(
    fake_browser,
):
    """So a failure can say what the page sent INSTEAD, rather than assert that
    nothing was sent — which this layer never has the evidence for."""
    session = await _session(allowlist={"example.com"})
    session.arm_commit("POST", "https://example.com/cart/add")
    await _verdict(
        session, url="https://example.com/analytics/beacon", method="POST",
        navigation=False,
    )
    assert session.commit_fired() is False
    assert session.commit_observations() == ["POST https://example.com/analytics/beacon"]


async def test_observations_ignore_other_hosts_and_stay_bounded(fake_browser):
    session = await _session(allowlist={"example.com", "other.example"})
    session.arm_commit("POST", "https://example.com/cart/add")
    await _verdict(
        session, url="https://other.example/track", method="POST", navigation=False
    )
    assert session.commit_observations() == []
    for i in range(12):
        await _verdict(
            session, url=f"https://example.com/x/{i}", method="POST", navigation=False
        )
    assert len(session.commit_observations()) <= 8


async def test_arming_resets_the_previous_windows_observations(fake_browser):
    """Multi-commit: form #2's report must not inherit form #1's evidence."""
    session = await _session(allowlist={"example.com"})
    session.arm_commit("POST", "https://example.com/cart/add")
    await _verdict(
        session, url="https://example.com/noise", method="POST", navigation=False
    )
    assert session.commit_observations()

    session.arm_commit("POST", "https://example.com/checkout")
    assert session.commit_observations() == []
    assert session.commit_submitted_url() == ""
    assert session.commit_fired() is False


# ---- waiting for the REQUEST, not for the paint
async def test_wait_for_commit_returns_as_soon_as_the_request_is_seen(fake_browser):
    """settle() is a DOM-quiet detector that returns in ~250ms on an already
    painted page, so the old `submit → settle → read` sequence gave an async
    handler no chance. This waits on the request itself and returns the instant
    it lands — a prompt form costs no more than before."""
    session = await _session(allowlist={"example.com"})
    session.arm_commit("POST", "https://example.com/cart/add")

    async def _fire_soon():
        await asyncio.sleep(0.05)
        await _verdict(
            session, url="https://example.com/cart/add.js", method="POST",
            navigation=False,
        )

    task = asyncio.ensure_future(_fire_soon())
    assert await session.wait_for_commit(timeout=5.0) is True
    await task


async def test_wait_for_commit_times_out_quietly_when_nothing_posts(fake_browser):
    session = await _session(allowlist={"example.com"})
    session.arm_commit("POST", "https://example.com/cart/add")
    assert await session.wait_for_commit(timeout=0.15) is False


async def test_wait_for_commit_without_an_arm_never_raises(fake_browser):
    session = await _session(allowlist={"example.com"})
    assert await session.wait_for_commit(timeout=0.1) is False


# ======================================================================
# THE INTERCEPTION TAX — we drive Fetch ourselves so the cache stays on
# ======================================================================
# Registering ANY Playwright route makes its driver pair `Fetch.enable` with
# `Network.setCacheDisabled: true` for the whole session
# (chromium/crNetworkManager.js). The cache is then OFF — every image, script,
# stylesheet and font re-downloaded on every navigation, while the agent loop
# navigates repeatedly around one site.
#
# MEASURED on books.toscrape.com, second (warm) load:
#     A  Playwright route only ................................ 2.14s
#     B  Playwright route + our own setCacheDisabled(false) .... 2.83s
#     C  no Playwright route, our own Fetch.enable ............. 0.07s
# B is why this is not a one-liner: `cacheDisabled` is per-session state and
# Chromium takes the OR across attached sessions, so a second session saying
# "false" can never overrule Playwright's "true". Owning Fetch is the only way.
#
# The patterns we request are IDENTICAL to Playwright's, and _intercept is
# untouched — every rule test above still drives it directly, which is the point:
# this changed the TRANSPORT, not the guard.
class FakeCdpSession:
    """A CDP session that answers Page.getFrameTree and records what was sent —
    and can deliver a Fetch.requestPaused the way Chromium would."""

    def __init__(self, page, main_frame_id="MAIN"):
        self.page = page
        self.sent = []
        self.handlers = {}
        self.main_frame_id = main_frame_id

    def on(self, event, handler):
        self.handlers[event] = handler

    async def send(self, method, params=None):
        self.sent.append((method, dict(params or {})))
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": self.main_frame_id}}}
        return {}

    @property
    def methods(self):
        return [method for method, _ in self.sent]

    async def fire(self, *, url, method="GET", resource_type="Image",
                   frame_id="MAIN", request_id="R1"):
        """Deliver one paused request and return the verdict Chromium would see."""
        self.handlers["Fetch.requestPaused"](
            {
                "requestId": request_id,
                "request": {"url": url, "method": method},
                "resourceType": resource_type,
                "frameId": frame_id,
            }
        )
        for _ in range(200):                       # let the scheduled guard run
            for name, params in self.sent:
                if params.get("requestId") != request_id:
                    continue
                if name == "Fetch.continueRequest":
                    return "continue"
                if name == "Fetch.failRequest":
                    return f"abort:{params.get('errorReason')}"
            await asyncio.sleep(0.005)
        return None


class FakeContextBrowser(FakeBrowser):
    """The REAL _RealBrowser's shape: route/unroute on the CONTEXT, and the
    ability to open a raw CDP session. FakeBrowser models neither, which is why
    the bugs below survived 2400 green tests."""

    def __init__(self, page=None):
        super().__init__(page)
        self.routes = []
        self.unroute_calls = []
        self.cdp_sessions = []
        self.main_frame_id = "MAIN"

    async def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    async def unroute(self, pattern, handler=None):
        self.unroute_calls.append((pattern, handler))

    async def new_cdp_session(self, page):
        cdp = FakeCdpSession(page, self.main_frame_id)
        self.cdp_sessions.append(cdp)
        return cdp


@pytest.fixture
def context_browser(monkeypatch):
    browser = FakeContextBrowser()
    monkeypatch.setattr(browser_session, "BROWSER_FACTORY", lambda: browser)
    return browser


async def test_interception_is_installed_over_our_own_cdp_fetch(context_browser):
    """The whole speed fix in one assertion: no Playwright route is registered,
    so nothing disables Chromium's cache — and Fetch is enabled with exactly the
    patterns Playwright would have asked for, so coverage is unchanged."""
    session = await BrowserSession.open({"example.com"})
    assert context_browser.routes == [], "a Playwright route would disable the cache"
    cdp = context_browser.cdp_sessions[0]
    assert ("Fetch.enable", {
        "patterns": [{"urlPattern": "*", "requestStage": "Request"}]
    }) in cdp.sent
    assert session.stats.http_cache_on is True
    assert session.stats.as_dict()["http_cache_on"] is True


async def test_a_paused_request_flows_through_the_guard(context_browser):
    """The adapter end to end: a real Fetch.requestPaused shape reaches
    _intercept and comes back as a Chromium verdict."""
    session = await BrowserSession.open({"example.com"})
    cdp = context_browser.cdp_sessions[0]
    assert await cdp.fire(url="https://example.com/logo.png") == "continue"
    assert session.stats.total_requests == 1


async def test_an_aborted_request_uses_the_reason_playwright_used(context_browser):
    """`Failed` is what Playwright's own route.abort() sends, so a blocked
    request looks to the page exactly as it did before this change."""
    await BrowserSession.open({"example.com"})
    cdp = context_browser.cdp_sessions[0]
    verdict = await cdp.fire(
        url="https://doubleclick.net/ad.js", request_id="AD"
    )
    assert verdict == "abort:Failed"


async def test_the_adapter_tells_a_main_frame_navigation_from_an_iframes(
    context_browser,
):
    """Rule 3 is 'main-frame navigation only'. The adapter must judge that from
    the CDP frame id — an iframe document to an off-allowlist origin is ordinary
    page content and must still load, or nothing renders."""
    await BrowserSession.open({"example.com"})
    cdp = context_browser.cdp_sessions[0]
    # top-level navigation off the allowlist → refused
    assert await cdp.fire(
        url="https://attacker.com/", resource_type="Document",
        frame_id="MAIN", request_id="TOP",
    ) == "abort:Failed"
    # the same document inside a SUBFRAME → allowed through
    assert await cdp.fire(
        url="https://attacker.com/", resource_type="Document",
        frame_id="CHILD", request_id="SUB",
    ) == "continue"


async def test_without_a_main_frame_id_it_refuses_the_fast_path(context_browser):
    """FAIL SAFE, and the reason this is not just 'try CDP'. Rule 3 needs to
    know which frame is top-level; an adapter that cannot tell would fail OPEN.
    So an unknown frame tree falls back to the slower Playwright route rather
    than running a quietly weaker guard.

    The fallback is PER PAGE (2026-08-01). It used to be context-level, which a
    shared context turns into "this session's allowlist judges every other tab's
    requests" — Rule 3 is per-session, so that inverts the origin guard both
    ways."""
    context_browser.main_frame_id = ""
    session = await BrowserSession.open({"example.com"})
    assert session.page.routes, "should have fallen back to Playwright routing"
    assert context_browser.routes == [], (
        "a context-level route would judge OTHER tabs' requests by this "
        "session's allowlist"
    )
    assert session.stats.http_cache_on is False


async def test_a_handle_without_cdp_falls_back_to_playwright_routing(fake_browser):
    """Every fake in this suite, and any Playwright version without a CDP
    session. The guard is installed the old way and the summary says the cache
    is still disabled rather than claiming a fix that did not happen."""
    session = await _session()
    assert session.stats.http_cache_on is False
    assert fake_browser.page.routes, "no interceptor was installed at all"


async def test_the_fast_path_relaxes_no_rule(context_browser, monkeypatch):
    """THE SAFETY PROPERTY, pinned. Changing the transport must buy nothing past
    the guard. If this goes green while one of these is broken, a speed fix has
    become a hole."""
    monkeypatch.setattr(browser_session, "_host_is_blocked", lambda h: h == "localhost")
    browser_session.reset_host_cache()
    session = await BrowserSession.open({"example.com"})
    assert session.stats.http_cache_on is True
    cdp = context_browser.cdp_sessions[0]

    # Rule 1 — an unapproved form-POST navigation.
    assert await cdp.fire(
        url="https://example.com/api", method="POST",
        resource_type="Document", request_id="R1",
    ) == "abort:Failed"
    # Rule 2 — SSRF, on a non-allowlisted host.
    assert await cdp.fire(url="http://localhost:8000/x", request_id="R2") == "abort:Failed"
    # Rule 3 — an off-allowlist main-frame navigation.
    assert await cdp.fire(
        url="https://attacker.com/", resource_type="Document", request_id="R3",
    ) == "abort:Failed"
    # ...and an ordinary first-party read still flows.
    assert await cdp.fire(url="https://example.com/logo.png", request_id="R4") == "continue"


async def test_a_followed_popup_gets_its_own_guarded_target(context_browser):
    """A new tab is a new TARGET, so it needs its own Fetch session or it would
    be driven un-guarded — the popup is where job boards put the application."""
    session = await BrowserSession.open({"example.com"})
    popup = FakePage(url="https://example.com/apply")
    await session._adopt_new_page(popup)
    assert [c.page for c in context_browser.cdp_sessions] == [
        context_browser.page,
        popup,
    ]
    assert await context_browser.cdp_sessions[1].fire(
        url="https://attacker.com/", resource_type="Document", request_id="P1"
    ) == "abort:Failed"


async def test_playback_lifts_the_interceptor_by_disabling_fetch(context_browser):
    """The hand-off must actually stop intercepting, or the user's own window
    keeps paying the tax the hand-off exists to remove."""
    session = await BrowserSession.open({"youtube.com"})
    await session.enter_playback_mode(reload=False)
    assert "Fetch.disable" in context_browser.cdp_sessions[0].methods
    assert session.page.unroute_calls == []


async def test_playback_lifts_the_route_on_the_page_that_was_armed(context_browser):
    """THE SILENT BUG, frozen (found 2026-07-27): the lift must happen AT THE
    LEVEL THE ROUTE WAS INSTALLED, or it matches nothing, returns successfully,
    and logs 'interception LIFTED for playback' about a window that is still
    fully intercepted.

    Since 2026-08-01 that level is the PAGE, and the lift is scoped to this
    session's own tabs — a context-level lift would disarm the guard on every
    OTHER live agent tab because an unrelated task started playing a video."""
    context_browser.main_frame_id = ""          # force the Playwright-route path
    session = await BrowserSession.open({"youtube.com"})
    await session.enter_playback_mode(reload=False)
    assert session.page.unroute_calls == [("**/*", session._intercept)]
    assert context_browser.unroute_calls == [], (
        "lifting at the context would disarm every other tab's guard"
    )


async def test_playback_lifts_only_this_sessions_tabs(context_browser):
    """SAFETY. One tab finishing and handing off to the user must not leave
    another session's running browse with an unguarded browser."""
    context_browser.main_frame_id = ""
    playing = await BrowserSession.open({"youtube.com"})
    working = await BrowserSession.open({"example.com"})

    await playing.enter_playback_mode(reload=False)

    assert playing.page.unroute_calls, "the finished tab lifts its own guard"
    assert working.page.unroute_calls == [], "the running tab keeps its guard"
    assert working._read_only is True


async def test_a_superseded_tabs_guard_handles_are_forgotten(context_browser):
    """REGRESSION (found in review, 2026-08-01). Adopting a popup CLOSES the tab
    it supersedes, and the route installed on that tab dies with it — but the
    session went on holding the handle. `_unroute_intercept` walks those handles
    at playback, a dead one raises, and the caller catches that as "could not
    lift": a session that had ever followed a popup would silently run the
    DEGRADED path for the rest of its life."""
    context_browser.main_frame_id = ""      # force the page-route path
    session = await BrowserSession.open({"example.com"})
    original = session.page
    popup = FakePage(url="https://example.com/apply")

    await session._adopt_new_page(popup)

    assert original not in session._routed_pages, "the dead tab's route is forgotten"
    assert popup in session._routed_pages

    # The lift now touches only live tabs, so it actually performs.
    async def _dead(pattern, handler=None):
        raise RuntimeError("Target page, context or browser has been closed")

    original.unroute = _dead
    await session.enter_playback_mode(reload=False)
    assert popup.unroute_calls == [("**/*", session._intercept)]


async def test_playback_never_claims_a_lift_it_could_not_perform(context_browser):
    """A no-op that reports success is worse than a failure, because nothing
    ever looks at it again. When the route cannot be lifted, the honest DEGRADED
    path must run rather than a silent 'success'."""
    context_browser.main_frame_id = ""
    session = await BrowserSession.open({"youtube.com"})

    async def _cannot_unroute(pattern, handler=None):
        raise RuntimeError("this page cannot unroute")

    session.page.unroute = _cannot_unroute
    await session.enter_playback_mode(reload=False)   # must not raise
    # The degraded fallback still relaxes Rule 1, so the player keeps working.
    assert session._read_only is False
    assert (
        await _verdict(
            session, url="https://youtube.com/api", method="POST", navigation=True
        )
        == "continue"
    )


async def test_goto_resolves_each_host_once_through_the_cached_guard(
    fake_browser, monkeypatch
):
    """goto used to call the SYNCHRONOUS _validate_url, whose SSRF check does a
    blocking socket.getaddrinfo — on the single browser loop, which is also the
    loop that answers every request paused in Chromium. Same rule, same helper,
    now reached through the interceptor's process-global cache and single
    flight, so a second navigation to a known host costs a dict lookup."""
    seen = []

    def _probe(host):
        seen.append(host)
        return False

    monkeypatch.setattr(browser_session, "_host_is_blocked", _probe)
    browser_session.reset_host_cache()
    session = await _session(allowlist={"example.com"})
    await session.goto("https://example.com/a")
    await session.goto("https://example.com/b")
    assert seen == ["example.com"]


async def test_goto_still_refuses_a_private_address_through_the_cache(
    fake_browser, monkeypatch
):
    """The guard is unchanged — only where it resolves moved."""
    monkeypatch.setattr(browser_session, "_host_is_blocked", lambda h: h == "internal")
    browser_session.reset_host_cache()
    session = await _session(allowlist={"internal"})
    with pytest.raises(BrowserBlocked, match="local/private"):
        await session.goto("http://internal/admin")


async def test_a_refused_offsite_hop_still_offers_the_origin_pause(context_browser):
    """THE REGRESSION THIS CHANGE ALMOST SHIPPED, frozen. Playwright's route
    handlers do NOT re-fire on redirect hops — `_verify_landing` is what judged
    an off-site 302, and it records `last_redirect_offsite` so the loop can offer
    an APPROVABLE origin pause (a legit Apply flow redirecting to an ATS is
    approvable; an ad is deniable). Driving Fetch ourselves DOES fire per hop, so
    Rule 3 now aborts the redirect before it can land — and MEASURED on a real
    youtu.be -> youtube.com 302, that silently lost the pause and left a dead
    navigation. Rule 3 must record the destination itself."""
    session = await BrowserSession.open({"youtu.be"})
    cdp = context_browser.cdp_sessions[0]
    assert await cdp.fire(
        url="https://www.youtube.com/watch?v=x",
        resource_type="Document",
        request_id="HOP",
    ) == "abort:Failed"
    assert session.last_redirect_offsite == {
        "host": "www.youtube.com",
        "url": "https://www.youtube.com/watch?v=x",
    }


async def test_a_refused_redirect_is_reported_in_our_words_not_chromiums(fake_browser):
    """Chromium reports a refused navigation as a bare `net::ERR_FAILED` — its
    words for OUR decision. The counter moving across the goto is what tells us
    the guard caused it, and the wording matches `_verify_landing` exactly so the
    loop cannot tell which layer caught the same event."""

    async def _refuse_after_blocking(url, **kwargs):
        session.stats.blocked_navigations += 1        # what the interceptor did
        session.last_redirect_offsite = {"host": "www.youtube.com", "url": url}
        raise RuntimeError("net::ERR_FAILED at https://youtu.be/x")

    session = await _session(allowlist={"youtu.be"})
    fake_browser.page.goto = _refuse_after_blocking
    with pytest.raises(BrowserBlocked, match="redirected to 'www.youtube.com'"):
        await session.goto("https://youtu.be/x")


async def test_a_real_network_failure_is_still_reported_as_one(fake_browser):
    """The counter is what separates the two — an ERR_FAILED with no refusal
    behind it must not be dressed up as a policy decision."""

    async def _dead(url, **kwargs):
        raise RuntimeError("net::ERR_CONNECTION_REFUSED at https://example.com")

    session = await _session(allowlist={"example.com"})
    fake_browser.page.goto = _dead
    with pytest.raises(browser_session.BrowserUnreachable, match="refused the connection"):
        await session.goto("https://example.com/x")


# ===================================================================
# A TAB COMING BACK FROM PLAYBACK (2026-08-01, the add-to-cart incident)
# ===================================================================
# Live chain, read out of backend.log and the run traces: "open junaidjamshed.com"
# was handed to the user by enter_playback_mode (interceptor OFF, _read_only
# False), and 90 seconds later an add-to-cart flow REUSED that same tab. The
# reuse path restored the allowlist and the history and nothing else, so an
# autonomous commit ran with Rules 1, 2 and 3 all stood down — the approved POST
# went out unobserved, the cart really changed, and the tool reported that
# nothing had been sent.


async def test_a_tab_returning_from_playback_is_re_armed(context_browser):
    """The fix in one assertion: Fetch is re-enabled with the SAME patterns and
    read-only is restored, so the tab is guarded again before anything drives
    it."""
    session = await BrowserSession.open({"example.com"})
    cdp = context_browser.cdp_sessions[0]

    await session.enter_playback_mode(reload=False)
    assert "Fetch.disable" in cdp.methods
    assert session._read_only is False

    assert await session.resume_agent_control() is True
    assert session._read_only is True
    enables = [p for m, p in cdp.sent if m == "Fetch.enable"]
    assert len(enables) == 2, "the re-arm must actually re-enable Fetch"
    assert enables[0] == enables[1], "a narrower re-arm would silently stop guarding"


async def test_a_re_armed_tab_blocks_the_mutation_it_had_stopped_blocking(
    context_browser,
):
    """Not the flag, the BEHAVIOUR. Rule 1 must really be back on: an
    unapproved form-POST navigation dies again, which is the guarantee the
    incident lost."""
    session = await BrowserSession.open({"example.com"})
    await session.enter_playback_mode(reload=False)
    await session.resume_agent_control()

    assert await _verdict(
        session,
        url="https://example.com/cart/add",
        method="POST",
        navigation=True,
    ) == "abort"


async def test_re_arming_does_not_double_register_the_handler(context_browser):
    """Calling _install_interception again would open a SECOND CDP session and a
    SECOND handler on one target, so every paused request would be answered
    twice. The re-arm reuses what is already recorded."""
    session = await BrowserSession.open({"example.com"})
    await session.enter_playback_mode(reload=False)
    await session.resume_agent_control()
    assert len(context_browser.cdp_sessions) == 1
    assert len(session._cdp_sessions) == 1


async def test_a_tab_that_cannot_be_re_armed_is_not_driven(context_browser):
    """Fail CLOSED. If the interceptor cannot be put back, the answer is "this
    tab is not ours to drive" — never "drive it anyway"."""
    session = await BrowserSession.open({"example.com"})
    await session.enter_playback_mode(reload=False)

    cdp = context_browser.cdp_sessions[0]

    async def _dead(method, params=None):
        raise RuntimeError("Target closed")

    cdp.send = _dead
    assert await session.resume_agent_control() is False
    assert session._read_only is False, "a refused re-arm must not claim the tab"


async def test_resume_is_a_cheap_no_op_on_a_tab_that_never_played(context_browser):
    """The common path pays nothing: an ordinary tab has no interception to put
    back, so nothing is re-sent."""
    session = await BrowserSession.open({"example.com"})
    before = len(context_browser.cdp_sessions[0].sent)
    assert await session.resume_agent_control() is True
    assert len(context_browser.cdp_sessions[0].sent) == before
    assert session._read_only is True


# ---------------------------------------------- closing is the user's call
async def test_release_closes_a_tab_we_opened(context_browser):
    """The real method, not a fake's mirror of it. A session we launched owns
    its window and still cleans it up."""
    session = await BrowserSession.open({"example.com"})
    assert session.tab_reused is False
    await session.release_after_run()
    # Its tab is gone, and with it the last-tab context.
    assert context_browser.closed is True


async def test_release_leaves_a_tab_we_inherited(context_browser):
    """User report 2026-08-01: "it closed the tab, which it shouldn't have —
    that power should be to me." """
    session = await BrowserSession.open({"example.com"})
    session.tab_reused = True
    await session.release_after_run()
    assert context_browser.closed is False, "the user's window survives the run"


async def test_disarm_drops_an_unconsumed_permit(context_browser):
    """Only FIRING consumes the one-shot permit, so a submit the site never
    issued leaves it armed. Harmless while the window always closed straight
    afterwards; not harmless now that a borrowed tab outlives the run."""
    session = await BrowserSession.open({"example.com"})
    session.arm_commit("POST", "https://example.com/cart/add")
    assert session._armed_commit is not None

    session.disarm_commit()

    assert session._armed_commit is None
    assert await _verdict(
        session,
        url="https://example.com/cart/add",
        method="POST",
        navigation=True,
    ) == "abort"
