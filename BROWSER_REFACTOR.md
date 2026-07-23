# Browser Automation Stack — Full Refactor

**Status: Phase 7 of 8 complete** (0–5 committed; **Phases 6–7 uncommitted**, git deferred). Suite green at every phase. **Phase 8 deliberately deferred** — no functional/security/correctness benefit, real regression surface (see below).
**Goal:** Skyvern / GPT-Atlas-class capability — "do what I do in a browser" on any site, with no per-site code — on top of Jarvis's existing safety model.
**Approved:** 2026-07-21 (plan file: `~/.claude/plans/hey-see-this-codebase-zippy-crane.md`). Test suite: **2103 green** as of the latest commit.

---

## Why the refactor

The Phase-14/15 browser stack (~7,500 lines, 12 modules) was built in ~10 rapid live-bug-fix rounds over 4 days. It worked for narrow demos but was a sediment of point fixes: three ~500-line god-functions each re-encoding the same pause taxonomy, five copy-pasted session registries, 10 browser-only fields bolted onto the core `AgentPlan`, plaintext autofill secrets, and ~10 verified correctness bugs (leaks, races, silently dropped hand-offs). Two structural ceilings blocked capability:

1. **The GET-only network interceptor broke every SPA** — search, filters, and lazy-loading all POST, so half the modern web couldn't even render for the agent.
2. **Vision was a last-resort fallback** (2 calls max, only when stuck) instead of the primary way the agent perceives a page.

## Owner decisions (locked)

1. **Action-level safety** replaces GET-only interception. Page traffic flows freely (SSRF guard + main-frame origin allowlist stay). Safety gates what the **agent** does, not what the page does: every agent submit gesture requires the one-shot code-read-fingerprint commit permit bound to a signature approval; CAPTCHAs are never auto-solved; credentials are never entered by Jarvis.
2. **Vision-first hybrid** perception. Every decision step sends a set-of-marks screenshot (numbered badges drawn in-page from the same rects the DOM element list carries) plus the full text prompt to Gemini; the DOM still *executes* every action via the index/obs-id staleness contract ("vision LOCATES, DOM ACTS"). DeepSeek text-only loop remains the fallback when vision is unconfigured or fails.
3. **Full refactor authorized**, phased so the app stays usable daily; the hermetic suite (conftest refuses real browser/vision launches) stays green at every phase.

## Target architecture

Everything browser lives in **`backend/app/browser/`**:

| Module | Owns |
|---|---|
| `runtime.py` | Dedicated Proactor-thread event loop (`run_browser` marshaling), `BROWSE_HARD_TIMEOUT` outer belt |
| `session.py` | `BrowserSession` lifecycle, launch chain + orphan reclaim, interception, profile hardening, login/clean windows |
| `observe.py` | DOM extraction → numbered `Element` list, index/obs-id staleness contract, challenge probe, set-of-marks screenshot (`capture_marked`) |
| `loop.py` | The observe→decide→act loop (`run_browse`), hybrid decision, action executors, wall detectors, budgets |
| `commit_flow.py` | `discover`/`perform` — form discovery, held sessions, the approved one-shot submit, multi-commit resume |
| `grounding.py` | Origin/fill/upload grounding — everything must trace to the user's words or profile, never a page |
| `registry.py` | ONE generic `HeldSessionRegistry` + `REGISTRIES` table {media, result_window, commit, challenge, discovery} + `close_all_held()` |
| `state.py` | `Handoff` enum + `HandoffPayload` — the single typed pause vocabulary; sole owner of the `_commit`/`_commits_done` step params |

Old paths (`app/core/browser_session.py`, `app/core/dom_observe.py`, `app/core/browser_runtime.py`, `app/agents/browser_{loop,commit,grounding}.py`) are **sys.modules self-replacement shims** until Phase 8 — chosen because ~100 test monkeypatches set attributes on the old paths.

---

## Phases — done

