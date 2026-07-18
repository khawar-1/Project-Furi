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
   This is the guarantee, and it bounds the AGENT LOOP. A loop that cannot issue
   a POST/PUT/PATCH/DELETE cannot submit a form, send a message, or buy anything,
   no matter what the page's text talks it into. It is what makes `browse` a READ
   tool that passes registry.execute_tool's gate untouched, and why the whole
   YouTube case needs ZERO approvals. Subresources are included deliberately and
   it is load-bearing: an SPA (LinkedIn's included) submits via a background
   fetch, not a form POST.

   All three rules are in force for the ENTIRE autonomous loop. At the keep_open
   media handoff — enter_playback_mode(), once the loop has reached `done` and the
   window is the user's own to watch — the interceptor is LIFTED ENTIRELY (unroute)
   and the window drops to exactly the open_login_window posture: user-driven, no
   interception. This is not just to let the site's player/API POSTs through (else
   YouTube reports "you're offline"): keeping the per-request interceptor on a
   continuously-streaming video made the network unusably slow (user report
   2026-07-18) — every media segment paid a round-trip to the single
   browser_runtime loop thread plus a per-host DNS SSRF lookup, a tax a normal
   Chrome never pays. It is sound because all three rules bound the AGENT LOOP, and
   once the loop is `done` it never touches this window again — the only actor left
   is the user watching or the site's own player, exactly as with the sign-in
   window. As a best-effort FALLBACK, enter_playback_mode also flips _read_only so
   that if unroute somehow fails the interceptor stays installed but stops aborting
   non-GET — the player works while Rules 2 & 3 keep guarding: degraded (slow), never
   unsafe. During the loop itself Rules 2 and 3 are unconditional.

   ⚠️ BE HONEST ABOUT WHAT THIS IS. "non-GET = mutation" is an HTTP CONVENTION
   (RFC 7231 safe methods), not a substring test. It is NOT the structural proof
   that planner._recipient_violation is — that one compares an address against a
   corpus and cannot be wrong. This one is a strong bound on the dominant case.
   Sites violate the convention (GET /logout, GET /delete?id=5) and those get
   through; the origin allowlist is all that bounds them. Overclaiming this as
   "the browser recipient lock" is how a future round gets surprised — the same
   way SUMMARY_PROMPT's "never invent" turned out not to be a guarantee either.

   COMMIT MODE (14.5) — the ONE approved way past Rule 1, and it stays narrow.
   arm_commit(method, url) permits a SINGLE non-GET matching exactly (method,
   normalized-url); the interceptor lets that one request through and clears the
   permit in the same breath (re-lock), so a double-submit finds nothing armed.
   It is never a blanket "commit mode on" — no flag stays flipped. The permit is
   set only in the SUBMIT phase, after the plan has PAUSED for signature approval
   on the code-read form state (target URL + method + every field value, rendered
   into the approval card by planner._render_commit_detail — the LLM's prose
   cannot hide what is sent), and the approved request still passes Rules 2 & 3.
   This is genuinely a mutation the tool performs, which is why the commit tool is
   PermissionLevel.DESTRUCTIVE and `browse` stays READ. See app/agents/
   browser_commit.py for the two-phase discover→approve→submit orchestration.

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
#
# --autoplay-policy=no-user-gesture-required: an automation-launched window has no
# user "gesture", so Chromium suppresses autoplay-with-sound and a "play"/"watch"
# goal opens the video PAUSED (live report 2026-07-17). This lets the site's own
# autoplay — and the explicit .play() ensure_playing() issues at the handoff —
# start. It only affects MEDIA autoplay permission; it touches none of the
# READ-mode guarantees (Rule 1 still aborts every non-GET during the agent loop).
_LAUNCH_ARGS = [
    "--disable-background-networking",
    "--disable-blink-features=AutomationControlled",
    "--autoplay-policy=no-user-gesture-required",
]

