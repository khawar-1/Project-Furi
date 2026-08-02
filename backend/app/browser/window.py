"""THE SHARED BROWSER WINDOW — one persistent context, many tabs.

WHY THIS EXISTS. `BrowserSession.open()` used to call `_launch()`, which does a
full `launch_persistent_context` on ~/.jarvis/browser — a whole *browser* per
session — and `close()` closed that context. Chromium permits exactly one live
context per profile (the single-instance lock), so "one profile = one live
context" had to be enforced at every launch site: each browse first closed the
sign-in window, the result window, the media session AND the held agent window,
then launched again. That teardown is what the user sees as Chrome closing and
reopening, and it is why Jarvis could only ever have ONE tab.

The fix is one level up from a fix this stack already made. `ensure_playwright_driver`
turned the Node driver from per-session into a startup singleton for exactly this
reason (a cold spawn inside a chat turn). The persistent CONTEXT is the next
level: launched once, reused, outliving any individual session — and each
BrowserSession owns a PAGE in it. `_RealBrowser` already wrapped
(playwright, context) and already exposed new_page(); the seam was there.

OWNERSHIP, stated once because two modules used to disagree about it:
- This module owns the CONTEXT and the set of live tabs. Nothing else closes a
  context.
- A BrowserSession owns its PAGE. `session.close()` releases the tab here; the
  context goes down only when the last tab does.
- `_mark_profile_released()` therefore fires HERE, on a real context close, and
  nowhere else. Stamping it per tab-close would make every following launch pay a
  spurious `_PROFILE_SETTLE_SECONDS` for a lock that was never released.

LIVENESS IS PROVEN BY USE, NOT BY A PROBE. A separate "is the context alive?"
round-trip is both slower and weaker than the operation we are about to perform
anyway: `new_page()` raises on a dead context. So `open_tab()` attempts it and,
on failure, drops the handle and relaunches ONCE. That also covers the case the
old single-slot code needed an explicit probe for — the user closing the window
by hand.

EVENT-LOOP DISCIPLINE. Same rule as registry.HeldSessionRegistry: in production
every call runs on the ONE dedicated browser loop, but the hermetic suite drives
these from a fresh loop per test, and an asyncio.Lock binds to the first loop
that awaits it and then raises "bound to a different event loop" forever. The
lock is (re)created whenever the running loop differs from the one it was made
on — safe precisely because that only happens when the previous loop is gone.

IMPORTS. The launch primitives stay in `session` (BROWSER_FACTORY,
_PROFILE_REAPER and friends are monkeypatched by ~100 tests through the
`app.core.browser_session` shim, which self-replaces to that module — moving them
would silently disarm the fixture that stops the suite launching a real Chromium
against the user's own profile). This module imports them lazily, inside the
functions, so the patches are read at call time and the package has no cycle.
"""
from typing import Any, Optional

import asyncio

from loguru import logger

# The one live persistent context (a `_RealBrowser`), or None.
_context: Optional[Any] = None
# Every BrowserSession with a live page on `_context`, in creation order. This IS
# the refcount — a separate integer would drift the first time a close path was
# missed, and the tab registry (Phase 3) needs the identities anyway.
_tabs: list[Any] = []

_lock: Optional[asyncio.Lock] = None
_lock_loop: Optional[asyncio.AbstractEventLoop] = None

# The session currently being DRIVEN by an agent run, if any. A tab that appears
# with no opener (see _dispatch_new_page) belongs to whoever is acting — nobody
# else is clicking anything. Set/cleared around a run by the browse lock.
_driving: Optional[Any] = None
# Non-zero while WE are deliberately creating a page. Chromium fires the context
# 'page' event for a `new_page()` of our own, and the dispatcher must not be
# deciding who owns a tab the caller has not finished claiming.
#
# HONEST ABOUT WHICH GUARD CARRIES THE WEIGHT (measured, by reverting each):
# claim-at-birth — rule 0 in _owner_of_new_page — is what actually closes this,
# because `open_tab` assigns `session.page` and appends to `_tabs` with NO await
# after `new_page()` returns, so the scheduled dispatch cannot run until the tab
# is already claimed. This counter is the belt: it removes the dependence on that
# no-await window, which a later refactor could quietly widen.
_creating: int = 0
# Strong references to in-flight dispatch tasks — a dropped task is a popup
# nobody ever adopts (the _inflight rule in session._install_cdp_interception).
_dispatches: set = set()


