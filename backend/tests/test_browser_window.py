"""
The shared browser window — one persistent context, many tabs (2026-08-01).

THE DEFECT THESE PIN. `BrowserSession.open()` did a full
`launch_persistent_context` per session and `close()` closed that context.
Chromium allows one live context per profile, so every launch site first closed
the sign-in window, the result window, the media session and the held agent
window — the Chrome-closes-and-reopens flicker, and the reason Jarvis could only
ever hold ONE tab. The context is now a singleton that outlives sessions; a
session is a tab in it.

Every behavioural test here was run against the pre-change session.py and FAILS
there (the 2026-07-30 rule: a regression test that passes on the broken code is
not a regression test).
"""
import pytest

from app.browser import window
from app.browser.session import BrowserSession
from app.core import browser_session


# --------------------------------------------------------------- fake pieces
class FakePage:
    """One tab. Distinct per new_page() — the whole point is that tabs differ."""

    def __init__(self, n: int) -> None:
        self.n = n
        self.url = "about:blank"
        self.closed = False
        self.routes: list = []

    async def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    async def unroute(self, pattern, handler):
        self.routes = [r for r in self.routes if r != (pattern, handler)]

    async def evaluate(self, expression, *args):
        if self.closed:
            raise RuntimeError("Target page, context or browser has been closed")
        return 1

    def on(self, event, handler):
        pass

    async def close(self):
        self.closed = True


class FakeContext:
    """A persistent context: mints a NEW page per new_page(), like the real one.

    No `new_cdp_session`, so `_install_cdp_interception` declines and the session
    falls through to the routing path — which is what we want under test: the
    slower path is the one with the sharing hazards.
    """

    def __init__(self, name: str = "ctx") -> None:
        self.name = name
        self.pages: list[FakePage] = []
        self.closed = False
        self.page_listener = None
        self.fail_new_page = False

    async def new_page(self):
        if self.closed or self.fail_new_page:
            raise RuntimeError("Target page, context or browser has been closed")
        page = FakePage(len(self.pages) + 1)
        self.pages.append(page)
        # REAL CHROMIUM FIRES THE 'page' EVENT FOR THIS CREATION TOO, and a fake
        # that stayed quiet would hide the whole race: the dispatcher would be
        # deciding who owns a tab that the caller has not yet assigned to its
        # session. Modelling it is what makes the _creating guard testable.
        if self.page_listener is not None:
            self.page_listener(page)
        return page

    async def route(self, pattern, handler):
        pass

    async def unroute(self, pattern, handler):
        pass

    def on_page(self, callback):
        self.page_listener = callback

    async def close(self):
        self.closed = True
        for page in self.pages:
            page.closed = True


@pytest.fixture
def contexts(monkeypatch):
    """Records every context the factory is asked to launch. len() == how many
    times a real browser would have been launched."""
    made: list[FakeContext] = []

    def _factory():
        ctx = FakeContext(f"ctx{len(made) + 1}")
        made.append(ctx)
        return ctx

    monkeypatch.setattr(browser_session, "BROWSER_FACTORY", _factory)
    return made


# ------------------------------------------------------------ one context
async def test_two_sessions_share_one_browser_context(contexts):
    """THE HEADLINE. Two browses = two tabs in ONE window, not two browsers.
    Pre-change this launched twice — and could not have, since the second launch
    would have hit the profile's single-instance lock."""
    a = await BrowserSession.open({"example.com"})
    b = await BrowserSession.open({"other.com"})

    assert len(contexts) == 1, "a second session must not launch a second browser"
    assert a._browser is b._browser
    assert a.page is not b.page, "each session must get its OWN tab"
    assert window.tab_count() == 2


async def test_each_tab_keeps_its_own_allowlist(contexts):
    """Sharing a context must not share the origin guard. Rule 3 is per-session,
    and a tab judged by another tab's allowlist is a safety inversion."""
    a = await BrowserSession.open({"example.com"})
    b = await BrowserSession.open({"other.com"})

    assert a.origin_allowed("example.com") and not a.origin_allowed("other.com")
    assert b.origin_allowed("other.com") and not b.origin_allowed("example.com")


