"""The shared Playwright driver singleton (the 2026-07-22 "processing forever"
fix).

The Node driver is spawned ONCE and reused across browses, not per session — so
the cold subprocess spawn (and any stall) happens at startup in the background,
never inside a user's chat turn. These tests pin that the singleton is
idempotent, that a STALLED spawn becomes a bounded, retryable failure (the exact
incident — 98s of silence at async_playwright().start()) rather than an endless
hang, that an abandoned-but-late driver is stopped not leaked, and that
restart-on-death re-warms.

The autouse `_hermetic_browser_session` conftest fixture points _PLAYWRIGHT_STARTER
at a refuser and clears _shared_playwright between tests, so nothing here ever
spawns a real driver; each test injects its own fake starter.
"""
import asyncio

import pytest

from app.core import browser_session
from app.core.browser_session import BrowserUnavailable


class _FakeDriver:
    def __init__(self) -> None:
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


async def test_ensure_starts_once_and_is_idempotent(monkeypatch):
    starts = 0
    driver = _FakeDriver()

    async def _starter():
        nonlocal starts
        starts += 1
        return driver

    monkeypatch.setattr(browser_session, "_PLAYWRIGHT_STARTER", _starter)
    a = await browser_session.ensure_playwright_driver()
    b = await browser_session.ensure_playwright_driver()
    assert a is driver and b is driver
    assert starts == 1  # spawned once, reused across browses


async def test_concurrent_ensure_starts_once(monkeypatch):
    """Two browses racing the very first ensure must not spawn two drivers — the
    per-loop lock serializes them and the second reuses the first's driver."""
    starts = 0
    driver = _FakeDriver()

    async def _starter():
        nonlocal starts
        starts += 1
        await asyncio.sleep(0.02)  # overlap the two callers
        return driver

    monkeypatch.setattr(browser_session, "_PLAYWRIGHT_STARTER", _starter)
    a, b = await asyncio.gather(
        browser_session.ensure_playwright_driver(),
        browser_session.ensure_playwright_driver(),
    )
    assert a is driver and b is driver
    assert starts == 1


async def test_a_stalled_start_is_bounded_and_retryable(monkeypatch):
    """The incident: async_playwright().start() hung and the 30s guard never fired
    (asyncio.wait_for awaited an uncancellable spawn). ensure_ must RETURN a named
    BrowserUnavailable within DRIVER_START_TIMEOUT — never hang — cache nothing, and
    leave the singleton usable for a later start."""
    monkeypatch.setattr(browser_session, "DRIVER_START_TIMEOUT_SECONDS", 0.1)
    started = asyncio.Event()

    async def _stalled_starter():
        started.set()
        await asyncio.Event().wait()  # never resolves (the stall)

    monkeypatch.setattr(browser_session, "_PLAYWRIGHT_STARTER", _stalled_starter)
    # The whole point: this returns, it does not hang. The outer 2s wait_for is the
    # test's own safety net — a regression (an unbounded ensure) trips it loudly.
    with pytest.raises(BrowserUnavailable, match="did not start in time"):
        await asyncio.wait_for(browser_session.ensure_playwright_driver(), timeout=2)
    assert started.is_set()  # it really tried
    assert browser_session._shared_playwright is None  # nothing cached

    # Cancel the abandoned (detached) stalled start so the loop teardown is clean.
    for t in list(browser_session._DETACHED_STARTS):
        t.cancel()

    # A later start still works — the stall did not poison the singleton.
    driver = _FakeDriver()

    async def _ok_starter():
        return driver

    monkeypatch.setattr(browser_session, "_PLAYWRIGHT_STARTER", _ok_starter)
    assert await browser_session.ensure_playwright_driver() is driver


async def test_a_stray_late_driver_is_stopped_not_leaked(monkeypatch):
    """A start we ABANDONED on timeout that COMPLETES a moment later must have its
    stray driver stopped, never leaked as an orphaned Node process."""
    monkeypatch.setattr(browser_session, "DRIVER_START_TIMEOUT_SECONDS", 0.05)
    gate = asyncio.Event()
    driver = _FakeDriver()

    async def _slow_starter():
        await gate.wait()  # completes only once we release it
        return driver

    monkeypatch.setattr(browser_session, "_PLAYWRIGHT_STARTER", _slow_starter)
    with pytest.raises(BrowserUnavailable):
        await browser_session.ensure_playwright_driver()
    gate.set()  # the abandoned start now finishes
    await asyncio.sleep(0.05)  # let the stray-stop done-callback run
    assert driver.stopped is True


async def test_reset_forces_a_fresh_start(monkeypatch):
    """restart-on-death: after reset_playwright_driver() the next ensure_ starts a
    brand-new driver."""
    a, b = _FakeDriver(), _FakeDriver()
    seq = [a, b]

    async def _starter():
        return seq.pop(0)

    monkeypatch.setattr(browser_session, "_PLAYWRIGHT_STARTER", _starter)
    assert await browser_session.ensure_playwright_driver() is a
    browser_session.reset_playwright_driver()
    assert await browser_session.ensure_playwright_driver() is b


async def test_stop_playwright_driver_stops_and_clears(monkeypatch):
    driver = _FakeDriver()

    async def _starter():
        return driver

    monkeypatch.setattr(browser_session, "_PLAYWRIGHT_STARTER", _starter)
    await browser_session.ensure_playwright_driver()
    await browser_session.stop_playwright_driver()
    assert driver.stopped is True
    assert browser_session._shared_playwright is None
    await browser_session.stop_playwright_driver()  # idempotent — no raise, no re-stop


async def test_realbrowser_close_keeps_the_shared_driver(monkeypatch):
    """A session close tears down only its persistent CONTEXT — the shared driver
    survives (stopping it per browse is the per-turn cold spawn this design removes)."""

    class _Ctx:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    ctx = _Ctx()
    driver = _FakeDriver()
    rb = browser_session._RealBrowser(driver, ctx)
    await rb.close()
    assert ctx.closed is True
    assert driver.stopped is False  # the shared driver is not stopped by a session close


async def test_a_missing_playwright_install_fails_clean(monkeypatch):
    """The default starter turns a missing Playwright into a clean BrowserUnavailable
    (a base install without the optional dep), surfaced through ensure_."""

    async def _import_error_starter():
        raise BrowserUnavailable(
            "Browser control needs Playwright, which is not installed."
        )

    monkeypatch.setattr(browser_session, "_PLAYWRIGHT_STARTER", _import_error_starter)
    with pytest.raises(BrowserUnavailable, match="not installed"):
        await browser_session.ensure_playwright_driver()
    assert browser_session._shared_playwright is None