| Phase | Commit | What shipped |
|---|---|---|
| **0** | `df057ad`, `69244ce` | Baseline commit of all uncommitted Phase-15 work; httpx duplicate pin removed. 2053 tests. |
| **1 — package extraction** | `f1870a5` | `app/browser/` package created (6 modules moved whole); old paths become self-replacement shims; verbatim duplications collapsed (form-read JS twins, `_normalize_origin`, vision-config loader); challenge probe split out of `_EXTRACT_JS`. Zero behavior change. |
| **2 — generic registry** | `018afe3` | ONE `HeldSessionRegistry` class replaces five copy-pasted registries; `REGISTRIES` table + `close_all_held()` = leak-proofing **by construction** (a meta-test iterates every slot). Fixed: shutdown leaking commit/challenge holds (profile-lock orphans), cap-exhaustion leaking the discovery hold, cross-thread profile-stamp race. |
| **3a — typed hand-off state** | `c4950be` | `state.py`: `Handoff` enum + frozen `HandoffPayload` (JSON round-trip for parked plans); single-writer discipline for `_commit`/`_commits_done` step params; `stamp_start_url` refuses approval-bound steps. |
| **3b — one dispatcher** | `971cbb2` | The planner's three ~500-line pause decision trees collapse into ONE `_handle_browse_handoff`; challenges now count against the 25-hand-off budget (they were wrongly burning the scarce `MAX_QUESTIONS=3`); discover's five branch bodies collapse via `_hold_for_handoff`/`_discovery_from_handoff`. |
| **3c — multi-commit resume** | `c4a279b` | The road to form N+1 can surface **any** hand-off (login/fill/auth/challenge/origin were silently dropped before — verified bug); `auth_resolved` rides the session so a decided sign-in offer is never re-asked; restart honesty via serialized `AgentPlan.browse_note` ("that window is gone — starting again"). 2087 tests. |
| **4 — action-level safety** | `0cdeb9e` | The capability unlock: page XHR/POST traffic **flows** (SPAs work); network Rule 1 only aborts unapproved top-level form-POST navigations; the **submit-gesture gate** in `_act` refuses click-on-submit-control / Enter-in-form in code (search-shaped + GET forms exempt); the commit permit matches the SPA fetch transport too; context-level routing closes the popup first-request gap; unrelated popups closed, superseded tabs closed; downloads refused; challenge-vendor traffic carve-out deleted (it's ordinary traffic now). 2096 tests. |
| **5 — vision-first hybrid** | `c4a7ed1` | `capture_marked` set-of-marks overlay; `_decide` sends the marked screenshot + full prompt to the vision provider as the PRIMARY channel with per-step text fallback; old stuck-only vision escalation (`MAX_VISION_CALLS=2`) deleted; richer action space: `select_option`, `hover`, `scroll`, `press_key` (whitelist, never Enter — that's the submit gesture), `wait`, `back` — motion actions exempt from the repeat-dedupe and wandering detector. 2099 tests. |
| **6 — speed** | *uncommitted (2026-07-23)* | Event-driven `settle()`: a MutationObserver 250ms quiet-window (`_QUIET_JS`) RACED against networkidle (demoted from gate to race participant) under a 2s cap — replaces the old sequential networkidle(≤2.5s)+node-count-poll(≥0.5s) EVERY step; an already-painted page returns at ~250ms. Typed `goto` retry: `_NAV_TIMEOUT_ERRORS` (Playwright `TimeoutError` + builtin, guarded import) replaces the `"Timeout" in str(exc)` string-sniff. Pipelined screenshot: the base viewport is captured CONCURRENTLY with `observe()` (only when a vision provider is configured) and the set-of-marks badges are drawn in Python (`observe.overlay_marks`, Pillow — scale = image/viewport absorbs DPR+downscale), skipping the 3 in-page `_MARK_JS`/screenshot/`_UNMARK_JS` round-trips; full fallback chain preserved (base None / no Pillow → in-page marks → plain shot → text-only). 2206 tests. |

### Post-refactor live-incident fixes (from the first real acceptance runs, 2026-07-21)

| Commit | Incident → fix |
|---|---|
| `25b1c22` | **"Processing forever, Chrome never opened."** First browse paid ~56s importing `google.generativeai` inside its own 180s budget, and the launch chain's worst case (3 channels × 2 × 45s) exceeded the entire belt — with failures logged nowhere. Fixed: startup pre-warm of playwright + genai imports; `LAUNCH_CHAIN_BUDGET_SECONDS=120` shared launch deadline; every launch attempt logs immediately; reclaim after every timeout (a cancelled launch can leak a half-spawned Chrome that holds the profile lock); detached cleanup when the outer belt cancels mid-launch; budgets resized for real flows (`MAX_BROWSER_ACTIONS` 15→25, loop deadline 120→300s, outer belt 180→600s) with a **pinned test enforcing the inequality** so no constant can quietly shrink back. Result: Chrome launched in 1s on the next run. |
| `78ea677` | **"After 'Apply as guest' nothing happened" + "why does Chrome close and reopen?"** The flow was actually working (guest choice resumed the held window, search ran, the site threw a CAPTCHA; close/reopen = the deliberate interstitial hand-off to a clean non-automated window). Two real defects fixed: a per-run **vision circuit breaker** (an out-of-quota Gemini key 429'd every step and the fallback retried it every step — now 3 consecutive failures → text-only for the rest of the run), and the CAPTCHA hand-off text now says a normal-looking page means the check already passed invisibly (clean windows usually aren't challenged — the user saw nothing to solve and read it as a hang). |

---

## Phase 7 — secrets-at-rest (done, uncommitted 2026-07-23)

Autofill SECRET values (a form password, an API key) were **plaintext in the
`autofill_fields.value` column** (verified bug #7) — the API masked them on READ,
but any local process, backup, or synced copy of `jarvis.db` read them in the
clear, contradicting the project's own posture (auth-token 0600, credentials
never entered by Jarvis). Now encrypted at rest:

- **`app/core/secrets_store.py`** — Windows DPAPI (`CryptProtectData` /
  `CryptUnprotectData` via **ctypes, zero new dependency**; the key is derived +
  held by the OS for the logged-in Windows user, so ciphertext is bound to this
  user+machine with no key file of our own to guard). Injectable `CRYPTO_BACKEND`
  seam (the `STT_MODEL_FACTORY` pattern; conftest autouse `_hermetic_secrets`
  installs a reversible fake so the suite never calls Win32 and is
  platform-independent). Values stored in the SAME column, discriminated by a
  `dpapi:<b64>` prefix — an unprefixed value is legacy plaintext and passes
  through `decrypt_secret` unchanged.
- **Graceful degradation** (the memory-engine rule): a host without DPAPI, or a
  crypto failure, falls back to plaintext with a LOUD one-time warning rather
  than losing the secret; a prefixed blob that can't be decrypted (corrupt, or
  sealed for a different Windows user) yields `""` (the fill then pauses to ask)
  — never surfaced as ciphertext.
- **`autofill.encrypt_plaintext_secrets(db)`** — idempotent startup migration
  (in the ONE table accessor, the reminders rule; called best-effort from the
  `main.py` lifespan): rewrites every unprefixed SECRET row in place. No-op once
  prefixed, no-op without DPAPI, safe to run every boot. **No Alembic migration**
  (same column, discriminated in-value). `upsert_field` encrypts SECRET writes
  after validation; `to_snapshot` decrypts in code into `_secrets` (never a
  prompt/history — the password-never-read rule is unchanged). Only SECRET kind
  is encrypted; text/link/document stay readable (curated grounding data).
- Tests: `test_secrets_store.py` (11 — prefix/round-trip/idempotence/legacy
  passthrough/unavailable-fallback/undecryptable→empty, a win32-guarded REAL
  DPAPI round-trip, and DB integration through `upsert_field`/`load_profile` +
  the in-place migration). **2244 green.**

## Phase 8 — importer migration (DEFERRED — no benefit now)

**Not implemented, by design.** Reviewed 2026-07-23: rewrite the ~6 non-test +
~10 test importers of the old `app.core.browser_*` / `app.agents.browser_*`
paths to `app.browser.*`, delete the 6 `sys.modules` self-replacement shims
(11–13 lines each), and rewrite the (now-stale) browser sections of `CLAUDE.md`.

This is **pure mechanical tidiness + doc freshness with zero functional,
correctness, or security benefit**, against a real regression surface (~100 test
monkeypatches reference the old paths — the exact reason the shims exist — plus a
large, error-prone `CLAUDE.md` rewrite). The shims are transparent and working.
Lead-engineer call: not worth the churn/risk today. Revisit only if the browser
stack is being actively re-touched anyway, so the import rewrite and doc refresh
ride along with work that must open those files regardless.

> Owner instruction (still in force): **do not start Phase 8 without asking.**

### Also outstanding (not phases)

- **Live acceptance run with the owner** — the standing gate for Phases 4+5: a real job-application-shaped multi-form flow end-to-end (each submit user-approved, sent once, auditable in `activity_log`), plus a DOM-only fallback check with the vision key unset. The 2026-07-21 weworkremotely runs are partial progress: launch, auth-offer, guest resume, and the CAPTCHA hand-off all verified live; a completed form → Red Approve Card → approved submit has not been observed live yet.
- **Environment prerequisites the code can't fix:**
  - The **Gemini API key is out of quota** (free tier) — vision-first is falling back to text-only until billing is enabled or quota resets. Judge navigation quality only with vision live.
  - The **autofill profile is empty** (Settings → Autofill) — until it's populated, every form fill pauses to ask (and now learns each answer).

## Safety model (unchanged by the refactor — the point of it)

- A submit fires only via the one-shot `arm_commit` permit bound to a **signature approval** the user saw (URL, method, every field value, any attached file).
- Origins, fill values, and upload paths must **ground** in the user's own words or their curated profile — never in page content (the exfiltration bound).
- CAPTCHAs are never solved or touched; credentials are never entered by Jarvis; sign-in happens in a user-driven window.
- The window is headed and watchable — the last honest control.
- **Honest residual limit:** within an allowlisted, authenticated origin a compromised loop has full user authority, and a site that commits on input/blur via XHR can receive agent-typed values pre-approval (bounded by grounding). Documented in `session.py`'s docstring, not hidden.

## How to work on this

- Run tests: `cd backend && venv\Scripts\python -m pytest tests/ -q` (~7 min, hermetic — no real browser/vision ever launched; must stay green each phase).
- Commit per phase, message via `git commit -F <file>` (PowerShell here-strings mangle multi-line `-m`).
- Live diagnostics: `~/.jarvis/logs/backend.log` — every launch attempt, hand-off pause, and session summary now logs.