# ------------------------------------------------------- opening a tab safely
async def test_opening_a_tab_never_hands_it_to_another_session(contexts):
    """⚠️ THE RACE A SHARED CONTEXT CREATES. Chromium fires the context 'page'
    event when B's tab is created — BEFORE `open_tab` has assigned it to B. The
    dispatcher must not be deciding ownership in that gap: pre-change,
    `_adopt_new_page` adopted any tab whose opener was null, and a
    `context.new_page()` has a null opener, so A would have seized B's brand-new
    tab and closed its own page out from under a running task."""
    import asyncio

    a = await BrowserSession.open({"example.com"})
    a_page = a.page

    b = await BrowserSession.open({"other.com"})
    await asyncio.sleep(0)  # let any (wrongly) scheduled dispatch run

    assert a.page is a_page, "A must keep its own page"
    assert not a_page.closed, "A's page must not be closed as 'superseded'"
    assert b.page is not a_page
    assert window.tab_count() == 2


async def test_the_first_tab_is_not_dispatched_to_itself(contexts):
    """The same guard on the single-tab path: a session's own opening tab must
    not go round the popup-adoption machinery."""
    import asyncio

    a = await BrowserSession.open({"example.com"})
    await asyncio.sleep(0)
    assert a.page is contexts[0].pages[0]
    assert not a.page.closed


# ------------------------------------------------------------ tab lifetime
async def test_closing_one_tab_leaves_the_others_open(contexts):
    """The behaviour the user asked for: finishing one browser task must not
    close the window out from under the others."""
    a = await BrowserSession.open({"example.com"})
    b = await BrowserSession.open({"other.com"})
    b_page = b.page

    await a.close()

    assert a.page.closed, "the finished tab's page closes"
    assert not b_page.closed, "the other tab stays open"
    assert not contexts[0].closed, "the window stays open"
    assert window.tab_count() == 1


async def test_closing_the_last_tab_closes_the_window(contexts):
    """Nothing is left holding the profile lock once the last tab goes."""
    a = await BrowserSession.open({"example.com"})
    b = await BrowserSession.open({"other.com"})

    await a.close()
    await b.close()

    assert contexts[0].closed
    assert window.tab_count() == 0
    assert window.current_context() is None


async def test_releasing_a_tab_twice_is_harmless(contexts):
    """A caller unwinding an error may not know how far it got."""
    a = await BrowserSession.open({"example.com"})
    await a.close()
    await a.close()
    assert window.tab_count() == 0


# ---------------------------------------------------- the profile-lock stamp
async def test_a_tab_close_does_not_stamp_the_profile_release(contexts):
    """REGRESSION. The stamp means 'a Chromium on the shared profile let go',
    which is true of a context close and FALSE of a tab close. Stamping per tab
    would make every following launch sleep out _PROFILE_SETTLE_SECONDS waiting
    for a lock nobody held."""
    a = await BrowserSession.open({"example.com"})
    await BrowserSession.open({"other.com"})
    browser_session._profile_released_monotonic = 0.0

    await a.close()

    assert browser_session._profile_released_monotonic == 0.0


async def test_closing_the_window_does_stamp_the_profile_release(contexts):
    """The other half: a real context close must still settle the next launch."""
    a = await BrowserSession.open({"example.com"})
    browser_session._profile_released_monotonic = 0.0

    await a.close()

    assert browser_session._profile_released_monotonic > 0.0


# ------------------------------------------------------------ dead contexts
async def test_a_dead_window_is_relaunched_for_the_next_tab(contexts):
    """The user closing the window by hand is an ordinary event. Liveness is
    proven by USE — new_page() raises on a dead context — so there is no separate
    probe to go stale."""
    await BrowserSession.open({"example.com"})
    contexts[0].fail_new_page = True

    b = await BrowserSession.open({"other.com"})

    assert len(contexts) == 2, "a dead context must be replaced, not reused"
    assert b._browser is contexts[1]
    assert window.tab_count() == 1, "the tabs of a dead window are gone with it"


async def test_a_window_that_cannot_be_relaunched_raises(monkeypatch):
    """One retry, not an infinite loop: a browser that will never open must
    surface as a failure the tool can report, not spin."""
    launched: list[FakeContext] = []

    def _broken_factory():
        ctx = FakeContext(f"broken{len(launched) + 1}")
        ctx.fail_new_page = True
        launched.append(ctx)
        return ctx

    monkeypatch.setattr(browser_session, "BROWSER_FACTORY", _broken_factory)

    with pytest.raises(Exception):
        await BrowserSession.open({"example.com"})

    assert len(launched) == 2, "exactly one retry — not zero, and not a loop"