def _get_lock() -> asyncio.Lock:
    global _lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock is None or _lock_loop is not loop:
        _lock = asyncio.Lock()
        _lock_loop = loop
    return _lock


async def _maybe_await(value: Any) -> Any:
    """Await a value if it is awaitable (fakes are often plain functions)."""
    if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
        return await value
    return value


async def _close_quietly(target: Any, what: str) -> None:
    """Best-effort close of a page/context handle. A half-dead handle raising
    here must never break a teardown — the caller is already unwinding."""
    if target is None:
        return
    closer = getattr(target, "close", None)
    if not callable(closer):
        return
    try:
        await _maybe_await(closer())
    except Exception as exc:
        logger.debug(f"window: {what} close: {type(exc).__name__}: {exc}")


async def _launch_context() -> Any:
    """Launch a fresh persistent context, settling the profile lock first."""
    from app.browser import session as _session

    # ONE PROFILE, ONE CHROMIUM — but only against EXTERNAL processes now. A
    # sign-in window and the clean media window are separate Chrome processes on
    # the same user-data-dir and still hold the single-instance lock, so they
    # must go before we launch. What no longer needs closing is everything that
    # is merely another TAB (a result window, a held media session, another
    # task's browse) — closing those was the whole flicker.
    #
    # Here rather than at each call site deliberately: the launch sites used to
    # hand-list what to close, and a hand-maintained list is the bug this
    # codebase has already had to unpick twice (see registry.close_all_held).
    try:
        await _session.close_login_window()
        await _session.stop_media_window()
    except Exception as exc:
        logger.debug(f"window: pre-launch sweep: {type(exc).__name__}: {exc}")
    # If a Chromium on this profile was closed moments ago it may still hold the
    # single-instance lock; launching into that races the dying process (the
    # TargetClosedError churn, 2026-07-19). No-op when nothing closed recently.
    await _session._settle_profile()
    browser = await _session._launch()
    # ONE listener for the whole window, not one per session. With a context per
    # session it did not matter; sharing one, N per-session listeners would each
    # run the same ownership decision on every new tab, and any divergence
    # between them is a cross-tab bug. Deciding in one place is the point.
    hook = getattr(browser, "on_page", None)
    if callable(hook):
        try:
            hook(_on_context_page)
        except Exception as exc:
            logger.debug(f"window: page listener: {type(exc).__name__}: {exc}")
    return browser


def set_driving(session: Optional[Any]) -> None:
    """Mark the session an agent run is currently driving (or None). Used to
    attribute a tab that opens with no opener — see _dispatch_new_page — and to
    keep a tab in use from being evicted for room."""
    global _driving
    _driving = session


_run_lock: Optional[asyncio.Lock] = None
_run_lock_loop: Optional[asyncio.AbstractEventLoop] = None


def _get_run_lock() -> asyncio.Lock:
    global _run_lock, _run_lock_loop
    loop = asyncio.get_running_loop()
    if _run_lock is None or _run_lock_loop is not loop:
        _run_lock = asyncio.Lock()
        _run_lock_loop = loop
    return _run_lock


class driving_run:
    """ONE agent run drives the browser at a time; a second QUEUES.

    ⚠️ THIS IS NOT NEW SERIALIZATION — IT IS SERIALIZATION THAT WAS ACCIDENTAL
    AND IS NOW DELIBERATE. `run_browser` marshals every browse onto one loop but
    never held a lock, so two DELEGATE'd background browse tasks could always
    interleave as coroutines. What hid it was the teardown: each run closed
    everything and launched its own context, so a second run starting mid-flight
    destroyed the first one's browser and the failure looked like a crash rather
    than a race. Removing that teardown is exactly what would let the race show,
    so the lock ships in the same change.

    Queued, not rejected: the second task waits, then opens its own tab. Also
    marks the driven tab, which is what keeps it from being evicted for room and
    what attributes a `rel="noopener"` tab to the run that caused it.

    Used as `async with driving_run(): ...` around the whole run, including
    acquiring the tab — two runs racing to acquire is the interleaving.
    """

    def __init__(self) -> None:
        self._lock: Optional[asyncio.Lock] = None

    async def __aenter__(self) -> "driving_run":
        self._lock = _get_run_lock()
        if self._lock.locked():
            logger.info("browser: another browse is running — queueing behind it")
        await self._lock.acquire()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        set_driving(None)
        if self._lock is not None and self._lock.locked():
            self._lock.release()
        self._lock = None