# Native HTML5 media control, run at the playback handoff. GENERIC across every
# site that uses <video>/<audio> (YouTube, Spotify web, Netflix, ...) — this is
# deliberately NOT a per-site "Skip Ad"/"Play" button selector, which is exactly
# the brittle, ToS-adjacent coupling Phase 14 exists to avoid. It starts whatever
# the element has LOADED: an ad plays, then the content follows in the same
# element on its own — ad-skipping is the user's, by design (their call,
# 2026-07-17). play() returns a promise that rejects when autoplay is still
# blocked; the .catch swallows it so there is no unhandled rejection.
_ENSURE_PLAYING_JS = """() => {
  const media = Array.from(document.querySelectorAll('video, audio'));
  let playing = 0;
  for (const m of media) {
    try {
      if (m.paused || m.ended) {
        const p = m.play();
        if (p && typeof p.catch === 'function') { p.catch(() => {}); }
      }
    } catch (e) {}
    if (!m.paused && !m.ended && m.currentTime >= 0) { playing += 1; }
  }
  return { found: media.length, playing: playing };
}"""

# COMMIT mode (14.5). Reads the form ENCLOSING a chosen element and returns
# exactly what a submit would send — the absolute action URL, the method, and
# each named field's CURRENT value — so the approval card shows the real
# contract, not the LLM's description. A PASSWORD field is never read (its value
# is skipped and has_password is flagged); a form with one is a sign-in, handled
# by 14.4's login-wall path, never submitted here. The chosen form is STAMPED
# (data-jarvis-commit) so the submit phase can re-find and re-verify the exact
# same form without trusting an index across the approval pause.
_READ_COMMIT_FORM_JS = """(el) => {
  const form = el.closest('form') || el.form || null;
  if (!form) return null;
  document.querySelectorAll('[data-jarvis-commit]').forEach(
    (f) => f.removeAttribute('data-jarvis-commit'));
  form.setAttribute('data-jarvis-commit', '1');
  const method = (form.getAttribute('method') || 'GET').toUpperCase();
  const action = form.action || location.href;   // form.action resolves absolute
  const fields = [];
  let hasPassword = false;
  for (const c of Array.from(form.elements || [])) {
    const type = (c.type || '').toLowerCase();
    if (type === 'password') { hasPassword = true; continue; }  // never read a credential
    if (!c.name) continue;
    if (['submit', 'button', 'reset', 'file', 'image'].includes(type)) continue;
    if ((type === 'checkbox' || type === 'radio') && !c.checked) continue;
    let v = (c.value == null) ? '' : String(c.value);
    if (v.length > 300) v = v.slice(0, 300) + '…';
    fields.push({ name: String(c.name), value: v });
  }
  return { action: String(action), method: method, fields: fields, has_password: hasPassword };
}"""

# Re-read the STAMPED form (no element handle needed — the marker survives the
# approval pause because nothing navigates the held session). Used to VERIFY the
# form still matches what the user approved before the one allowed submit fires.
_REREAD_COMMIT_FORM_JS = """() => {
  const form = document.querySelector('form[data-jarvis-commit]');
  if (!form) return null;
  const method = (form.getAttribute('method') || 'GET').toUpperCase();
  const action = form.action || location.href;
  const fields = [];
  let hasPassword = false;
  for (const c of Array.from(form.elements || [])) {
    const type = (c.type || '').toLowerCase();
    if (type === 'password') { hasPassword = true; continue; }
    if (!c.name) continue;
    if (['submit', 'button', 'reset', 'file', 'image'].includes(type)) continue;
    if ((type === 'checkbox' || type === 'radio') && !c.checked) continue;
    let v = (c.value == null) ? '' : String(c.value);
    if (v.length > 300) v = v.slice(0, 300) + '…';
    fields.push({ name: String(c.name), value: v });
  }
  return { action: String(action), method: method, fields: fields, has_password: hasPassword };
}"""

