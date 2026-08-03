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
from app.browser import window as _window
from app.browser.publicsuffix import registrable
from app.tools.browser_tools import (
    _host_is_blocked,
    blocked_host_error,
    parse_web_url,
)

# --------------------------------------------------------------------- limits
BROWSER_PROFILE_DIR = Path.home() / ".jarvis" / "browser"
# OPT-IN unpacked extensions (2026-07-22). The ~/.jarvis/browser profile is a
# Playwright automation browser, so the Chrome Web Store refuses installs; the
# only way an extension reaches this profile is loaded at LAUNCH from an unpacked
# folder (--load-extension). Every immediate subdirectory of this dir that holds
# a manifest.json is loaded, into EVERY window (agent loop + hand-off windows).
# OFF BY DEFAULT: an empty/absent dir adds no flags, so the extension-free posture
# is unchanged until the user drops a folder in. See _extension_load_args().
BROWSER_EXTENSIONS_DIR = Path.home() / ".jarvis" / "browser_extensions"
NAV_TIMEOUT_MS = 20_000
# TWO-PHASE NAVIGATION (2026-07-26). goto() used to wait for "domcontentloaded"
# and treat a timeout as fatal. Live, that killed three of four browser tasks:
# daraz.pk and ebay.com each timed out twice at 20s and the whole browse died —
# while the session stats showed 95 and 349 requests, i.e. the page was loading
# fine, it just never fired DCL inside the budget. DCL waits for the full HTML
# parse plus deferred scripts, which on a heavy commercial page is a coin flip,
# and it is the WRONG QUESTION anyway. What the loop needs to know is not "did
# the parser finish" but "is there anything here to act on yet".
#
#   Phase A — commit: the response arrived and the document is being built.
#             Server-side 30x hops are already followed at commit, so page.url
#             is the final server target (what _verify_landing reads).
#   Phase B — readiness poll: ask the PAGE whether it has actionable content.
#             NEVER fatal. A page with bytes but no readiness is handed to
#             observe() and the page-quality gate decides what it is.
#   Phase C — _verify_landing, now AFTER the poll, so a client-side redirect
#             (meta-refresh, location.replace) gets the whole poll window —
#             strictly more than the sliver DCL used to give it.
#
# Worst case 10 + 10 + 15 = 35s, DOWN from the old 40s double-DCL timeout.
NAV_COMMIT_MS = 10_000
READY_POLL_MS = 15_000
READY_STEP_MS = 250
# STABILITY, not just substance — and this is a MEASURED constant, not a guess.
#
# The first cut asked only "does the page have content yet" and returned the
# instant it did. Measured against daraz.pk's real search results (2026-07-26):
#
#     2.31s  ready=True   acts=  54  nodes= 345   <- the old predicate fired HERE
#     3.82s               acts= 322  nodes=2558
#     5.79s               acts=1026  nodes=4838   <- the products actually arrive
#     6.36s                                       <- node count goes still
#
# A site's HEADER alone satisfies "has content", so the observation ran at 2.3s
# and the loop was handed 11 elements of nav chrome on a page that would shortly
# have 200 including 80 product links. It then asked the model to compare
# products that were not there. Requiring the DOM to STOP GROWING is what moves
# readiness to ~6.4s, where the answer is.
#
# Costs ~500ms on a page that was already still. That is the trade, knowingly:
# half a second per navigation against observing the wrong page.
READY_COMPLETE_POLLS = 2     # readyState 'complete' + this much stillness ⇒ done
READY_STABLE_POLLS = 4       # consecutive settled polls ⇒ substantive AND done
READY_STALL_POLLS = 4        # settled this long ⇒ done even if still thin
# Node counts jitter by a handful as lazy images swap in (measured: 4833 → 4830 →
# 4833 on a settled page), so "settled" is a tolerance, not equality. Exact
# equality would read that jitter as growth and poll to the deadline every time.
READY_GROWTH_TOLERANCE = 1.02
READY_GROWTH_FLOOR = 2
# The navigation-timeout types goto() retries on. Playwright's TimeoutError is
# resolved ONCE here, guarded so a base install without Playwright never fails at
# import (the lazy-dependency rule) — a missing Playwright yields the builtin
# TimeoutError alone (which IS asyncio.TimeoutError on 3.11+). Replaces the old
# brittle `"Timeout" in str(exc)` string-sniff in goto().
try:
    from playwright.async_api import TimeoutError as _PlaywrightTimeoutError
    _NAV_TIMEOUT_ERRORS: tuple[type[BaseException], ...] = (
        _PlaywrightTimeoutError, TimeoutError,
    )
except Exception:
    _NAV_TIMEOUT_ERRORS = (TimeoutError,)
# A launch on the SHARED single-instance profile can HANG indefinitely (not fail)
# when an orphaned Chromium still holds the OS profile lock — Chromium the process
# waits/hands off rather than erroring, and launch_persistent_context has no
# native cap for that state. Live incident 2026-07-20: a plain sign-in window left
# open by the previous backend held ~/.jarvis/browser overnight, and the next
# "play on youtube" browse spun forever (no `browser: launched via chrome` ever
# logged). Bound the launch so a locked profile becomes a clean failure that the
# reclaim-and-retry (below) can self-heal, never an endless spinner.
LAUNCH_TIMEOUT_SECONDS = 45.0
# The CHAIN of channel attempts gets a SHARED budget on top of the per-attempt
# cap (live incident 2026-07-21: a browse burned its whole 180s outer belt inside
# the launch chain — worst case was 3 channels x 2 attempts x 45s = 270s, more
# than the belt itself, so the browse timed out before Chrome ever opened AND
# before the chain could even report which channel failed). An attempt started
# with little budget left runs with the remainder; remainder spent = an honest
# BrowserUnavailable naming every attempt, never a silent outer-belt kill. The
# outer BROWSE_HARD_TIMEOUT is sized against THIS number (pinned test in
# test_browser_runtime.py).
LAUNCH_CHAIN_BUDGET_SECONDS = 120.0
# EVENT-DRIVEN SETTLE (Phase 6 "speed", 2026-07-23). The page tells US when it has
# painted, instead of us polling for it. Two signals RACED, not run in sequence:
#   1. a MutationObserver quiet-window — the DOM going SETTLE_QUIET_MS with no
#      mutations means lazy SPA content has finished appearing (mutations ARE the
#      content painting; measured live 2026-07-17 a YouTube results page observed
#      before its video links rendered). A page already stable fires no mutations
#      and resolves at ~250ms — the residual floor, down from the old sequential
#      networkidle(≤2.5s) + node-count poll(≥0.5s) EVERY step (up to 25 steps).
#   2. networkidle — DEMOTED from the gate it used to be to a mere race participant
#      (a busy analytics/ad page never truly goes idle, so waiting on it alone just
#      burned time). Whichever fires first wins; a hard cap bounds a churning page.
# Generic — a property of client-rendered pages, no per-site knowledge.
SETTLE_QUIET_MS = 250              # DOM quiet window ⇒ painted (in-page)
SETTLE_HARD_CAP_MS = 2_000        # in-page + networkidle belt (ms)
SETTLE_HARD_CAP_SECONDS = 2.0     # outer race deadline (a never-quiet page)

# How long the SUBMIT phase waits for the approved request to actually be
# observed after firing the form (2026-07-26). Bounded and event-driven: it
# returns the instant the interceptor sees the submission, so the normal cost is
# a few hundred ms and only a form that never posts pays the full wait. Sized to
# cover a handler that awaits validation/recaptcha before posting, without
# stalling a genuinely dead form for long.
COMMIT_WAIT_SECONDS = 6.0

# The exact Fetch patterns Playwright's own routing asks Chromium for, so the
# page target's coverage is identical to the fallback path's. ONE definition,
# because the playback lift and the re-arm that undoes it have to agree: a
# re-arm that enabled a narrower pattern set would silently stop guarding some
# traffic while reporting the tab as armed.
_FETCH_PATTERNS = [{"urlPattern": "*", "requestStage": "Request"}]

# A form's declared `action` is not reliably the url its submit hits: the Rails/
# Shopify convention is to POST the SAME path with a `.js`/`.json` representation
# suffix from JS. Recognising exactly those (and nothing else) is what lets an
# ordinary AJAX add-to-cart be reported truthfully — see _is_commit_variant,
# which is RECORDING-only and grants no permission.
_COMMIT_VARIANT_SUFFIXES = (".js", ".json")

# The MutationObserver quiet-window, run in-page. Resolves the instant the DOM has
# been QUIET (no childList/attribute/text mutations) for SETTLE_QUIET_MS, or at the
# in-page belt cap — whichever first. A PLAIN observer (never dom_observe's stamping
# extraction), so sampling never mutates the page it watches; every branch resolves
# (a failed observe() resolves immediately), so page.evaluate never hangs on it and
# the outer race deadline is only a backstop. {q, cap} are injected so the constants
# live in Python. Returns a short reason string the caller ignores.
_QUIET_JS = """({ q, cap }) => new Promise((resolve) => {
  let settled = false, timer = null;
  const done = (why) => {
    if (settled) return;
    settled = true;
    try { obs.disconnect(); } catch (e) {}
    if (timer) clearTimeout(timer);
    resolve(why);
  };
  const arm = () => { if (timer) clearTimeout(timer); timer = setTimeout(() => done('quiet'), q); };
  let obs;
  try {
    obs = new MutationObserver(arm);
    obs.observe(document.documentElement || document, {
      subtree: true, childList: true, attributes: true, characterData: true,
    });
  } catch (e) { return done('no-observer'); }
  arm();                                  // a page that never mutates resolves at q
  setTimeout(() => done('cap'), cap);     // in-page belt
})"""

# The readiness question, asked of the page itself: is there anything here worth
# handing to the loop yet? Deliberately CHEAPER and LOOSER than dom_observe's
# extraction — this runs up to 60 times per navigation, so it counts a plain
# selector and measures two lengths, and it stamps nothing.
#
# "Ready" is two clauses, either sufficient:
#   1. Substantive: ≥3 actionable controls AND ≥200 chars of prose. A real page.
#   2. Done-enough: the parser is no longer 'loading' and there is at least ONE
#      control. A sparse-but-finished page (a login screen, a redirect stub) is
#      ready even though clause 1 will never fire on it.
# The caller adds a third exit outside this script: a node count that stops
# growing (see READY_STALL_POLLS).
_READY_JS = """() => {
  const d = document;
  if (!d || !d.body) return { ready: false, acts: 0, text: 0, nodes: 0 };
  const acts = d.querySelectorAll(
    'a[href],button,input:not([type=hidden]),select,textarea,' +
    '[role=button],[role=link],[role=textbox],[role=searchbox]'
  ).length;
  const text = (d.body.innerText || '').trim().length;
  const nodes = d.getElementsByTagName('*').length;
  const loading = d.readyState === 'loading';
  return {
    ready: (acts >= 3 && text >= 200) || (!loading && acts >= 1),
    // 'complete' means the load event fired and every subresource finished — the
    // one DEFINITIVE "this page is done" signal the platform offers. Measured
    // 2026-07-26: example.com reports it on the first poll (0.66s) while
    // daraz.pk's results page stays 'interactive' until 8.8s, i.e. it separates
    // a trivial page from one still assembling itself. Used as a FAST PATH so a
    // simple page is not made to prove stillness it demonstrated immediately.
    complete: d.readyState === 'complete',
    acts: acts, text: text, nodes: nodes,
  };
}"""

# Chromium net-error markers that mean the site could not be reached AT ALL: the
# plain-language reason for each, plus a machine-readable CLASS.
#
# A slow page is not in this list — that is the whole point of the readiness
# poll. Neither is a policy refusal, which is BrowserBlocked. These are the cases
# where retrying the same URL is pointless and the honest move is to say so and
# let the planner choose another source.
#
# WHY THE CLASS EXISTS, and why only "dns" is acted on (2026-08-01). Voice is a
# first-class input now, and STT cannot spell a proper noun it has never heard —
# live, "junaidjamshed.com" was transcribed "junitjamsheed.com" and the whole
# task died on a domain that simply does not exist. That is a QUESTION worth
# asking ("did you mean…?"), and it is knowable only for NXDOMAIN:
#
#   dns       nothing answers to this NAME. The user may have named the wrong
#             one — the only class where suggesting an alternative makes sense.
#   cert /    the domain EXISTS and answered; the user named it correctly and
#   refused / the site is having a problem. Offering a neighbouring domain here
#   timeout   would be noise at best and a typosquat invitation at worst.
#   offline   OUR network is down. Every candidate lookup would fail too, and
#             the honest report is "no internet" — never a spelling suggestion.
#
# The class is matched on the ERR_ token like the prose is, so the two can never
# disagree about which failure a message describes.
_UNREACHABLE_MARKERS: tuple[tuple[str, str, str], ...] = (
    ("err_cert_", "its HTTPS certificate isn't valid", "cert"),
    ("err_ssl_", "its secure connection failed", "cert"),
    ("err_bad_ssl_", "its secure connection failed", "cert"),
    ("err_name_not_resolved", "that address doesn't resolve", "dns"),
    ("err_name_resolution_failed", "that address doesn't resolve", "dns"),
    ("err_connection_refused", "it refused the connection", "refused"),
    ("err_connection_reset", "the connection was reset", "refused"),
    ("err_connection_closed", "the connection was closed", "refused"),
    ("err_connection_timed_out", "it didn't answer", "timeout"),
    ("err_connection_failed", "the connection failed", "refused"),
    ("err_address_unreachable", "it is unreachable", "refused"),
    ("err_empty_response", "it returned nothing", "refused"),
    ("err_internet_disconnected", "there's no internet connection", "offline"),
    ("err_proxy_connection_failed", "the proxy connection failed", "offline"),
)

