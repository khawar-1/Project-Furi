# Phase 15 — Agentic Multi-Step Flows

The goal of Phase 15 is to close the gap between "Furi can drive any single
site" (Phase 14, complete) and "Furi does the multi-step things I do" —
searching a job board, opening each posting, filling and submitting an
application, uploading a resume, signing in when asked, and getting past the
visual-only pages DOM observation can't read.

**This is capability depth, not a rewrite.** The Phase 14 foundation is right:
one real Chromium, an observe→decide→act loop, no per-site code, grounding as the
exfiltration bound, signature-based approval, structural-over-prompt guarantees,
LLM proposes / code decides. Every part below extends that doctrine; none of it
reinvents the safety model.

## The honest residual limit (applies to all parts, stated up front)

Phase 15 does **not** turn READ-mode's "non-GET = mutation" boundary into a
structural proof — it stays an HTTP convention (RFC 7231). Within an allowlisted,
authenticated origin, a compromised loop has full user authority. The real
controls remain: **grounding** (what data/where, from the user's words, never a
page), the **approval gate** (every send is seen and one-shot), the **headed
watchable window** (the last honest control), and **CAPTCHAs are never
auto-solved**. Phase 15 makes Furi do far more of what you do; it does not
remove the need for you to watch the important sends.

## Dependencies & sequencing

- **15.1** (pause-capable loop) and **15.2** (grounded profile store) are the
  load-bearing prerequisites — build first, in either order.
- **15.3** (vision fallback) and **15.4** (login/CAPTCHA resilience) are
  independent resilience layers — build in either order after 15.1/15.2.
- **15.5** (capstone: UX + live acceptance) last.

Every part ends with **"Do not commit"** — git is deferred per the standing
project preference.

---

## 15.1 — Pause-capable multi-commit task engine

**Closes:** "one approved submit per browse" + the flat `MAX_BROWSER_ACTIONS=15`
cap. This is the crux; everything else is easier.

**The hard truth driving the design:** you cannot pre-approve commit #2, because
its form does not exist until you have submitted #1 and navigated. So multi-commit
is *inherently sequential*: `…act (READ) → reach a form → DISCOVER → PAUSE for
approval → one-shot submit → re-lock → continue…`, repeated. Each submit stays a
separate signature, separate approval, separate one-shot permit — the 14.5
guarantee **repeated, never batched or replayed**.

**Key design decisions:**
- The `browse` loop becomes **pause-capable**: mid-flow it can yield a *commit
  approval request* to the task runner, which parks the plan through the existing
  `parked_plans` machinery. The **held `BrowserSession` stays alive in a
  registry** across the pause (the media / result-window registry pattern); on
  approval the loop resumes from where it stood. This is the real engineering —
  the current model pauses the whole plan *once*; now a single browse step
  pauses/resumes N times.
- A **commit budget** (`max_commits`, user-set, default 1, hard-capped in code at
  ~5) bounds how many approvals one goal may request — the runaway-loop backstop,
  enforced in code, not prompt.
