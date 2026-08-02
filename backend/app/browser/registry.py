"""ONE generic held-session registry — the five copy-pasted (session, meta,
lock) triples in browser_session collapsed into a single class.

WHY THIS EXISTS. The browser stack holds a live BrowserSession across a pause
in five distinct situations — a media window left playing, a submitted-form
result window left open, a commit awaiting signature approval, an embedded
CAPTCHA awaiting the user's solve, and a commit discovery paused on a
fill/origin/auth question. Each grew its own module-global triple with
structurally identical register/take/discard/peek helpers, and the two
"close everything" aggregators (shutdown_browser_windows, reset_media) each
hand-listed a DIFFERENT incomplete subset of them: shutdown missed the commit
and challenge slots — leaking exactly the orphaned Chromium holding the
~/.jarvis/browser profile lock that the reclaim machinery was built to kill —
and reset_media missed discovery. A hand-maintained list of slots is the bug;
a table the aggregate iterates is the fix: close_all_held() covers every slot
BY CONSTRUCTION, and a newly added slot cannot be forgotten anywhere.

OWNERSHIP RULES (unchanged from the originals, now stated once):
- hold() transfers the session's lifetime to the registry — the caller must
  NOT close it afterwards. Holding closes any previous occupant (one slot).
- take() removes AND returns the session — the caller owns it thereafter; a
  taken session can never be taken twice.
- discard() is take()+close: cancel / shutdown / a superseding discovery.
- peek() is cheap and lock-free (meta only, never the session) so main-loop
  API routes and the StatusBar can poll it freely (the active_media rule).
- Memory-only BY DESIGN: a running Chromium page is not serializable, and
  pretending otherwise is where double-submit lives. A restart drops every
  slot — the honest outcome, since nothing was persisted to silently resume.

EVENT-LOOP DISCIPLINE. In production every mutating call runs on the ONE
dedicated browser loop (browser_runtime), so a plain asyncio.Lock would do —
but the hermetic suite drives these functions from a fresh event loop per
test, and an asyncio.Lock binds to the first loop that awaits it, then raises
"bound to a different event loop" forever after. The lock is therefore
(re)created whenever the running loop differs from the one it was created on.
Rebinding is safe exactly because it only ever happens when the previous
loop is gone (tests run sequentially) or never existed; on the long-lived
browser loop the branch never fires after the first call.
"""
from typing import Any, Optional

import asyncio
from loguru import logger


class HeldSessionRegistry:
    """One slot holding a live browser session across a pause. See the module
    docstring for the ownership rules."""

    def __init__(self, slot: str) -> None:
        self.slot = slot
        self._session: Optional[Any] = None
        self._meta: dict[str, Any] = {}
        self._lock: Optional[asyncio.Lock] = None
        self._lock_loop: Optional[asyncio.AbstractEventLoop] = None

    def _get_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    async def hold(self, session: Any, meta: dict[str, Any]) -> None:
        """Adopt `session` as THE held session for this slot, closing any
        previous occupant. The registry owns its lifetime from here."""
        async with self._get_lock():
            previous = self._session
            self._session = session
            self._meta = dict(meta or {})
        if previous is not None and previous is not session:
            await previous.close()

    async def take(self) -> Optional[Any]:
        """Remove and return the held session (the caller owns it now), or
        None — a restart/timeout dropped it, and the caller must report that
        rather than invent a resumption."""
        async with self._get_lock():
            session = self._session
            self._session = None
            self._meta = {}
        return session

    async def discard(self) -> bool:
        """Close and clear without acting on the session (cancel / shutdown /
        a superseding hold). True when one was actually closed. Idempotent —
        discarding nothing is not an error."""
        session = await self.take()
        if session is None:
            return False
        await session.close()
        return True

    def peek(self) -> Optional[dict[str, Any]]:
        """The held session's meta, or None. Cheap, no I/O, no lock — safe to
        poll from any loop (the meta dict is replaced whole, never mutated)."""
        if self._session is None:
            return None
        return dict(self._meta)

    def clear_nowait(self) -> None:
        """Drop the slot WITHOUT closing — test hygiene only (the suite holds
        inert fakes; production teardown must use discard())."""
        self._session = None
        self._meta = {}


# The slot table. close_all_held()/reset_for_tests() iterate THIS — adding a
# slot here is the whole registration step, and every aggregate covers it.
# The "browse" slot is deliberately GONE (2026-08-01). It held THE one agent
# window, which is precisely why a second browser task had to close it; agent
# tabs now live in app/browser/window.py, keyed by site and bounded by
# MAX_BROWSE_TABS. What remains here are the states a tab can be SUSPENDED in,
# which is what a one-at-a-time slot models correctly.
REGISTRIES: dict[str, HeldSessionRegistry] = {
    slot: HeldSessionRegistry(slot)
    for slot in ("media", "result_window", "commit", "challenge", "discovery")
}


def is_held(session: Any) -> bool:
    """Whether `session` occupies ANY slot — i.e. it is a tab the user is
    mid-something with: awaiting a signature approval, solving a CAPTCHA,
    answering a discovery question, watching media, reading a result page.

    DERIVED, never a flag on the session. Tab eviction must not close such a tab
    (a pending approval whose window vanished can only report that it expired),
    and a boolean someone has to remember to set is exactly the hand-maintained
    state this module exists to delete — it iterates the table, so a new slot is
    covered here by construction."""
    return any(reg._session is session for reg in REGISTRIES.values())


async def close_all_held() -> None:
    """Discard every held session, every slot, best-effort — one failing close
    never blocks the rest. The ONE aggregate teardown (shutdown, reset, and
    'one profile = one live context' pre-launch sweeps all route here)."""
    for reg in REGISTRIES.values():
        try:
            await reg.discard()
        except Exception as exc:
            logger.debug(
                f"close_all_held [{reg.slot}]: {type(exc).__name__}: {exc}"
            )


def reset_for_tests() -> None:
    """Synchronously drop every slot without closing (fakes only — the
    conftest between-tests hygiene, mirroring reset_host_cache)."""
    for reg in REGISTRIES.values():
        reg.clear_nowait()