# Fire the stamped form's own submit. requestSubmit() runs validation and fires
# the submit event (an SPA handler can intercept it); .submit() is the fallback.
_SUBMIT_COMMIT_FORM_JS = """() => {
  const form = document.querySelector('form[data-jarvis-commit]');
  if (!form) return false;
  if (typeof form.requestSubmit === 'function') form.requestSubmit();
  else form.submit();
  return true;
}"""

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
    """What the guard refused (and, for COMMIT, the one thing it let through).
    Surfaced in the tool result so a page that misbehaves is VISIBLE rather than
    mysteriously broken — an aborted POST is a breakage, not a mutation, and the
    user deserves to know which."""

    blocked_mutations: int = 0
    blocked_navigations: int = 0
    blocked_hosts: int = 0
    allowed_commits: int = 0
    mutation_urls: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "blocked_mutations": self.blocked_mutations,
            "blocked_navigations": self.blocked_navigations,
            "blocked_hosts": self.blocked_hosts,
            "allowed_commits": self.allowed_commits,
            "mutation_urls": self.mutation_urls[:10],
        }


def _normalize_commit_url(raw: str) -> str:
    """Canonical form used to match an armed commit against a live request:
    scheme + host + path + query, fragment dropped, host lowercased. A trailing
    slash on the path is normalized away so 'https://x/submit' and
    'https://x/submit/' match. Matching is EXACT on this form — a request that
    differs (a changed path, an added query) fails closed (aborted), which is
    the safe direction: an approved submit that gets blocked is a visible
    breakage, an unapproved one that slips through is a mutation."""
    try:
        p = urlparse((raw or "").strip())
    except Exception:
        return (raw or "").strip().lower()
    host = (p.hostname or "").lower().rstrip(".")
    port = f":{p.port}" if p.port else ""
    path = p.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    scheme = (p.scheme or "https").lower()
    query = f"?{p.query}" if p.query else ""
    return f"{scheme}://{host}{port}{path}{query}"


def _commit_fingerprint(state: dict[str, Any]) -> tuple:
    """A comparable identity for an approved submit — method + normalized action
    URL + the ordered (name, value) of every field + the ordered (name, path) of
    every attached file (14.6). Accepts either the JS read shape ({action, ...})
    or the stored shape ({url, ...}); the same fingerprint is what verify_commit
    compares, so an approval binds to the exact values AND the exact files. An
    absent `uploads` yields () — a pre-14.6 no-upload state fingerprints exactly
    as before, so the change is backwards compatible."""
    method = str(state.get("method") or "POST").upper()
    url = _normalize_commit_url(str(state.get("url") or state.get("action") or ""))
    fields = tuple(
        (str(f.get("name") or ""), str(f.get("value") or ""))
        for f in (state.get("fields") or [])
        if isinstance(f, dict)
    )
    uploads = tuple(
        (str(u.get("name") or ""), str(u.get("path") or ""))
        for u in (state.get("uploads") or [])
        if isinstance(u, dict)
    )
    return (method, url, fields, uploads)


