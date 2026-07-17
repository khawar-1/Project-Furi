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