# The one class that licenses a "did you mean…?" question. Named so the rule is
# greppable from every consumer instead of a bare == "dns" in four places.
UNREACHABLE_DNS = "dns"


def _network_error(exc: BaseException) -> tuple[str, str]:
    """(plain-language reason, class) for a Chromium net error, or ("", "") when
    the exception is not one. Matched on the ERR_ token, which Chromium puts in
    the message verbatim — the marker is stable across Playwright versions in a
    way the surrounding prose is not."""
    text = str(exc).lower()
    for marker, reason, kind in _UNREACHABLE_MARKERS:
        if marker in text:
            return reason, kind
    return "", ""

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
    # The CDP equivalents (see the _CdpRoute adapter): a request already
    # continued/failed, or one whose page went away, no longer has a valid
    # interception id. Same benign shape, same swallow.
    "invalid interceptionid",
    "invalid interception id",
    "invalid request id",
    "session closed",
)


# ------------------------------------------------- CDP interception adapters
# WHY WE DRIVE Fetch OURSELVES (2026-07-27, MEASURED). Registering ANY Playwright
# route makes its driver pair `Fetch.enable` with `Network.setCacheDisabled: true`
# for the whole session (chromium/crNetworkManager.js). The cache is then OFF —
# every image, script, stylesheet and font re-downloaded on every navigation,
# while the agent loop navigates repeatedly around one site.
#
# Three configurations were measured on books.toscrape.com, second (warm) load:
#     A  Playwright route only ................................ 2.14s
#     B  Playwright route + our own setCacheDisabled(false) .... 2.83s
#     C  no Playwright route, our own Fetch.enable ............. 0.07s
# B is the point: `cacheDisabled` is per-session state and Chromium takes the OR
# across attached sessions, so a second session saying "false" can never overrule
# Playwright's "true". The cache comes back only if Playwright's interception is
# never enabled — which means owning Fetch ourselves.
#
# The patterns we ask for are IDENTICAL to Playwright's (`*`, requestStage
# Request), so coverage of the page target is unchanged; these adapters simply
# present a CDP event in the shape `_intercept` already reads, which is why every
# rule — and every test that pins one — is untouched by this change.
class _CdpFrame:
    """A frame identity that compares the way `_is_main_frame_navigation` needs:
    by CDP frame id."""

    __slots__ = ("id", "page")

    def __init__(self, frame_id: str, owner: Any = None) -> None:
        self.id = frame_id
        self.page = owner

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, _CdpFrame) and other.id == self.id

    def __hash__(self) -> int:
        return hash(self.id)


class _CdpPage:
    """Just enough page for `frame.page.main_frame`."""

    __slots__ = ("main_frame",)

    def __init__(self, main_frame: _CdpFrame) -> None:
        self.main_frame = main_frame


class _CdpRequest:
    """A Fetch.requestPaused event in Playwright-request shape."""

    __slots__ = ("url", "method", "frame", "_document")

    def __init__(self, event: dict, main_frame_id: str) -> None:
        request = event.get("request") or {}
        self.url = str(request.get("url") or "")
        self.method = str(request.get("method") or "GET")
        # CDP names the top-level document load "Document"; Playwright's
        # is_navigation_request() is true for exactly that class of request.
        self._document = str(event.get("resourceType") or "") == "Document"
        main = _CdpFrame(main_frame_id)
        self.frame = _CdpFrame(str(event.get("frameId") or ""), _CdpPage(main))

    def is_navigation_request(self) -> bool:
        return self._document


