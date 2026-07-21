"""
Jarvis OS — Browser Session (Phase 14, Part 1)

A real Chromium Jarvis can drive, under ACTION-LEVEL SAFETY (2026-07-21, owner
decision): **the page may talk to the network freely, but the agent cannot
commit — every agent-performed submit requires the one-shot, code-read
commit permit bound to a signature approval.**

Why this module exists at all
----------------------------
Per-site tools (a YouTubeTool, a LinkedInTool) are weeks of work each and break
on every layout change. The universal alternative is one browser plus a loop
that reads the page and decides the next action — no site-specific code, ever.
This module is the browser half; app/agents/browser_loop.py (Part 2) is the loop.

THE SAFETY MODEL, and its honest limits
---------------------------------------
The predecessor model aborted EVERY non-GET request ("READ mode cannot
mutate"). That was a strong-sounding network guarantee with a fatal capability
cost: modern SPAs do everything over POST/XHR — search, filters, lazy
content — so most of the web simply did not work, and site-specific hacks
(navigate-instead-of-click) piled up. The owner chose the Skyvern model
instead: gate what the AGENT does, not what the page does. Safety now lives in
three layers: (a) the agent-gesture gate in the loop — an unarmed submit
gesture (clicking a submit control of a commit-shaped form, submitting a form)
is refused in code during read-mode browsing; (b) the one-shot COMMIT permit
(below) — the only sanctioned submit path, behind signature approval on the
code-read contract; (c) the network rules here as backstop and exfiltration
bound. Every request still passes _intercept():

1. UNAPPROVED TOP-LEVEL FORM NAVIGATION IS ABORTED. A non-GET main-frame
   document navigation (a classic form-POST submit) is refused unless it
   matches the armed commit permit — the network backstop under the gesture
   gate. All OTHER page traffic (XHR/fetch POSTs, subframes, widget
   verification) flows: it is the page being a page, the same traffic every
   browser the user has ever used sends, and the loop cannot choose its
   payload.

   ⚠️ BE HONEST ABOUT THE TRADE. This is a WEAKER network bound than the old
   blanket abort: a click on an authenticated origin can trigger the site's
   own mutating XHR (an "unsubscribe" button, a "dismiss" call) exactly as a
   human's click would. What is preserved absolutely: the agent SUBMITS
   nothing without approval (gesture gate + permit), values it types are
   grounded in the user's words/profile (never page content), credentials are
   never entered, CAPTCHAs never touched, and the allowlist still bounds where
   it can go. Within an allowlisted, authenticated origin a compromised loop
   has user authority — TRUE UNDER BOTH MODELS — and the headed window remains
   the last honest control.

   At the keep_open media handoff — enter_playback_mode(), once the loop has
   reached `done` and the window is the user's own to watch — the interceptor
   is LIFTED ENTIRELY (unroute): per-request interception on a streaming video
   made the network unusably slow (2026-07-18), and once the loop is `done`
   the only actor left is the user. Best-effort fallback: if unroute fails,
   _read_only flips off and Rules 2 & 3 keep guarding.

   COMMIT MODE (14.5) — the sanctioned submit, and it stays narrow.
   arm_commit(method, url) permits a SINGLE non-GET matching exactly (method,
   normalized-url) — whatever its transport, a classic form POST or the SPA's
   background fetch to the action URL — consumed on first match (re-lock), so
   a double-submit finds nothing armed. The permit is set only in the SUBMIT
   phase, after the plan PAUSED for signature approval on the code-read form
   state (URL + method + every field value, rendered into the approval card by
   planner._render_commit_detail — the LLM's prose cannot hide what is sent),
   and the approved request still passes Rules 2 & 3. The commit tool is
   PermissionLevel.DESTRUCTIVE; `browse` stays READ. See app/agents/
   browser_commit.py for the discover→approve→submit orchestration.

   DOWNLOADS ARE REFUSED wholesale (page.on("download") → cancel): nothing in
   the stack consumes a downloaded file, and an unprompted download is a
   classic drive-by.

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
  once, per origin, in the visible window. The profile keeps SESSIONS, NOT
  CREDENTIALS: _harden_profile turns off Chrome's password manager/autofill and
  clears any saved password on every launch, so a sign-in is always a deliberate
  human action and a typed password is never silently re-entered (the cookie
  persists; the raw password does not).
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
import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from loguru import logger

from app.browser import registry as _held
from app.tools.browser_tools import _host_is_blocked, _validate_url

# --------------------------------------------------------------------- limits
BROWSER_PROFILE_DIR = Path.home() / ".jarvis" / "browser"
NAV_TIMEOUT_MS = 20_000
# A launch on the SHARED single-instance profile can HANG indefinitely (not fail)
# when an orphaned Chromium still holds the OS profile lock — Chromium the process
# waits/hands off rather than erroring, and launch_persistent_context has no
# native cap for that state. Live incident 2026-07-20: a plain sign-in window left
# open by the previous backend held ~/.jarvis/browser overnight, and the next
# "play on youtube" browse spun forever (no `browser: launched via chrome` ever
# logged). Bound the launch so a locked profile becomes a clean failure that the
# reclaim-and-retry (below) can self-heal, never an endless spinner.
LAUNCH_TIMEOUT_SECONDS = 45.0
# networkidle is a BEST-EFFORT quiet signal, not a correctness gate — a busy
# analytics/ad page never truly goes idle, so waiting the old 5s on it just
# burned time every step. 2.5s is enough for a normal page to settle; a busy one
# times out and we observe anyway (the adaptive render poll below is what waits
# for real content, not this).
SETTLE_TIMEOUT_MS = 2_500       # best-effort wait for the page to go quiet
# SPAs lazy-render their real content AFTER the network briefly goes idle, so
# networkidle can return before the elements the loop needs have painted
# (measured live 2026-07-17: a YouTube results page observed with only its header
# and tabs, the video links not yet in the DOM). The OLD fix was a FLAT 2.5s sleep
# every step (up to 15 steps/browse) — most of it wasted on already-painted pages.
# Instead POLL the DOM node count until it stops growing (lazy content appearing
# IS the node count growing), stopping early on a stable page and hard-capping so
# a perpetually-churning page can't stall the loop. Generic — a property of
# client-rendered pages, not a YouTube special-case.
SETTLE_RENDER_MAX_SECONDS = 1.5   # hard cap on the stability poll
SETTLE_RENDER_POLL_SECONDS = 0.25  # sample interval
SETTLE_RENDER_STABLE_SAMPLES = 2   # consecutive unchanged samples ⇒ settled

# ~/.jarvis/browser is a SINGLE persistent profile: at most one live Chromium may
# hold it. A Chromium keeps the OS single-instance lock for a short moment after
# its close() returns, and a plain `chrome.exe --user-data-dir=<that profile>`
# launched into that gap does NOT open its own window — it hands the URL to the
# dying instance and EXITS (live report 2026-07-19: after choosing "Sign in", the
# hand-off window "said it opened but it didn't; I had to do it myself"). Two
# guards defend the hand-off: _settle_profile() waits out the lock when a session
# was JUST closed, and _open_clean_login() VERIFIES the launched subprocess stays
# alive — a fast exit means it handed off, so we fall back to the Playwright
# window we launch and control. _profile_released_monotonic is stamped by every
# BrowserSession.close() (and the hand-off teardowns), so the settle triggers no
# matter which path freed the profile — including an upstream discovery discard.
_PROFILE_SETTLE_SECONDS = 1.5
_CLEAN_LOGIN_VERIFY_SECONDS = 2.5
_profile_released_monotonic: float = 0.0
# The stamp is written from the browser loop AND from reclaim_orphaned_profile's
# asyncio.to_thread worker — a cross-thread float write with no ordering. A
# threading.Lock (never held across an await) makes both sides well-defined.
_profile_stamp_lock = threading.Lock()


def _mark_profile_released() -> None:
    """Record (monotonic) that a Chromium on the shared profile was just closed."""
    global _profile_released_monotonic
    with _profile_stamp_lock:
        _profile_released_monotonic = time.monotonic()


async def _settle_profile() -> None:
    """Wait for the shared profile's single-instance lock to clear when a session
    was closed within the last _PROFILE_SETTLE_SECONDS — otherwise a hand-off
    window launched immediately hands off to the dying instance and never appears.
    A no-op when nothing was closed recently (so the common path pays nothing)."""
    with _profile_stamp_lock:
        stamp = _profile_released_monotonic
    remaining = _PROFILE_SETTLE_SECONDS - (time.monotonic() - stamp)
    if remaining > 0:
        await asyncio.sleep(remaining)


# --------------------------------------------------- orphaned-profile reclaim
# _settle_profile only guards an IN-PROCESS recent close. It cannot see a Chromium
# left holding ~/.jarvis/browser by a PREVIOUS backend process — a plain sign-in
# window (open_login_window's detached subprocess) or a leaked automation context
# after a crash/kill. That orphan holds the single-instance lock, and the next
# launch hangs on it (2026-07-20 incident). reclaim_orphaned_profile() kills that
# orphan and ONLY that orphan: every window Jarvis launches — the automation
# context (launch_persistent_context user_data_dir=...) AND the clean window
# (_default_clean_launcher --user-data-dir=...) — carries the exact profile path
# on its command line, so matching that path can never touch the user's everyday
# Chrome (a different user-data-dir). Behind an injectable seam so the suite never
# enumerates or kills a real process.
#
# A reaper enumerates the machine's processes and returns the PIDs whose command
# line contains --user-data-dir=<profile>; reclaim then kills them. The real
# implementation is Windows-only (a CIM/tasklist query); off Windows it is a
# no-op (the single-instance-lock-hang is a Windows behavior, and Jarvis browser
# control ships Windows-first).
_PROFILE_REAPER: Optional[Callable[[str], list[int]]] = None


def _windows_profile_pids(profile_marker: str) -> list[int]:
    """Return PIDs of chrome/msedge processes whose command line contains
    `profile_marker` (the exact --user-data-dir=<profile> path). Windows-only,
    best-effort — any failure yields [] (no kill), never raises."""
    if os.name != "nt":
        return []
    marker = profile_marker.strip().lower()
    if not marker:
        return []
    # CIM over PowerShell: the command line is the one field that distinguishes the
    # Jarvis-profile Chrome from every other chrome.exe. -Filter narrows to the two
    # browser image names before we read command lines.
    ps = (
        "Get-CimInstance Win32_Process -Filter "
        "\"Name='chrome.exe' OR Name='msedge.exe'\" | "
        "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception as exc:
        logger.debug(f"profile reaper enumerate: {type(exc).__name__}: {exc}")
        return []
    return _parse_reaper_output(proc.stdout or "", marker)


def _parse_reaper_output(stdout: str, marker: str) -> list[int]:
    """Pure parse of the `<pid>\\t<commandline>` lines: keep the PID of every row
    whose command line contains `marker` (the exact --user-data-dir=<profile>).
    The safety property lives HERE — a chrome.exe on ANY other profile has a
    different --user-data-dir and never matches, so the everyday browser is never
    returned. Case-insensitive; a blank marker matches nothing (never kill all)."""
    marker = (marker or "").strip().lower()
    if not marker:
        return []
    pids: list[int] = []
    for line in stdout.splitlines():
        pid_str, _, cmdline = line.partition("\t")
        if marker not in cmdline.lower():
            continue
        try:
            pids.append(int(pid_str.strip()))
        except ValueError:
            continue
    return pids


def reclaim_orphaned_profile() -> int:
    """Kill any Chromium still holding the ~/.jarvis/browser profile lock (scoped
    to processes whose command line names that exact profile — never the user's
    everyday Chrome). Returns how many were killed. Best-effort and SYNCHRONOUS
    (pure subprocess work — no browser loop needed); safe to call from startup,
    shutdown, and a failed launch. Off Windows / no reaper injected → 0."""
    reaper = _PROFILE_REAPER or _windows_profile_pids
    marker = f"--user-data-dir={BROWSER_PROFILE_DIR}"
    try:
        pids = reaper(marker) or []
    except Exception as exc:
        logger.debug(f"profile reaper: {type(exc).__name__}: {exc}")
        return 0
    killed = 0
    for pid in pids:
        if _kill_pid_tree(pid):
            killed += 1
    if killed:
        logger.info(
            f"browser: reclaimed the profile lock — killed {killed} orphaned "
            f"Jarvis-profile browser process(es)"
        )
        # A kill frees the lock a moment later, exactly like a close(); make the
        # next launch settle so it does not race the dying process.
        _mark_profile_released()
    return killed


def _kill_pid_tree(pid: int) -> bool:
    """taskkill /F /T the process tree for one PID (the _terminate_clean_proc
    mechanism). True when the kill command ran without raising. Best-effort."""
    if os.name != "nt":
        return False
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            timeout=10,
        )
        return True
    except Exception as exc:
        logger.debug(f"profile reaper kill {pid}: {type(exc).__name__}: {exc}")
        return False


# RFC 7231 safe methods. TRACE is safe on paper and a known XST vector — the
# loop has no use for it, so it is not on the list.
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Non-network schemes the browser drives itself (about:blank between pages,
# data:/blob: for generated content). Not requests to anywhere — never gated.
_LOCAL_SCHEMES = frozenset({"about", "data", "blob", "chrome", "chrome-error"})

# Playwright route-handler races that are BENIGN — the request has already been
# resolved (continued, aborted, redirected, or the page/context went away), so a
# second route action is both impossible and unnecessary. Matched as lowercase
# substrings of the exception message. The bug this fixes (2026-07-19 "sometimes
# says there's no internet"): the interceptor's fail-closed `except` used to call
# route.abort() for ANY exception — including a benign continue_() race on the
# MAIN document — turning a good page load into a connection error. A benign race
# must be SWALLOWED (the request already resolved), never re-aborted.
_BENIGN_ROUTE_ERRORS = (
    "already handled",
    "already been handled",
    "no longer handled",
    "target closed",
    "target page, context or browser has been closed",
    "browser has been closed",
    "context or browser has been closed",
    "request context is disposed",
    "request context disposed",
    "request is already routed",
    "response has been already",
)

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

# Launch flags shared by agent and login windows. The AutomationControlled
# switch drops the `navigator.webdriver` flag Google reads to refuse sign-in and
# to serve degraded pages to "a bot". It does NOT weaken any guarantee here —
# the READ-mode interceptor is what bounds the agent, not the browser's honesty
# about being scripted — it just lets a real person sign in through the window.
#
# NOTE (2026-07-19, "sometimes says there's no internet"): the old blanket
# --disable-background-networking ALSO disables Chrome's own connectivity probe,
# which can leave the window believing it is offline even when requests would
# work. Replaced with the NARROW switches below that trim the same chatter
# (component updates / domain-reliability beacons / translate / optimization
# hints / default-browser nag) WITHOUT touching connectivity detection.
#
# --autoplay-policy=no-user-gesture-required: an automation-launched window has no
# user "gesture", so Chromium suppresses autoplay-with-sound and a "play"/"watch"
# goal opens the video PAUSED (live report 2026-07-17). This lets the site's own
# autoplay — and the explicit .play() ensure_playing() issues at the handoff —
# start. It only affects MEDIA autoplay permission; it touches none of the
# READ-mode guarantees (Rule 1 still aborts every non-GET during the agent loop).
_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--autoplay-policy=no-user-gesture-required",
    # Narrow chatter trims that DON'T disable the connectivity probe (see note
    # above — the blanket --disable-background-networking did, and read as offline).
    "--disable-component-update",
    "--disable-domain-reliability",
    "--no-default-browser-check",
    # Jarvis's browser keeps SESSIONS, never CREDENTIALS: stop autofill's server
    # chatter as a belt (the real mechanism is the Preferences seeding in
    # _harden_profile — a flag name can churn, the prefs keys do not). Translate /
    # OptimizationHints are folded in here so the whole set is one switch.
    "--disable-features=AutofillServerCommunication,Translate,OptimizationHints",
]

# Preferences keys that turn Chrome's password manager + autofill OFF in the
# ~/.jarvis/browser profile. THE PROFILE MUST NEVER SAVE OR AUTO-FILL A PASSWORD:
# a saved credential auto-filling in the user-driven login window reads exactly
# like "the AI signed in by itself" (user report 2026-07-18), and silently
# retaining a typed password is a posture we do not want. Cookies stay — the
# SESSION persists (the "log in once, the profile keeps you signed in" property),
# only the raw password does not. These are the standard keys Chrome reads from
# Default/Preferences at startup; Playwright's persistent context exposes no
# `prefs` option, so we seed the file ourselves.
_PROFILE_PREFS: dict[str, Any] = {
    "credentials_enable_service": False,
    "profile": {"password_manager_enabled": False},
    "autofill": {"profile_enabled": False, "credit_card_enabled": False},
}
# Credential stores to drop from the profile so a password saved before hardening
# can never auto-fill again. Cookies / session state live in OTHER files and are
# deliberately not touched.
_CREDENTIAL_FILES = ("Login Data", "Login Data For Account")


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge `overlay` into `base` (nested dicts merged, scalars
    overwritten) — so seeding our prefs keeps everything else Chrome stored."""
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _harden_profile(profile_dir: Path) -> None:
    """Ensure the persistent profile never SAVES or AUTO-FILLS credentials, and
    drop any password already stored in it. Idempotent, best-effort, NEVER raises
    (a launch must never fail because hardening did — the reset_host_cache /
    narration discipline); re-applied on every launch so a Chrome rewrite of
    Preferences can't make it stick. Cookies are left untouched: the session
    persists, only the raw password does not. Called BEFORE launch, while the
    profile is unlocked, so the credential files can actually be removed."""
    default = profile_dir / "Default"
    try:
        default.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.debug(f"profile harden mkdir: {type(exc).__name__}: {exc}")
        return
    # 1) Disable the password manager + autofill via Preferences (merge — keep
    #    everything else Chrome stored; a corrupt file reseeds from scratch).
    prefs_path = default / "Preferences"
    try:
        current: Any = {}
        if prefs_path.exists():
            try:
                current = json.loads(prefs_path.read_text(encoding="utf-8") or "{}")
            except Exception:
                current = {}
        if not isinstance(current, dict):
            current = {}
        _deep_merge(current, _PROFILE_PREFS)
        prefs_path.write_text(json.dumps(current), encoding="utf-8")
    except Exception as exc:
        logger.debug(f"profile harden prefs: {type(exc).__name__}: {exc}")
    # 2) Remove any credential already saved in this profile (cookies untouched).
    for name in _CREDENTIAL_FILES:
        target = default / name
        try:
            if target.exists():
                target.unlink()
        except Exception as exc:
            logger.debug(f"profile harden clear {name}: {type(exc).__name__}: {exc}")

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
# The shared read-the-contract body: given `form` in scope, return exactly what
# a submit would send. ONE copy — the initial read and the pre-submit re-read
# were verbatim duplicates that had to be kept in sync by hand.
_FORM_CONTRACT_JS_BODY = """
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
"""