- The flat action cap becomes a **per-sub-goal budget** with **progress
  detection** (generalize the existing dedupe signal: no new element interacted
  with in K steps → declare stuck, don't spin to the cap).
- **No auto-approval of "similar" forms in v1.** Every send is seen. A future
  "approve this kind of form for this flow" affordance is explicitly deferred —
  strict first.

**Files:** `browser_loop.py` (yield/resume state machine, budget),
`browser_session.py` (long-lived held-session registry across pauses),
`task_runner.py` + `plan_store.py` (resume-into-a-running-browse, not just
resume-the-plan), `planner.py` (a browse step that can pause more than once),
`browser_agent_tools.py` (`max_commits` param).

**Tests:** a loop that discovers 2 forms pauses twice, each with its own
contract/signature; the budget refuses a 3rd; a held session survives a
park/resume; the one-shot permit is re-consumed per submit (never replayed); an
unapproved submit is still structurally blocked.

**Implementation prompt:**

> Implement Phase 15.1: make `browse` a pause-capable multi-commit task engine.
> Currently a browse step pauses the plan exactly once for a single approved
> submit. Extend it so one browse goal can perform up to `max_commits` (new
> `browse` param, default 1, hard-capped in code at 5) sequential approved
> submits — e.g. "apply to the first 3 jobs." Design it as a resumable loop: on
> reaching a form it enters DISCOVER, yields a commit-approval request that parks
> the plan through the existing `parked_plans`/`task_runner` machinery, keeps the
> live `BrowserSession` in a long-lived registry (the media/result-window
> registry pattern) across the pause, and resumes from the same loop position on
> approval. **Every submit remains a separate signature, separate approval,
> separate one-shot `arm_commit` permit — nothing batched or replayed; preserve
> the 14.5 guarantee exactly, just repeated.** Replace the flat
> `MAX_BROWSER_ACTIONS` cap with a per-sub-goal budget plus progress detection
> (generalize the existing dedupe: stuck = no new element interacted with in K
> steps). Enforce `max_commits` structurally. Add tests: two discovered forms →
> two pauses with distinct contracts, budget refuses the third, held session
> survives park/resume, permit re-consumed per submit, unapproved submit blocked.
> State the residual risk honestly. Do not commit.

---

## 15.2 — Autofill profile store (the grounded data source)

**Closes:** form-filling that today either invents values or interrogates you
field-by-field. Job applications need a curated source of *your* data — and it
must be the **grounding corpus for fills**, exactly as recipient/upload grounding
works: fill values come from your profile or your words, **never from a page,
never invented**.

**Key design decisions:**
- A new `AutofillProfile` store (SQLite, one curated row-set): name, email,
  phone, location, links, plus **documents** (resume, cover-letter) as grounded
  file paths reusing `file_tools` path safety. Managed in a Settings card — you
  own it.
- A `_fill_grounding` corpus = profile fields + user words. The loop's
  `type`/`upload` actions may only emit a value **traceable to that corpus**
  (mirror `_recipient_violation`/`_upload_path_violation`, checked in the planner
  reject chain before discovery). A page asking for something not in your profile
  → the loop **pauses and asks you**, never guesses.
- Sensitive fields (anything you mark secret) are **write-through,
  display-masked, and never sent to the LLM** — only referenced by key; code
  substitutes the value at fill/`set_input_files` time (the password-never-read
  rule).

**Files:** new `app/db/models.py` `AutofillProfile` + migration;
`app/core/autofill.py` (accessor, the reminders-domain pattern);
`browser_grounding.py` (`fill_value_is_grounded`); `browser_loop.py` (grounded
fill); `planner.py` (`_fill_violation`); new `/api/autofill` + Settings
`AutofillCard`.

**Tests:** a profile value fills; an ungrounded value is refused →
pause-and-ask; a masked secret never appears in any LLM prompt or history;
document paths reuse path safety.

**Implementation prompt:**

> Implement Phase 15.2: an autofill profile store that is the grounded data
> source for browser form-filling. Add an `AutofillProfile` (SQLite + migration +
> `app/core/autofill.py` accessor following the reminders-domain pattern):
> curated personal fields (name/email/phone/location/links) and documents
> (resume/cover-letter) stored as file paths that reuse `file_tools` path safety.
> Add `browser_grounding.fill_value_is_grounded(value, profile, goal,
> conversation, answers)` — a fill value must trace to the profile or the user's
> own words, **never to page content, never invented** (mirror
> `_recipient_violation`/`upload_path_is_grounded`). Enforce it in the planner
> reject chain (`_fill_violation`) before discovery, and in `browser_loop`'s
> `type` action. A field not covered by the profile → the loop pauses and asks
> the user (the existing AWAITING_CHOICE machinery), never guesses. Fields the
> user marks secret are display-masked, write-through, and **never placed in any
> LLM prompt or chat history** — code substitutes them at fill time (the
> password-never-read rule). Add `/api/autofill` CRUD + a Settings `AutofillCard`.
> Tests: grounded fill works, ungrounded refused→pause, masked secret never in a
> prompt, document path safety. Do not commit.

---

## 15.3 — Vision fallback (DOM-first, screenshot when stuck)

**Closes:** icon-only buttons, canvas apps, visual-only SPAs — where pure DOM
observation can't identify the target.

**Key design decisions:**
- **DOM stays primary.** Vision is invoked **only when the loop is stuck**
  (element-not-found or repeated-failure — the exact triggers `evidence_resolver`
  uses to escalate, applied to the loop). Not every step — cost and latency.
- Introduce a **vision provider seam** (`VISION_PROVIDER_FACTORY`, the
  `create_provider()` pattern). DeepSeek has no image input, so this adds a
  second, opt-in model (e.g. a Gemini/Claude vision endpoint) used *only* for the
  stuck-element case. Off by default; a Settings toggle; fails clean to the
  DOM-only behavior when unconfigured.
- The screenshot is a **downscaled** capture (the Phase 8 screen-OCR discipline —
  no full-res frames), taken from the live page, held in memory, never persisted.
  Vision returns a described target that maps back to a DOM element by
  bounding-box overlap — **the click still goes through the DOM index contract**,
  so the index/obs-id staleness guarantee is preserved (vision *locates*, DOM
  *acts*).

**Files:** `app/providers/` (vision seam), `dom_observe.py` (screenshot + bbox
map), `browser_loop.py` (stuck → vision escalation, bounded), config + Settings
toggle.

**Tests:** DOM-sufficient page never calls vision (assert zero vision calls); a
stuck step triggers exactly one bounded vision call; vision-located target still
resolves through the DOM index; unconfigured vision fails clean to DOM-only.

**Implementation prompt:**

> Implement Phase 15.3: an opt-in vision fallback for the browse loop, DOM-first.
> Keep DOM observation primary; invoke vision **only when the loop is stuck**
> (element-not-found or repeated-failure — the `evidence_resolver` escalation
> triggers applied here), bounded per browse. Add a `VISION_PROVIDER_FACTORY`
> seam (the `create_provider()` pattern) for a second, opt-in image-capable model
> (DeepSeek has none); off by default, Settings toggle, fails clean to DOM-only
> when unconfigured. On escalation, take a downscaled in-memory screenshot (the
> Phase 8 screen-OCR no-full-res discipline, never persisted), ask the vision
> model to locate the target, and map its bounding box back to a DOM element so
> **the click still goes through the existing DOM index/obs-id contract** —
> vision locates, DOM acts, staleness guarantee intact. Tests: DOM-sufficient
> pages make zero vision calls; a stuck step makes exactly one bounded call;
> located target resolves through the index; unconfigured → DOM-only. Do not
> commit.

---

## 15.4 — Login & CAPTCHA resilience (human-in-the-loop, never auto-solve)

**Closes:** mid-flow login walls and CAPTCHAs that stop long flows dead.

**Key design decisions:**
- **Extend 14.4's login-wall detection to fire mid-flow**, not just at start: the
  loop detects a wall or CAPTCHA, **pauses the plan AWAITING the user**, surfaces
  the headed window, and resumes when the user has signed in / solved it. The
  held session (15.1's registry) carries the now-authenticated state forward.
- **CAPTCHAs are never solved automatically** — ToS and safety. Furi only
  detects, hands off to the watchable window, and waits. Stated as a hard rule.
- Detection is heuristic (known CAPTCHA iframes/challenge markers + a login-form
  signal), best-effort → on uncertainty the loop asks rather than barrels
  through.

**Files:** `browser_loop.py` (mid-flow wall/CAPTCHA detection → pause),
`browser_session.py` (resume with the authenticated session), `dom_observe.py`
(challenge markers), the AWAITING_CHOICE pause text.

**Tests:** a mid-flow login wall pauses (never types a credential); a detected
CAPTCHA pauses and is never auto-interacted-with; resume continues with the
authenticated session; a false-positive-prone page asks rather than guesses.

**Implementation prompt:**

> Implement Phase 15.4: mid-flow login and CAPTCHA resilience for the browse
> loop, human-in-the-loop. Extend 14.4's login-wall detection to fire at any
> point in a flow, not just at start. When the loop hits a login wall or a
> CAPTCHA, pause the plan AWAITING the user (existing AWAITING_CHOICE machinery),
> surface the headed watchable window, and resume — carrying the now-authenticated
> held session forward (15.1's registry). **CAPTCHAs are never solved or
> auto-interacted-with — detect, hand off to the window, wait; state this as a
> hard rule.** Credentials are never typed by Furi. Detection is heuristic and
> best-effort — on uncertainty, ask rather than proceed. Tests: mid-flow wall
> pauses without typing a credential; a detected CAPTCHA pauses and is never
> touched; resume uses the authenticated session; an ambiguous page asks. Do not
> commit.

---

## 15.5 — Capstone: end-to-end task flow + hardened multi-step approval UX

**Closes:** the pieces exist but the *experience* of a long approved flow needs to
be legible — you must see, at each pause, exactly what's about to happen, and be
able to stop the whole flow.

**Key design decisions:**
- A **flow-level PlanCard**: for a multi-commit browse, show progress ("commit 2
  of 3"), each pause rendering its own full contract (fields + file + destination
  — the send_email full-contract rule), plus a **Stop-the-whole-flow** control
  (the media-stop / task-cancel pattern).
- **Grounded flow summary**: the completion quotes each server response (the
  `_fmt_browse_commit` grounded-confirmation rule, applied per commit) — never an
  ungrounded "all done."
- **The live acceptance run**: exercise a real job-application-shaped flow on the
  real Selector event loop (`npm run dev` — a standalone `asyncio.run()` is
  Proactor and does **not** reproduce production), against a public multi-form
  target, confirming each submit is approved, sent once, and auditable in
  `activity_log`. **Close 14.6's still-open live upload run in the same pass.**
- Update CLAUDE.md's Phase 14/15 architecture section and the memory files; state
  residual risk (authenticated-origin authority; DOM/vision limits; CAPTCHA never
  auto-solved).

**Files:** `PlanCard.tsx` + `browserStore.ts` (flow progress + stop),
`rendering.py` (per-commit grounded summary), CLAUDE.md, memory.

**Tests + live:** flow progress renders; stop halts before the next commit;
per-commit grounded summary; **a real end-to-end run on the Selector loop**,
audit-verified.

**Implementation prompt:**

> Implement Phase 15.5, the capstone: a legible multi-step approval experience and
> a real end-to-end acceptance run. Build a flow-level PlanCard showing progress
> ("commit 2 of 3"), each pause rendering its own full contract (fields + file +
> destination, the send_email full-contract rule), and a Stop-the-whole-flow
> control (the media-stop/task-cancel pattern). Make the flow completion quote
> each server response per commit (the `_fmt_browse_commit` grounded-confirmation
> rule) — never an ungrounded summary. Then run a real job-application-shaped flow
> **on the real Selector event loop via `npm run dev`** against a public
> multi-form target, confirming each submit is user-approved, sent exactly once,
> and auditable in `activity_log`; close 14.6's outstanding live upload acceptance
> in the same pass. Update CLAUDE.md and the memory files; state residual risk
> honestly (full user authority within an authenticated allowlisted origin;
> DOM/vision limits; CAPTCHAs never auto-solved). Do not commit.

---

## What Phase 15 does and does not deliver

**After 15.1–15.5, Furi can:** search a site, open results, fill and submit
multiple forms in one goal (each approved), upload grounded files, autofill from a
curated profile you own, get past visual-only pages, and hand off login/CAPTCHA to
you mid-flow — all with no per-site code.

**It still will not:** solve CAPTCHAs for you, type your credentials, act without
approval on anything that leaves the machine, or guarantee correctness inside an
authenticated origin beyond the grounding + approval + watchable-window controls.
That last boundary is by design, not a missing feature.