# ------------------------------------------------------------ ownership
async def test_a_directly_constructed_session_still_closes_its_own_browser():
    """OWNERSHIP. A session the window did not open owns its handle and closes
    it — exactly as it did before the window existed. This is what keeps every
    hand-wired test session (and any caller holding its own handle) correct."""
    ctx = FakeContext()
    page = FakePage(1)
    session = BrowserSession(ctx, page, {"example.com"})

    assert not window.owns(session)
    await session.close()

    assert ctx.closed, "an unowned session closes the handle it was given"


async def test_a_failed_guard_release_only_takes_down_its_own_tab(contexts, monkeypatch):
    """A session failing to arm its interceptor must not take the other tabs'
    windows down with it — that would be the pre-change teardown by another
    name."""
    a = await BrowserSession.open({"example.com"})

    async def _boom(self, page):
        raise RuntimeError("could not arm the guard")

    monkeypatch.setattr(BrowserSession, "_install_interception", _boom)
    with pytest.raises(RuntimeError):
        await BrowserSession.open({"other.com"})

    assert not contexts[0].closed, "the window survives one tab failing to arm"
    assert not a.page.closed
    assert window.tab_count() == 1


# ------------------------------------------------------------ aggregate close
async def test_close_all_closes_every_tab_and_the_window(contexts):
    """The paths that genuinely need the profile free (a sign-in window is a
    separate Chrome process on the same user-data-dir) get one call that covers
    every tab BY CONSTRUCTION."""
    a = await BrowserSession.open({"example.com"})
    b = await BrowserSession.open({"other.com"})

    await window.close_all()

    assert a.page.closed and b.page.closed
    assert contexts[0].closed
    assert window.tab_count() == 0
    assert window.current_context() is None


async def test_close_all_on_an_empty_window_is_a_no_op(contexts):
    await window.close_all()
    assert len(contexts) == 0, "closing nothing must not launch anything"


# ------------------------------------------------ site keying and the LRU cap
async def test_the_same_site_reuses_its_tab(contexts):
    """Continuity, now per site: a follow-up about a site lands in that site's
    tab — same page, real history, no relaunch."""
    first, reused_first = await browser_session.acquire_browse_tab(
        {"books.toscrape.com"}, site="toscrape.com"
    )
    again, reused_again = await browser_session.acquire_browse_tab(
        {"books.toscrape.com"}, site="toscrape.com"
    )

    assert reused_first is False and reused_again is True
    assert again is first
    assert window.tab_count() == 1


async def test_a_different_site_gets_its_own_tab(contexts):
    """THE HEADLINE, at the registry level."""
    a, _ = await browser_session.acquire_browse_tab({"a.example"}, site="a.example")
    b, reused = await browser_session.acquire_browse_tab({"b.example"}, site="b.example")

    assert reused is False
    assert b is not a
    assert window.tab_count() == 2
    assert not a.page.closed, "the first site's tab stays open"


async def test_reuse_rescopes_the_allowlist_and_resets_per_run_state(contexts):
    """A reused tab is re-scoped to THIS task's grounded origins (the
    interceptor reads allowlist live) and starts with a fresh transcript."""
    first, _ = await browser_session.acquire_browse_tab(
        {"jobs.example.com"}, site="example.com"
    )
    first.browse_history = ["stale entry from an earlier task"]
    first.last_redirect_offsite = {"host": "stale.example"}

    again, reused = await browser_session.acquire_browse_tab(
        {"careers.example.com"}, site="example.com"
    )

    assert reused is True and again is first
    assert again.allowlist == {"careers.example.com"}
    assert again.browse_history == []
    assert again.last_redirect_offsite is None


async def test_a_dead_tab_is_replaced_not_reused(contexts):
    """The user closed that tab by hand — an ordinary event, not a failure."""
    first, _ = await browser_session.acquire_browse_tab({"a.example"}, site="a.example")
    first.page.closed = True   # FakePage.evaluate now raises

    again, reused = await browser_session.acquire_browse_tab(
        {"a.example"}, site="a.example"
    )

    assert reused is False
    assert again is not first
    assert window.tab_count() == 1


