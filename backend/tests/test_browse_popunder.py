"""The 2026-08-07 round-3 incident: a pop-under killed a run that had already won.

Live, twice, on `play latest ep of latest season of bleach on anikoto`. The
catalog work from round 2 did its job — both runs reached the airing cour and
dispatched `navigate .../the-calamity-752db/ep-2`, the correct final action.
Then, 164ms into that navigation:

    18:56:59.488  browse: latest-episode navigation -> .../ep-2
    18:56:59.652  browser: following a new tab as the active page
    18:57:03.231  browse failed: Page.evaluate: Execution context was destroyed

Two defects, and BOTH had to be fixed — either alone leaves the run dying:

  D1  `_adopt_new_page` took over a tab that had said nothing yet (about:blank,
      the shape a pop-under arrives in and equally the shape a legitimate
      target=_blank link arrives in) and CLOSED the page we were driving to do
      it. The promise-keeping and the destruction were in the wrong order.

  D2  `observe()`'s top-level evaluate was unguarded, so a page navigating mid
      read — which `session._await_readiness` has always documented as normal —
      escaped to the user as "The browser task failed".

⚠️ WHAT IS *NOT* NEW, because the user's report said it was: ads were being
triggered before any of this round's work. The 14:28 run of the SAME goal, on
the pre-round-1 code, adopted an ad twice (`Access popular coupons and cash
back`) and spent ~180s of a 391s run navigating back. What round 2 changed is
the TIMING — the take-over now lands inside a navigation instead of between
steps — which turned a slow recovery into a hard failure. The trigger is the
site's; the fatality was ours.
"""

from __future__ import annotations

import asyncio

import pytest

from app.browser import observe as dom_observe
from app.browser import session as browser_session
from app.browser.session import BrowserSession

from tests.test_browser_session import FakePage, fake_browser  # noqa: F401

pytestmark = pytest.mark.asyncio


AD_URL = "https://moonlighthathel.org/lander?sid=1"   # the live run's own ad host
GOOD_URL = "https://example.com/apply"                # a legitimate target=_blank


async def _session(allowlist={"example.com"}):
    return await BrowserSession.open(allowlist)


async def _settle_deferred(session, ticks: int = 60):
    """Let the deferred take-over poll run. The poll sleeps ADOPT_POLL_MS, so a
    handful of event-loop turns is not enough — sleep real time, briefly."""
    for _ in range(ticks):
        await asyncio.sleep(0.01)
        if not session._deferred_adopts:
            return


def _cancel(session):
    for task in list(session._deferred_adopts):
        task.cancel()


# --------------------------------------------------------------------- D1
async def test_the_incident_a_blank_popunder_never_costs_us_the_page(fake_browser):
    """THE INCIDENT, frozen at its mechanism. A pop-under opens blank while the
    real page is mid-navigation. Before the fix the blank tab became
    `session.page` and the anikoto page was CLOSED — after it, the page we are
    driving is untouched and still ours."""
    session = await _session()
    real = session.page
    popunder = FakePage(url="about:blank")

    await session._adopt_new_page(popunder)

    assert session.page is real, "the page we were driving must still be the active page"
    assert real.closed is False, "and it must not have been closed"
    _cancel(session)


async def test_a_blank_tab_that_lands_somewhere_allowed_is_still_followed(fake_browser):
    """THE REGRESSION GUARD, and the reason 'unknown' is not just a soft refusal.
    A `target=_blank` link — the WeWorkRemotely 'Apply' case the popup follow was
    built for — also opens blank. It must still be followed once it says where it
    went, or this fix trades one broken flow for another."""
    session = await _session()
    original = session.page
    tab = FakePage(url="about:blank")

    await session._adopt_new_page(tab)
    assert session.page is original, "not yet — it has not landed"

    tab.url = GOOD_URL
    await _settle_deferred(session)

    assert session.page is tab, "once it lands somewhere allowed, we follow it"
    assert original.closed is True, "and the page it replaces is closed, as before"


async def test_a_blank_tab_that_lands_on_an_ad_is_closed_and_we_stay_put(fake_browser):
    """The pop-under's real trajectory: blank, then an off-allowlist host. The
    decision is made on that real URL, which is the whole point of waiting."""
    session = await _session()
    real = session.page
    popunder = FakePage(url="about:blank")

    await session._adopt_new_page(popunder)
    popunder.url = AD_URL
    await _settle_deferred(session)

    assert session.page is real, "we never leave the page we are driving"
    assert real.closed is False
    assert popunder.closed is True, "the ad tab is closed, not left on screen"
    assert session.stats.blocked_ads == 1, "and it is counted, so a run's ad pressure shows"