def _on_context_page(page: Any) -> None:
    """Context 'page' event — a tab just opened. Playwright dispatches this
    synchronously on the browser loop, so schedule the async decision. Never
    raises into the dispatch."""
    if _creating:
        # A `new_page()` of our own: open_tab is about to hand it to its session.
        return
    try:
        task = asyncio.ensure_future(_dispatch_new_page(page))
        _dispatches.add(task)
        task.add_done_callback(_dispatches.discard)
    except Exception as exc:
        logger.debug(f"window: popup schedule: {type(exc).__name__}: {exc}")


async def _owner_of_new_page(page: Any) -> tuple[Optional[Any], bool]:
    """Decide which session a newly-opened tab belongs to.

    Returns (owner, close_it). This is THE ownership decision, and it is the
    reason a shared context is safe: before it, `_adopt_new_page` adopted any
    popup whose opener was None — and a `context.new_page()` has a null opener,
    so one session would have taken the tab another session had just created for
    itself and closed its own page out from under its run.

    The rules, in order:
      0. Already some session's page → claimed, nothing to do.
      1. Opener is a session's page → that session. (Our own tab opened it.)
      2. No opener at all → whoever is DRIVING, else the only tab if there is
         exactly one. `rel="noopener"` and `target=_blank` links genuinely
         produce a null opener, and that is the WeWorkRemotely case the popup
         follow exists for — refusing to attribute those would reintroduce the
         2026-07-18 bug where the loop kept reading the page it had left.
      3. Opener is a page nobody owns → a stray (an ad window); close it.
      4. Otherwise → leave it strictly alone. Never close another tab.
    """
    if any(t.page is page for t in _tabs):
        return None, False

    opener = None
    get_opener = getattr(page, "opener", None)
    if callable(get_opener):
        try:
            opener = await _maybe_await(get_opener())
        except Exception as exc:
            logger.debug(f"window: opener check: {type(exc).__name__}: {exc}")

    if opener is not None:
        for tab in _tabs:
            if tab.page is opener:
                return tab, False
        return None, True

    if _driving is not None and any(t is _driving for t in _tabs):
        return _driving, False
    if len(_tabs) == 1:
        return _tabs[0], False
    return None, False


async def _dispatch_new_page(page: Any) -> None:
    """Give a newly-opened tab to its owning session, or dispose of a stray.
    Best-effort — a popup must never break a running browse."""
    try:
        owner, close_it = await _owner_of_new_page(page)
        if close_it:
            await _close_quietly(page, "stray popup")
            logger.info("window: closed an unrelated popup (opened by no tab of ours)")
            return
        if owner is None:
            return
        await owner._adopt_new_page(page)
    except Exception as exc:
        logger.debug(f"window: popup dispatch: {type(exc).__name__}: {exc}")


async def _discard_context_locked() -> None:
    """Drop the current context and everything on it. Caller holds the lock."""
    from app.browser import session as _session

    global _context
    browser, _context = _context, None
    _tabs.clear()
    await _close_quietly(browser, "context")
    # THE PROFILE LOCK IS RELEASED HERE AND ONLY HERE. `_RealBrowser.close()`
    # stamps it too (it covers a handle closed outside the window), but this is
    # the semantically correct place: the stamp means "a Chromium on the shared
    # profile just let go", which is true of a CONTEXT close and false of a tab
    # close. Stamping per tab would make every following launch sleep out
    # _PROFILE_SETTLE_SECONDS waiting for a lock nobody was holding.
    _session._mark_profile_released()


async def open_tab(session: Any) -> None:
    """Open a tab in the shared window and hand it to `session`.

    Assigns `session._browser` and `session.page` and registers the session as a
    live tab. The assignment happens HERE, rather than the caller wiring it up
    afterwards, so a tab can never exist unregistered: the page and the record of
    it are created under one hold of the lock, and there is no window in which a
    concurrent close_all() would miss a page that is already open.

    The session is the registered identity, NOT the page — `_adopt_new_page`
    swaps `session.page` when the agent follows a popup, so a page-keyed record
    would go stale exactly when a tab is most active.

    Relaunches once if the held context turns out to be dead: the user closing
    the window by hand is an ordinary event, not a failure.
    """
    async with _get_lock():
        global _context, _creating
        for attempt in (1, 2):
            if _context is None:
                _context = await _launch_context()
            try:
                # Chromium fires the context 'page' event for this creation too.
                # Suppress the dispatcher for its duration: we already know whose
                # tab it is, and without this the event would race the
                # `session.page = page` assignment below and hand a brand-new
                # empty tab to whichever session happens to be driving.
                _creating += 1
                try:
                    page = await _context.new_page()
                finally:
                    _creating -= 1
            except Exception as exc:
                if attempt == 2:
                    raise
                logger.info(
                    "window: the shared browser context is gone "
                    f"({type(exc).__name__}) — relaunching"
                )
                await _discard_context_locked()
                continue
            session._browser = _context
            session.page = page
            _tabs.append(session)
            logger.info(f"window: opened a tab ({len(_tabs)} open)")
            return
    raise RuntimeError("unreachable: open_tab exhausted its attempts")