_READ_COMMIT_FORM_JS = ("""(el) => {
  const form = el.closest('form') || el.form || null;
  if (!form) return null;
  document.querySelectorAll('[data-jarvis-commit]').forEach(
    (f) => f.removeAttribute('data-jarvis-commit'));
  form.setAttribute('data-jarvis-commit', '1');
""" + _FORM_CONTRACT_JS_BODY + "}")

# Re-read the STAMPED form (no element handle needed — the marker survives the
# approval pause because nothing navigates the held session). Used to VERIFY the
# form still matches what the user approved before the one allowed submit fires.
_REREAD_COMMIT_FORM_JS = ("""() => {
  const form = document.querySelector('form[data-jarvis-commit]');
  if (!form) return null;
""" + _FORM_CONTRACT_JS_BODY + "}")

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

    async def route(self, pattern: str, handler: Callable[..., Any]) -> None:
        """Install the interceptor at CONTEXT level, so a popup / new tab is
        guarded from its very FIRST request. Page-level routing left a gap: a
        popup's initial document navigation could fire before _adopt_new_page
        attached the route to it."""
        await self._context.route(pattern, handler)

    def on_page(self, callback: Callable[[Any], None]) -> None:
        """Wire a listener for every NEW page opened in this context — popups and
        target=_blank tabs included. Lets BrowserSession follow a click that opens
        a new tab. Best-effort: never raises (a launch/registration hiccup must not
        break the session)."""
        try:
            self._context.on("page", callback)
        except Exception as exc:
            logger.debug(f"context page listener: {type(exc).__name__}: {exc}")

    async def close(self) -> None:
        for shutdown in (self._context.close, self._playwright.stop):
            try:
                await shutdown()
            except Exception as exc:  # a half-dead browser must not raise here
                logger.debug(f"browser teardown: {type(exc).__name__}: {exc}")
        # A Chromium on ~/.jarvis/browser can still hold the single-instance
        # profile lock for a moment after close() returns; record when we let go
        # so a hand-off window launched right after settles first (see
        # _settle_profile / the 2026-07-19 "opened a sign-in window but it didn't"
        # fix).
        _mark_profile_released()


