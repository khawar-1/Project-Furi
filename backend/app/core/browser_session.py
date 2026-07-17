"""
Jarvis OS — Browser Session (Phase 14, Part 1)

A real Chromium Jarvis can drive, wrapped in the guarantee that makes driving it
safe: **in READ mode the session is structurally incapable of mutating anything.**

Why this module exists at all
----------------------------
Per-site tools (a YouTubeTool, a LinkedInTool) are weeks of work each and break
on every layout change. The universal alternative is one browser plus a loop
that reads the page and decides the next action — no site-specific code, ever.
This module is the browser half; app/agents/browser_loop.py (Part 2) is the loop.

THE SAFETY MODEL, and its honest limits
---------------------------------------
Every request the page makes passes through _intercept(). Three rules, in order:

1. NON-GET IS ABORTED — everywhere, every origin, subresources included.
   This is the guarantee. A loop that cannot issue a POST/PUT/PATCH/DELETE
   cannot submit a form, send a message, or buy anything, no matter what the
   page's text talks it into. It is what makes `browse` a READ tool that passes
   registry.execute_tool's gate untouched, and why the whole YouTube case needs
   ZERO approvals. Subresources are included deliberately and it is load-bearing:
   an SPA (LinkedIn's included) submits via a background fetch, not a form POST.

   ⚠️ BE HONEST ABOUT WHAT THIS IS. "non-GET = mutation" is an HTTP CONVENTION
   (RFC 7231 safe methods), not a substring test. It is NOT the structural proof
   that planner._recipient_violation is — that one compares an address against a
   corpus and cannot be wrong. This one is a strong bound on the dominant case.
   Sites violate the convention (GET /logout, GET /delete?id=5) and those get
   through; the origin allowlist is all that bounds them. Overclaiming this as
   "the browser recipient lock" is how a future round gets surprised — the same
   way SUMMARY_PROMPT's "never invent" turned out not to be a guarantee either.

2. BLOCKED HOSTS ARE ABORTED — reusing browser_tools._host_is_blocked, so the
   SSRF rule that governs read_webpage governs the browser too, and cannot drift
   from it. Re-checked after navigation: a click is a navigation the guard never
   saw (the read_webpage "redirect can land somewhere private" precedent).

3. MAIN-FRAME NAVIGATION IS ALLOWLISTED — origins must be grounded in the user's
   own words (see browser_grounding), never in page content. This is what bounds
   exfiltration: injected text saying "go to attacker.com/?data=<secret>" is a
   navigation, and it is refused.

   ⚠️ THE ALLOWLIST GATES NAVIGATION ONLY, NOT SUBRESOURCES, and that is a
   deliberate, documented hole rather than an oversight. YouTube serves video
   from googlevideo.com and thumbnails from ytimg.com; gating subresources by
   origin means the page does not render and the feature does not exist. A
   cross-origin GET subresource is also not agent-controlled — every page you
   have ever opened in any browser makes them, and the loop cannot choose them.
   Sub-frame navigation is treated as a subresource for the same reason (consent
   dialogs and embeds are iframes).

Everything else the module holds to
-----------------------------------
- BROWSER_FACTORY is the injectable seam (the google_services /
  HTTP_FETCH_FACTORY pattern). Default None = the real Chromium. conftest's
  autouse _hermetic_browser_session points it at a refuser, because the planner
  can splice steps of its own accord and an un-refused seam means the suite
  launches real browsers on someone's laptop.
- A SEPARATE PROFILE at ~/.jarvis/browser (the ~/.jarvis/google_token.json
  hygiene precedent), never the user's real Chrome profile. An agent steered by
  untrusted page content must not hold every cookie the user owns. They log in
  once, per origin, in the visible window.
- HEADED. Not decoration: within an allowlisted, authenticated origin a
  compromised loop has full user authority, and no guard here changes that. The
  window the user can watch is the last honest control.
- service_workers="block". A service worker serves and queues requests OUTSIDE
  page.route(), which would silently void rule 1. Day one, not later.
- Playwright is imported lazily, so a base install without it (or without
  `playwright install chromium`) fails clean rather than breaking startup — the
  rapidocr-onnxruntime precedent.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from loguru import logger

from app.tools.browser_tools import _host_is_blocked, _validate_url

# --------------------------------------------------------------------- limits
BROWSER_PROFILE_DIR = Path.home() / ".jarvis" / "browser"
NAV_TIMEOUT_MS = 20_000
SETTLE_TIMEOUT_MS = 5_000       # best-effort wait for the page to go quiet
# SPAs lazy-render their real content AFTER the network briefly goes idle, so
# networkidle can return before the elements the loop needs have painted
# (measured live 2026-07-17: a YouTube results page observed with only its header
# and tabs, the video links not yet in the DOM). A short fixed settle after
# networkidle lets that content appear. Generic — a property of client-rendered
# pages, not a YouTube special-case.
SETTLE_RENDER_SECONDS = 2.5

# RFC 7231 safe methods. TRACE is safe on paper and a known XST vector — the
# loop has no use for it, so it is not on the list.
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Non-network schemes the browser drives itself (about:blank between pages,
# data:/blob: for generated content). Not requests to anywhere — never gated.
_LOCAL_SCHEMES = frozenset({"about", "data", "blob", "chrome", "chrome-error"})

# Chromium builds to try, in order. Real Google Chrome is preferred FIRST
# (user request, 2026-07-17: "on my chrome browser, not microsoft edge"): a user
# who signs into their account expects to see Chrome, and a real Chrome channel
# also trips Google's automation detection less than bundled Chromium or Edge.
# Bundled Chromium is the fallback (the version Playwright is tested against),
# then Edge — every Windows box has it, so the feature is never dead on arrival
# when neither Chrome nor the bundled build is present. The bundled build is a
# separate ~140MB download pip does NOT perform (`playwright install chromium`),
# which is exactly why the fallbacks are load-bearing, not decoration.
#
# Which chromium runs it costs NOTHING for isolation: only the BINARY is shared.
# user_data_dir stays ~/.jarvis/browser, so none of the user's real Chrome
# cookies, logins, or history come with it — the separate-profile property the
# docstring promises is a property of the profile DIRECTORY, not of the channel.
_CHANNELS: tuple[Optional[str], ...] = ("chrome", None, "msedge")

# Launch flags shared by agent and login windows. --disable-background-networking
# trims noise the interceptor would otherwise field; the AutomationControlled
# switch drops the `navigator.webdriver` flag Google reads to refuse sign-in and
# to serve degraded pages to "a bot". It does NOT weaken any guarantee here —
# the READ-mode interceptor is what bounds the agent, not the browser's honesty
# about being scripted — it just lets a real person sign in through the window.
_LAUNCH_ARGS = [
    "--disable-background-networking",
    "--disable-blink-features=AutomationControlled",
]

# BROWSER_FACTORY() -> browser handle exposing async new_page() and close().
# None = the real Chromium path below.
BROWSER_FACTORY: Optional[Callable[[], Any]] = None


class BrowserUnavailable(RuntimeError):
    """Playwright (or its Chromium download) is not installed."""


class BrowserBlocked(RuntimeError):
    """A navigation was refused by the allowlist or the SSRF guard. Carries a
    user-facing reason — code-authored, never LLM prose."""


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value) or asyncio.isfuture(value):
        return await value
    return value


# ------------------------------------------------------- host guard (cached)
# _host_is_blocked resolves DNS. The interceptor sees EVERY request a page
# makes — dozens of images and scripts per page — so calling it uncached, on the
# event loop, would both stall the loop and re-resolve the same CDN host
# hundreds of times. Cache per host, and resolve off-loop on a miss.
_host_block_cache: dict[str, bool] = {}


async def _host_blocked_cached(host: str) -> bool:
    key = host.strip().lower().rstrip(".")
    cached = _host_block_cache.get(key)
    if cached is not None:
        return cached
    blocked = await asyncio.to_thread(_host_is_blocked, key)
    _host_block_cache[key] = blocked
    return blocked


def reset_host_cache() -> None:
    """Test/shutdown hook — the cache is a performance detail, never state."""
    _host_block_cache.clear()


def _normalize_origin(raw: str) -> str:
    """'https://www.YouTube.com/results?q=x' or 'YouTube.com' → 'youtube.com'.
    Accepts a bare host or a full URL so callers never have to care."""
    text = (raw or "").strip().lower()
    if not text:
        return ""
    if "://" in text:
        text = urlparse(text).hostname or ""
    else:
        text = text.split("/")[0]
    return text.rstrip(".")


# -------------------------------------------------------- the real Chromium
class _RealBrowser:
    """Owns both the Playwright driver and the persistent context, so close()
    tears down everything a leaked process would otherwise hold."""

    def __init__(self, playwright: Any, context: Any) -> None:
        self._playwright = playwright
        self._context = context

    async def new_page(self) -> Any:
        return await self._context.new_page()

    async def close(self) -> None:
        for shutdown in (self._context.close, self._playwright.stop):
            try:
                await shutdown()
            except Exception as exc:  # a half-dead browser must not raise here
                logger.debug(f"browser teardown: {type(exc).__name__}: {exc}")


async def _default_browser_factory() -> Any:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise BrowserUnavailable(
            "Browser control needs Playwright, which is not installed. "
            "Install it with: pip install playwright"
        ) from exc

    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    playwright = await async_playwright().start()
    errors: list[str] = []
    for channel in _CHANNELS:
        try:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(BROWSER_PROFILE_DIR),
                headless=False,           # the user watches — see the docstring
                service_workers="block",  # rule 1 is void without this
                args=list(_LAUNCH_ARGS),
                **({"channel": channel} if channel else {}),
            )
        except Exception as exc:
            errors.append(f"{channel or 'bundled chromium'}: {str(exc)[:120]}")
            continue
        context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        logger.info(f"browser: launched via {channel or 'bundled chromium'}")
        return _RealBrowser(playwright, context)

    await playwright.stop()
    raise BrowserUnavailable(
        "Could not launch a browser. Install one of Playwright's Chromium, "
        "Microsoft Edge, or Google Chrome — the simplest is: "
        "playwright install chromium\n" + "\n".join(errors)
    )


async def _launch() -> Any:
    factory = BROWSER_FACTORY
    if factory is None:
        return await _default_browser_factory()
    return await _maybe_await(factory())


# ------------------------------------------------------------- the session
@dataclass
class InterceptStats:
    """What the guard refused. Surfaced in the tool result so a page that
    misbehaves is VISIBLE rather than mysteriously broken — an aborted POST is
    a breakage, not a mutation, and the user deserves to know which."""

    blocked_mutations: int = 0
    blocked_navigations: int = 0
    blocked_hosts: int = 0
    mutation_urls: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "blocked_mutations": self.blocked_mutations,
            "blocked_navigations": self.blocked_navigations,
            "blocked_hosts": self.blocked_hosts,
            "mutation_urls": self.mutation_urls[:10],
        }


class BrowserSession:
    """One page, one allowlist, one interceptor. Read-only by construction."""

    def __init__(self, browser: Any, page: Any, allowlist: set[str]) -> None:
        self._browser = browser
        self.page = page
        self.allowlist = allowlist
        self.stats = InterceptStats()

    # ------------------------------------------------------------ lifecycle
    @classmethod
    async def open(cls, allowlist: set[str]) -> "BrowserSession":
        origins = {o for o in (_normalize_origin(a) for a in allowlist) if o}
        browser = await _launch()
        try:
            page = await browser.new_page()
            session = cls(browser, page, origins)
            await page.route("**/*", session._intercept)
        except Exception:
            await _maybe_await(browser.close())
            raise
        return session

    async def close(self) -> None:
        try:
            await _maybe_await(self._browser.close())
        except Exception as exc:
            logger.debug(f"browser close: {type(exc).__name__}: {exc}")

    async def __aenter__(self) -> "BrowserSession":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    # ------------------------------------------------------------ the guard
    def origin_allowed(self, host: Optional[str]) -> bool:
        """Exact host or a subdomain of an allowlisted registrable origin.
        'evil-youtube.com' does NOT match 'youtube.com' — the dot matters."""
        if not host:
            return False
        host = host.strip().lower().rstrip(".")
        return any(
            host == origin or host.endswith("." + origin)
            for origin in self.allowlist
        )

    async def _intercept(self, route: Any, *_ignored: Any) -> None:
        """Every request, every frame. Never raises: an exception escaping a
        route handler hangs the page's own JavaScript, so the fallback is always
        to abort (fail closed — an unrendered page beats an unguarded one)."""
        try:
            request = route.request
            url = request.url or ""
            method = (request.method or "GET").upper()
            parsed = urlparse(url)

            # Browser-internal, not a request to anywhere.
            if parsed.scheme in _LOCAL_SCHEMES:
                await route.continue_()
                return

            # RULE 1 — the guarantee. Everywhere, every origin, subresources
            # included (an SPA submits via a background fetch, not a form POST).
            if method not in _READ_METHODS:
                self.stats.blocked_mutations += 1
                if len(self.stats.mutation_urls) < 10:
                    self.stats.mutation_urls.append(f"{method} {url[:120]}")
                logger.info(f"browser: aborted {method} {url[:120]} — READ mode")
                await route.abort()
                return

            # RULE 2 — SSRF, same rule read_webpage obeys, shared not copied.
            host = parsed.hostname
            if host and await _host_blocked_cached(host):
                self.stats.blocked_hosts += 1
                logger.info(f"browser: aborted request to blocked host {host}")
                await route.abort()
                return

            # RULE 3 — allowlist, MAIN-FRAME navigation only (see docstring:
            # gating subresources by origin means the page never renders).
            if self._is_main_frame_navigation(request) and not self.origin_allowed(host):
                self.stats.blocked_navigations += 1
                logger.info(f"browser: aborted navigation to {host} — not allowlisted")
                await route.abort()
                return

            await route.continue_()
        except Exception as exc:
            logger.debug(f"browser intercept: {type(exc).__name__}: {exc}")
            try:
                await route.abort()
            except Exception:
                pass

    def _is_main_frame_navigation(self, request: Any) -> bool:
        try:
            if not request.is_navigation_request():
                return False
            frame = getattr(request, "frame", None)
            main = getattr(self.page, "main_frame", None)
            if frame is None or main is None:
                return True  # cannot tell → treat as top-level (fail closed)
            return frame == main
        except Exception:
            return True

    # ------------------------------------------------------------ navigation
    async def goto(self, url: str) -> str:
        """Navigate and return the final URL. Raises BrowserBlocked with a
        code-authored reason when the guard refuses."""
        target, error = _validate_url(url)   # http/https + SSRF, shared with read_webpage
        if error:
            raise BrowserBlocked(error)

        host = urlparse(target).hostname
        if not self.origin_allowed(host):
            raise BrowserBlocked(
                f"Refusing to open '{host}': it is not one of the sites this "
                f"task is allowed to visit ({', '.join(sorted(self.allowlist)) or 'none'})."
            )

        await self.page.goto(target, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        return await self._verify_landing()

    async def _verify_landing(self) -> str:
        """Re-check where we ACTUALLY ended up. The interceptor sees each
        redirect hop, but this is the read_webpage precedent restated: the final
        URL is the one that matters, and it is cheap to be sure."""
        final = self.page.url or ""
        parsed = urlparse(final)
        if parsed.scheme in _LOCAL_SCHEMES:
            return final
        host = parsed.hostname
        if host and await _host_blocked_cached(host):
            raise BrowserBlocked(f"The page redirected to a blocked address ({host}).")
        if host and not self.origin_allowed(host):
            raise BrowserBlocked(
                f"The page redirected to '{host}', which this task is not allowed to visit."
            )
        return final

    async def settle(self) -> None:
        """Best-effort wait for the page to go quiet before observing. A busy
        page is a normal outcome, never an error — timing out here just means we
        observe slightly earlier."""
        try:
            await self.page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
        except Exception:
            pass
        # Let lazy-rendered content paint after networkidle (see SETTLE_RENDER_SECONDS).
        try:
            await asyncio.sleep(SETTLE_RENDER_SECONDS)
        except Exception:
            pass


# ---------------------------------------------------------- media sessions
# A "play"/"watch" browse leaves its window OPEN and playing after the tool
# returns. A tool call normally closes its session in a `finally`, which would
# stop the music the instant the loop reported success — so the live
# BrowserSession is held here instead. Memory-only BY DESIGN: a running Chromium
# page is not serializable, and the deferred 14.5 note is explicit that
# pretending otherwise is where double-submit lives. A restart drops it — the
# window closes with the process, which is the honest outcome (nothing was
# persisted, so nothing silently resumes).
#
# ONE active media session at a time: a second play STOPS the first. A person
# does not run two songs at once, and an unbounded set of orphaned windows is the
# "leaked Chromium the user cannot get rid of" hazard test_browser_session
# already names. The lock serializes start-vs-stop so the two can never both act
# on a half-closed session (the API stop route and a play loop share one loop but
# interleave at awaits).
_active_media: Optional["BrowserSession"] = None
_active_media_meta: dict[str, str] = {}
_media_lock = asyncio.Lock()


async def register_media(session: "BrowserSession", *, title: str, url: str) -> None:
    """Adopt a live session as THE current media session, closing any previous
    one. After this the caller must NOT close the session — the registry owns its
    lifetime until stop_media()."""
    global _active_media, _active_media_meta
    async with _media_lock:
        previous = _active_media
        _active_media = session
        _active_media_meta = {"title": title or "", "url": url or ""}
    if previous is not None and previous is not session:
        await previous.close()


async def stop_media() -> bool:
    """Close the current media session and clear the registry. True when a
    session was actually closed. Idempotent — stopping nothing is not an error."""
    global _active_media, _active_media_meta
    async with _media_lock:
        session = _active_media
        _active_media = None
        _active_media_meta = {}
    if session is None:
        return False
    await session.close()
    return True


def active_media() -> Optional[dict[str, str]]:
    """{title, url} for the current media session, or None. Cheap, no I/O — the
    StatusBar polls this freely (the context_status precedent)."""
    if _active_media is None:
        return None
    return dict(_active_media_meta)


async def reset_media() -> None:
    """Test/shutdown hook — close and clear, without pretending it is a feature.
    Mirrors reset_host_cache: the registry is live state, never persisted."""
    await stop_media()
    await close_login_window()


# ----------------------------------------------------------- login window
# The ONE-TIME sign-in flow (the deferred 14.4 "manual login wall", brought
# forward by user request 2026-07-17: "play on youtube signed in as the account
# I added in Jarvis"). The account the user connected in Settings is a Google
# API token — it does NOT put a session cookie in a browser. The only honest way
# to sign the browser in is for the USER to log in by hand, once, in a real
# window; the persistent ~/.jarvis/browser profile then keeps that session for
# every later agent browse.
#
# THIS WINDOW IS NOT AGENT-DRIVEN. It has NO read-only interceptor: the user
# types their own password and completes the POST the interceptor would abort.
# That does not weaken the READ-mode guarantee — that guarantee is about what the
# AGENT LOOP can do, and the loop never touches this window. It is the user's own
# hands in the user's own profile, exactly like opening Chrome themselves.
#
# One profile = one live persistent context: opening login first stops any media
# session, and a browse first closes login (BrowseTool calls close_login_window).
# Credentials are NEVER seen, stored, or transmitted by Jarvis — the user enters
# them directly into Google's own page.
DEFAULT_LOGIN_URL = "https://accounts.google.com/"

_login_browser: Optional[Any] = None
_login_lock = asyncio.Lock()


async def open_login_window(url: str = DEFAULT_LOGIN_URL) -> None:
    """Open the Jarvis browser profile as a normal, user-driven window at a
    sign-in page. Closes any active media session first (one profile, one live
    context). Raises BrowserUnavailable when no browser can launch."""
    global _login_browser
    await stop_media()
    async with _login_lock:
        if _login_browser is not None:
            # Already open — bring the existing window forward to the URL rather
            # than fighting the profile lock with a second context.
            try:
                page = await _login_browser.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                return
            except Exception as exc:
                logger.debug(f"login window reuse failed, relaunching: {exc}")
                try:
                    await _maybe_await(_login_browser.close())
                except Exception:
                    pass
                _login_browser = None

        browser = await _launch()
        try:
            page = await browser.new_page()
            # No page.route(): a real, fully-interactive window the user drives.
            await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except Exception:
            await _maybe_await(browser.close())
            raise
        _login_browser = browser
        logger.info("browser: opened sign-in window (user-driven, no interceptor)")


async def close_login_window() -> bool:
    """Close the sign-in window if open. True when one was actually closed."""
    global _login_browser
    async with _login_lock:
        browser = _login_browser
        _login_browser = None
    if browser is None:
        return False
    try:
        await _maybe_await(browser.close())
    except Exception as exc:
        logger.debug(f"login window close: {type(exc).__name__}: {exc}")
    return True


def login_window_open() -> bool:
    """Cheap, no-I/O status for the API/StatusBar (the active_media precedent)."""
    return _login_browser is not None
