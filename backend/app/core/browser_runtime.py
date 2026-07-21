"""
Jarvis OS — Browser runtime (Phase 14)

Why this module exists
----------------------
Playwright launches its Node driver as a SUBPROCESS. On Windows, only the
ProactorEventLoop can spawn subprocesses; the SelectorEventLoop raises
NotImplementedError from asyncio.create_subprocess_exec.

uvicorn, when run with --reload (or --workers) — i.e. `use_subprocess=True` —
FORCES the Windows SelectorEventLoop policy (venv/.../uvicorn/loops/asyncio.py:
`asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())`). That
is exactly the mode `npm run dev` uses. So the backend's MAIN loop cannot launch
Playwright, and every `browse` died in production with

    NotImplementedError   (at async_playwright().start())

while passing a standalone `asyncio.run()` live check — which is Proactor by
default. Root-caused 2026-07-17 after a live "it didn't work" report.

Why a whole dedicated loop, not a policy tweak
----------------------------------------------
The policy uvicorn sets is applied AFTER this module imports and it OWNS the main
loop; we cannot swap the main loop out from under uvicorn. Instead, every
Playwright operation runs on a PRIVATE ProactorEventLoop in a dedicated daemon
thread — the voice `_TTS_EXECUTOR` precedent (one long-lived worker so a
loop-bound resource is only ever touched from the thread that created it).

Playwright objects are loop-bound: a page created on this loop must be observed,
clicked, and closed on THIS loop too. So an entire browser session's life —
launch → navigate → observe → act → close, and even the LLM decision calls the
loop makes along the way — runs inside ONE coroutine marshaled here with
run_browser(). The marshaling boundary is the tool's execute() and the
/api/browser routes; nothing inside a marshaled coroutine calls run_browser again
(no nesting, so no self-deadlock).

Everything that is NOT Playwright stays on the main loop: push() touches
WebSocket objects bound to the main loop, and the cached LLM provider's httpx
client is bound to the main loop — the browser coroutine builds its OWN provider
(factory.build_provider) so its client binds to this loop instead.
"""
from __future__ import annotations

import asyncio
import sys
import threading
from typing import Any, Coroutine

from loguru import logger

_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_start_lock = threading.Lock()

# The OUTERMOST browse timeout (2026-07-20), owned here at the one marshaling
# boundary every browse tool crosses. No single browse may wedge a chat turn
# forever, whatever hangs inside (a locked-profile launch, a stuck LLM call, a
# non-terminating loop). A continuous browse is bounded by the loop's own
# MAX_BROWSER_ACTIONS and every pausing flow RETURNS promptly, so a generous cap
# is only ever reached by a genuine hang. Callers pass it to run_browser(timeout=).
BROWSE_HARD_TIMEOUT = 180.0


def _new_loop() -> asyncio.AbstractEventLoop:
    # Windows: the Proactor loop is the ONLY one that can spawn subprocesses,
    # which Playwright's driver needs. Everywhere else a plain new loop already
    # supports subprocesses, so this is a no-op difference off Windows.
    if sys.platform == "win32":
        return asyncio.ProactorEventLoop()
    return asyncio.new_event_loop()


def _ensure_loop() -> asyncio.AbstractEventLoop:
    """Start (once) and return the dedicated browser loop. Idempotent and
    thread-safe — the first browse spins the thread, later ones reuse it."""
    global _loop, _thread
    with _start_lock:
        if _loop is not None and not _loop.is_closed():
            return _loop
        loop = _new_loop()

        def _run() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(
            target=_run, name="jarvis-browser-loop", daemon=True
        )
        thread.start()
        _loop, _thread = loop, thread
        logger.info("browser runtime: dedicated Proactor loop started")
        return loop


def is_running() -> bool:
    """True when the dedicated browser loop has been started and is live. Lets
    shutdown skip marshaling window-cleanup onto a loop that was never spun up
    (a session with no browse ever run has no windows to close)."""
    return _loop is not None and not _loop.is_closed()


async def run_browser(
    coro: Coroutine[Any, Any, Any], *, timeout: float | None = None
) -> Any:
    """Run a Playwright-touching coroutine on the dedicated browser loop and
    return its result to the caller's loop. The ONE marshaling boundary: every
    caller that opens/drives/closes a BrowserSession goes through here, so
    Playwright is only ever driven from the loop that launched it.

    Exceptions raised inside `coro` propagate to the caller unchanged (a
    BrowserUnavailable stays a BrowserUnavailable). Awaited from within the
    caller's running loop, so it never blocks that loop.

    `timeout` (seconds) is the OUTERMOST belt: no browse may wedge a chat turn
    forever, whatever hangs inside (a locked-profile launch, a stuck LLM call, a
    non-terminating loop). On expiry the browser-loop coroutine is cancelled and
    asyncio.TimeoutError propagates — the tool boundary turns it into a clean
    failure. Legitimate pausing flows (sign-in hand-off, per-form approval)
    RETURN promptly, so a generous cap never truncates real work."""
    loop = _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        if timeout is not None:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout)
        return await asyncio.wrap_future(future)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        # wait_for already cancelled the wrapped future on timeout, but cancel the
        # cross-thread Future too so the coroutine on the browser loop is torn down
        # (closing its half-open browser) rather than left running detached.
        future.cancel()
        raise


def shutdown_browser_runtime() -> None:
    """Stop the browser loop and join its thread. Best-effort — for process
    shutdown. The thread is a daemon, so a missed shutdown never blocks exit."""
    global _loop, _thread
    with _start_lock:
        loop, thread = _loop, _thread
        _loop, _thread = None, None
    if loop is None:
        return
    try:
        loop.call_soon_threadsafe(loop.stop)
    except Exception as exc:
        logger.debug(f"browser runtime stop: {type(exc).__name__}: {exc}")
    if thread is not None:
        thread.join(timeout=5)
    try:
        loop.close()
    except Exception as exc:
        logger.debug(f"browser runtime close: {type(exc).__name__}: {exc}")
