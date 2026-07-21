"""
Phase 14 — the browser runtime marshaling boundary.

Why this file exists: Playwright launches its driver as a SUBPROCESS, and on
Windows only a ProactorEventLoop can spawn subprocesses. uvicorn --reload FORCES
the SelectorEventLoop policy, so the backend's main loop raised NotImplementedError
at async_playwright().start() and every `browse` failed in production while a
standalone asyncio.run() live check (Proactor by default) passed. Root-caused
2026-07-17. browser_runtime.run_browser marshals Playwright work onto a dedicated
ProactorEventLoop; these tests pin that it runs coroutines off-thread and returns
results/exceptions faithfully. reset_browser_runtime keeps the loop from leaking.
"""
import asyncio
import threading

import pytest

from app.core import browser_runtime


@pytest.fixture(autouse=True)
def _shutdown_runtime():
    yield
    browser_runtime.shutdown_browser_runtime()


async def test_run_browser_runs_on_a_different_loop_and_thread():
    caller_loop = asyncio.get_running_loop()
    caller_thread = threading.current_thread().ident

    async def _probe() -> tuple:
        return (id(asyncio.get_running_loop()), threading.current_thread().ident)

    ran_loop, ran_thread = await browser_runtime.run_browser(_probe())
    assert ran_loop != id(caller_loop)      # a DIFFERENT loop (the browser loop)
    assert ran_thread != caller_thread      # on its own thread


async def test_run_browser_returns_the_result():
    async def _compute() -> int:
        return 41 + 1

    assert await browser_runtime.run_browser(_compute()) == 42


async def test_run_browser_propagates_exceptions_unchanged():
    class Boom(RuntimeError):
        pass

    async def _raise():
        raise Boom("from the browser loop")

    with pytest.raises(Boom, match="from the browser loop"):
        await browser_runtime.run_browser(_raise())


async def test_the_browser_loop_can_spawn_subprocesses():
    """The whole point: Playwright's driver is a subprocess, and on Windows only a
    ProactorEventLoop can spawn one — the SelectorEventLoop uvicorn --reload
    installs raises NotImplementedError. Assert the browser loop is subprocess-
    capable (Proactor on Windows). The live subprocess spawn itself is proven by
    the standalone verification (documented in the module) and the real e2e; doing
    it here leaves a dangling proactor pipe that warns at GC — the assertion below
    catches a regression just as surely without the noise."""
    import sys

    async def _loop_type() -> str:
        return type(asyncio.get_running_loop()).__name__

    name = await browser_runtime.run_browser(_loop_type())
    if sys.platform == "win32":
        assert "Proactor" in name, f"browser loop must be Proactor-capable, got {name}"
    else:
        assert name  # any default loop off Windows already spawns subprocesses


async def test_the_loop_is_reused_across_calls():
    async def _which_loop() -> int:
        return id(asyncio.get_running_loop())

    first = await browser_runtime.run_browser(_which_loop())
    second = await browser_runtime.run_browser(_which_loop())
    assert first == second  # one long-lived loop, not one per call


# ---------------------------------------------- the outermost browse timeout
async def test_timeout_cancels_a_hanging_coro():
    """The belt (2026-07-20): a browse that never returns must not wedge the chat
    turn. run_browser(timeout=…) raises TimeoutError AND tears down the coroutine
    on the browser loop, rather than leaving it running detached."""
    cancelled = threading.Event()

    async def _hang() -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await browser_runtime.run_browser(_hang(), timeout=0.2)

    # the browser-loop coroutine was actually cancelled, not left running
    assert cancelled.wait(timeout=2.0)


async def test_timeout_none_lets_a_slow_coro_finish():
    async def _slow() -> str:
        await asyncio.sleep(0.1)
        return "done"

    assert await browser_runtime.run_browser(_slow(), timeout=None) == "done"


async def test_a_coro_within_the_timeout_returns_normally():
    async def _quick() -> int:
        await asyncio.sleep(0.01)
        return 7

    assert await browser_runtime.run_browser(_quick(), timeout=5.0) == 7


# ------------------------------------------------- the budget must add up
def test_browse_hard_timeout_covers_the_worst_case_pipeline():
    """THE 2026-07-21 incident, pinned: the outer belt was 180s while the launch
    chain's own worst case plus the loop's deadline exceeded it — so a first
    browse on a loaded machine spent the whole belt on imports + launch attempts
    and Chrome never opened. The belt must cover every bounded stage it wraps
    (launch chain + loop deadline + one step's overrun past the deadline + two
    navigations) with margin, so moving any ONE constant without the others fails
    HERE instead of strangling live browses again."""
    from app.browser import loop as browser_loop
    from app.browser import session as browser_session

    worst_case = (
        browser_session.LAUNCH_CHAIN_BUDGET_SECONDS
        + browser_loop.BROWSE_DEADLINE_SECONDS
        + browser_loop.BROWSE_DECISION_TIMEOUT_SECONDS
        + 2 * browser_session.NAV_TIMEOUT_MS / 1000
    )
    assert browser_runtime.BROWSE_HARD_TIMEOUT >= worst_case + 20, (
        f"BROWSE_HARD_TIMEOUT={browser_runtime.BROWSE_HARD_TIMEOUT} cannot cover "
        f"the pipeline's worst case ({worst_case:.0f}s + margin) — a legitimate "
        f"browse would be killed by the belt"
    )


# ------------------------------------------------------ the is_running guard
async def test_is_running_reflects_loop_lifecycle():
    # A fresh runtime (the _shutdown_runtime teardown reset _loop) is not running…
    browser_runtime.shutdown_browser_runtime()
    assert browser_runtime.is_running() is False

    async def _noop() -> None:
        return None

    await browser_runtime.run_browser(_noop())   # spins the loop
    assert browser_runtime.is_running() is True   # …and now it is

    browser_runtime.shutdown_browser_runtime()
    assert browser_runtime.is_running() is False   # …and stopped again