class _CdpRoute:
    """A Fetch.requestPaused event in Playwright-route shape: exactly one of
    continue_/abort, once. `abort` uses Chromium's `Failed` reason, which is what
    Playwright's own default `route.abort()` sends — so a blocked request looks
    to the page precisely as it did before."""

    __slots__ = ("request", "_cdp", "_id")

    def __init__(self, cdp: Any, event: dict, main_frame_id: str) -> None:
        self._cdp = cdp
        self._id = event.get("requestId")
        self.request = _CdpRequest(event, main_frame_id)

    async def continue_(self) -> None:
        await self._cdp.send("Fetch.continueRequest", {"requestId": self._id})

    async def abort(self) -> None:
        await self._cdp.send(
            "Fetch.failRequest", {"requestId": self._id, "errorReason": "Failed"}
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
# Chrome reads only the LAST --disable-features on a command line, so every
# feature we want off MUST live in ONE string shared by every window (agent +
# clean hand-off) — a second --disable-features anywhere silently drops the rest.
#   - Autofill/Translate/OptimizationHints: chatter trims (the belt for the
#     no-credentials posture; the real mechanism is _harden_profile's prefs).
#   - DisableLoadExtensionCommandLineSwitch: Chrome 137+ (mid-2025) turned OFF the
#     --load-extension switch by default; on Chrome 150 our unpacked-extension flag
#     was silently ignored and a dropped-in uBlock never loaded (live report
#     2026-07-22). Disabling this feature re-enables the switch where Chrome still
#     honors it. (A profile's OWN Web-Store-installed extensions load without any
#     of this — see _extension_load_args.)
_DISABLE_FEATURES = (
    "AutofillServerCommunication,Translate,OptimizationHints,"
    "DisableLoadExtensionCommandLineSwitch"
)

_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--autoplay-policy=no-user-gesture-required",
    # Narrow chatter trims that DON'T disable the connectivity probe (see note
    # above — the blanket --disable-background-networking did, and read as offline).
    "--disable-component-update",
    "--disable-domain-reliability",
    "--no-default-browser-check",
    f"--disable-features={_DISABLE_FEATURES}",
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

def _extension_load_args() -> list[str]:
    """The --load-extension flag for every unpacked extension the user dropped into
    BROWSER_EXTENSIONS_DIR (an immediate subdir with a manifest.json), or [] when
    there are none — so the default extension-free posture adds NOTHING. Best-effort
    and NEVER raises (the _harden_profile discipline: a launch must never fail
    because extension discovery did); a scan/mkdir failure yields [] and the browser
    launches without extensions.

    --load-extension ONLY — NEVER --disable-extensions-except (live report
    2026-07-22, the double-symptom this fixes): that switch disables every OTHER
    extension in the profile, including a uBlock the user installed from the Web
    Store, AND makes a fresh Web-Store install fail to take effect (a new extension
    is not in the "except" list). --load-extension is additive, so the profile's own
    installed extensions keep loading alongside any unpacked one. The switch itself
    is only honored because DisableLoadExtensionCommandLineSwitch is off (see
    _DISABLE_FEATURES) — where Chrome has removed even that escape hatch, a profile's
    Web-Store-installed extension is the reliable path in and needs none of this.

    Applied to the Playwright context AND the clean hand-off subprocess. NOTE the
    CDP agent window refuses --load-extension regardless (see the RULE 0 note in the
    tests) — the extension that matters is the ad blocker in the clean MEDIA/watch
    window, where the interceptor is off during playback."""
    try:
        BROWSER_EXTENSIONS_DIR.mkdir(parents=True, exist_ok=True)
        paths = [
            str(child.resolve())
            for child in sorted(BROWSER_EXTENSIONS_DIR.iterdir())
            if child.is_dir() and (child / "manifest.json").is_file()
        ]
    except Exception as exc:
        logger.debug(f"extension discovery: {type(exc).__name__}: {exc}")
        return []
    if not paths:
        return []
    csv = ",".join(paths)
    logger.info(f"browser: loading {len(paths)} unpacked extension(s) from {BROWSER_EXTENSIONS_DIR}")
    return [f"--load-extension={csv}"]


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
    // THE HUMAN-READABLE LABEL, where the control has one (2026-08-02). A
    // storefront's variant field carries an opaque id — the live approval card
    // read `properties[_Barcode]: PM135415-100-999-M`, which nobody can check.
    // Only two sources, both the page's OWN text: the selected <option>'s
    // caption, and a checked radio's label. DISPLAY ONLY — the approval
    // fingerprint is (name, value), so this can never change what was approved.
    let label = '';
    try {
      if (c.tagName === 'SELECT' && c.selectedOptions && c.selectedOptions.length) {
        label = String(c.selectedOptions[0].text || '').trim();
      } else if (type === 'radio') {
        const lab = (c.labels && c.labels.length) ? c.labels[0] : null;
        label = String((lab && lab.innerText) || c.getAttribute('aria-label') || '').trim();
      }
    } catch (e) {}
    if (label.length > 120) label = label.slice(0, 120) + '…';
    const entry = { name: String(c.name), value: v };
    if (label && label !== v) entry.label = label;
    fields.push(entry);
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

# Find the TRACK a range-slider handle rides on (for the drag `to_fraction`
# path). From the handle, walk a few ancestors and return the box of the first
# that looks like a slider track: an explicit slider/track/rail role or class, or
# (fallback) simply an ancestor much longer than the handle on the drag axis —
# a horizontal track is far wider than its knob, a vertical one far taller. Best
# guess only; null when nothing plausible is found, and the caller then refuses
# the drag with an honest note rather than sliding to a made-up position.
_SLIDER_TRACK_JS = """(el) => {
  const hr = el.getBoundingClientRect();
  let node = el.parentElement;
  let hops = 0;
  while (node && hops < 6) {
    const r = node.getBoundingClientRect();
    const role = (node.getAttribute && (node.getAttribute('role') || '')) || '';
    const cls = (node.className && node.className.toString
      ? node.className.toString() : '').toLowerCase();
    const named = role === 'slider' || role === 'group' ||
      /slider|track|rail|range|scrubber/.test(cls);
    const widerX = r.width >= (hr.width || 1) * 2 && r.width > 40;
    const tallerY = r.height >= (hr.height || 1) * 2 && r.height > 40;
    if ((named && (widerX || tallerY)) || widerX || tallerY) {
      return { x: r.left, y: r.top, w: r.width, h: r.height };
    }
    node = node.parentElement;
    hops++;
  }
  return null;
}"""

# BROWSER_FACTORY() -> browser handle exposing async new_page() and close().
# None = the real Chromium path below.
BROWSER_FACTORY: Optional[Callable[[], Any]] = None


class BrowserUnavailable(RuntimeError):
    """Playwright (or its Chromium download) is not installed."""


class BrowserBlocked(RuntimeError):
    """A navigation was refused by the allowlist or the SSRF guard. Carries a
    user-facing reason — code-authored, never LLM prose."""


class BrowserUnreachable(RuntimeError):
    """The site could not be reached at all — bad certificate, DNS failure,
    refused/reset connection, no internet.

    Three things this is NOT, and the distinctions are load-bearing:
      - NOT a policy refusal. That is BrowserBlocked, and it means the site was
        reachable and we chose not to go there.
      - NOT a slow page. Since the readiness poll, a page that loads slowly is a
        non-event — it gets observed anyway.
      - NOT an invitation to guess another address. outfitters.com failing its
        certificate check does NOT license a browse to try outfitters.com.pk:
        an origin the user never named is outside the grounding corpus by
        construction (browser/grounding.ground_origins), and inventing one here
        would route around the exfiltration bound the whole browse stack rests
        on. The honest move is to report which site failed and why, and let the
        planner ask or choose another source.

    `kind` (2026-08-01) is the machine-readable class from _UNREACHABLE_MARKERS —
    "dns" | "cert" | "refused" | "timeout" | "offline" | "". It exists so a
    consumer can tell "no such NAME" from "that site is having a problem"
    WITHOUT re-reading our own prose. It does not relax the rule above by one
    inch: a "dns" failure still licenses no guess, only a QUESTION whose options
    the user has to pick from (planner._site_correction_question). Suggesting is
    not visiting; the user's answer is what grounds the origin.

    `host` is the site that failed, so a consumer never has to parse it back out
    of the message.
    """

    def __init__(self, message: str, *, kind: str = "", host: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.host = host


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


_host_block_inflight: dict[str, asyncio.Future] = {}


async def _host_blocked_cached(host: str) -> bool:
    key = host.strip().lower().rstrip(".")
    cached = _host_block_cache.get(key)
    if cached is not None:
        return cached

    # SINGLE FLIGHT. A page's first load fires many requests at the same handful
    # of cold CDN hosts within milliseconds, and every one of them used to miss
    # the cache and spawn its OWN to_thread(getaddrinfo) before the first wrote
    # its result — a dozen threads resolving one name. The cache made the STEADY
    # state cheap and left the stampede in place. Later arrivals now await the
    # first lookup instead of duplicating it.
    inflight = _host_block_inflight.get(key)
    if inflight is not None:
        return await asyncio.shield(inflight)

    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    _host_block_inflight[key] = future
    try:
        blocked = await asyncio.to_thread(_host_is_blocked, key)
    except BaseException as exc:
        if not future.done():
            future.set_exception(exc)
        _host_block_inflight.pop(key, None)
        # Never swallow: the caller's own guard decides what an unresolvable host
        # means, and it already fails closed.
        raise
    _host_block_cache[key] = blocked
    if not future.done():
        future.set_result(blocked)
    _host_block_inflight.pop(key, None)
    return blocked


def reset_host_cache() -> None:
    """Test/shutdown hook — the cache is a performance detail, never state."""
    _host_block_cache.clear()
    _host_block_inflight.clear()


# ------------------------------------------------------- ad / tracker block
# WHY THIS LIVES IN CODE, NOT IN AN EXTENSION (2026-07-22). The agent-loop
# window is driven over CDP, and current Chrome REFUSES to load an unpacked
# extension (uBlock) in a CDP-controlled browser — proven live: chrome://
# extensions shows zero, no extension service worker registers, a known ad
# script still loads. So ad/tracker network blocking lives HERE, in the
# interceptor we already own for READ-mode. It only ever ABORTS requests to
# known ad/tracker hosts, so it STRENGTHENS the READ-mode guarantee — it never
# lets anything through. The payoff is two-fold and exactly what the user asked
# for: an ad's iframe/script that never loads is a fake "Play" button the loop
# can never SEE or MISCLICK, and far fewer requests means a faster, cleaner
# browse on ad-heavy streaming sites. Content hosts are NEVER on this list, so
# no legitimate goal is affected. uBlock still covers the normal hand-off
# window (a real, non-automation Chrome, where it does load).
#
# A compact, high-value list — the big trackers plus the ad networks that
# blanket pirate/streaming/anime sites with pop-unders and fake players. This
# is not a full EasyList; it targets the hosts that actually produce
# misclickable ads. Matched subdomain-aware (host == d or endswith '.'+d).
_AD_HOSTS: frozenset[str] = frozenset({
    # Google ad/analytics stack
    "doubleclick.net", "googlesyndication.com", "googletagservices.com",
    "googleadservices.com", "google-analytics.com", "googletagmanager.com",
    "adservice.google.com", "pagead2.googlesyndication.com",
    # Big programmatic exchanges / trackers
    "amazon-adsystem.com", "adnxs.com", "rubiconproject.com", "pubmatic.com",
    "criteo.com", "criteo.net", "casalemedia.com", "openx.net", "3lift.com",
    "moatads.com", "scorecardresearch.com", "quantserve.com", "taboola.com",
    "outbrain.com", "revcontent.com", "mgid.com", "bidswitch.net",
    "sharethrough.com", "smartadserver.com", "yieldmo.com", "adform.net",
    # Pop-under / redirect ad networks that plague streaming & anime sites
    "popads.net", "popcash.net", "propellerads.com", "propellerclick.com",
    "propu.sh", "exoclick.com", "exosrv.com", "juicyads.com", "hilltopads.net",
    "adsterra.com", "adsterranetwork.com", "poweredby.jads.co", "clickadu.com",
    "trafficjunky.net", "trafficjunky.com", "admaven.com", "onclickalgo.com",
    "onclickmax.com", "clickmoi.com", "adcash.com", "coinzilla.com",
    "a-ads.com", "monetag.com", "pushncode.com", "highperformanceformat.com",
    "bebi.com", "histats.com", "luckyorange.com", "hotjar.com",
})


def _is_ad_host(host: Optional[str]) -> bool:
    """True when a request host is a known ad/tracker (subdomain-aware). Cheap,
    no I/O, no DNS — a pure suffix check, so it can front every request."""
    if not host:
        return False
    h = host.strip().lower().rstrip(".")
    for d in _AD_HOSTS:
        if h == d or h.endswith("." + d):
            return True
    return False


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

    async def unroute(self, pattern: str, handler: Callable[..., Any]) -> None:
        """Remove the interceptor AT THE LEVEL IT WAS INSTALLED — the context.

        This exists because its absence was a silent bug (found 2026-07-27).
        `enter_playback_mode` called `page.unroute(...)`, and Playwright's
        page-level unroute filters only that page's OWN route list; a
        context-level handler is not in it, so the call found nothing, returned
        successfully, and we logged "interception LIFTED for playback
        (full-speed window)" about a window that was still fully intercepted
        with its HTTP cache disabled. A no-op that reports success is worse than
        a failure, because nothing ever looks at it again."""
        await self._context.unroute(pattern, handler)

    async def new_cdp_session(self, page: Any) -> Any:
        """A raw CDP session on one page's target — how we drive the Fetch domain
        ourselves instead of registering a Playwright route, which would disable
        Chromium's HTTP cache session-wide (see
        BrowserSession._install_cdp_interception)."""
        return await self._context.new_cdp_session(page)

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
        # Close only the persistent CONTEXT — the Playwright driver is SHARED
        # (started once, reused across browses; see ensure_playwright_driver) and
        # is stopped only at shutdown via stop_playwright_driver. Stopping it per
        # browse is exactly the per-turn cold-spawn this design removes.
        try:
            await self._context.close()
        except Exception as exc:  # a half-dead context must not raise here
            logger.debug(f"browser teardown: {type(exc).__name__}: {exc}")
        # A Chromium on ~/.jarvis/browser can still hold the single-instance
        # profile lock for a moment after close() returns; record when we let go
        # so a hand-off window launched right after settles first (see
        # _settle_profile / the 2026-07-19 "opened a sign-in window but it didn't"
        # fix).
        _mark_profile_released()


# ------------------------------------------------- shared Playwright driver
# The Playwright Node driver (async_playwright().start()) is spawned ONCE and
# reused for every launch — NOT per session. WHY (live incident 2026-07-22, the
# "processing forever" hang): the driver is a Node subprocess spawn that can
# STALL COLD on the first browse — 98s of dead silence, no log, no timeout,
# hung at async_playwright().start() BEFORE any launch attempt logged. Three
# faults compounded: (1) the spawn had no log on either side (invisible); (2)
# its asyncio.wait_for(..., 30) did not bound it — wait_for cancels the inner
# coroutine then AWAITS that cancellation, and a Windows subprocess spawn that
# ignores cancellation hangs wait_for past its own timeout (proven: the 30s
# mark passed with zero events); (3) every _RealBrowser.close() stopped its own
# driver, so a cold spawn happened inside the chat turn on EVERY browse.
#
# The fix: start the driver once at backend startup on the dedicated browser
# loop (main._prewarm_browser_stack → ensure_playwright_driver), keep it as a
# module singleton, and reuse it — so a cold spawn (and any stall) happens in
# the background at startup, never in a user's chat turn, and every later
# launch is just a launch_persistent_context.
#
# _PLAYWRIGHT_STARTER is the injectable seam (the BROWSER_FACTORY / _PROFILE_REAPER
# precedent) so the hermetic suite drives start / stall / restart without ever
# spawning a real Node driver.
DRIVER_START_TIMEOUT_SECONDS = 30.0
_shared_playwright: Optional[Any] = None
_driver_lock: Optional[asyncio.Lock] = None
_driver_lock_loop: Optional[asyncio.AbstractEventLoop] = None
# A start we abandoned (timed out) may still complete later; hold a reference so
# it is not GC'd mid-flight, and stop the stray driver when it lands.
_DETACHED_STARTS: set = set()
_PLAYWRIGHT_STARTER: Optional[Callable[[], Any]] = None


async def _default_playwright_starter() -> Any:
    """Import Playwright lazily and start its Node driver — the one place the
    optional dependency is touched, so a base install without it fails clean."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise BrowserUnavailable(
            "Browser control needs Playwright, which is not installed. "
            "Install it with: pip install playwright"
        ) from exc
    return await async_playwright().start()


def _get_driver_lock() -> asyncio.Lock:
    """Serialize driver start, rebinding the lock per running loop (the
    HeldSessionRegistry._get_lock rule) — the hermetic suite drives this from a
    fresh loop per test, and an asyncio.Lock binds to the first loop that awaits
    it. On the long-lived browser loop the rebind branch never fires after the
    first call."""
    global _driver_lock, _driver_lock_loop
    loop = asyncio.get_running_loop()
    if _driver_lock is None or _driver_lock_loop is not loop:
        _driver_lock = asyncio.Lock()
        _driver_lock_loop = loop
    return _driver_lock


def _stop_stray_driver(task: "asyncio.Future") -> None:
    """A driver start we ABANDONED (timed out) may still complete later; stop the
    stray driver so a slow spawn does not leak a Node process. Best-effort."""
    if task.cancelled() or task.exception() is not None:
        return
    driver = task.result()

    async def _stop() -> None:
        try:
            await driver.stop()
        except Exception:
            pass

    try:
        asyncio.get_running_loop().create_task(_stop())
    except Exception:
        pass


async def ensure_playwright_driver() -> Any:
    """Return the live shared Playwright driver, starting it if absent. Idempotent
    and serialized. GENUINELY BOUNDED: a stalled spawn (which ignores cancellation,
    so asyncio.wait_for would itself hang awaiting the cancel — the 2026-07-22
    incident) is ABANDONED in a detached task and turned into a named, retryable
    BrowserUnavailable within DRIVER_START_TIMEOUT_SECONDS, never an endless
    spinner. Logs the spawn on both sides so the step is never invisible again.
    Runs on the dedicated browser loop (Playwright objects are loop-bound)."""
    global _shared_playwright
    if _shared_playwright is not None:
        return _shared_playwright
    async with _get_driver_lock():
        if _shared_playwright is not None:  # settled while we waited for the lock
            return _shared_playwright
        starter = _PLAYWRIGHT_STARTER or _default_playwright_starter
        logger.info("browser: starting Playwright driver…")
        started = time.monotonic()
        task = asyncio.ensure_future(starter())
        try:
            done, _pending = await asyncio.wait(
                {task}, timeout=DRIVER_START_TIMEOUT_SECONDS
            )
        except asyncio.CancelledError:
            task.cancel()
            raise
        if task not in done:
            # Stalled. Do NOT await its cancellation (awaiting an uncancellable
            # subprocess spawn is the hang itself). Detach it so a late driver is
            # stopped rather than leaked, and fail clean + retryable.
            _DETACHED_STARTS.add(task)
            task.add_done_callback(_DETACHED_STARTS.discard)
            task.add_done_callback(_stop_stray_driver)
            logger.warning(
                "browser: Playwright driver did not start within "
                f"{DRIVER_START_TIMEOUT_SECONDS:.0f}s — abandoning; will retry"
            )
            raise BrowserUnavailable(
                "The browser driver did not start in time — try again in a moment."
            )
        driver = task.result()  # re-raises a real start error (ImportError → BrowserUnavailable)
        _shared_playwright = driver
        logger.info(
            f"browser: Playwright driver ready in {time.monotonic() - started:.1f}s"
        )
        return driver


def reset_playwright_driver() -> None:
    """Drop the shared driver reference so the NEXT ensure_playwright_driver()
    re-warms a fresh one (restart-on-death). Does NOT await a stop — used after a
    launch that failed because the driver was gone, and a dead driver has nothing
    to stop."""
    global _shared_playwright
    _shared_playwright = None


async def stop_playwright_driver() -> None:
    """Stop the shared Playwright driver — called once at shutdown. Best-effort and
    idempotent. Must run on the browser loop (the driver is loop-bound)."""
    global _shared_playwright
    driver = _shared_playwright
    _shared_playwright = None
    if driver is None:
        return
    try:
        await driver.stop()
    except Exception as exc:
        logger.debug(f"stop playwright driver: {type(exc).__name__}: {exc}")


async def _default_browser_factory() -> Any:
    # The SHARED driver, warmed once at startup (see ensure_playwright_driver) —
    # so this factory is just a launch_persistent_context, never a cold Node
    # subprocess spawn inside the chat turn.
    playwright = await ensure_playwright_driver()

    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    # SESSIONS, NOT CREDENTIALS: turn off the profile's password manager/autofill
    # and drop any saved password before launch, so no credential is ever
    # auto-filled in the user-driven login window (see _harden_profile). Covers
    # both the agent session and open_login_window — every window reaches here.
    _harden_profile(BROWSER_PROFILE_DIR)

    chain_deadline = time.monotonic() + LAUNCH_CHAIN_BUDGET_SECONDS

    async def _launch_channel(channel: Optional[str], remaining: float) -> Any:
        """Launch one channel, bounded by LAUNCH_TIMEOUT_SECONDS AND the chain's
        remaining shared budget. A launch that HANGS on a locked profile (rather
        than erroring) becomes a timeout — a working browser launches in ~2-3s
        (per the live logs), so a timeout is a strong lock signature, distinct
        from an install error."""
        return await asyncio.wait_for(
            playwright.chromium.launch_persistent_context(
                user_data_dir=str(BROWSER_PROFILE_DIR),
                headless=False,           # the user watches — see the docstring
                service_workers="block",  # rule 1 is void without this
                args=list(_LAUNCH_ARGS) + _extension_load_args(),
                **({"channel": channel} if channel else {}),
            ),
            timeout=min(LAUNCH_TIMEOUT_SECONDS, remaining),
        )

    # Every attempt outcome LOGS IMMEDIATELY (2026-07-21: the whole chain died
    # inside the outer browse belt with zero log lines, because failures only
    # accumulated into this list for the terminal raise that never ran — the
    # incident could not be root-caused from data).
    errors: list[str] = []
    try:
        for channel in _CHANNELS:
            name = channel or "bundled chromium"
            # Two attempts per channel: the original, and one retry after a
            # reclaim actually freed the profile.
            for _attempt in range(2):
                remaining = chain_deadline - time.monotonic()
                if remaining <= 0:
                    errors.append(
                        f"launch budget ({LAUNCH_CHAIN_BUDGET_SECONDS:.0f}s) "
                        f"spent before trying {name}"
                    )
                    logger.warning(f"browser: {errors[-1]}")
                    raise _launch_failure(errors)
                logger.info(f"browser: launching via {name}…")
                try:
                    context = await _launch_channel(channel, remaining)
                except asyncio.TimeoutError:
                    errors.append(f"{name}: launch timed out (profile likely locked)")
                    logger.warning(f"browser: {errors[-1]}")
                    # A timeout is the lock signature — an orphaned Jarvis-profile
                    # Chrome from a prior run, OR the half-spawned Chrome this very
                    # cancelled launch may have left behind (a wait_for cancel does
                    # not un-spawn the process). Reclaim after EVERY timeout so a
                    # self-inflicted orphan can't poison the rest of the chain;
                    # retry this channel iff the reclaim actually freed something.
                    # Off-loop (it shells out) so the browser loop is never blocked.
                    if await asyncio.to_thread(reclaim_orphaned_profile) and _attempt == 0:
                        await _settle_profile()  # let the killed process let go
                        continue  # retry this same channel
                    break  # nothing freed (or already retried) → next channel
                except Exception as exc:
                    # An install/config error (channel not present) — the reclaim
                    # would not help; fall through to the next channel.
                    errors.append(f"{name}: {str(exc)[:120]}")
                    logger.warning(f"browser: launch failed — {errors[-1]}")
                    break
                context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
                logger.info(f"browser: launched via {name}")
                return _RealBrowser(playwright, context)

        raise _launch_failure(errors)
    except BrowserUnavailable:
        # A total launch failure may mean the SHARED driver wedged (vs. just a
        # locked profile); drop it so the NEXT browse re-warms a fresh one. Never
        # stop it here — it is shared, and re-warming is cheap and logged.
        reset_playwright_driver()
        raise
    except asyncio.CancelledError:
        # The OUTER browse belt fired mid-launch. Reclaim any half-spawned
        # profile-holding Chrome in a detached task (a cancelled coroutine's own
        # finally cannot await without re-raising). The SHARED driver is left
        # running — it is reused by the next browse, not owned by this launch.
        _schedule_launch_cleanup()
        raise


def _launch_failure(errors: list[str]) -> BrowserUnavailable:
    return BrowserUnavailable(
        "Could not launch a browser. Install one of Playwright's Chromium, "
        "Microsoft Edge, or Google Chrome — the simplest is: "
        "playwright install chromium\n" + "\n".join(errors)
    )


# References to detached cleanup tasks — an unreferenced asyncio task can be
# garbage-collected mid-flight, silently skipping the cleanup.
_CLEANUP_TASKS: set = set()


def _schedule_launch_cleanup() -> None:
    """Best-effort reclaim after an ABANDONED launch (the outer browse belt
    cancelled us mid-chain): kill any half-spawned Chrome already holding the
    profile lock. Detached because a cancelled coroutine's own finally cannot
    await without re-raising CancelledError. The SHARED Playwright driver is NOT
    stopped here — it outlives any single launch (see ensure_playwright_driver)."""
    async def _cleanup() -> None:
        try:
            await asyncio.to_thread(reclaim_orphaned_profile)
        except Exception as exc:
            logger.debug(f"abandoned-launch reclaim: {type(exc).__name__}: {exc}")

    try:
        task = asyncio.get_running_loop().create_task(_cleanup())
        _CLEANUP_TASKS.add(task)
        task.add_done_callback(_CLEANUP_TASKS.discard)
    except Exception as exc:
        logger.debug(f"abandoned-launch cleanup not scheduled: {type(exc).__name__}: {exc}")


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
    blocked_ads: int = 0
    allowed_commits: int = 0
    blocked_downloads: int = 0
    mutation_urls: list[str] = field(default_factory=list)
    # Performance measurement (2026-07-19 "slow browser" round) — surfaced in the
    # per-session close summary so the interception tax is measured, not guessed.
    total_requests: int = 0     # requests the interceptor fielded
    # CONSULTATIONS, not DNS calls. _host_block_cache is process-global, so the
    # Nth request to a host costs a dict lookup. This counter is incremented
    # BEFORE the cache is consulted, and reading it as a resolution count is a
    # mistake that has already been made once: a 349-request page showing
    # "ssrf_checks=313" was read as 313 DNS lookups and nearly bought a
    # performance fix for a cost that does not exist (2026-07-26). The real
    # navigation cost was domcontentloaded, not this.
    ssrf_checks: int = 0        # host-block lookups consulted (allowlisted GETs skipped)
    settle_seconds: float = 0.0  # cumulative time spent in settle() this session
    # Navigations that committed but never reached readiness inside the poll
    # budget — a heavy site observed mid-build, which is a fact worth having in
    # the log when reading back what the loop saw.
    slow_navigations: int = 0
    # True when interception was installed over our own CDP Fetch, which is the
    # only arrangement that leaves Chromium's HTTP cache ON (Playwright's route
    # disables it session-wide — see the _CdpRoute adapters). In the close
    # summary because a silent fall back to the slow path costs multiples on
    # every repeat page load, and "it got slow again" is not a diagnosis.
    http_cache_on: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "blocked_mutations": self.blocked_mutations,
            "blocked_navigations": self.blocked_navigations,
            "blocked_hosts": self.blocked_hosts,
            "blocked_ads": self.blocked_ads,
            "allowed_commits": self.allowed_commits,
            "blocked_downloads": self.blocked_downloads,
            "mutation_urls": self.mutation_urls[:10],
            "total_requests": self.total_requests,
            "ssrf_checks": self.ssrf_checks,
            "settle_seconds": round(self.settle_seconds, 2),
            "slow_navigations": self.slow_navigations,
            "http_cache_on": self.http_cache_on,
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
        # Has this tab been handed to the USER by enter_playback_mode? While
        # True the interceptor is off and _read_only is False, so the tab is
        # NOT drivable — resume_agent_control() must put both back first. Kept
        # as explicit state rather than inferred from _read_only because the
        # degraded playback branch (unroute failed) leaves the interceptor
        # installed, and "which of the two got lifted" is not recoverable after
        # the fact.
        self._playback = False
        # Did THIS run open this tab, or inherit one the user already had?
        # Only the opener may close it — see release_after_run. Defaults False
        # so a hand-wired session (tests, direct callers) owns and closes its
        # own window exactly as it always did; acquire_browse_tab is the one
        # place that sets it.
        self.tab_reused = False
        # Pages this session armed with a Playwright page route (the fallback
        # path). Recorded so the playback lift can reach every one of THIS
        # session's tabs and none of anybody else's — the interceptor used to be
        # installed and lifted at CONTEXT level, which a shared context turns
        # into "one tab finishing disarms the guard on all the others".
        self._routed_pages: list[Any] = []
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
        # COMMIT OBSERVATION (2026-07-26, the junaidjamshed add-to-cart incident):
        # a form's `action` is NOT reliably the URL its submit actually hits. The
        # site's own theme reads the form and posts to `/cart/add.js` while the
        # form declares action="/cart/add" — so the exact-match permit could never
        # fire, and the submit phase reported "Nothing was sent" about a request it
        # simply could not recognise. These three record what the page ACTUALLY
        # did during the submit window so the report is grounded in observation
        # rather than in the absence of one exact match:
        #   _commit_variant_url — the representation-variant URL that WAS accepted
        #                         as the approved submission (see _is_commit_variant)
        #   _commit_observed    — other same-origin non-GETs seen while armed, so a
        #                         failure can name what the page sent instead
        #   _commit_event       — signalled the instant the submission is observed,
        #                         so the submit phase can WAIT for the request
        #                         instead of for the paint (settle() returns in
        #                         ~250ms and was racing every async handler).
        self._commit_variant_url: str = ""
        self._commit_observed: list[str] = []
        self._commit_event: Optional[asyncio.Event] = None
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
        # Raw CDP sessions driving Fetch ourselves (see _install_cdp_interception)
        # — one per page target. Held, not detached: a detached session's Fetch
        # domain goes away and the guard with it. `_inflight` keeps a strong
        # reference to each in-flight guard task, because a dropped task is a
        # paused request nobody ever answers, which hangs the page.
        self._cdp_sessions: list[Any] = []
        self._cdp_routed = False
        self._inflight: set[Any] = set()
        # MULTI-TAB bookkeeping (2026-08-01). `tab_site` is the registrable
        # domain this tab belongs to — the key a later browse for the same site
        # reuses it by; `tab_meta` is what it is showing (title/url/goal) for the
        # StatusBar; `tab_used_monotonic` orders LRU eviction. Plain attributes
        # rather than a parallel table because the session IS the tab's identity
        # (`_adopt_new_page` swaps `page`, so a page-keyed record would go stale
        # exactly when a tab is most active).
        self.tab_site: str = ""
        self.tab_meta: dict[str, str] = {}
        self.tab_used_monotonic: float = 0.0

    # ------------------------------------------------------------ lifecycle
    @classmethod
    async def open(cls, allowlist: set[str]) -> "BrowserSession":
        """Open a TAB in the shared window (app/browser/window.py).

        This used to launch a whole browser — a persistent context per session —
        which is why one profile could hold only one session and every new browse
        first closed the old window. The context is now a singleton that outlives
        any one session; a session is a tab in it. `window.open_tab` fills in
        `_browser`/`page` and settles the profile lock on the launch path only.
        """
        origins = {o for o in (_normalize_origin(a) for a in allowlist) if o}
        session = cls(None, None, origins)
        await _window.open_tab(session)
        page = session.page
        try:
            await session._install_interception(page)
            session._refuse_downloads(page)
            # Popups / new tabs are followed by the WINDOW's single context
            # listener, wired when the context is launched (window._launch_context).
            # Many job boards (WeWorkRemotely, live 2026-07-18) open the
            # application — or a CAPTCHA — in a NEW TAB, and the loop only ever
            # observes session.page, so an un-adopted popup is invisible. What
            # changed is only WHO decides the tab is ours: registering a listener
            # per session would have every session run that decision on every
            # tab, which is how a session ends up adopting another's page.
        except Exception:
            # Release the TAB, not the browser: other tabs may be live on this
            # shared context, and one session failing to arm its guard must not
            # take their windows down with it. release_tab closes the context
            # only when this was the last tab.
            await _window.release_tab(session)
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
                f"blocked_ads={s.blocked_ads} commits={s.allowed_commits} "
                f"cache={'on' if s.http_cache_on else 'DISABLED'}"
            )
        except Exception:
            pass
        # OWNERSHIP (see app/browser/window.py). A session opened through
        # BrowserSession.open is a TAB of the shared window: closing it closes
        # its page, and the context only when it was the last tab — otherwise one
        # finishing browse would tear down every other open tab, which is the
        # whole behaviour this change exists to remove. A session constructed
        # directly (tests, and any caller holding its own handle) owns that
        # handle and closes it, exactly as it did before the window existed.
        if _window.owns(self):
            await _window.release_tab(self)
            return
        try:
            await _maybe_await(self._browser.close())
        except Exception as exc:
            logger.debug(f"browser close: {type(exc).__name__}: {exc}")

    async def __aenter__(self) -> "BrowserSession":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    async def _rearm_intercept(self) -> None:
        """Put this session's interceptor back on every page the playback lift
        took it off. The exact inverse of _unroute_intercept, and deliberately
        written as its mirror: re-enable on the pages ALREADY recorded rather
        than installing fresh ones. Calling _install_interception again would
        open a SECOND CDP session and register a SECOND handler on the same
        target, so every paused request would be answered twice.

        RAISES on failure, for the same reason _unroute_intercept does: the
        caller must be able to tell an armed tab from an unarmed one, and a
        re-arm that reports success it did not achieve is how a tab ends up
        driving an autonomous loop with Rules 1-3 off."""
        for _page, cdp in self._cdp_sessions:
            await cdp.send("Fetch.enable", {"patterns": _FETCH_PATTERNS})
        for page in self._routed_pages:
            await page.route("**/*", self._intercept)
        if not self._cdp_sessions and not self._routed_pages:
            await self.page.route("**/*", self._intercept)

    async def resume_agent_control(self) -> bool:
        """Take a tab back from the user and make it drivable again. True when
        this tab is safe for a new autonomous run.

        WHY THIS EXISTS (live 2026-08-01, the junaidjamshed add-to-cart): a tab
        that had been handed to the user by enter_playback_mode was later REUSED
        by acquire_browse_tab, which restored the allowlist and the history but
        nothing else. The interceptor was still disabled and _read_only still
        False, so an entire commit flow ran on that tab with Rules 1, 2 and 3
        off — no origin allowlist, no SSRF guard, no one-shot commit permit. The
        approved POST went out unobserved, the cart really did change, and the
        tool reported that nothing had been sent.

        Restoring _read_only is not enough on its own and restoring the route is
        not enough on its own: the first without the second leaves a flag nobody
        reads, the second without the first leaves Rule 1 stood down. Both, or
        the tab is not driven."""
        if not self._playback:
            self._read_only = True
            return True
        try:
            await self._rearm_intercept()
        except Exception as exc:
            logger.warning(
                "browser: could NOT re-arm the interceptor on a tab returning "
                f"from playback ({type(exc).__name__}: {exc}) — leaving it to "
                "the user rather than driving it unguarded"
            )
            return False
        self._read_only = True
        self._playback = False
        logger.info("browser: interception RE-ARMED — the tab is the agent's again")
        return True

    async def release_to_user(self) -> bool:
        """Hand THIS tab to the user and stop driving it — the mirror of
        resume_agent_control, and the non-destructive way to pause on something
        only a human can do.

        WHY THIS EXISTS (live 2026-08-03, the eBay CAPTCHA): an interstitial
        challenge closed the agent session and called open_login_window, whose
        first act is _window.close_all(). Under one-tab-per-window that cost
        nothing — the session WAS the window. Under the shared window it
        destroyed every unrelated tab: the user had junaidjamshed.com open
        beside eBay, eBay showed a CAPTCHA, and both tabs vanished so a clean
        window could reopen eBay alone.

        The escape is that the human does not need a different window to solve a
        challenge that has ALREADY RENDERED in this one. That is not a new
        claim: the embedded-widget hand-off has worked this way since
        2026-07-19 ("the user solves the widget by hand in the very window the
        agent was driving"), and the vendor traffic carve-out it once needed was
        REMOVED on 2026-07-21 as unnecessary. Lifting interception here removes
        the last way our rules could interfere with their solve.

        Safe because it is the same posture enter_playback_mode establishes and
        the same one acquire_browse_tab already refuses to drive: a released tab
        comes back only through resume_agent_control(), and a tab that cannot be
        re-armed is left to the user rather than driven.

        False only when the lift itself explodes — the caller then falls back to
        the separate clean window, which is destructive but at least works."""
        try:
            # reload=False: re-fetching a challenge can issue a NEW one (or spend
            # a one-time token), so the page the user is looking at is the page
            # they must solve.
            await self.enter_playback_mode(reload=False)
            return True
        except Exception as exc:
            logger.warning(
                "browser: could not release the tab to the user "
                f"({type(exc).__name__}: {exc})"
            )
            return False

    async def _install_interception(self, page: Any) -> None:
        """Put the interceptor on `page` by the fastest route that still enforces
        every rule, and record which path won.

        PREFERRED — our own CDP `Fetch.enable`, because Playwright's route would
        also disable Chromium's HTTP cache for the whole session (see the
        _CdpRoute adapters above for the measurement). Same patterns Playwright
        asks for, so the page target's coverage is identical. Already per-PAGE,
        so it needed no change when the context became shared.

        FALLBACK — Playwright routing, PER PAGE.

        ⚠️ THE FALLBACK USED TO BE CONTEXT-LEVEL, and that became unsafe the
        moment one context held several sessions' tabs (2026-08-01): a
        context-level handler sees EVERY tab's requests and would judge them
        against THIS session's allowlist. Rule 3 is per-session, so that inverts
        the origin guard in both directions — wrongly allowing a request another
        tab's task never grounded, and wrongly aborting one it did.

        The honest cost: context routing guarded a popup from its very first
        request, and page routing cannot arm a tab that does not exist yet, so a
        popup's opening request is unguarded for one round trip until
        `_adopt_new_page` installs the guard. Judging another task's traffic with
        the wrong allowlist is worse; the SSRF guard and `_verify_landing` still
        catch where it lands; and a rule that is context-level only when one tab
        is open is the conditional-safety shape this codebase has repeatedly had
        to unpick."""
        if await self._install_cdp_interception(page):
            return
        await page.route("**/*", self._intercept)
        self._routed_pages.append(page)

    async def _install_cdp_interception(self, page: Any) -> bool:
        """Drive Fetch ourselves on this page's target. True when fully armed.

        Establishes the MAIN FRAME ID first and refuses the whole path without
        it: Rule 3 is "main-frame navigation only", and an adapter that cannot
        tell a top-level document from an iframe's would fail OPEN — a silently
        weaker guard is not an acceptable price for speed, so an unknown frame
        tree simply falls back to Playwright routing.

        Best-effort and never raises (the _harden_profile discipline): any
        failure returns False and the caller installs the slower guard."""
        opener = getattr(self._browser, "new_cdp_session", None)
        if not callable(opener):
            return False
        try:
            cdp = await opener(page)
            tree = await cdp.send("Page.getFrameTree")
            main_frame_id = str(
                ((tree or {}).get("frameTree") or {}).get("frame", {}).get("id") or ""
            )
            if not main_frame_id:
                raise RuntimeError("no main frame id — cannot judge Rule 3")

            def _paused(event: dict) -> None:
                # CDP events dispatch synchronously on the browser loop; the
                # guard is async, so schedule it and keep a strong reference
                # (a dropped task is a request that never gets answered, and an
                # unanswered Fetch pause hangs the page).
                try:
                    task = asyncio.ensure_future(
                        self._intercept(_CdpRoute(cdp, event, main_frame_id))
                    )
                    self._inflight.add(task)
                    task.add_done_callback(self._inflight.discard)
                except Exception as exc:
                    logger.debug(f"cdp intercept schedule: {type(exc).__name__}: {exc}")

            cdp.on("Fetch.requestPaused", _paused)
            await cdp.send("Fetch.enable", {"patterns": _FETCH_PATTERNS})
            self._cdp_sessions.append((page, cdp))
            self._cdp_routed = True
            self.stats.http_cache_on = True
            return True
        except Exception as exc:
            logger.debug(f"cdp interception unavailable: {type(exc).__name__}: {exc}")
            return False

    async def _unroute_intercept(self) -> None:
        """Remove this session's interceptor from every page it armed.

        Both paths can be in play at once: CDP may have worked for the first page
        and not for an adopted popup, which then fell back to a page route. Lift
        each one actually installed, or playback stays half-intercepted.

        SCOPED TO THIS SESSION'S TABS, deliberately. It used to unroute at
        CONTEXT level, which with a shared context would lift interception on
        every OTHER live agent tab — handing a running task an unguarded browser
        because an unrelated one finished and started playing a video. Nothing
        here can reach another tab: `_cdp_sessions` and `_routed_pages` are this
        session's own.

        RAISES rather than no-opping when a page cannot be unrouted, so the
        caller's honest "DEGRADED fallback" warning fires. The previous version
        could not fail, and so reported a lift it had not performed (2026-07-27).
        """
        for _page, cdp in self._cdp_sessions:
            await cdp.send("Fetch.disable")
        for page in self._routed_pages:
            await page.unroute("**/*", self._intercept)
        if not self._cdp_sessions and not self._routed_pages:
            # Nothing recorded (a hand-wired session, or a fake): fall back to the
            # active page so the lift is still attempted rather than silently skipped.
            await self.page.unroute("**/*", self._intercept)

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

            # RULE 0 — AD / TRACKER BLOCK (2026-07-22). Fronts every request
            # because it is the cheapest check (a pure suffix test, no DNS) and
            # the highest value on ad-heavy sites: an ad iframe/script that never
            # loads is a fake "Play" button the loop can never SEE or misclick.
            # It only ABORTS known ad/tracker hosts, so it can never weaken the
            # READ-mode guarantee — it is uBlock's network filtering, done in the
            # interceptor because Chrome refuses the real extension under CDP.
            if _is_ad_host(parsed.hostname):
                self.stats.blocked_ads += 1
                await self._safe_route(route.abort)
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
                blockable = self._read_only and self._is_main_frame_navigation(request)
                if self._commit_allows(method, url):
                    self._record_commit_fired(url, variant=False)
                    logger.info(
                        f"browser: allowed APPROVED {method} {url[:120]} "
                        "(commit) — re-locking"
                    )
                    # fall through to Rules 2 & 3 — an approved commit is not
                    # exempt from the SSRF and allowlist guards.
                elif not blockable and self._is_commit_variant(method, url):
                    # The approved submission at its representation url — the
                    # `.js`/`.json` twin of the form's own action (2026-07-26).
                    # Only reached for traffic Rule 1 was letting through anyway
                    # (`not blockable`), so this RECORDS a submission, it never
                    # unblocks one: a main-frame navigation to the variant url
                    # still falls to the abort branch below.
                    self._record_commit_fired(url, variant=True)
                    logger.info(
                        f"browser: APPROVED submission observed as {method} "
                        f"{url[:120]} (the form's action's .js/.json twin) "
                        "— re-locking"
                    )
                elif blockable:
                    self.stats.blocked_mutations += 1
                    if len(self.stats.mutation_urls) < 10:
                        self.stats.mutation_urls.append(f"{method} {url[:120]}")
                    logger.info(
                        f"browser: aborted unapproved form navigation "
                        f"{method} {url[:120]}"
                    )
                    await self._safe_route(route.abort)
                    return
                else:
                    # The page's own non-GET traffic (or the playback hand-off) —
                    # flows; Rules 2 & 3 below still apply. While a commit is
                    # armed, same-origin traffic is RECORDED so a submission that
                    # went somewhere unexpected can be named in the failure
                    # message instead of reported as "nothing was sent".
                    self._note_commit_traffic(method, url)

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
                # RECORD WHERE IT TRIED TO GO, exactly as _verify_landing does on
                # the landing path. Driving Fetch ourselves fires on redirect
                # HOPS, which Playwright's route handlers do not — so this branch
                # now catches off-site redirects that used to be judged after
                # they landed. Without this the refusal is a dead end: the loop
                # reads this field to offer the user the SAME origin-approval
                # pause an off-site link gets (a legitimate "Apply" flow
                # redirecting to an ATS is approvable; an ad is deniable).
                # MEASURED 2026-07-27: a youtu.be -> youtube.com 302 lost that
                # pause entirely until this line existed.
                if host:
                    self.last_redirect_offsite = {"host": host, "url": url}
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
        """A popup or target=_blank tab just opened.

        The listener on the shared context is the WINDOW's (one for the whole
        window, not one per session — see window._on_context_page), so this
        delegates to the same ownership decision rather than adopting outright.
        A session must never adopt a tab merely because it heard about it."""
        _window._on_context_page(page)

    async def _adopt_new_page(self, page: Any) -> None:
        """Take over a new tab as this session's active page.

        WHO owns a new tab is decided in ONE place — `window._owner_of_new_page`
        — and this method is what the owner then does about it. That split
        exists because the decision used to live here and got it wrong for a
        shared context: a tab with no opener was adopted unconditionally, and a
        `context.new_page()` has a null opener, so one session would seize the
        tab another had just created and close its own page out from under its
        run.

        The superseded tab is closed: the loop drives ONE page, and un-closed old
        tabs accumulated for the life of the session. Adoption grants NO new
        capability — the tab gets this session's own interceptor, SSRF guard and
        allowlist before the loop ever drives it. Best-effort throughout; a
        popup must never break a running browse."""
        # A new tab is a new TARGET and needs its own guard: a CDP session of its
        # own, or a page route of its own. There is no longer a context-level
        # route that could cover it for free.
        try:
            await self._install_interception(page)
        except Exception as exc:
            logger.debug(f"adopt popup route: {type(exc).__name__}: {exc}")
        self._refuse_downloads(page)
        superseded = self.page
        self.page = page
        logger.info("browser: following a new tab as the active page")
        if superseded is not None and superseded is not page:
            # FORGET ITS GUARD HANDLES BEFORE CLOSING IT. `_unroute_intercept`
            # walks these to lift interception at playback, and a route or CDP
            # session belonging to a page we have closed raises there — which the
            # caller catches as "could not lift", so a session that had followed
            # a popup would silently take the DEGRADED path for the rest of its
            # life. The route dies with the page; the record of it must too.
            self._routed_pages = [p for p in self._routed_pages if p is not superseded]
            self._cdp_sessions = [
                (p, c) for (p, c) in self._cdp_sessions if p is not superseded
            ]
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
    async def _validate_url_cached(self, url: str) -> tuple[Optional[str], Optional[str]]:
        """_validate_url's exact checks, with the DNS half off the event loop.

        _validate_url is synchronous and its SSRF guard calls socket.getaddrinfo,
        so EVERY navigation froze the single browser loop for a full DNS
        resolution — and with a route installed, every request paused in Chromium
        is waiting on that same loop to resume it. Same rule and the same
        _host_is_blocked, reached through the interceptor's process-global cache
        and single-flight (so a host seen once costs a dict lookup)."""
        target, host, error = parse_web_url(url)
        if error:
            return None, error
        if host and await _host_blocked_cached(host):
            return None, blocked_host_error(host)
        return target, None

    async def goto(self, url: str) -> str:
        """Navigate and return the final URL. Raises BrowserBlocked with a
        code-authored reason when the guard refuses."""
        # http/https + SSRF — the same rule read_webpage obeys, off the loop.
        target, error = await self._validate_url_cached(url)
        if error:
            raise BrowserBlocked(error)

        host = urlparse(target).hostname
        if not self.origin_allowed(host):
            raise BrowserBlocked(
                f"Refusing to open '{host}': it is not one of the sites this "
                f"task is allowed to visit ({', '.join(sorted(self.allowlist)) or 'none'})."
            )

        # PHASE A — commit. ONE retry on a timeout (2026-07-19): ad-heavy sites
        # intermittently blow the budget on the first hit and load fine on the
        # second. A GET is safe to reissue; bounded to exactly one retry. A
        # network error (bad cert, DNS, refused) is NEVER retried — retrying a
        # site that cannot be reached just spends the budget twice.
        # Our own Rule 3 refusals during this navigation are told apart from a
        # real network failure by watching the counter across the call. A
        # server-side redirect to an off-allowlist origin is aborted by the
        # interceptor, and Chromium then reports a bare `net::ERR_FAILED` — which
        # is Chromium's words for OUR decision. MEASURED 2026-07-27: the
        # Playwright-route path blocked the same redirect and landed silently on
        # `chrome-error://chromewebdata/` instead, so neither surfacing said what
        # had actually happened. (It also falsifies the standing comment that
        # "Playwright route handlers never re-fire on redirect hops" — that arm
        # recorded blocked_nav=1 too.)
        refusals_before = self.stats.blocked_navigations
        try:
            await self.page.goto(target, wait_until="commit", timeout=NAV_COMMIT_MS)
        except _NAV_TIMEOUT_ERRORS:
            logger.info(f"browser: commit timed out once for {target[:100]} — retrying")
            try:
                await self.page.goto(target, wait_until="commit", timeout=NAV_COMMIT_MS)
            except _NAV_TIMEOUT_ERRORS as exc2:
                raise BrowserUnreachable(
                    f"Couldn't load {host or target}: it didn't respond in time.",
                    kind="timeout",
                    host=host or "",
                ) from exc2
            except Exception as exc2:
                reason, kind = _network_error(exc2)
                if reason:
                    raise BrowserUnreachable(
                        f"Couldn't load {host or target}: {reason}.",
                        kind=kind,
                        host=host or "",
                    ) from exc2
                raise
        except Exception as exc:
            if self.stats.blocked_navigations > refusals_before:
                # Same wording _verify_landing raises, because it is the same
                # event judged one hop earlier — the loop should not be able to
                # tell which layer caught it.
                off = (self.last_redirect_offsite or {}).get("host") or "another site"
                raise BrowserBlocked(
                    f"The page redirected to '{off}', which this task is not "
                    "allowed to visit."
                ) from exc
            reason, kind = _network_error(exc)
            if reason:
                raise BrowserUnreachable(
                    f"Couldn't load {host or target}: {reason}.",
                    kind=kind,
                    host=host or "",
                ) from exc
            raise

        # PHASE B — readiness. Bytes are arriving; wait for something to act on.
        # NOT fatal: a page that never satisfies the predicate but has content is
        # handed to observe() anyway and the loop's page-quality gate decides what
        # it is. This is the whole fix for the 2026-07-26 daraz/ebay deaths — the
        # old code raised here and threw the page away.
        if not await self._await_readiness():
            self.stats.slow_navigations += 1
            logger.info(
                f"browser: {target[:100]} never reached readiness in "
                f"{READY_POLL_MS}ms — observing it anyway"
            )

        # PHASE C — landing check, AFTER the poll so a client-side redirect
        # (meta-refresh, location.replace) has had the whole window to happen.
        return await self._verify_landing()

    async def await_ready(self) -> bool:
        """Public readiness wait, for a navigation that did NOT go through goto().

        A form submit or an SPA route change moves the page just as much as a
        goto does, and until 2026-07-26 only goto waited for the result. Live,
        that was the difference between seeing daraz.pk's search results and not:
        the loop typed into the search box, pressed Enter, and observed 8
        elements of a page that would have 155 once it finished — because the
        only wait on that path was settle(), whose ceiling is 2s against a render
        that takes ~6s.

        Cheap on a page that is already done: readyState 'complete' plus stillness
        exits in two polls (~500ms)."""
        return await self._await_readiness()

    async def _await_readiness(self) -> bool:
        """Poll the page until it has something actionable on it. True when it
        got there, False when the budget ran out — never raises, never fatal.

        THE TEST-SHAPE RULE, and it is load-bearing in production too: a poll
        result that does not look like our payload (no "ready" key) is treated as
        READY and returns immediately. The suite's fake pages return canned dicts
        from evaluate(), and without this every session test that navigates would
        block for the full 15 real seconds. It is also the right production
        answer — if we cannot tell what state the page is in, proceed and let
        assess_page judge the observation, rather than burning the budget on a
        question nothing can answer."""
        deadline = time.monotonic() + (READY_POLL_MS / 1000.0)
        last_nodes = -1
        settled = 0
        while True:
            try:
                state = await self.page.evaluate(_READY_JS)
            except Exception as exc:
                # A navigation mid-poll destroys the execution context. That is
                # normal (a redirect), not an error — and unknowable state means
                # proceed, per the rule above.
                logger.debug(f"readiness poll: {type(exc).__name__}: {exc}")
                return True
            if not isinstance(state, dict) or "ready" not in state:
                return True

            # Is the DOM still growing? Tolerance, not equality — a settled page
            # jitters by a few nodes as lazy images swap in.
            nodes = int(state.get("nodes") or 0)
            if last_nodes >= 0 and nodes <= last_nodes * READY_GROWTH_TOLERANCE + READY_GROWTH_FLOOR:
                settled += 1
            else:
                settled = 0
            last_nodes = nodes

            # FAST PATH — the platform says the page is finished. 'complete'
            # means the load event fired and every subresource resolved; a page
            # that reports it and is holding still needs no further proof, and
            # making a trivial page wait a full second for stillness it showed on
            # the first poll is pure latency.
            if state.get("complete") and settled >= READY_COMPLETE_POLLS:
                return True

            # SUBSTANTIVE **AND** STILL. Either half alone is wrong: substance
            # alone fires on a bare header while the real content is still
            # arriving (the daraz.pk measurement above), and stillness alone
            # fires on a blank page that has not started.
            if state.get("ready") and settled >= READY_STABLE_POLLS:
                return True

            # Still but THIN: the page has finished and simply does not have much
            # on it — a login screen, a redirect stub, an SPA that painted once.
            if settled >= READY_STALL_POLLS and int(state.get("acts") or 0) >= 1:
                return True

            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(READY_STEP_MS / 1000.0)

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

        EVENT-DRIVEN (Phase 6): RACE a MutationObserver quiet-window (_QUIET_JS,
        the primary signal — the DOM tells us when it stopped painting) against
        networkidle (demoted to a race participant), whichever fires first, under
        an outer SETTLE_HARD_CAP_SECONDS deadline for a page that never quiets.
        Replaces the old sequential networkidle(≤2.5s) + node-count poll(≥0.5s)
        run EVERY step — an already-painted page now returns at ~250ms. Time spent
        accrues into stats.settle_seconds for the close summary. Never raises."""
        started = time.monotonic()
        quiet = asyncio.ensure_future(
            self.page.evaluate(_QUIET_JS, {"q": SETTLE_QUIET_MS, "cap": SETTLE_HARD_CAP_MS})
        )
        idle = asyncio.ensure_future(
            self.page.wait_for_load_state("networkidle", timeout=SETTLE_HARD_CAP_MS)
        )
        try:
            await asyncio.wait(
                {quiet, idle},
                timeout=SETTLE_HARD_CAP_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            # Always cancel the loser and DRAIN both — even if settle itself is
            # cancelled mid-wait (a browse hard-timeout) — so neither child leaks
            # nor logs an "exception never retrieved" warning. A busy networkidle
            # timeout, a quiet evaluate on a closed page, or our own cancel is
            # never an error here (the child's CancelledError, not settle's).
            for task in (quiet, idle):
                if not task.done():
                    task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            self.stats.settle_seconds += time.monotonic() - started

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
        self._commit_variant_url = ""
        self._commit_observed = []
        self._commit_event = asyncio.Event()
        logger.info(f"browser: armed one-shot commit {self._armed_commit[0]} {url[:120]}")

    def disarm_commit(self) -> None:
        """Drop an unconsumed permit and re-lock.

        The permit is one-shot, but only FIRING consumes it (see the interceptor
        clearing it on the matching request). A submit the site never issued
        therefore leaves it armed — which was harmless while the tab was always
        closed immediately afterwards, and is not harmless now that a tab this
        run did not open outlives the run. Explicit, because "the window closed"
        was never the guarantee, only its side effect."""
        self._armed_commit = None

    async def release_after_run(self) -> None:
        """Finish with this tab. Closes it ONLY if this run opened it.

        User report 2026-08-01: an add-to-cart flow reused the tab a previous
        "open junaidjamshed.com" task had opened, and closed it on the way out —
        "it closed the tab, which it shouldn't have; that power should be to
        me." Closing an inherited window is not ours to do, and it was never
        load-bearing: what stops a submit being replayed is the one-shot permit
        (now explicitly dropped by disarm_commit), not the window going away."""
        if not self.tab_reused:
            await self.close()
            return
        logger.info(
            "browser: leaving the tab open — this run inherited it rather than "
            "opening it, so closing it is the user's call"
        )

    def _commit_allows(self, method: str, url: str) -> bool:
        """True only when a permit is armed AND this exact request matches it.

        DELIBERATELY UNCHANGED by the 2026-07-26 observation round: this is the
        PERMISSION test — the only thing that can turn an abort into an allow —
        and widening it would grant new capability. The variant recognition below
        is a separate, RECORDING-only test applied to traffic that was going to
        flow either way."""
        if self._armed_commit is None:
            return False
        want_method, want_url = self._armed_commit
        return (method or "GET").upper() == want_method and (
            _normalize_commit_url(url) == want_url
        )

    def _is_commit_variant(self, method: str, url: str) -> bool:
        """True when this request is the approved submission expressed at its
        REPRESENTATION url — the armed url plus a `.js`/`.json` suffix, same
        method, same origin, same path, same query.

        Why this exists (junaidjamshed.com, 2026-07-26): a Shopify storefront
        declares `<form action="/cart/add">` and its theme posts to `/cart/add.js`.
        Nothing about the exact-match permit could ever recognise that, so a
        perfectly ordinary add-to-cart was reported as "Nothing was sent".

        NARROW ON PURPOSE — string equality against `armed + suffix` on the
        already-normalized form. `/cart/add` matches `/cart/add.js` and does NOT
        match `/cart/addresses` (no suffix boundary), `/cart/add/confirm`
        (different path) or `/cart/add?x=1.js` (query is inside the normalized
        form). Anything outside this is merely OBSERVED and reported, never
        counted as the user's approved submission.

        Grants NO new permission: this is only ever consulted for a request the
        interceptor was about to let through anyway (an in-page fetch/XHR is not
        a main-frame navigation, so Rule 1 never aborted it)."""
        if self._armed_commit is None:
            return False
        want_method, want_url = self._armed_commit
        if (method or "GET").upper() != want_method:
            return False
        got = _normalize_commit_url(url)
        return any(got == want_url + suffix for suffix in _COMMIT_VARIANT_SUFFIXES)

    def _record_commit_fired(self, url: str, *, variant: bool) -> None:
        """Consume the one-shot permit and mark the approved submission observed."""
        self._armed_commit = None       # one-shot: consume, re-lock
        self._commit_fired = True
        if variant:
            self._commit_variant_url = url
        self.stats.allowed_commits += 1
        event = self._commit_event
        if event is not None:
            try:
                event.set()
            except Exception:
                pass

    def _note_commit_traffic(self, method: str, url: str) -> None:
        """Record a same-origin non-GET seen while a commit is armed but which is
        NOT the approved submission — so a failure can say what the page sent
        instead of asserting that nothing was sent. Bounded; never raises."""
        if self._armed_commit is None or len(self._commit_observed) >= 8:
            return
        try:
            armed_host = urlparse(self._armed_commit[1]).hostname or ""
            if (urlparse(url).hostname or "") != armed_host:
                return
            self._commit_observed.append(f"{(method or 'GET').upper()} {url[:160]}")
        except Exception:
            pass

    def commit_fired(self) -> bool:
        """Whether the armed commit was actually consumed by a live request —
        so the submit phase can distinguish a real submission from a form the
        site never posted (a JS handler that swallowed it, a validation block)."""
        return self._commit_fired

    def commit_submitted_url(self) -> str:
        """The representation url the submission actually used, when it differed
        from the form's declared action (else "")."""
        return self._commit_variant_url

    def commit_observations(self) -> list[str]:
        """Same-origin non-GETs seen during the submit window that were NOT the
        approved submission — evidence for an honest failure message."""
        return list(self._commit_observed)

    async def wait_for_commit(
        self, timeout: float = COMMIT_WAIT_SECONDS
    ) -> bool:
        """Wait (bounded) for the approved submission to actually be observed.

        This replaces reading commit_fired() straight after settle(). settle() is
        a page-QUIET detector — its own contract is "an already-painted page
        returns at ~250ms" — so on a product page that had been sitting idle
        through the approval pause it returned essentially instantly and the
        session was torn down ~240ms after the submit was fired (measured,
        2026-07-26). A theme handler that `await`s anything never stood a chance.
        Quiet is not the signal; the request is. Never raises."""
        if self._commit_fired:
            return True
        event = self._commit_event
        if event is None:
            return False
        try:
            await asyncio.wait_for(event.wait(), timeout=max(0.1, float(timeout)))
        except Exception:
            pass
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

    async def drag(
        self,
        observation: Any,
        index: int,
        *,
        to_index: Optional[int] = None,
        to_fraction: Optional[float] = None,
        axis: str = "x",
    ) -> tuple[bool, str]:
        """Perform a real mouse drag (down → move → up) — the one gesture the
        action vocabulary lacked and vision could not add (vision LOCATES a point
        for a click; there is no drag). Two forms:
          • to_index  — drag the source element onto a target element (sortables,
            drag-and-drop uploads); the target is the DROP point.
          • to_fraction (0..1, along `axis`) — drag a RANGE-SLIDER handle to a
            position on its own track (the price/date filter with no number box).

        READ-safe: a drag is a UI gesture; any request it triggers is governed by
        the session interceptor exactly like a click (a slider filter's request is
        a GET). Returns (ok, note); NEVER raises — a bad target is an event the
        loop reacts to, not a crash. Geometry is read LIVE (bounding_box /
        track-rect eval), not from the observation, so it is act-time accurate."""
        from app.core import dom_observe  # local: dom_observe never imports us

        try:
            src = await dom_observe.resolve(self.page, observation, index)
        except dom_observe.StaleObservation:
            return False, "the element changed before it could be dragged"
        except Exception as exc:
            return False, f"could not find the element to drag ({type(exc).__name__})"

        try:
            box = await src.bounding_box()
        except Exception as exc:
            return False, f"could not measure the element ({type(exc).__name__})"
        if not box or not box.get("width") or not box.get("height"):
            return False, "the element is not visible to drag"
        sx = box["x"] + box["width"] / 2.0
        sy = box["y"] + box["height"] / 2.0

        tx = ty = None
        if to_index is not None:
            try:
                dst = await dom_observe.resolve(self.page, observation, to_index)
                dbox = await dst.bounding_box()
            except dom_observe.StaleObservation:
                return False, "the drop target changed before it could be used"
            except Exception as exc:
                return False, f"could not find the drop target ({type(exc).__name__})"
            if not dbox or not dbox.get("width") or not dbox.get("height"):
                return False, "the drop target is not visible"
            tx = dbox["x"] + dbox["width"] / 2.0
            ty = dbox["y"] + dbox["height"] / 2.0
        elif to_fraction is not None:
            frac = max(0.0, min(1.0, float(to_fraction)))
            try:
                track = await src.evaluate(_SLIDER_TRACK_JS)
            except Exception:
                track = None
            if not isinstance(track, dict) or not track.get("w") or not track.get("h"):
                return False, (
                    "couldn't find the slider track to drag along — try a number "
                    "input or a preset option if the page offers one"
                )
            if axis == "y":
                tx = sx
                ty = track["y"] + frac * track["h"]
            else:
                tx = track["x"] + frac * track["w"]
                ty = sy
        else:
            return False, "a drag needs either a drop target or a track fraction"

        mouse = getattr(self.page, "mouse", None)
        if mouse is None:
            return False, "the mouse is unavailable on this page"
        try:
            await mouse.move(sx, sy)
            await mouse.down()
            # Move in steps so a site's drag/pointermove JS actually fires — a
            # single teleport is often ignored by slider widgets.
            await mouse.move(tx, ty, steps=12)
            await mouse.up()
        except Exception as exc:
            # Best-effort release so a half-finished drag never wedges the pointer.
            try:
                await mouse.up()
            except Exception:
                pass
            return False, f"the drag failed ({type(exc).__name__})"
        try:
            await self.settle()  # let a filter/AJAX (a GET) apply
        except Exception:
            pass
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

    async def submit_commit(self) -> Optional[bool]:
        """Fire the stamped form's own submit — the one request the arm permits.
        Best-effort; whether the POST actually went out is read from
        commit_fired() afterwards, not assumed here.

        Returns True when the stamped form was found and its submit fired, False
        when the form is GONE from the page, and None when we could not tell (the
        evaluate failed, or a fake). That distinction was DISCARDED until
        2026-07-26, which is why a submit that never had a form to fire and a
        submit whose request we simply failed to recognise produced the identical
        message — one asserting a cause neither had evidence for."""
        try:
            return bool(await self.page.evaluate(_SUBMIT_COMMIT_FORM_JS))
        except Exception as exc:
            logger.debug(f"submit_commit: {type(exc).__name__}: {exc}")
            return None

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
        # Both branches below end with the tab in the user's hands, so mark it
        # here rather than in the success arm: the degraded branch is MORE
        # dangerous to re-drive silently, not less.
        self._playback = True
        try:
            # UNROUTE AT THE LEVEL IT WAS INSTALLED. On the real path the route is
            # installed on the CONTEXT (see BrowserSession.open), and Playwright's
            # page.unroute filters only that page's own route list — so the old
            # `self.page.unroute(...)` here found nothing, SUCCEEDED, and logged
            # the line below about a window that was still fully intercepted. The
            # 2026-07-18 "the net is very slow in your profile" fix has therefore
            # never actually run in production (found 2026-07-27). Page-level
            # stays the fallback for the page-routed path and for fakes.
            await self._unroute_intercept()
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
            # "commit" for the same reason goto() uses it — this reload runs on a
            # media page whose player scripts routinely keep DCL pending, and the
            # caller (ensure_playing) polls for a playing <video> afterwards
            # anyway, so waiting for the parser here buys nothing but delay.
            await self.page.reload(wait_until="commit", timeout=NAV_COMMIT_MS)
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
    """Close the current media — the in-place BrowserSession media session AND the
    clean normal-window hand-off (2026-07-22) — and clear both. True when
    something was actually closed. Idempotent — stopping nothing is not an error.
    This is the ONE stop entry every caller uses (the stop_media tool, the API,
    and every 'free the profile lock before launching' site), so it must cover
    both media surfaces."""
    closed_session = await _MEDIA.discard()
    closed_window = await stop_media_window()
    return closed_session or closed_window


def active_media() -> Optional[dict[str, str]]:
    """{title, url} for the current media — the in-place session OR the clean
    normal-window hand-off (2026-07-22). Cheap, no I/O — the StatusBar polls this
    freely (the context_status precedent). Only one is ever active (one profile,
    one live context)."""
    return _MEDIA.peek() or active_media_window()


async def reset_media() -> None:
    """Test/shutdown hook — close and clear EVERY held session slot plus the
    sign-in window. Delegates to registry.close_all_held(), so every slot is
    covered BY CONSTRUCTION — the old hand-listed version silently missed the
    discovery slot, the exact bug class the registry table exists to end. The clean
    media window (2026-07-22) is a subprocess, not a held BrowserSession, so it is
    closed explicitly."""
    await _held.close_all_held()
    await close_login_window()
    await stop_media_window()


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


# ------------------------------------------------- persistent browse window
# Session continuity (2026-07-21). Live testing showed every `browse` step of a
# plan launching its OWN Chrome and closing it when the step ended: step 2
# relaunched at the start URL and RE-DID step 1's navigation (the books.toscrape
# "opened the Himalaya book twice" report), a fresh session's `back` had no
# history to go back through, and the window closed the instant a task finished
# so the user never saw the result (the LinkedIn compose report). The agent's
# window is held HERE between browse runs instead: the next browse TAKES it and
# continues exactly where the last one left off (no relaunch, no flicker, real
# history), and after the last run it simply stays open until the user closes it
# or another browser task needs the profile.
#
# SECURITY — same argument as the result window, stated once there: the held
# session keeps its interceptor (it is still the AGENT's window and may be
# resumed — a viewer between runs, never a free-driving window), there is no
# armed commit permit, and holds are memory-only, so a restart closes the window
# with the process (the honest outcome). A user-driven form submit in this
# window is still gated — the Close button (StatusBar) is the exit to a normal
# browser.
# MULTI-TAB (2026-08-01). This was ONE slot, so a second browser task had
# nowhere to go: it closed the held window (and the whole context with it) and
# launched again — the Chrome-closes-and-reopens flicker, and the reason only one
# browser task could be open at a time. Tabs now live in the shared window
# (app/browser/window.py) and are keyed BY SITE: a follow-up about the same site
# continues that site's tab — which is what preserves the continuity above — and
# a different site opens a tab of its own.

# The window holds at most this many agent tabs. Beyond it the least-recently-used
# tab is closed, so a long session cannot grow an unbounded number of Chromium
# tabs. Never applies to a tab the user is mid-something with (see _evictable).
MAX_BROWSE_TABS = 6


def browse_site_key(allowlist: set[str], start_url: str = "") -> str:
    """The site a browse belongs to — its registrable domain.

    Registrable, not host: a task that starts on `www.example.com` and one that
    starts on `jobs.example.com` are the same site and should share a tab, and
    `publicsuffix.registrable` is already the codebase's answer to that (it gets
    `outfitters.com.pk` right, which `labels[-2]` did not). Empty string when
    there is nothing to key on — such a task simply gets its own tab.
    """
    host = ""
    if start_url:
        try:
            host = (urlparse(start_url).hostname or "").lower()
        except Exception:
            host = ""
    if not host:
        origins = sorted(o for o in (_normalize_origin(a) for a in allowlist) if o)
        host = origins[0] if origins else ""
    return registrable(host) if host else ""


def _tab_site(session: "BrowserSession") -> str:
    return getattr(session, "tab_site", "") or ""


def _evictable(session: "BrowserSession") -> bool:
    """A tab we may close to make room. NOT one being driven by a run, and NOT
    one held in any registry slot — a tab awaiting a signature approval, holding
    a CAPTCHA, part-filled behind a discovery question, playing media or showing
    a submitted form's response is a tab the user is mid-something with, and
    closing it can only produce "that expired"."""
    return not _window.is_driving(session) and not _held.is_held(session)


async def _make_room_for_a_tab() -> None:
    """Close least-recently-used evictable tabs until there is room for one more."""
    while _window.tab_count() >= MAX_BROWSE_TABS:
        candidates = [s for s in _window.live_tabs() if _evictable(s)]
        if not candidates:
            logger.info(
                f"browser: {_window.tab_count()} tabs open and every one is busy "
                "— opening another rather than closing work in progress"
            )
            return
        oldest = min(candidates, key=lambda s: getattr(s, "tab_used_monotonic", 0.0))
        logger.info(
            f"browser: closing the least-recently-used tab ({_tab_site(oldest)!r}) "
            f"to stay within {MAX_BROWSE_TABS}"
        )
        await oldest.close()


async def acquire_browse_tab(
    allowlist: set[str], *, site: str
) -> tuple["BrowserSession", bool]:
    """The tab this browse should run in, as (session, reused).

    Reuses the live tab for `site` when there is one — same page, real history,
    no relaunch (the 2026-07-21 continuity property, now per site instead of
    globally). Otherwise opens a new tab, first making room within
    MAX_BROWSE_TABS.

    A held tab may be DEAD (the user closed it by hand); that is an ordinary
    event, so it is probed and dropped rather than failing the browse.

    TWO TABS ARE NEVER REUSED, and either rule alone would have prevented the
    2026-08-01 add-to-cart incident:

    A tab HELD in a registry slot is the user's, not ours — they are watching
    the video, reading the submitted form's response, or owe an answer to a
    pause. `_evictable` already refuses to CLOSE such a tab to make room;
    seizing it to drive is the same intrusion by another route, and it is how a
    playback tab (interceptor lifted, by design) became the tab an autonomous
    commit flow ran on.

    A tab that cannot be RE-ARMED is left to the user rather than driven. This
    is the fail-closed direction on purpose: an extra tab costs a tab, and
    driving an unguarded one costs every guarantee in the module docstring.
    """
    origins = {o for o in (_normalize_origin(a) for a in allowlist) if o}
    if site:
        for candidate in _window.live_tabs():
            if _tab_site(candidate) != site:
                continue
            if _held.is_held(candidate):
                logger.info(
                    f"browser: the {site!r} tab is held (the user is mid-"
                    "something with it) — opening a fresh one"
                )
                break
            try:
                await candidate.page.evaluate("1")
            except Exception as exc:
                logger.info(
                    f"browser: the {site!r} tab is gone ({type(exc).__name__}) "
                    "— opening a fresh one"
                )
                await candidate.close()
                break
            if not await candidate.resume_agent_control():
                break
            # The interceptor reads session.allowlist LIVE (the origin-approval
            # union precedent), so re-scoping this tab to THIS task's grounded
            # origins is one write.
            candidate.allowlist = origins
            # Continuity is the PAGE, not the transcript: each run gets a fresh
            # action history (stale history from an earlier task only confuses
            # the model).
            candidate.browse_history = []
            candidate.last_redirect_offsite = None
            candidate.tab_reused = True
            note_browse_tab(candidate)
            logger.info(
                f"browser: reusing the {site!r} tab "
                f"(at {str(candidate.page.url)[:120]})"
            )
            return candidate, True

    await _make_room_for_a_tab()
    session = await BrowserSession.open(origins)
    session.tab_site = site
    note_browse_tab(session)
    return session, False


def note_browse_tab(
    session: "BrowserSession",
    *,
    title: Optional[str] = None,
    url: Optional[str] = None,
    goal: Optional[str] = None,
) -> None:
    """Record what a tab is showing and stamp it least-recently-used. Called when
    a tab is acquired and again when a run finishes. Cheap, sync, never raises —
    tab bookkeeping must not be able to fail a browse."""
    try:
        session.tab_used_monotonic = time.monotonic()
        meta = dict(getattr(session, "tab_meta", None) or {})
        if title is not None:
            meta["title"] = title or ""
        if url is not None:
            meta["url"] = url or ""
        if goal is not None:
            meta["goal"] = goal or ""
        session.tab_meta = meta
    except Exception as exc:
        logger.debug(f"note_browse_tab: {type(exc).__name__}: {exc}")


async def close_browse_window(site: str = "") -> bool:
    """Close agent browse tabs — the one for `site`, or ALL of them when no site
    is given. True when at least one was closed. Idempotent.

    AN EXPLICIT REQUEST BEATS THE BUSY RULE, and that asymmetry is deliberate.
    Automatic EVICTION never touches a tab the user is mid-something with,
    because nobody asked for it and the only possible outcome is "that expired".
    A user clicking Close (or a path that needs the profile free) DID ask: a
    button labelled "close all tabs" that silently leaves one open is worse than
    one that closes it, and a pending approval already reports honestly that its
    session is gone.
    """
    if not site:
        return await _window.close_all()
    targets = [s for s in _window.live_tabs() if _tab_site(s) == site]
    for session in targets:
        await session.close()
    return bool(targets)


def active_browse_tabs() -> list[dict[str, str]]:
    """One {site, title, url, goal, busy} per open agent tab, most recently used
    first. Cheap, no I/O — the StatusBar/API poll it (the active_media rule)."""
    tabs = sorted(
        _window.live_tabs(),
        key=lambda s: getattr(s, "tab_used_monotonic", 0.0),
        reverse=True,
    )
    rows: list[dict[str, str]] = []
    for session in tabs:
        meta = dict(getattr(session, "tab_meta", None) or {})
        rows.append(
            {
                "site": _tab_site(session),
                "title": meta.get("title", ""),
                "url": meta.get("url", ""),
                "goal": meta.get("goal", ""),
                "busy": not _evictable(session),
            }
        )
    return rows


def active_browse_window() -> Optional[dict[str, str]]:
    """The most recently used agent tab's {title, url, goal}, or None. Kept so
    the existing single-window StatusBar/API surface still reads correctly while
    the multi-tab one lands beside it."""
    rows = active_browse_tabs()
    return rows[0] if rows else None


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


# ------------------------------------------------- challenge hand-off escalation
# In-place FIRST, the clean window only when in-place is MEASURED to have failed
# (2026-08-03). Handing the challenge tab straight to the user costs nothing and
# keeps every other tab, so it is always tried first. But the clean window is not
# decoration: Cloudflare Turnstile and Google fingerprint the AUTOMATED browser
# and re-issue the challenge however many times a human solves it (live
# 2026-07-19, "I fill the box and it unchecks again and again"). Dropping it to
# save the tabs would trade one live-observed defect for another.
#
# So: hand over in place; if the SAME site challenges again within the window
# below, the in-place solve did not stick and we escalate — paying the tabs only
# once the cheap path has been tried and observed to fail. This is the
# evidence_resolver escalation shape, and it is why there is no threshold to
# tune: one failed attempt is the whole signal.
#
# Memory-only, keyed by site, self-expiring — no clear-path plumbing to forget.
# A restart forgets and starts with the NON-destructive path, which is the safe
# default. A second challenge on the same site more than this far apart is a
# genuinely new one, not a solve that failed to stick.
_CHALLENGE_HANDOFF_TTL_SECONDS = 600.0
_challenge_handoffs: dict[str, float] = {}


def _challenge_key(site: str) -> str:
    return (site or "").strip().lower().lstrip(".")


def note_challenge_handoff(site: str) -> None:
    """Record that this site's challenge was handed over IN PLACE, so a repeat
    inside the TTL escalates to the clean window instead of asking the user to
    solve the same uncooperative check twice in the same tab."""
    key = _challenge_key(site)
    if key:
        _challenge_handoffs[key] = time.monotonic()


def challenge_handed_over_recently(site: str) -> bool:
    """True when an in-place hand-over for this site is still fresh — i.e. the
    user already solved it there and the site is challenging again anyway."""
    key = _challenge_key(site)
    if not key:
        return False
    stamped = _challenge_handoffs.get(key)
    if stamped is None:
        return False
    if time.monotonic() - stamped > _CHALLENGE_HANDOFF_TTL_SECONDS:
        _challenge_handoffs.pop(key, None)
        return False
    return True


def reset_challenge_handoffs() -> None:
    """Test/shutdown hook — the reset_context_store precedent."""
    _challenge_handoffs.clear()


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


def _spawn_clean_browser(url: str, *, extra_args: tuple[str, ...] = ()) -> Optional[Any]:
    """Launch a plain system Chrome/Edge on the ~/.jarvis/browser profile as a
    normal, user-driven window — no CDP, no --enable-automation, no
    remote-debugging port (the whole point: a browser Turnstile/Google do not
    read as a bot). `extra_args` adds per-purpose flags (e.g. the media window's
    autoplay policy). Returns the Popen handle, or None when no browser is found."""
    exe = _find_system_browser()
    if not exe:
        return None
    # The no-saved-password / no-autofill posture holds in the clean window too;
    # done HERE (not in the callers) so an injected test launcher never touches the
    # real ~/.jarvis/browser profile on disk.
    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    _harden_profile(BROWSER_PROFILE_DIR)
    args = [
        exe,
        f"--user-data-dir={BROWSER_PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        # The profile's OWN installed extensions (e.g. a Web-Store uBlock) load
        # here automatically — this window is a normal, non-CDP Chrome. The
        # disable-features set is what lets any *unpacked* --load-extension below be
        # honored on Chrome 137+ (and folds in the autofill/chatter trims).
        f"--disable-features={_DISABLE_FEATURES}",
        *_extension_load_args(),
        *extra_args,
        "--new-window",
        url,
    ]
    # A new process group on Windows so terminating it (below) does not signal
    # the backend, and so taskkill /T can reach Chrome's child tree.
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    return subprocess.Popen(args, creationflags=creationflags)


def _default_clean_launcher(url: str) -> Optional[Any]:
    """The sign-in / verification hand-off window: a plain user-driven Chrome/Edge
    on the shared profile. Returns the Popen handle, or None when no browser is
    found."""
    return _spawn_clean_browser(url)


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


async def _verify_clean_proc(proc: Any) -> Optional[Any]:
    """Return `proc` if it stayed alive past the profile-handoff verify window,
    else None. A real subprocess.Popen exposes poll(): while the browser process
    runs it returns None; if the process EXITED within a couple of seconds it
    handed the URL to another Chromium on the same profile and closed with no
    visible window (the 2026-07-19 reported bug). A test fake with no poll() cannot
    be verified and is assumed alive (unchanged)."""
    if proc is None:
        return None
    poll = getattr(proc, "poll", None)
    if callable(poll):
        deadline = time.monotonic() + _CLEAN_LOGIN_VERIFY_SECONDS
        while time.monotonic() < deadline:
            if poll() is not None:
                return None
            await asyncio.sleep(0.2)
    return proc


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
    verified = await _verify_clean_proc(proc)
    if verified is None and proc is not None:
        logger.info(
            "browser: the clean sign-in window handed off to an existing "
            "Chrome on the profile and exited — falling back to the "
            "automation window"
        )
    return verified


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
    # A sign-in window is a SEPARATE Chrome process on the same user-data-dir, so
    # it cannot coexist with the shared automation context however many tabs are
    # in it. This is the one constraint multi-tab does NOT remove: signing in
    # closes the tabs. One call covers every tab BY CONSTRUCTION — media, result
    # window, a paused commit, a plain browse tab — where the hand-listed closes
    # this replaces covered only the slots someone remembered.
    await _window.close_all()
    await stop_media_window()  # the clean media window is a subprocess, not a tab
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


# --------------------------------------------------- clean-window media hand-off
# WATCH/PLAY in a NORMAL window (2026-07-22, user request). A "play this on
# anikoto/youtube" goal used to play IN the automation window: the agent found the
# video, then enter_playback_mode() lifted the interceptor and it played in place.
# That works for YouTube but is miserable on ad-heavy streaming/piracy sites — the
# interceptor is fully OFF during playback (streaming throughput), so the code-side
# ad block (Rule 0) cannot cover watching, and the user hit pop-under ads and a
# dead page clicking around.
#
# The fix: the agent still does the FINDING in the automation window (interceptor
# on, Rule 0 blocking ads), then hands the final video URL to a plain, user-driven
# Chrome/Edge window on the SAME ~/.jarvis/browser profile — signed in, and with
# uBlock loaded (an unpacked extension loads in a NON-CDP window; the agent window
# cannot load it). uBlock's filter lists cover the rotating pop-under domains the
# static Rule 0 host list never can, so WATCHING is ad-free.
#
# THE ONE HONEST TRADE-OFF: a non-automation window has no CDP, so Jarvis cannot
# press play in it. --autoplay-policy=no-user-gesture-required starts standard
# players (YouTube) on their own; a custom anime/streaming player may need ONE user
# click — and uBlock then blocks the ad-popup that click usually triggers.
#
# One profile = one live context: the clean media window holds the profile lock, so
# a new browse/login/commit closes it first via stop_media() (which now covers this
# window too). The handle is a subprocess.Popen — NOT a BrowserSession — so it
# cannot live in the _held registry table (that closes .close() on Playwright
# pages); it is tracked here beside the sign-in window, whose machinery it reuses.
_MEDIA_AUTOPLAY_ARGS = ("--autoplay-policy=no-user-gesture-required",)

# The injectable seam (the CLEAN_BROWSER_LAUNCHER precedent): tests point it at a
# fake so the suite never spawns a real Chrome; production leaves it None and uses
# the default launcher below.
CLEAN_MEDIA_LAUNCHER: Optional[Callable[[str], Any]] = None

_clean_media_proc: Optional[Any] = None
_clean_media_meta: Optional[dict[str, str]] = None
_media_window_lock = asyncio.Lock()


def _default_clean_media_launcher(url: str) -> Optional[Any]:
    """The watch/play hand-off window: a plain user-driven Chrome/Edge on the
    shared profile with autoplay enabled so standard players start on their own.
    Returns the Popen handle, or None when no browser is found."""
    return _spawn_clean_browser(url, extra_args=_MEDIA_AUTOPLAY_ARGS)


def clean_media_enabled() -> bool:
    """Whether to hand a watch/play goal off to a clean normal window. In
    production (no injected BROWSER_FACTORY) yes; under an injected factory (tests)
    only when a clean media launcher is also injected — so the hermetic suite never
    spawns a real Chrome and stays on the in-place-playback fallback unless a test
    opts in explicitly (the _clean_login_enabled precedent)."""
    if CLEAN_MEDIA_LAUNCHER is not None:
        return True
    return BROWSER_FACTORY is None


async def _open_clean_media(url: str) -> Optional[Any]:
    """Launch the clean, non-automation media window and return its handle, or None
    on any failure (no browser found, spawn error, or a profile-handoff exit). The
    default launcher hardens the profile; an injected test launcher touches no real
    FS. Best-effort — never raises."""
    launcher = CLEAN_MEDIA_LAUNCHER or _default_clean_media_launcher
    try:
        proc = await _maybe_await(launcher(url))
    except Exception as exc:
        logger.warning(
            f"clean media window launch failed: {type(exc).__name__}: {exc}"
        )
        return None
    verified = await _verify_clean_proc(proc)
    if verified is None and proc is not None:
        logger.info(
            "browser: the clean media window handed off to an existing Chrome on "
            "the profile and exited — nothing is playing"
        )
    return verified


async def _close_media_window_locked() -> bool:
    """Terminate the clean media window WITHOUT taking _media_window_lock (the
    caller holds it). True when one was actually closed. Best-effort."""
    global _clean_media_proc, _clean_media_meta
    proc, _clean_media_proc = _clean_media_proc, None
    _clean_media_meta = None
    if proc is not None:
        _terminate_clean_proc(proc)
        _mark_profile_released()  # a browse re-run must wait out the profile lock
        return True
    return False


async def open_media_window(url: str, *, title: str = "") -> bool:
    """Open the shared profile as a NORMAL, user-driven window playing `url`
    (uBlock loaded, autoplay on). Closes every other live context on the profile
    first (one profile, one window), waits out the single-instance lock, then
    launches. Returns True when the window came up, False otherwise (no system
    browser, or a profile-handoff exit) — the caller reports 'not playing'
    honestly. Best-effort — never raises."""
    global _clean_media_proc, _clean_media_meta
    # Free the single-profile lock: this is a separate Chrome process, so the
    # whole shared context must go, not just some of its tabs. (The caller only
    # takes this path when no OTHER tab is open — playing a video is not a reason
    # to close somebody else's work; see BrowseTool's keep_open branch.)
    await close_login_window()
    await _window.close_all()
    async with _media_window_lock:
        await _close_media_window_locked()  # a prior clean media window
        await _settle_profile()
        proc = await _open_clean_media(url)
        if proc is None:
            return False
        _clean_media_proc = proc
        _clean_media_meta = {"title": title or "", "url": url or ""}
        logger.info("browser: handed the video off to a clean normal window (uBlock, autoplay)")
        return True


async def stop_media_window() -> bool:
    """Close the clean media window if open. True when one was actually closed.
    Idempotent."""
    async with _media_window_lock:
        return await _close_media_window_locked()


def active_media_window() -> Optional[dict[str, str]]:
    """{title, url} for the clean media window, or None (also None once the user
    has closed it themselves — poll() then reports it exited). Cheap, no I/O — the
    StatusBar polls it via active_media() (the active_media precedent)."""
    if _clean_media_proc is None or not _clean_media_meta:
        return None
    poll = getattr(_clean_media_proc, "poll", None)
    if callable(poll) and poll() is not None:
        return None  # the user closed the window
    return dict(_clean_media_meta)


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
    try:
        await stop_media_window()  # the clean media window is a subprocess, not held
    except Exception as exc:
        logger.debug(f"shutdown close media window: {type(exc).__name__}: {exc}")
    try:
        # The shared context outlives individual sessions now, so closing every
        # held session is no longer proof the window is down: a tab that was
        # never held in a slot would keep the context — and the profile lock —
        # alive past shutdown, which is the orphan that hangs the next run's
        # launch. This is the belt that makes "no Chromium survives" true again.
        await _window.close_all()
    except Exception as exc:
        logger.debug(f"shutdown close shared window: {type(exc).__name__}: {exc}")