async def _start_playwright() -> Any:
    """Import Playwright lazily and start its driver — the one place the optional
    dependency is touched, so a base install without it fails clean (the seam a
    test overrides to drive the launch/retry path without a real Chromium)."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise BrowserUnavailable(
            "Browser control needs Playwright, which is not installed. "
            "Install it with: pip install playwright"
        ) from exc
    return await async_playwright().start()


async def _default_browser_factory() -> Any:
    playwright = await _start_playwright()

    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    # SESSIONS, NOT CREDENTIALS: turn off the profile's password manager/autofill
    # and drop any saved password before launch, so no credential is ever
    # auto-filled in the user-driven login window (see _harden_profile). Covers
    # both the agent session and open_login_window — every window reaches here.
    _harden_profile(BROWSER_PROFILE_DIR)

    async def _launch_channel(channel: Optional[str]) -> Any:
        """Launch one channel, bounded by LAUNCH_TIMEOUT_SECONDS. A launch that
        HANGS on a locked profile (rather than erroring) becomes a timeout — a
        working browser launches in ~2-3s (per the live logs), so a timeout is a
        strong lock signature, distinct from an install error."""
        return await asyncio.wait_for(
            playwright.chromium.launch_persistent_context(
                user_data_dir=str(BROWSER_PROFILE_DIR),
                headless=False,           # the user watches — see the docstring
                service_workers="block",  # rule 1 is void without this
                args=list(_LAUNCH_ARGS),
                **({"channel": channel} if channel else {}),
            ),
            timeout=LAUNCH_TIMEOUT_SECONDS,
        )

    errors: list[str] = []
    reclaimed_once = False
    for channel in _CHANNELS:
        # Two attempts per channel: the original, and one retry AFTER reclaiming an
        # orphan — but the reclaim is spent at most once across the whole chain.
        for _attempt in range(2):
            try:
                context = await _launch_channel(channel)
            except asyncio.TimeoutError:
                errors.append(
                    f"{channel or 'bundled chromium'}: launch timed out after "
                    f"{LAUNCH_TIMEOUT_SECONDS:.0f}s (profile likely locked)"
                )
                # A timeout is the lock signature. The classic cause is an orphaned
                # Jarvis-profile Chrome holding the single-instance lock (a sign-in
                # window from a prior run, a leaked context after a crash). Kill it
                # ONCE and retry THIS channel on the freed profile, so the first
                # browse after a restart self-heals instead of spinning. Off-loop
                # (it shells out) so the browser loop is never blocked.
                if not reclaimed_once:
                    reclaimed_once = True
                    if await asyncio.to_thread(reclaim_orphaned_profile):
                        await _settle_profile()  # let the killed process let go
                        continue  # retry this same channel
                break  # nothing to reclaim (or already tried) → next channel
            except Exception as exc:
                # An install/config error (channel not present) — the reclaim would
                # not help; fall through to the next channel.
                errors.append(f"{channel or 'bundled chromium'}: {str(exc)[:120]}")
                break
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
    blocked_downloads: int = 0
    mutation_urls: list[str] = field(default_factory=list)
    # Performance measurement (2026-07-19 "slow browser" round) — surfaced in the
    # per-session close summary so the interception tax is measured, not guessed.
    total_requests: int = 0     # requests the interceptor fielded
    ssrf_checks: int = 0        # host-block lookups actually invoked (allowlisted skipped)
    settle_seconds: float = 0.0  # cumulative time spent in settle() this session

    def as_dict(self) -> dict[str, Any]:
        return {
            "blocked_mutations": self.blocked_mutations,
            "blocked_navigations": self.blocked_navigations,
            "blocked_hosts": self.blocked_hosts,
            "allowed_commits": self.allowed_commits,
            "blocked_downloads": self.blocked_downloads,
            "mutation_urls": self.mutation_urls[:10],
            "total_requests": self.total_requests,
            "ssrf_checks": self.ssrf_checks,
            "settle_seconds": round(self.settle_seconds, 2),
        }


# (The challenge-VENDOR traffic carve-out was REMOVED 2026-07-21: under the
# action-level network policy a widget's verification XHR is ordinary page
# traffic and flows on its own — no arming window needed. The no-touch
# guarantees are untouched: a widget's elements are never stamped, the vision
# point maps to nothing, and _act refuses the challenge zone.)


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
        # True when the interceptor was installed at CONTEXT level (real path)
        # — adopted popups then need no per-page route of their own.
        self._context_routed = False
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
        # MULTI-COMMIT (15.1): a single browse goal may perform several sequential
        # approved submits ("apply to the first 3 jobs"). Between each submit the
        # SAME live session is held in the commit registry across the approval
        # pause and the loop RESUMES on it (never a fresh session from start_url) —
        # so these two carry the resumable loop's position across the pause:
        #   commits_done  — how many approved submits have fired on this session
        #                   (the budget is enforced against it; browser_commit
        #                   .perform is the only writer);
        #   browse_history — the loop's action history, so a resumed run_browse
        #                    keeps the model's context of what it already did.
        # Memory-only, like the session itself — a restart drops it (the submit
        # then reports the session expired and sends nothing).
        self.commits_done: int = 0
        self.browse_history: list[str] = []
        # REDIRECT OFF-SITE (2026-07-19, the WWR-ad incident): a click on an
        # in-allowlist link (an ad's same-site click-tracker) can 302 to an origin
        # the task may not visit — Playwright route handlers never re-fire on
        # redirect hops, so Rule 3 can't see it. _verify_landing() catches the
        # LANDING, backs the page out, and records where it tried to go here so
        # the loop can surface the SAME origin-approval pause an off-site link
        # gets (a legit "Apply" flow redirecting to an ATS is approvable; an ad
        # is deniable). {host, url} or None; consumed by the loop.
        self.last_redirect_offsite: Optional[dict[str, str]] = None

    # ------------------------------------------------------------ lifecycle
    @classmethod
    async def open(cls, allowlist: set[str]) -> "BrowserSession":
        origins = {o for o in (_normalize_origin(a) for a in allowlist) if o}
        # If a hand-off/media/prior session was just closed, wait out the shared
        # profile's single-instance lock before launching, or this launch races
        # the dying Chromium (the TargetClosedError churn, 2026-07-19).
        await _settle_profile()
        browser = await _launch()
        try:
            page = await browser.new_page()
            session = cls(browser, page, origins)
            # CONTEXT-level interception when the handle supports it (the real
            # path): a popup is then guarded from its very FIRST request —
            # page-level routing left the popup's initial document navigation
            # un-intercepted until _adopt_new_page caught up. Page-level stays
            # the fallback for fakes that only model page.route.
            ctx_route = getattr(browser, "route", None)
            if callable(ctx_route):
                await ctx_route("**/*", session._intercept)
                session._context_routed = True
            else:
                await page.route("**/*", session._intercept)
            session._refuse_downloads(page)
            # Follow popups / new tabs. Many job boards (WeWorkRemotely, live
            # 2026-07-18) open the application — or a CAPTCHA — in a NEW TAB, and
            # the loop only ever observes session.page, so an un-adopted popup is
            # invisible: the loop keeps reading the old page and reports "no form
            # / no CAPTCHA here" for a page it cannot see. Adopt each new tab under
            # the SAME interceptor + allowlist. A fake browser without on_page just
            # never fires it (the suite stays hermetic).
            hook = getattr(browser, "on_page", None)
            if callable(hook):
                hook(session._on_new_page)
        except Exception:
            await _maybe_await(browser.close())
            raise
        return session

    async def close(self) -> None:
        # Layer D (2026-07-19 "slow browser" round): ONE grep-able line per session
        # so the interception tax is measured, not guessed. Best-effort — logging
        # must never get in the way of the teardown.
        try:
            s = self.stats
            logger.info(
                "browser session summary: "
                f"requests={s.total_requests} ssrf_checks={s.ssrf_checks} "
                f"settle={s.settle_seconds:.1f}s blocked_mut={s.blocked_mutations} "
                f"blocked_host={s.blocked_hosts} blocked_nav={s.blocked_navigations} "
                f"commits={s.allowed_commits}"
            )
        except Exception:
            pass
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

    async def _safe_route(self, action: Callable[[], Any]) -> None:
        """Perform a route continue_/abort, SWALLOWING Playwright's benign
        'already handled / target closed' races (see _BENIGN_ROUTE_ERRORS). The
        request is already resolved in those cases, so a second action is both
        impossible and unnecessary — and a re-abort of a good request is exactly
        the false 'no internet'. A NON-benign error re-raises to the caller's
        fail-closed handler (an unrendered page beats an unguarded one)."""
        try:
            await action()
        except Exception as exc:
            if any(s in str(exc).lower() for s in _BENIGN_ROUTE_ERRORS):
                logger.debug(f"browser route benign race: {type(exc).__name__}: {exc}")
                return
            raise

    async def _intercept(self, route: Any, *_ignored: Any) -> None:
        """Every request, every frame. Fails closed on a genuine decision error,
        but a BENIGN Playwright route race (the request already resolved) is
        swallowed, never re-aborted — re-aborting a good main-document request is
        the false 'no internet' this guard used to cause (2026-07-19)."""
        try:
            request = route.request
            url = request.url or ""
            method = (request.method or "GET").upper()
            parsed = urlparse(url)
            self.stats.total_requests += 1

            # Browser-internal, not a request to anywhere.
            if parsed.scheme in _LOCAL_SCHEMES:
                await self._safe_route(route.continue_)
                return

            # RULE 1 — the NAVIGATION guard (action-level safety, 2026-07-21).
            # The page's own traffic — XHR/fetch POSTs, search, filters, lazy
            # content, widget verification — flows freely: blanket non-GET
            # aborting broke every SPA (search boxes that POST, "load more",
            # in-page apply flows), and the owner chose the Skyvern model —
            # gate what the AGENT does, not what the page does. What Rule 1
            # still refuses is an UNAPPROVED top-level document non-GET
            # navigation (a classic form-POST submit): the network backstop
            # under the real gates, which are the agent-gesture gate in the
            # loop (an unarmed submit gesture is refused in code) and the
            # one-shot commit permit below.
            #
            # COMMIT (14.5): a non-GET matching the armed, user-approved commit
            # is recognized WHATEVER its transport (a classic form POST or the
            # SPA's background fetch to the same action URL) — the permit is
            # consumed on first match (re-lock) and _commit_fired records the
            # real submission for the submit phase. The approved request still
            # falls through to Rules 2 & 3 — approval never buys past them.
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
                elif self._read_only and self._is_main_frame_navigation(request):
                    self.stats.blocked_mutations += 1
                    if len(self.stats.mutation_urls) < 10:
                        self.stats.mutation_urls.append(f"{method} {url[:120]}")
                    logger.info(
                        f"browser: aborted unapproved form navigation "
                        f"{method} {url[:120]}"
                    )
                    await self._safe_route(route.abort)
                    return
                # else: the page's own non-GET traffic (or the playback
                # hand-off) — flows; Rules 2 & 3 below still apply.

            # RULE 2 — SSRF, same rule read_webpage obeys, shared not copied.
            # SKIPPED for a READ (GET/HEAD/OPTIONS) to an ALLOWLISTED host
            # (2026-07-19 speed round, user-approved): the allowlist is
            # user-grounded trust, goto()/_verify_landing already SSRF-check every
            # navigation there, so a per-request DNS lookup on first-party read
            # subresources is pure tax (it was the dominant per-request cost). Two
            # things keep it safe: a third-party/unknown host is STILL fully checked
            # here, and any NON-GET that reaches this point — an armed commit, a
            # challenge post, a playback POST — is STILL checked even to an
            # allowlisted host, so approval never buys past the SSRF backstop.
            host = parsed.hostname
            skip_ssrf = method in _READ_METHODS and self.origin_allowed(host)
            if host and not skip_ssrf:
                self.stats.ssrf_checks += 1
                if await _host_blocked_cached(host):
                    self.stats.blocked_hosts += 1
                    logger.info(f"browser: aborted request to blocked host {host}")
                    await self._safe_route(route.abort)
                    return

            # RULE 3 — allowlist, MAIN-FRAME navigation only (see docstring:
            # gating subresources by origin means the page never renders).
            if self._is_main_frame_navigation(request) and not self.origin_allowed(host):
                self.stats.blocked_navigations += 1
                logger.info(f"browser: aborted navigation to {host} — not allowlisted")
                await self._safe_route(route.abort)
                return

            await self._safe_route(route.continue_)
        except Exception as exc:
            logger.debug(f"browser intercept: {type(exc).__name__}: {exc}")
            # A benign route race already resolved the request — re-aborting it is
            # the false-offline bug, so only fail-closed-abort on a GENUINE error.
            if any(s in str(exc).lower() for s in _BENIGN_ROUTE_ERRORS):
                return
            try:
                await route.abort()
            except Exception:
                pass

    def _is_main_frame_navigation(self, request: Any) -> bool:
        try:
            if not request.is_navigation_request():
                return False
            frame = getattr(request, "frame", None)
            # Judge against the request's OWN page main frame. This interceptor is
            # shared across every tab we adopt (a popup), so self.page may be a
            # DIFFERENT tab than the one this request came from — using self.page's
            # main frame would mislabel a popup's top-level navigation. Fall back to
            # self.page when the frame's page is unavailable (fakes / edge cases).
            main = None
            owner = getattr(frame, "page", None)
            if owner is not None:
                main = getattr(owner, "main_frame", None)
            if main is None:
                main = getattr(self.page, "main_frame", None)
            if frame is None or main is None:
                return True  # cannot tell → treat as top-level (fail closed)
            return frame == main
        except Exception:
            return True

    # ---------------------------------------------------------- popup / new tab
    def _on_new_page(self, page: Any) -> None:
        """Context 'page' event — a popup or target=_blank tab just opened.
        Playwright dispatches this synchronously on the browser loop, so schedule
        the async adopt (routing + observe-swap). Never raises into the dispatch."""
        try:
            asyncio.ensure_future(self._adopt_new_page(page))
        except Exception as exc:
            logger.debug(f"popup schedule: {type(exc).__name__}: {exc}")

    async def _adopt_new_page(self, page: Any) -> None:
        """Follow a popup / new tab the AGENT'S OWN interaction opened, and only
        that: a tab whose opener is a page we drive. An unrelated popup (an ad
        window) used to unconditionally become self.page — hijacking the loop's
        active page mid-task — so anything else is CLOSED, not adopted. The
        superseded tab is closed too: the loop only ever drives one page, and
        un-closed old tabs accumulated for the life of the session. Following a
        popup grants NO new capability — the new tab is under the same
        interceptor (context-level from birth on the real path), SSRF guard,
        and allowlist before the loop ever drives it. Best-effort — never
        raises, and a fake page without opener/close just adopts as before."""
        try:
            get_opener = getattr(page, "opener", None)
            if callable(get_opener):
                opener = await _maybe_await(get_opener())
                if opener is not None and opener is not self.page:
                    await _maybe_await(page.close())
                    logger.info("browser: closed an unrelated popup (not our tab's)")
                    return
        except Exception as exc:
            logger.debug(f"popup opener check: {type(exc).__name__}: {exc}")
        if not getattr(self, "_context_routed", False):
            try:
                await page.route("**/*", self._intercept)
            except Exception as exc:
                logger.debug(f"adopt popup route: {type(exc).__name__}: {exc}")
        self._refuse_downloads(page)
        superseded = self.page
        self.page = page
        logger.info("browser: following a new tab as the active page")
        if superseded is not None and superseded is not page:
            try:
                await _maybe_await(superseded.close())
            except Exception as exc:
                logger.debug(f"close superseded tab: {type(exc).__name__}: {exc}")

    def _refuse_downloads(self, page: Any) -> None:
        """Downloads are refused wholesale (action-level policy): nothing in the
        stack consumes a downloaded file, and an unprompted download is a
        classic drive-by. Best-effort — a fake page without .on just skips."""
        try:
            hook = getattr(page, "on", None)
            if not callable(hook):
                return

            def _cancel(download: Any) -> None:
                self.stats.blocked_downloads += 1
                logger.info("browser: cancelled a page-initiated download")
                try:
                    asyncio.ensure_future(_maybe_await(download.cancel()))
                except Exception:
                    pass

            hook("download", _cancel)
        except Exception as exc:
            logger.debug(f"download hook: {type(exc).__name__}: {exc}")

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

        # ONE retry on a navigation timeout (2026-07-19): ad-heavy sites (WWR)
        # intermittently blow the 20s budget on the first hit and load fine on
        # the second — three separate live runs each burned a whole browse (and a
        # replan) on a transient first-load timeout. A GET is safe to reissue;
        # bounded to exactly one retry so a truly dead site still fails in ~40s.
        try:
            await self.page.goto(target, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except Exception as exc:
            if "Timeout" not in type(exc).__name__ and "Timeout" not in str(exc):
                raise
            logger.info(f"browser: goto timed out once for {target[:100]} — retrying")
            await self.page.goto(target, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        return await self._verify_landing()

    async def _verify_landing(self) -> str:
        """Re-check where we ACTUALLY ended up. Playwright route handlers do NOT
        re-fire on redirect hops (measured live 2026-07-19: a WWR ad's same-site
        click-tracker 302'd to metana.io and Rule 3 never saw it), so this is the
        one place a redirect landing is judged — the read_webpage precedent.

        A refused landing is BACKED OUT, not just reported: without the go_back
        the session sits ON the off-limits page, the next observation feeds its
        content to the model, and the loop can act on it — the incident run spent
        its whole budget bouncing off an ad page. The off-site case also records
        the target (last_redirect_offsite) so the loop can offer the user the
        same origin-approval pause an off-site link gets."""
        final = self.page.url or ""
        parsed = urlparse(final)
        if parsed.scheme in _LOCAL_SCHEMES:
            return final
        host = parsed.hostname
        if host and await _host_blocked_cached(host):
            await self._back_out()
            raise BrowserBlocked(f"The page redirected to a blocked address ({host}).")
        if host and not self.origin_allowed(host):
            self.last_redirect_offsite = {"host": host, "url": final}
            await self._back_out()
            raise BrowserBlocked(
                f"The page redirected to '{host}', which this task is not allowed to visit."
            )
        return final

    async def _back_out(self) -> None:
        """Best-effort retreat from a refused landing. Failure is tolerable —
        the caller still raises, and the loop's next observation of a wrong page
        is bounded by the same allowlist on every further navigation."""
        try:
            await self.page.go_back(wait_until="domcontentloaded", timeout=10_000)
        except Exception as exc:
            logger.debug(f"browser back-out failed: {type(exc).__name__}: {exc}")

    async def settle(self) -> None:
        """Best-effort wait for the page to go quiet before observing. A busy
        page is a normal outcome, never an error — timing out just means we
        observe slightly earlier.

        Two-stage: a short networkidle wait, then an ADAPTIVE render poll (see
        _wait_for_render). Replaces the old flat 2.5s sleep EVERY step, which paid
        full price on already-stable pages — a big share of per-step latency
        across up to MAX_BROWSER_ACTIONS steps (the 2026-07-19 'slow browser'
        round). Time spent here accrues into stats.settle_seconds for the close
        summary."""
        started = time.monotonic()
        try:
            await self.page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
        except Exception:
            pass
        await self._wait_for_render()
        self.stats.settle_seconds += time.monotonic() - started

    async def _wait_for_render(self) -> None:
        """Poll the DOM node count until it stops growing (or the cap): lazy SPA
        content appearing IS the node count rising, so a stable count means the
        page has painted. Stops after SETTLE_RENDER_STABLE_SAMPLES unchanged
        samples, hard-capped at SETTLE_RENDER_MAX_SECONDS. Generic — no per-site
        knowledge, and a PLAIN count (never dom_observe's stamping extraction), so
        the probe never mutates the page it samples. Never raises."""
        deadline = time.monotonic() + SETTLE_RENDER_MAX_SECONDS
        last = -1
        stable = 0
        while time.monotonic() < deadline:
            try:
                count = int(await self.page.evaluate(
                    "document.getElementsByTagName('*').length"
                ))
            except Exception:
                return  # navigated / closed mid-poll — nothing left to wait for
            if count == last:
                stable += 1
                if stable >= SETTLE_RENDER_STABLE_SAMPLES:
                    return
            else:
                stable = 0
                last = count
            try:
                await asyncio.sleep(SETTLE_RENDER_POLL_SECONDS)
            except Exception:
                return

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
            # Layer C (2026-07-19): make it grep-able which path won, since media
            # being slow means the interceptor was NOT lifted (the degraded branch).
            logger.info("browser: interception LIFTED for playback (full-speed window)")
        except Exception as exc:
            logger.warning(
                "browser: playback unroute FAILED — running DEGRADED fallback "
                "(interceptor stays installed; player works but the window is "
                f"slow): {type(exc).__name__}: {exc}"
            )
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
# The slot itself lives in app.browser.registry (ONE generic implementation
# for all five held-session slots); these wrappers keep the domain-named API
# every caller and test uses.
_MEDIA = _held.REGISTRIES["media"]


async def register_media(session: "BrowserSession", *, title: str, url: str) -> None:
    """Adopt a live session as THE current media session, closing any previous
    one. After this the caller must NOT close the session — the registry owns its
    lifetime until stop_media()."""
    await _MEDIA.hold(session, {"title": title or "", "url": url or ""})


async def stop_media() -> bool:
    """Close the current media session and clear the registry. True when a
    session was actually closed. Idempotent — stopping nothing is not an error."""
    return await _MEDIA.discard()


def active_media() -> Optional[dict[str, str]]:
    """{title, url} for the current media session, or None. Cheap, no I/O — the
    StatusBar polls this freely (the context_status precedent)."""
    return _MEDIA.peek()


async def reset_media() -> None:
    """Test/shutdown hook — close and clear EVERY held session slot plus the
    sign-in window. Delegates to registry.close_all_held(), so every slot is
    covered BY CONSTRUCTION — the old hand-listed version silently missed the
    discovery slot, the exact bug class the registry table exists to end."""
    await _held.close_all_held()
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
_RESULT = _held.REGISTRIES["result_window"]


async def register_result_window(
    session: "BrowserSession", *, title: str, url: str
) -> None:
    """Adopt a just-submitted session as THE open result window, closing any
    previous one. After this the caller must NOT close the session — the registry
    owns its lifetime until close_result_window()."""
    await _RESULT.hold(session, {"title": title or "", "url": url or ""})


async def close_result_window() -> bool:
    """Close the open result window and clear the registry. True when a window was
    actually closed. Idempotent — closing nothing is not an error."""
    return await _RESULT.discard()


def active_result_window() -> Optional[dict[str, str]]:
    """{title, url} for the open result window, or None. Cheap, no I/O — the
    StatusBar polls it (the active_media precedent)."""
    return _RESULT.peek()


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
_COMMIT = _held.REGISTRIES["commit"]


async def hold_commit(session: "BrowserSession", *, state: dict[str, Any]) -> None:
    """Hold a discovered-but-unsubmitted session across the approval pause,
    closing any previously held one. After this the caller must NOT close the
    session — the registry owns it until take_commit()/discard_commit()."""
    await _COMMIT.hold(session, state or {})


async def take_commit() -> Optional["BrowserSession"]:
    """Remove and return the held commit session (the submit phase owns it now),
    or None when there is none — a restart/timeout dropped it, and the submit
    must report that rather than invent a submission."""
    return await _COMMIT.take()


async def discard_commit() -> bool:
    """Close and clear a held commit session without submitting (cancel /
    shutdown / a superseding discovery). True when one was actually closed."""
    return await _COMMIT.discard()


def pending_commit() -> Optional[dict[str, Any]]:
    """The approved-form state of the held commit session, or None. Cheap, no
    I/O (the active_media precedent)."""
    return _COMMIT.peek()


# ------------------------------------------------------- challenge sessions
# EMBEDDED-challenge hand-off (2026-07-19). An embedded widget's token is bound
# to the page render in THIS window — it is not a cookie and cannot transfer
# from a separate hand-off window (the defect behind the "solved it, asked
# again" loop). So when a commit flow pauses on an unsolved widget, the live
# session — sitting on the FILLED form, vendor traffic armed — is held HERE
# across the pause, and the user solves the widget by hand in the very window
# the agent was driving (it is headed and watchable by design). The resumed
# discovery takes the session back, disarms the vendor carve-out, and carries
# on to the approval pause. One slot, memory-only, exactly the commit-session
# rules: a restart drops it and the resume reports it honestly; a new discovery
# or a plan cancel discards it.
_CHALLENGE = _held.REGISTRIES["challenge"]


async def hold_challenge(session: "BrowserSession", *, meta: dict[str, Any]) -> None:
    """Hold a mid-flow session across an embedded-challenge hand-off, closing
    any previously held one. The registry owns the session until
    take_challenge()/discard_challenge()."""
    await _CHALLENGE.hold(session, meta or {})


async def take_challenge() -> Optional["BrowserSession"]:
    """Remove and return the held challenge session (the resumed discovery owns
    it now), or None — a restart dropped it and the resume starts fresh."""
    return await _CHALLENGE.take()


async def discard_challenge() -> bool:
    """Close and clear a held challenge session (cancel / shutdown / a
    superseding discovery). True when one was actually closed."""
    session = await take_challenge()
    if session is None:
        return False
    await session.close()
    return True


def pending_challenge() -> Optional[dict[str, Any]]:
    """{kind, site, url} for the held challenge session, or None. Cheap, no I/O."""
    return _CHALLENGE.peek()


# ------------------------------------------------------- discovery sessions
# HOLD-ACROSS-PAUSE for a commit DISCOVERY that stopped to ask the user something
# (2026-07-19). Before this, a discovery that paused to ask for a missing form
# value (fill) or to approve leaving the named site (origin) was CLOSED in the
# finally — so Chrome visibly shut the moment the question appeared ("it filled
# two fields and then closed the chrome"; "it closed chrome before asking my
# permission"), and the resumed run relaunched and re-did everything. Now the
# live, part-filled session is HELD here across the pause and RE-ATTACHED on
# resume, so the window stays open and the loop carries on from where it stopped.
#
# One slot, memory-only — the commit/challenge-session rules exactly: a restart
# drops it and the resumed discovery starts fresh (honest, never a phantom
# window); a new discovery, a plan cancel, or a sign-in hand-off discards it
# (one profile = one live context). `meta.reason` is "fill" | "origin" | "auth"
# and `meta.goal` scopes the re-attach to THIS goal (a stale hold from an
# abandoned flow is discarded, never resumed onto the wrong page).
_DISCOVERY = _held.REGISTRIES["discovery"]


async def hold_discovery(session: "BrowserSession", *, meta: dict[str, Any]) -> None:
    """Hold a commit-discovery session across a fill/origin/auth pause, closing
    any previously held one. The registry owns the session until
    take_discovery()/discard_discovery() — the caller must NOT close it."""
    await _DISCOVERY.hold(session, meta or {})


async def take_discovery() -> Optional["BrowserSession"]:
    """Remove and return the held discovery session (the resumed discovery owns
    it now), or None — a restart dropped it and the resume starts fresh."""
    return await _DISCOVERY.take()


async def discard_discovery() -> bool:
    """Close and clear a held discovery session (cancel / shutdown / a
    superseding discovery / a sign-in hand-off that needs the profile lock).
    True when one was actually closed."""
    return await _DISCOVERY.discard()


def pending_discovery() -> Optional[dict[str, Any]]:
    """{goal, reason} for the held discovery session, or None. Cheap, no I/O."""
    return _DISCOVERY.peek()


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

# THE CLEAN HAND-OFF WINDOW (2026-07-19). The sign-in / CAPTCHA window is
# launched, in production, as a PLAIN Chrome SUBPROCESS — not Playwright — so it
# carries NONE of automation's tells: no CDP connection, no --enable-automation.
# Cloudflare Turnstile and Google fingerprint the AUTOMATED browser (the CDP
# wire + the automation switch), not merely the checkbox click, and re-issue the
# challenge no matter how many times a human solves it (live report 2026-07-19:
# "I fill the box and it unchecks again and again"). A genuinely clean browser
# lets the user's own solve stick, and the clearance cookie / session lands in
# the shared ~/.jarvis/browser profile the agent then re-attaches to (the
# "warmed profile" resume). This is the LEGITIMATE, non-arms-race fix — we stop
# sabotaging the human with automation flags; we do NOT patch CDP tells, spoof,
# or proxy. It does not GUARANTEE a pass (Turnstile can still re-detect on the
# automated resume), which is exactly why the planner's honest loop-detection
# (_MAX_CHALLENGE_PAUSES) is the guaranteed-correct backstop.
#
# CLEAN_BROWSER_LAUNCHER is the injectable seam (the BROWSER_FACTORY precedent):
# tests point it at a fake so the suite never spawns a real process, and the
# clean path is used in production only (BROWSER_FACTORY is None) — under the
# hermetic test fixture BROWSER_FACTORY is a refuser, so tests stay on the
# Playwright-fake path unless they inject a launcher explicitly. A launcher
# returns a process-like handle (a subprocess.Popen, or a test fake) whose window
# _terminate_clean_proc can close; a failure to find/launch a browser falls back
# to the Playwright login window so the hand-off is never lost.
CLEAN_BROWSER_LAUNCHER: Optional[Callable[[str], Any]] = None

# Standard-location Chrome/Edge executables to try for the clean window, in
# preference order (real Chrome first — the user signs into their own account).
# msedge is the always-present Windows fallback so the clean path is rarely dead.
_CLEAN_BROWSER_RELPATHS = (
    ("ProgramFiles", "Google/Chrome/Application/chrome.exe"),
    ("ProgramFiles(x86)", "Google/Chrome/Application/chrome.exe"),
    ("LOCALAPPDATA", "Google/Chrome/Application/chrome.exe"),
    ("ProgramFiles", "Microsoft/Edge/Application/msedge.exe"),
    ("ProgramFiles(x86)", "Microsoft/Edge/Application/msedge.exe"),
)

_login_browser: Optional[Any] = None
_clean_login_proc: Optional[Any] = None
_login_lock = asyncio.Lock()


def _find_system_browser() -> Optional[str]:
    """Path to a real system Chrome (preferred) or Edge for the clean hand-off
    window, or None when neither is found. Standard install locations first, then
    PATH. None → the caller falls back to the Playwright window."""
    for env_var, rel in _CLEAN_BROWSER_RELPATHS:
        base = os.environ.get(env_var)
        if not base:
            continue
        candidate = Path(base) / rel
        if candidate.exists():
            return str(candidate)
    import shutil

    for name in ("chrome", "chrome.exe", "msedge", "msedge.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _default_clean_launcher(url: str) -> Optional[Any]:
    """Launch a plain system Chrome/Edge on the ~/.jarvis/browser profile as a
    normal, user-driven window — no CDP, no --enable-automation, no
    remote-debugging port (the whole point: a browser Turnstile/Google do not
    read as a bot). Returns the Popen handle, or None when no browser is found."""
    exe = _find_system_browser()
    if not exe:
        return None
    # The no-saved-password / no-autofill posture holds in the clean window too;
    # done HERE (not in _open_clean_login) so an injected test launcher never
    # touches the real ~/.jarvis/browser profile on disk.
    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    _harden_profile(BROWSER_PROFILE_DIR)
    args = [
        exe,
        f"--user-data-dir={BROWSER_PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        url,
    ]
    # A new process group on Windows so terminating it (below) does not signal
    # the backend, and so taskkill /T can reach Chrome's child tree.
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    return subprocess.Popen(args, creationflags=creationflags)


def _clean_login_enabled() -> bool:
    """Whether to use the clean subprocess window. In production (no injected
    BROWSER_FACTORY) yes; under an injected factory (tests) only when a clean
    launcher is also injected — so the hermetic suite never spawns a real Chrome
    and stays on the Playwright fake unless a test opts in explicitly."""
    if CLEAN_BROWSER_LAUNCHER is not None:
        return True
    return BROWSER_FACTORY is None


def _terminate_clean_proc(proc: Any) -> None:
    """Best-effort close of a clean-window subprocess. On Windows a plain
    terminate reaches only the launcher, not Chrome's child processes, so
    taskkill /T closes the whole window tree; falls back to terminate(). Never
    raises — closing is best-effort like every other teardown here."""
    pid = getattr(proc, "pid", None)
    if os.name == "nt" and pid:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=10,
            )
            return
        except Exception as exc:
            logger.debug(f"clean login taskkill: {type(exc).__name__}: {exc}")
    try:
        proc.terminate()
    except Exception as exc:
        logger.debug(f"clean login terminate: {type(exc).__name__}: {exc}")


async def _open_clean_login(url: str) -> Optional[Any]:
    """Launch the clean, non-automation hand-off window and return its handle, or
    None on any failure (no browser found, spawn error, or a hand-off exit) so the
    caller falls back to the Playwright window. The default launcher hardens the
    profile; an injected test launcher touches no real FS. Best-effort — never
    raises."""
    launcher = CLEAN_BROWSER_LAUNCHER or _default_clean_launcher
    try:
        proc = await _maybe_await(launcher(url))
    except Exception as exc:
        logger.warning(
            f"clean sign-in window launch failed, falling back to the automation "
            f"window: {type(exc).__name__}: {exc}"
        )
        return None
    if proc is None:
        return None
    # VERIFY the window actually came up. A real subprocess.Popen exposes poll():
    # while the browser process runs it returns None; if the process EXITED within
    # a couple of seconds it handed the URL to another Chromium on the same profile
    # and closed with no visible window (the reported bug). Treat that as a launch
    # failure so the caller falls back to the Playwright window we control. A test
    # fake with no poll() cannot be verified and is assumed alive (unchanged).
    poll = getattr(proc, "poll", None)
    if callable(poll):
        deadline = time.monotonic() + _CLEAN_LOGIN_VERIFY_SECONDS
        while time.monotonic() < deadline:
            if poll() is not None:
                logger.info(
                    "browser: the clean sign-in window handed off to an existing "
                    "Chrome on the profile and exited — falling back to the "
                    "automation window"
                )
                return None
            await asyncio.sleep(0.2)
    return proc


async def _close_login_handles_locked() -> bool:
    """Close whichever hand-off window is open — the clean subprocess and/or the
    Playwright window — WITHOUT taking _login_lock (the caller holds it). True
    when something was actually closed. Best-effort."""
    global _login_browser, _clean_login_proc
    browser, _login_browser = _login_browser, None
    proc, _clean_login_proc = _clean_login_proc, None
    closed = False
    if browser is not None:
        closed = True
        try:
            await _maybe_await(browser.close())
        except Exception as exc:
            logger.debug(f"login window close: {type(exc).__name__}: {exc}")
    if proc is not None:
        closed = True
        _terminate_clean_proc(proc)
    if closed:
        # A browse re-run right after a sign-in window closes must wait out the
        # same single-instance profile lock (see _settle_profile).
        _mark_profile_released()
    return closed


async def open_login_window(url: str = DEFAULT_LOGIN_URL) -> None:
    """Open the Jarvis browser profile as a normal, user-driven window at a
    sign-in / verification page. Closes any active media session first (one
    profile, one live context). Prefers a CLEAN Chrome subprocess (no automation
    fingerprint — see CLEAN_BROWSER_LAUNCHER) and falls back to the Playwright
    window; raises BrowserUnavailable only when the fallback cannot launch either."""
    global _login_browser, _clean_login_proc
    await stop_media()
    await close_result_window()  # one live persistent context (the profile lock)
    async with _login_lock:
        # Clean, non-automation window (2026-07-19) — strongly preferred so
        # Cloudflare Turnstile / Google don't fingerprint it and the user's
        # manual solve sticks (the clearance cookie lands in the profile).
        if _clean_login_enabled():
            # One profile, one live window: close any prior hand-off so the
            # tracked handle is always the live one.
            await _close_login_handles_locked()
            # If a session (an agent browse, a held discovery, media) was JUST
            # closed, wait out the single-instance profile lock — else the clean
            # window hands off to the dying instance and never appears.
            await _settle_profile()
            proc = await _open_clean_login(url)
            if proc is not None:
                _clean_login_proc = proc
                logger.info(
                    "browser: opened CLEAN sign-in/verification window "
                    "(plain Chrome, no automation)"
                )
                return
            logger.info(
                "browser: no clean system browser available — using the "
                "automation window (Turnstile may still challenge)"
            )

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

        # Same lock discipline for the automation fallback: a session just closed
        # (media/result above, or an upstream discovery discard) may still hold
        # the profile. A no-op when nothing closed recently (or the clean path
        # already spent its verify window).
        await _settle_profile()
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
    """Close the sign-in / verification window if open (clean subprocess and/or
    Playwright). True when one was actually closed."""
    async with _login_lock:
        return await _close_login_handles_locked()


def login_window_open() -> bool:
    """Cheap, no-I/O status for the API/StatusBar (the active_media precedent)."""
    return _login_browser is not None or _clean_login_proc is not None


async def shutdown_browser_windows() -> None:
    """Close every browser window Jarvis has open — EVERY held-session slot
    (media, result window, pending commit, challenge, discovery) AND the
    sign-in/verification window — so a clean backend shutdown leaves NO
    Chromium holding the ~/.jarvis/browser profile lock (the orphan that hangs
    the next run's launch). Completeness is BY CONSTRUCTION: close_all_held()
    iterates the registry table, so a new slot cannot be forgotten here — the
    old hand-listed version shipped without the commit and challenge slots,
    leaking exactly the orphan its own docstring promised to prevent whenever
    the backend stopped mid-approval or mid-challenge. Marshaled onto the
    browser loop from main.py (these touch Playwright objects bound to that
    loop). Best-effort — never raises."""
    try:
        await _held.close_all_held()
    except Exception as exc:
        logger.debug(f"shutdown close held sessions: {type(exc).__name__}: {exc}")
    try:
        await close_login_window()
    except Exception as exc:
        logger.debug(f"shutdown close login window: {type(exc).__name__}: {exc}")
