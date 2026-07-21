# Browser Automation Stack — Full Refactor

**Status: Phases 0–5 of 8 complete** (committed, suite green at every phase). Phases 6–8 pending owner go-ahead.
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

### Post-refactor live-incident fixes (from the first real acceptance runs, 2026-07-21)

| Commit | Incident → fix |
|---|---|
| `25b1c22` | **"Processing forever, Chrome never opened."** First browse paid ~56s importing `google.generativeai` inside its own 180s budget, and the launch chain's worst case (3 channels × 2 × 45s) exceeded the entire belt — with failures logged nowhere. Fixed: startup pre-warm of playwright + genai imports; `LAUNCH_CHAIN_BUDGET_SECONDS=120` shared launch deadline; every launch attempt logs immediately; reclaim after every timeout (a cancelled launch can leak a half-spawned Chrome that holds the profile lock); detached cleanup when the outer belt cancels mid-launch; budgets resized for real flows (`MAX_BROWSER_ACTIONS` 15→25, loop deadline 120→300s, outer belt 180→600s) with a **pinned test enforcing the inequality** so no constant can quietly shrink back. Result: Chrome launched in 1s on the next run. |
| `78ea677` | **"After 'Apply as guest' nothing happened" + "why does Chrome close and reopen?"** The flow was actually working (guest choice resumed the held window, search ran, the site threw a CAPTCHA; close/reopen = the deliberate interstitial hand-off to a clean non-automated window). Two real defects fixed: a per-run **vision circuit breaker** (an out-of-quota Gemini key 429'd every step and the fallback retried it every step — now 3 consecutive failures → text-only for the rest of the run), and the CAPTCHA hand-off text now says a normal-looking page means the check already passed invisibly (clean windows usually aren't challenged — the user saw nothing to solve and read it as a hang). |

---

## Phases — left

> Owner instruction: complete the current phase only; **do not start the next phase without asking.**

| Phase | Size | What it is |
|---|---|---|
| **6 — speed** | small-medium | Event-driven settle: MutationObserver quiet-window (250ms) raced with a 2s cap, immediate short-circuit on already-stable pages (removes the residual settle floor; networkidle demoted to a race participant). Typed `goto` retry on Playwright `TimeoutError` (replaces string-sniffing "Timeout"). Pipelined screenshot capture. *(The budget-assertion piece of Phase 6 already shipped early in `25b1c22`, forced by the live incident.)* |
| **7 — secrets** | small | Autofill secrets are **plaintext in SQLite today** (verified bug #7). New `app/core/secrets_store.py`: Windows DPAPI via ctypes (zero new dependency, key managed by the OS user account), injectable `CRYPTO_BACKEND` seam for tests, `dpapi:<b64>` prefix-discriminated values in the same column, idempotent startup migration encrypting non-prefixed rows, API keeps masking on read. |
| **8 — importer migration** | medium | Rewrite the ~30 importers to `app.browser.*`, delete the compatibility shims, rewrite the browser architecture sections of `CLAUDE.md` (several are now stale — they describe the pre-refactor stack). |

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