class BrowserSession:
    """One page, one allowlist, one interceptor. Read-only by construction."""

    def __init__(self, browser: Any, page: Any, allowlist: set[str]) -> None:
        self._browser = browser
        self.page = page
        self.allowlist = allowlist
        self.stats = InterceptStats()
        # Rule 1 (abort non-GET) is the AGENT-LOOP guarantee and is in force for
        # the whole autonomous browse. enter_playback_mode() flips this to False
        # at the keep_open handoff so the user's own playback window works; Rules
        # 2 & 3 stay on regardless. See the module docstring.
        self._read_only = True
        # COMMIT mode (14.5): a ONE-SHOT permit for a single non-GET the user
        # explicitly approved (a form submit). arm_commit() sets (method,
        # normalized-url); the interceptor lets EXACTLY that request through once,
        # then clears this (re-lock). It is never a blanket "commit mode on" — the
        # permit is consumed by the first matching request. Rules 2 & 3 still
        # apply to the approved request. _commit_fired records that the permit was
        # actually consumed, so the submit phase can tell a real submission from
        # one the site never issued. See the module docstring.
        self._armed_commit: Optional[tuple[str, str]] = None
        self._commit_fired = False
        # COMMIT + UPLOAD (14.6): files attached to the form during discovery via
        # set_input_files. Python is the source of truth for the PATH — a browser
        # strips file paths from JS, so _READ_COMMIT_FORM_JS cannot see them. Each
        # entry {name, path} is folded into commit_state (the approval binds to
        # the exact file) and re-checked by verify_commit before the one submit.
        self.uploads: list[dict[str, str]] = []

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
            # In force for the whole AGENT LOOP; relaxed only after the keep_open
            # handoff (enter_playback_mode) so the user's playback window can use
            # the site's own POST API. Rules 2 & 3 below never relax.
            #
            # COMMIT (14.5) is the ONE narrow exception: a non-GET that matches an
            # armed, user-approved commit is let through EXACTLY ONCE. The permit
            # is consumed here (re-lock) before the request even proceeds, so a
            # duplicate — a double-submit — finds no permit and is aborted like any
            # other mutation. The approved request still falls through to Rules 2 &
            # 3 below (SSRF + allowlist), so approval never buys a way past them.
            if method not in _READ_METHODS:
                if self._commit_allows(method, url):
                    self._armed_commit = None       # one-shot: consume, re-lock
                    self._commit_fired = True
                    self.stats.allowed_commits += 1
                    logger.info(
                        f"browser: allowed APPROVED {method} {url[:120]} "
                        "(commit) — re-locking"
                    )
                    # fall through to Rules 2 & 3 — an approved commit is not
                    # exempt from the SSRF and allowlist guards.
                elif self._read_only:
                    self.stats.blocked_mutations += 1
                    if len(self.stats.mutation_urls) < 10:
                        self.stats.mutation_urls.append(f"{method} {url[:120]}")
                    logger.info(f"browser: aborted {method} {url[:120]} — READ mode")
                    await route.abort()
                    return
                # else: not read-only (playback handoff) — non-GET allowed, fall through

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

    # -------------------------------------------------------------- commit
    # COMMIT mode (14.5): the ONLY path by which this session ever issues a
    # non-GET, and it does so exactly once, for exactly the request the user
    # approved. arm_commit is called from the SUBMIT phase (after signature
    # approval) with the same (method, url) the approval card showed; the
    # interceptor consumes the permit on the first matching request and re-locks.
    def arm_commit(self, method: str, url: str) -> None:
        """Permit ONE non-GET matching (method, url) — the approved submit. The
        permit is one-shot: the interceptor clears it the instant it fires, so a
        double-submit finds nothing armed and is aborted like any mutation."""
        self._armed_commit = ((method or "POST").upper(), _normalize_commit_url(url))
        self._commit_fired = False
        logger.info(f"browser: armed one-shot commit {self._armed_commit[0]} {url[:120]}")

    def _commit_allows(self, method: str, url: str) -> bool:
        """True only when a permit is armed AND this exact request matches it."""
        if self._armed_commit is None:
            return False
        want_method, want_url = self._armed_commit
        return (method or "GET").upper() == want_method and (
            _normalize_commit_url(url) == want_url
        )

    def commit_fired(self) -> bool:
        """Whether the armed commit was actually consumed by a live request —
        so the submit phase can distinguish a real submission from a form the
        site never posted (a JS handler that swallowed it, a validation block)."""
        return self._commit_fired

    async def read_commit_target(
        self, observation: Any, index: int
    ) -> Optional[dict[str, Any]]:
        """Read the form ENCLOSING element `index`: its absolute action URL,
        method, and every named field's current value — the exact contract a
        submit would send, for the approval card. Stamps the form so the submit
        phase can re-find it. None when the element is not in a form or the read
        fails (never raises — a missing form is a normal 'nothing to submit')."""
        from app.core import dom_observe  # local: dom_observe never imports us

        try:
            handle = await dom_observe.resolve(self.page, observation, index)
        except Exception as exc:
            logger.debug(f"read_commit_target resolve: {type(exc).__name__}: {exc}")
            return None
        try:
            raw = await handle.evaluate(_READ_COMMIT_FORM_JS)
        except Exception as exc:
            logger.debug(f"read_commit_target eval: {type(exc).__name__}: {exc}")
            return None
        return raw if isinstance(raw, dict) else None

    async def upload_file(
        self, observation: Any, index: int, path: str
    ) -> tuple[bool, str]:
        """Attach `path` to the file input at `index` via Playwright
        set_input_files — the 14.6 upload action. This issues NO network request
        (the file only leaves the machine on the approved submit), so it stays
        inside READ mode; the interceptor never sees it. Returns (ok, note),
        NEVER raises — a bad target or an unsafe path is an event the loop reacts
        to (re-observe, try again), not a crash.

        Path safety is re-checked here in code even though the planner already
        grounded and safety-checked upload_path before discovery: the
        belt-and-suspenders rule, so the one place that actually touches the
        filesystem can never be handed a protected path. The recorded upload
        (Python is the source of truth — the DOM hides file paths) is folded into
        commit_state and re-verified before the submit."""
        from app.agents import browser_grounding  # lazy: agents↔core cycle
        from app.core import dom_observe  # local: dom_observe never imports us
        from app.tools.file_tools import _resolve_path

        unsafe = browser_grounding.upload_path_unsafe(path)
        if unsafe:
            logger.info(f"browser: refused upload — {unsafe}")
            return False, unsafe
        try:
            resolved = _resolve_path(path)
        except Exception as exc:
            return False, f"could not resolve the file path ({type(exc).__name__})"

        try:
            handle = await dom_observe.resolve(self.page, observation, index)
        except dom_observe.StaleObservation:
            return False, "the file input changed before it could be used"
        except Exception as exc:
            return False, f"could not find the file input ({type(exc).__name__})"
        try:
            await handle.set_input_files(str(resolved))
        except Exception as exc:
            return False, f"could not attach the file ({type(exc).__name__})"

        name = ""
        try:
            name = str(await handle.get_attribute("name") or "")
        except Exception:
            pass
        if not name:
            name = resolved.name
        # Dedupe per input: re-attaching to the same field replaces, so a
        # retried upload never records the same file twice.
        self.uploads = [u for u in self.uploads if u.get("name") != name]
        self.uploads.append({"name": name, "path": str(resolved)})
        logger.info(f"browser: attached file {resolved} to input '{name}'")
        return True, ""

    async def verify_commit(self, approved: dict[str, Any]) -> bool:
        """Re-read the stamped form and confirm it STILL matches what the user
        approved (method + action + every field value + every attached file).
        Nothing should have changed the form between approval and submit — the
        held session just sat there — so a mismatch means the page tampered with
        it, and the submit is refused (fail closed). Password presence also
        disqualifies. The attached FILES come from self.uploads (the DOM hides
        their paths), which is stable across the pause because nothing navigated
        the held session."""
        try:
            raw = await self.page.evaluate(_REREAD_COMMIT_FORM_JS)
        except Exception as exc:
            logger.debug(f"verify_commit eval: {type(exc).__name__}: {exc}")
            return False
        if not isinstance(raw, dict) or raw.get("has_password"):
            return False
        current = {**raw, "uploads": self.uploads}
        return _commit_fingerprint(current) == _commit_fingerprint(approved)

    async def submit_commit(self) -> None:
        """Fire the stamped form's own submit — the one request the arm permits.
        Best-effort; whether the POST actually went out is read from
        commit_fired() afterwards, not assumed here."""
        try:
            await self.page.evaluate(_SUBMIT_COMMIT_FORM_JS)
        except Exception as exc:
            logger.debug(f"submit_commit: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------- handoff
    async def enter_playback_mode(self, *, reload: bool = True) -> None:
        """Hand this session off from the autonomous loop to the user.

        LIFTS request interception entirely (unroute): after handoff the window is
        the user's own to watch, and keeping the per-request interceptor on a
        continuously-streaming video made the network unusably slow (user report
        2026-07-18: "the net is very slow in your profile, normal in mine") — EVERY
        media segment paid a round-trip to the single browser_runtime loop thread
        plus a per-host DNS SSRF lookup, a persistent tax a normal Chrome never
        pays. So this window drops to exactly the open_login_window posture:
        user-driven, no interceptor. Sound because all three rules bound the AGENT
        LOOP, and once the loop has reached `done` it never touches this window
        again — the only actor left is the user (watching) or the site's own player.

        Also flips _read_only=False as a best-effort FALLBACK: if unroute fails, the
        interceptor stays installed but stops aborting non-GET, so the player's POST
        still works (else YouTube reports "you're offline") while Rules 2 & 3 keep
        guarding — degraded (slow) but never unsafe.

        The reload makes an already-stuck player retry the POST that was aborted
        while read-only (now permitted) and actually play. Best-effort throughout:
        an unroute or reload failure must never break the already-open window."""
        self._read_only = False
        try:
            await self.page.unroute("**/*", self._intercept)
        except Exception as exc:
            logger.debug(f"playback unroute: {type(exc).__name__}: {exc}")
        if not reload:
            return
        try:
            await self.page.reload(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except Exception as exc:
            logger.debug(f"playback reload: {type(exc).__name__}: {exc}")

    async def ensure_playing(
        self, *, attempts: int = 6, gap_seconds: float = 1.0
    ) -> bool:
        """Best-effort: start any paused <video>/<audio> so a 'play'/'watch'/
        'listen' goal actually produces sound. An automation-launched window opens
        media PAUSED (no user gesture — see _LAUNCH_ARGS), and "press the play
        button" is what the user asked for (2026-07-17).

        Polls because the player and its media element appear a moment AFTER the
        handoff reload's domcontentloaded — each tick re-issues .play() until an
        element reports playing or the attempts run out (~attempts×gap seconds,
        bounded). Generic native-media control, never a per-site button; must run
        AFTER enter_playback_mode() lifts Rule 1, or the player's stream POST is
        still aborted. Never raises — a page with no media, or an evaluate that
        fails, just returns False."""
        for _ in range(max(1, attempts)):
            try:
                result = await self.page.evaluate(_ENSURE_PLAYING_JS)
            except Exception as exc:
                logger.debug(f"ensure_playing: {type(exc).__name__}: {exc}")
                result = None
            if isinstance(result, dict) and result.get("playing"):
                logger.info("browse: media is playing after the handoff")
                return True
            try:
                await asyncio.sleep(gap_seconds)
            except Exception:
                pass
        logger.debug("ensure_playing: no media reported playing within the window")
        return False


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
    await discard_commit()
    await close_result_window()
    await close_login_window()


# ------------------------------------------------ committed-form result window
# After an APPROVED commit submit (14.5), the window can be LEFT OPEN so the user
# can SEE the site's response — the "File Uploaded!" page — instead of it closing
# the instant the submit reports success (user report 2026-07-18). This is the
# grounded-confirmation UI's visual half: the completion text quotes the response,
# and this window shows it.
#
# SECURITY — why leaving it open is safe, and why it is NOT enter_playback_mode().
# The submit consumed the one-shot commit arm (arm_commit → the interceptor
# clears _armed_commit the instant the approved request fires) and _read_only is
# still True, so the interceptor still ABORTS every non-GET. A lingering result
# window is therefore structurally incapable of issuing a second mutation: there
# is no armed permit, and only the SUBMIT phase — after a fresh signature approval
# — can ever arm one. That is exactly the property the old `finally: close()`
# protected ("the approval can never be replayed against a lingering window"); the
# spent permit protects it now, so keeping the window open reintroduces no risk.
# We deliberately do NOT call enter_playback_mode() here — media LIFTS interception
# for streaming speed, but a static result page needs no throughput and MUST stay
# read-only. The window is a viewer, nothing more.
#
# One live persistent context (the media/login rule): a new browse/commit/login
# closes this first (BrowseTool, browser_commit.discover, and open_login_window
# call close_result_window() before launching). Memory-only — a restart just
# closes it, like every browser registry.
_result_window: Optional["BrowserSession"] = None
_result_meta: dict[str, str] = {}
_result_lock = asyncio.Lock()


async def register_result_window(
    session: "BrowserSession", *, title: str, url: str
) -> None:
    """Adopt a just-submitted session as THE open result window, closing any
    previous one. After this the caller must NOT close the session — the registry
    owns its lifetime until close_result_window()."""
    global _result_window, _result_meta
    async with _result_lock:
        previous = _result_window
        _result_window = session
        _result_meta = {"title": title or "", "url": url or ""}
    if previous is not None and previous is not session:
        await previous.close()


async def close_result_window() -> bool:
    """Close the open result window and clear the registry. True when a window was
    actually closed. Idempotent — closing nothing is not an error."""
    global _result_window, _result_meta
    async with _result_lock:
        session = _result_window
        _result_window = None
        _result_meta = {}
    if session is None:
        return False
    await session.close()
    return True


def active_result_window() -> Optional[dict[str, str]]:
    """{title, url} for the open result window, or None. Cheap, no I/O — the
    StatusBar polls it (the active_media precedent)."""
    if _result_window is None:
        return None
    return dict(_result_meta)


# ---------------------------------------------------------- commit sessions
# COMMIT mode (14.5) discovers a form, then PAUSES for the user's signature
# approval. The live BrowserSession — sitting on the filled form, ready to submit
# — must survive that pause, and it is held here exactly as a media session is:
# memory-only BY DESIGN, because a running Chromium page is not serializable and
# pretending otherwise is where DOUBLE-SUBMIT lives (the deferred-14.5 note said
# so). The session is never replayed or reconstructed — the ONE held session is
# the only thing that can be submitted, and a restart drops it (the submit then
# reports the session expired, and nothing silently re-sends).
#
# ONE pending commit at a time, one-slot like media: a new discovery closes the
# previous held session. take_commit() removes AND returns it (the submit phase
# owns it thereafter), so a taken commit can never be taken twice.
_commit_session: Optional["BrowserSession"] = None
_commit_meta: dict[str, Any] = {}
_commit_lock = asyncio.Lock()


async def hold_commit(session: "BrowserSession", *, state: dict[str, Any]) -> None:
    """Hold a discovered-but-unsubmitted session across the approval pause,
    closing any previously held one. After this the caller must NOT close the
    session — the registry owns it until take_commit()/discard_commit()."""
    global _commit_session, _commit_meta
    async with _commit_lock:
        previous = _commit_session
        _commit_session = session
        _commit_meta = dict(state or {})
    if previous is not None and previous is not session:
        await previous.close()


async def take_commit() -> Optional["BrowserSession"]:
    """Remove and return the held commit session (the submit phase owns it now),
    or None when there is none — a restart/timeout dropped it, and the submit
    must report that rather than invent a submission."""
    global _commit_session, _commit_meta
    async with _commit_lock:
        session = _commit_session
        _commit_session = None
        _commit_meta = {}
    return session


async def discard_commit() -> bool:
    """Close and clear a held commit session without submitting (cancel /
    shutdown / a superseding discovery). True when one was actually closed."""
    session = await take_commit()
    if session is None:
        return False
    await session.close()
    return True


def pending_commit() -> Optional[dict[str, Any]]:
    """The approved-form state of the held commit session, or None. Cheap, no
    I/O (the active_media precedent)."""
    if _commit_session is None:
        return None
    return dict(_commit_meta)


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
    await close_result_window()  # one live persistent context (the profile lock)
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