async def release_tab(session: Any) -> None:
    """Close `session`'s page and, when it was the last tab, the context itself.

    Idempotent: releasing a session that is not registered still closes its page,
    because a caller unwinding an error may not know how far it got.
    """
    global _driving
    async with _get_lock():
        if _driving is session:
            _driving = None
        try:
            _tabs.remove(session)
        except ValueError:
            pass
        await _close_quietly(getattr(session, "page", None), "page")
        if _tabs:
            logger.info(f"window: closed a tab ({len(_tabs)} still open)")
            return
        if _context is not None:
            logger.info("window: last tab closed — closing the shared window")
            await _discard_context_locked()


async def close_all() -> bool:
    """Close every tab and the context. Returns whether anything was actually
    closed — a held session counts, not just a tab, so a caller can report
    honestly when the only thing open was (say) a submitted-form result window.

    Shutdown, and the paths that genuinely need the profile free (a sign-in
    window is a separate Chrome process on the same user-data-dir and cannot
    coexist with the context).

    Closes each SESSION rather than just its page, and clears the held slots
    first: a slot left pointing at a session whose page we had closed underneath
    it would go on reporting media that is no longer playing, or offer a commit
    that can no longer be submitted.

    ⚠️ Takes the lock only at the END. `session.close()` calls back into
    `release_tab`, which takes the same lock, and an asyncio.Lock is not
    reentrant — holding it across those calls would deadlock the browser loop.
    """
    from app.browser import registry as _held

    global _driving
    _driving = None
    closed_any = bool(_tabs) or any(
        reg.peek() is not None for reg in _held.REGISTRIES.values()
    )
    try:
        await _held.close_all_held()
    except Exception as exc:
        logger.debug(f"window: close held: {type(exc).__name__}: {exc}")
    for session in list(_tabs):
        try:
            await session.close()
        except Exception as exc:
            logger.debug(f"window: close tab: {type(exc).__name__}: {exc}")
    async with _get_lock():
        for session in list(_tabs):  # anything a failing close left behind
            await _close_quietly(getattr(session, "page", None), "page")
        closed_any = closed_any or _context is not None
        await _discard_context_locked()
    return closed_any


def current_context() -> Optional[Any]:
    """The live context handle, or None. Cheap and lock-free — for wiring a
    context-level listener and for status reads."""
    return _context


def tab_count() -> int:
    """How many tabs are open on the shared window."""
    return len(_tabs)


def live_tabs() -> list[Any]:
    """A copy of the live sessions, creation order."""
    return list(_tabs)


def owns(session: Any) -> bool:
    """Whether `session` is a registered tab of the shared window."""
    return any(t is session for t in _tabs)


def is_driving(session: Any) -> bool:
    """Whether an agent run is currently driving this tab. Such a tab is never
    evicted to make room, and a tab that opens with no opener belongs to it."""
    return _driving is session and session is not None


def track_for_tests(session: Any) -> None:
    """Register a session the SUITE created without going through open_tab.

    Test-only, and named so it cannot be mistaken for production API (the
    reset_for_tests convention). Tests that fake `BrowserSession.open` are faking
    the very thing that creates tabs, so their fakes must register too — a fake
    that quietly did not would leave the tab registry empty and make every
    multi-tab assertion vacuous."""
    if not any(t is session for t in _tabs):
        _tabs.append(session)


def reset_for_tests() -> None:
    """Drop the context and every tab WITHOUT closing (fakes only) — the
    conftest between-tests hygiene, mirroring registry.reset_for_tests. Without
    this a fake context from one test would be reused by the next."""
    global _context, _lock, _lock_loop, _driving, _creating
    global _run_lock, _run_lock_loop
    _context = None
    _tabs.clear()
    _dispatches.clear()
    _driving = None
    _creating = 0
    _lock = None
    _lock_loop = None
    _run_lock = None
    _run_lock_loop = None