async def _fill_the_window():
    """MAX_BROWSE_TABS tabs, each with a DISTINCT last-used stamp.

    Stamped explicitly rather than by the clock: time.monotonic() has ~15ms
    resolution on Windows, so six tabs opened in a loop all carry the same value
    and 'least recently used' would be decided by list order instead of by the
    policy under test.
    """
    sessions = []
    for n in range(browser_session.MAX_BROWSE_TABS):
        s, _ = await browser_session.acquire_browse_tab(
            {f"s{n}.example"}, site=f"s{n}.example"
        )
        s.tab_used_monotonic = 100.0 + n     # s0 is the oldest
        sessions.append(s)
    return sessions


async def test_the_cap_closes_the_least_recently_used_tab(contexts):
    """Bounded memory and a legible window: past MAX_BROWSE_TABS the oldest idle
    tab goes, not the newest and not a random one."""
    sessions = await _fill_the_window()
    assert window.tab_count() == browser_session.MAX_BROWSE_TABS

    # Touch the oldest so it is no longer the LRU — the SECOND tab now is.
    sessions[0].tab_used_monotonic = 999.0
    await browser_session.acquire_browse_tab({"new.example"}, site="new.example")

    assert window.tab_count() == browser_session.MAX_BROWSE_TABS
    assert sessions[1].page.closed, "the least-recently-used tab is the one that goes"
    assert not sessions[0].page.closed, "the freshly touched tab survives"


async def test_a_busy_tab_is_never_evicted(contexts):
    """⚠️ A tab awaiting a signature approval, holding a CAPTCHA, part-filled
    behind a question or playing media is a tab the user is mid-something with.
    Closing it to make room can only produce 'that expired'."""
    sessions = await _fill_the_window()

    # The oldest tab is mid-approval — held in the commit slot.
    await browser_session.hold_commit(sessions[0], state={"url": "https://s0.example"})
    await browser_session.acquire_browse_tab({"new.example"}, site="new.example")

    assert not sessions[0].page.closed, "the paused commit's tab must survive"
    assert sessions[1].page.closed, "the next-oldest idle tab goes instead"


async def test_the_tab_being_driven_is_never_evicted(contexts):
    """The other half of 'busy': the run in flight owns its tab."""
    sessions = await _fill_the_window()
    window.set_driving(sessions[0])
    try:
        await browser_session.acquire_browse_tab({"new.example"}, site="new.example")
    finally:
        window.set_driving(None)

    assert not sessions[0].page.closed, "the tab being driven must survive"
    assert sessions[1].page.closed


async def test_when_every_tab_is_busy_the_cap_yields(contexts):
    """Better a seventh tab than closing work in progress.

    Each slot holds ONE session (hold() closes the previous occupant), so every
    tab is made busy through a slot of its own, plus the one being driven."""
    sessions = await _fill_the_window()
    holders = [
        browser_session.register_media(sessions[0], title="m", url="https://s0.example"),
        browser_session.register_result_window(sessions[1], title="r", url="https://s1.example"),
        browser_session.hold_commit(sessions[2], state={"url": "https://s2.example"}),
        browser_session.hold_challenge(sessions[3], meta={"kind": "captcha"}),
        browser_session.hold_discovery(sessions[4], meta={"reason": "fill"}),
    ]
    for coro in holders:
        await coro
    window.set_driving(sessions[5])
    try:
        await browser_session.acquire_browse_tab({"new.example"}, site="new.example")
    finally:
        window.set_driving(None)

    assert window.tab_count() == browser_session.MAX_BROWSE_TABS + 1
    assert not any(s.page.closed for s in sessions), "no work in progress was closed"


async def test_site_keys_use_the_registrable_domain(contexts):
    """`www.` and `jobs.` are the same site; a public-suffix domain is not
    truncated to its suffix (the outfitters.com.pk case)."""
    key = browser_session.browse_site_key
    assert key(set(), "https://jobs.indeed.com/x") == "indeed.com"
    assert key(set(), "https://www.indeed.com/") == "indeed.com"
    assert key(set(), "https://shop.outfitters.com.pk/x") == "outfitters.com.pk"
    assert key({"books.toscrape.com"}, "") == "toscrape.com"
    assert key(set(), "") == ""