async def test_a_tab_that_never_says_where_it_is_going_is_left_alone(fake_browser):
    """Rule 3 aborts the ad's own navigation, so the tab often just stays blank
    forever. Leaving it is deliberate: closing a tab we cannot classify is the
    same act-on-incomplete-information mistake this change removes, and it costs
    only an idle tab that close_all reclaims."""
    session = await _session()
    real = session.page
    stuck = FakePage(url="about:blank")

    monkey = browser_session.ADOPT_RESOLVE_MS
    browser_session.ADOPT_RESOLVE_MS = 120
    try:
        await session._adopt_new_page(stuck)
        await _settle_deferred(session)
    finally:
        browser_session.ADOPT_RESOLVE_MS = monkey

    assert session.page is real
    assert stuck.closed is False, "not closed — we could not classify it"
    assert session.stats.blocked_ads == 0, "and not counted as an ad either"


async def test_a_blank_tab_is_guarded_from_its_first_request(fake_browser):
    """WAITING IS ONLY SAFE BECAUSE THE GUARD DOES NOT WAIT. If the interceptor
    went on at take-over time, an un-adopted ad tab would load for real — every
    request out, no SSRF check, no Rule 3. The guard is installed the moment the
    tab appears, whether or not we ever drive it."""
    session = await _session()
    tab = FakePage(url="about:blank")

    await session._adopt_new_page(tab)

    guarded = bool(tab.routes) or any(p is tab for p, _ in session._cdp_sessions)
    assert guarded, "the tab must carry this session's interceptor while we wait"
    _cancel(session)


async def test_a_tab_already_on_an_ad_is_still_refused_outright(fake_browser):
    """Round 1's guard is untouched: a tab that has ALREADY landed off-allowlist
    needs no waiting — it has answered the question."""
    session = await _session()
    real = session.page
    ad = FakePage(url=AD_URL)

    await session._adopt_new_page(ad)

    assert session.page is real
    assert ad.closed is True
    assert session.stats.blocked_ads == 1
    assert not session._deferred_adopts, "nothing to defer — the verdict was certain"


async def test_may_adopt_reads_the_verdict_rather_than_repeating_it(fake_browser):
    """One rule, one implementation. A predicate kept in two places is this
    codebase's most-recorded hole, and `_may_adopt` is public enough to have
    grown its own copy."""
    session = await _session()
    assert session._adopt_verdict(FakePage(url="about:blank")) == "unknown"
    assert session._adopt_verdict(FakePage(url=GOOD_URL)) == "allow"
    assert session._adopt_verdict(FakePage(url=AD_URL)) == "refuse"

    assert session._may_adopt(FakePage(url="about:blank")) is True
    assert session._may_adopt(FakePage(url=GOOD_URL)) is True
    assert session._may_adopt(FakePage(url=AD_URL)) is False


async def test_closing_the_session_cancels_a_pending_take_over(fake_browser):
    """A decision that lands after teardown would swap `self.page` to a tab on a
    context that is going away, and close a page the window has reclaimed."""
    session = await _session()
    tab = FakePage(url="about:blank")
    await session._adopt_new_page(tab)
    pending = list(session._deferred_adopts)
    assert pending, "the take-over is genuinely deferred"

    await session.close()

    assert not session._deferred_adopts
    # cancel() only REQUESTS it; the task reaches the cancelled state when the
    # loop next runs it. Give it that turn before asking.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert all(t.cancelled() or t.done() for t in pending)


# --------------------------------------------------------------------- D2
class _NavigatingPage:
    """A page whose first evaluate dies the way Playwright's does when the page
    navigates mid-read, and whose second succeeds."""

    def __init__(self, failures: int = 1, payload: dict | None = None):
        self.url = "https://example.com/watch/ep-2"
        self.failures = failures
        self.calls = 0
        self.payload = payload if payload is not None else {
            "url": self.url, "title": "Episode 2", "text": "", "elements": [], "total": 0,
        }

    async def evaluate(self, expression, *args):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError(
                "Page.evaluate: Execution context was destroyed, most likely "
                "because of a navigation"
            )
        return self.payload

    @property
    def frames(self):
        return []


async def test_the_incident_a_navigation_mid_observe_is_survived():
    """THE SECOND HALF OF THE INCIDENT, frozen. This exact exception ended both
    live runs. `session._await_readiness` has always treated it as normal ("a
    navigation mid-poll destroys the execution context. That is normal (a
    redirect), not an error") — observe() propagated it instead, and it reached
    the user as a failed browse on a run that had already found the answer."""
    page = _NavigatingPage(failures=1)

    obs = await dom_observe.observe(page)

    assert page.calls == 2, "it retried exactly once"
    assert obs.title == "Episode 2", "and returned the real observation"


async def test_a_page_that_is_genuinely_dead_still_says_so():
    """The bound is what keeps the retry honest. Retrying on ANY exception is
    deliberate — the driver's wording is not a contract — so the SECOND failure
    must propagate, or a genuinely broken page becomes invisible."""
    page = _NavigatingPage(failures=2)

    with pytest.raises(RuntimeError, match="Execution context was destroyed"):
        await dom_observe.observe(page)

    assert page.calls == 2, "exactly one retry, not a loop"


async def test_a_healthy_page_costs_nothing_extra():
    """The common path must not pay for the guard."""
    page = _NavigatingPage(failures=0)
    await dom_observe.observe(page)
    assert page.calls == 1