# ============================================================================
# NEVER SEIZE A TAB THE USER IS MID-SOMETHING WITH (2026-08-01, add-to-cart)
# ============================================================================
# The live chain: "open junaidjamshed.com" ended in the MEDIA slot with the
# interceptor lifted (a second defect, fixed in the loop), and the next task's
# add-to-cart flow reused that very tab — running an autonomous commit with
# Rules 1-3 stood down. `_evictable` already refused to CLOSE a held tab to make
# room; seizing one to DRIVE is the same intrusion by another route.


async def test_a_held_tab_is_never_reused_by_a_new_run(contexts):
    """THE INCIDENT, at the registry level: a tab in the media slot belongs to
    the user, so the next run for that site opens its own."""
    playing, _ = await browser_session.acquire_browse_tab(
        {"junaidjamshed.com"}, site="junaidjamshed.com"
    )
    await browser_session.register_media(playing, title="J.", url="https://x/")

    again, reused = await browser_session.acquire_browse_tab(
        {"junaidjamshed.com"}, site="junaidjamshed.com"
    )

    assert reused is False
    assert again is not playing
    assert not playing.page.closed, "the user's window is left exactly as it was"
    assert window.tab_count() == 2


async def test_a_reused_tab_is_handed_back_re_armed(contexts):
    """Second, independent guard: any tab that IS reused gets its interceptor
    put back first. Either of these alone stops the incident."""
    first, _ = await browser_session.acquire_browse_tab({"a.example"}, site="a.example")
    first._playback = True                      # as enter_playback_mode leaves it
    first._read_only = False

    again, reused = await browser_session.acquire_browse_tab(
        {"a.example"}, site="a.example"
    )

    assert reused is True and again is first
    assert again._read_only is True, "a reused tab must be guarded before it is driven"
    assert again._playback is False


async def test_a_tab_that_refuses_to_re_arm_is_left_alone_not_driven(contexts):
    """Fail closed, and without destroying the window: we open our own tab and
    leave theirs untouched."""
    first, _ = await browser_session.acquire_browse_tab({"a.example"}, site="a.example")

    async def _refuse():
        return False

    first.resume_agent_control = _refuse

    again, reused = await browser_session.acquire_browse_tab(
        {"a.example"}, site="a.example"
    )

    assert reused is False and again is not first
    assert not first.page.closed


async def test_reuse_marks_the_tab_as_inherited(contexts):
    """Only the run that OPENED a tab may close it — so the reuse path has to
    say which is which."""
    first, _ = await browser_session.acquire_browse_tab({"a.example"}, site="a.example")
    assert first.tab_reused is False, "we opened this one"

    again, _ = await browser_session.acquire_browse_tab({"a.example"}, site="a.example")
    assert again.tab_reused is True, "we inherited this one"


async def test_a_tab_released_for_a_captcha_is_reused_and_re_armed(contexts):
    """THE 2026-08-03 ROUND TRIP, end to end and with nothing set by hand.

    The CAPTCHA fix rests entirely on this: a challenge tab is RELEASED to the
    user (interception lifted so their solve is unimpeded), left in the
    site-keyed registry, and the resumed browse picks up THAT tab — re-armed —
    instead of relaunching. If reuse or the re-arm ever stopped working, the fix
    would silently become either "a second tab every time" or the 2026-08-01
    unguarded-tab incident."""
    challenged, _ = await browser_session.acquire_browse_tab(
        {"ebay.com"}, site="ebay.com"
    )
    # A second, unrelated tab: the whole point is that it survives.
    other, _ = await browser_session.acquire_browse_tab(
        {"junaidjamshed.com"}, site="junaidjamshed.com"
    )

    assert await challenged.release_to_user() is True
    assert challenged._playback is True

    resumed, reused = await browser_session.acquire_browse_tab(
        {"ebay.com"}, site="ebay.com"
    )

    assert reused is True and resumed is challenged   # same page, solved check
    assert resumed._read_only is True                 # guarded before driven
    assert resumed._playback is False
    assert not other.page.closed                      # the bystander is untouched
    assert window.tab_count() == 2                    # no third window, no relaunch
