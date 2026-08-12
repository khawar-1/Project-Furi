# Furi OS — Project Dossier

*A complete account of what this project is, how it is built, and why every
significant decision was made the way it was.*

Written 2026-08-12. Companion to `CLAUDE.md` (the incident-by-incident
engineering log), `FEATURES.md` (the forward roadmap) and `README.md`.

> **Naming note.** The project was called *Jarvis* until 2026-08-12, when it was
> renamed **Furi** cosmetically. On-disk identifiers were deliberately left
> alone (`~/.jarvis/`, `backend/jarvis.db`, the `X-Jarvis-Token` header, the
> `com.jarvis.os` app id) because renaming them would break a live install. Both
> names are accepted everywhere a user might say one.

---

## 1. What this is, in one paragraph

Furi OS is a **local-first personal AI operating system** for the desktop. It is
not a chat wrapper. It is a persistent agent that remembers you across months,
plans and executes real work on your machine (files, shell, email, calendar),
drives a real Chrome browser to fill and submit forms on live websites, listens
and speaks with GPU-accelerated local voice, watches what you are doing to
volunteer help before you ask, controls your smart home and your desktop, and
can be approved from your phone over the LAN. Every action that changes the
world stops and asks first — and that gate is enforced in code, not by asking a
language model to behave.

---

## 2. The project at a glance

| Metric | Value |
|---|---|
| Development window | 2026-07-06 → 2026-08-12 (**37 days**) |
| Backend Python | 164 modules, **68,194 LOC** |
| Test suite | 139 files, **61,662 LOC**, 3,239 test functions (~3,900 collected) |
| Frontend + Electron | 66 TS/TSX files, **17,429 LOC** |
| Total application code | **~147,000 LOC** |
| Tools in the registry | **48** (READ / WRITE / DESTRUCTIVE) |
| Domain agents | **8** (general, file, email, calendar, research, browser, home, desktop) |
| HTTP routes | **105** across 30 router modules |
| Database tables | **23**, with **22** Alembic migrations |
| Scheduler job kinds | 7 (push, reminder, birthday, daily_briefing, reindex, initiative, routine) |
| Scored benchmarks | 3 (`route_bench`, `plan_bench`, `browse_bench`) |
| Verification scripts | 43 (`_falsify_*`, `_measure_*`, `_verify_*_runtime`) |

**Test-to-source ratio is roughly 0.9:1.** That number is the single best
summary of how this project was built.

---

## 3. Architecture — three processes, one application

```
┌──────────────────────────────────────────────────────────────────────┐
│  ELECTRON MAIN (Node)                                                │
│  · window + tray lifecycle, close-to-tray, global hotkey             │
│  · spawns/supervises the Python backend in production                │
│  · native OS notifications (renderer never touches Node)             │
│  · desktop sensing helper (active window, idle time) via Win32       │
│  · downscaled screen capture for OCR (per-session armed only)        │
│         │ contextBridge — a minimal, explicit window.jarvis API      │
│         ▼                                                            │
│  RENDERER (React 18 + TypeScript + Vite + Tailwind + Zustand)        │
│  · chat, plan approval cards, voice mode, 10 side panels             │
│  · owns the /ws push connection (kept alive while in tray)           │
│         │ HTTP + SSE + WebSocket, all authed                         │
│         ▼                                                            │
│  BACKEND (FastAPI + SQLAlchemy async + SQLite + Qdrant)              │
│  · memory engine, LangGraph planner, tool registry, schedulers       │
│  · a second Proactor-loop thread owning Playwright/Chromium          │
│  · a dedicated single thread owning the TTS engine                   │
│  · optional second HTTP listener for the phone (LAN, read+approve)   │
└──────────────────────────────────────────────────────────────────────┘
```

**Why Electron rather than a web app.** The product *is* the desktop: it needs
tray residency, global hotkeys, native toasts, a foreground-window sensor, and
the ability to survive its own windows being closed. None of that exists in a
browser tab.

**Why a separate Python backend rather than Node.** The entire ML ecosystem the
project depends on — `faster-whisper`, `kokoro-onnx`, `fastembed`, `rapidocr`,
`playwright`, `langgraph` — is Python-first. Electron talks to it over
`localhost` HTTP, which also means the backend is independently testable,
independently benchmarkable, and can be driven by a phone.

**Why SQLite + embedded Qdrant rather than Postgres + a hosted vector DB.**
Local-first is a product constraint, not a preference: this app reads your
email, your files and your screen. Nothing should require a server, an account,
or a network round-trip for the user's own data. Qdrant runs in embedded
on-disk mode (`./qdrant_data`), and the backend degrades to non-vector search if
it is unavailable rather than failing.

**`BACKEND_HOST` is pinned to `127.0.0.1`.** This API can run shell commands. It
must never bind `0.0.0.0`. (A 2026-08-03 audit found this setting was
*decorative* — it appeared in one log line and nothing enforced it; it is now
passed explicitly at both launch sites and pinned by a test. That story is
itself a design lesson: a setting that reads like a guarantee and makes none is
worse than no setting.)

---

## 4. Technology choices and the reasoning behind each

| Layer | Choice | Why this one |
|---|---|---|
| Desktop shell | **Electron 28** | Tray + hotkey + native notifications + a real Chromium already present |
| UI | **React 18 + Vite + Tailwind + Zustand** | Zustand over Redux: this app has ~14 independent stores that mostly don't talk; a global reducer would be ceremony |
| Markdown | **react-markdown + remark-gfm** | Assistant output is markdown; tool results are rendered as markdown by design so lists/tables/code render |
| Backend | **FastAPI + async SQLAlchemy 2.0** | Native SSE streaming, native WebSocket, first-class async — the whole app is I/O bound |
| Relational store | **SQLite (aiosqlite) + Alembic** | Zero-install, single file, WAL. Migrations auto-run at startup because nobody runs `alembic` by hand on a desktop app |
| Vector store | **Qdrant (embedded)** | Runs in-process on local disk; three collections (`jarvis_memory`, `file_chunks`, `conversation_messages`) |
| Embeddings | **fastembed / bge-small (384-dim, CPU)** | Local, tiny, no API cost, no data leaving the machine |
| Fuzzy matching | **rapidfuzz** | Deterministic identity resolution — the LLM cannot reliably tell "Jamil" from "Jamil Ali" |
| Planner | **LangGraph** | The plan/reflect/execute/revise loop is a state graph with cycles; hand-rolling that is a worse version of LangGraph |
| Scheduling | **APScheduler**, wrapped | Wrapped in `JarvisScheduler` so SQLite is the truth and timers are only the wake-up call |
| LLM | **DeepSeek** (`deepseek-v4-flash`) via an OpenAI-compatible provider | Swappable: Gemini, Groq, Ollama, OpenRouter all implement the same ABC. Provider is a `.env` line, not a code change |
| STT | **faster-whisper**, CUDA int8_float16 | Local. GPU took a 6.4s clip from 169s → 631ms (~268×) |
| TTS | **Kokoro (onnxruntime-gpu)** | Replaced Piper, then Chatterbox: Chatterbox pulled `torch` in, which caused an OpenMP DLL clash that crashed the process. Kokoro is torch-free |
| Wake word | **openWakeWord ONNX in the renderer** (`onnxruntime-web`) | Runs on-device in the browser context; no audio leaves the machine, ever |
| Screen OCR | **RapidOCR (onnxruntime)** — optional dep | Reuses the GPU stack; lazily imported so the base install stays clean |
| Browser control | **Playwright + real Chrome** | A real, headed, watchable browser is the last honest control over an agent that can buy things |
| Secrets at rest | **Windows DPAPI via ctypes** | Key held by the OS per Windows user; a synced or backed-up `jarvis.db` is useless to an attacker. Zero new dependency |
| Logging | **loguru** to `~/.jarvis/logs/backend.log` | Added after two root-cause investigations died on lost terminal scrollback |
| Tests | **pytest + pytest-asyncio** (`asyncio_mode=auto`) | 19 autouse hermetic fixtures make it structurally impossible for the suite to touch the network, a real GPU model, or the real home directory |

**Deliberately rejected technologies, and why:**

- **A `/plugins` directory with dynamic discovery.** Auto-loading dropped-in
  `.py` files is a code-execution vector aimed straight at the approval gate.
  Tools self-register via an explicit import line instead.
- **A 3-layer embedding-similarity router.** Cosine similarity to reference
  imperatives measures *topic*, not *intent* — "I nuked my downloads yesterday"
  scores like "nuke my downloads". Measured and rejected.
- **Google Programmable Search / CSE.** Verified 2026-08-02: "search the entire
  web" was discontinued, the JSON API is closed to new customers, and it retires
  Jan 2027. The deterministic title-reader that replaced it is the fix, not a
  stopgap.
- **`torch` in the backend.** It caused a reproducible `0xc0000005` crash via a
  `libiomp5md.dll` version clash with `ctranslate2`. Removing it is what made GPU
  voice safe.

---

## 5. The capability stack — every feature, layer by layer

### 5.1 Memory engine — the core

Every chat turn does two things around the LLM call:

1. **Before:** `MemoryEngine.retrieve_context()` pulls relevant semantic
   memories, contacts, preferences and episodes; `format_context()` renders them
   into a `MEMORY CONTEXT` block in the system prompt.
2. **After:** a background task sends the conversation to the LLM with a large
   structured-extraction prompt, validates the JSON against a Pydantic schema
   (one retry on invalid output), and writes results. It never blocks the stream
   and never surfaces errors to the user.

Six memory types: `SemanticMemory` (facts), `Contact` + `ContactInteraction`
(people, append-only fact log), `Episode` (milestones), `Preference` (inferred
behaviour), `UserProfile` (single-row identity), `EntityEdge` (typed relationship
graph). Plus `GoalThread` (open concerns) and `MemoryConflict` (a queue).

**Key design decisions:**

- **Dual-perspective shared facts.** "I had coffee with Ali" writes two rows:
  one to your About Me, one to Ali's fact log. The contact-side text is
  **derived in Python** from the user-perspective template — the LLM's version
  is only a guarded fallback, because it produced "Khawar went fishing with
  Khawar and Khawar".
- **Ambiguous names always ask.** An exact match that is a strict subset of a
  longer contact name ("jamil" when "Jamil Ali" exists) is *deliberately*
  ambiguous. Threshold `MIN_SCORE = 81`, `MIN_GAP = 8`, all deterministic
  rapidfuzz — no LLM in identity resolution.
- **Parked questions survive restarts.** An unanswered "which Jamil?" persists
  to SQLite with a 24h TTL; memory is only a hot cache.
- **Future events are stored as plans.** "Went fishing" with a future date is
  rewritten to "Planning to go fishing" by a deterministic net, not by asking
  the prompt nicely.
- **Meta-conversation facts are never stored.** "Mentioned X", "Inquired about
  Y" are filtered both by prompt rule and by a code-level predicate.

**Memory is bounded, and it decays.** (2026-08-03/04)

- `budget.py` — sections carry their *items*, so clipping drops whole facts and
  never half a line, and the cut is always marked. Measured: a year-three shape
  (50 contacts × 200 facts) rendered **76,758 chars before, 5,277 after**.
- `decay.py` — freshness as a ranking term, never a filter. Relevance dominates
  by construction (cosine 0.5–1.0 vs boosts totalling 0.25).
- `archive.py` — sets `archived_at`; issues no DELETE and never touches Qdrant,
  so restore is instant. Four conditions all required, and a PROTECTED-category
  deny-list ("I am allergic to penicillin" can go a year unmentioned and must
  still be there the day it matters).
- `consolidate.py` — an LLM-composed rolling digest of aged-out contact facts,
  **discarded if it does not rest on its sources** (a token-overlap comparator).
  Measured: 1 of 9 load-bearing facts reached the model before; 9 of 9 after, for
  +426 chars.
- `conflicts.py` — when the extractor says a new fact supersedes an old one but
  the coverage rule refuses, the disagreement used to die in a log line. It is
  now a **queue entry, never a verdict**. Nothing auto-resolves; resolution
  hard-deletes only on the user's click.

---

### 5.2 Tools + the agent planner — the safety spine

**48 tools** across nine domains, each self-registering into a global registry:

| Domain | READ | WRITE | DESTRUCTIVE |
|---|---|---|---|
| Files | `search_files` `read_file` `list_directory` `open_folder` `semantic_file_search` | `move_file` `move_files` `rename_file` `create_folder` `create_file` | `delete_file` `delete_files` |
| Terminal | — | — | `run_command` `execute_script` |
| Email (Gmail) | `search_emails` `read_email` `read_thread` | `create_email_draft` | `send_email` `reply_email` |
| Calendar | `list_events` `find_events` | `create_event` `update_event` | `delete_event` |
| Web | `web_search` `read_webpage` `browse` `browse_page` `stop_media` | — | `browse_commit` |
| Memory | `recall_memory` `lookup_contact` | — | — |
| Audit | `recall_actions` | — | — |
| Home | `list_devices` `get_device_state` | `set_device_state` `run_scene` `set_climate` | — |
| Desktop | `list_windows` `take_screenshot` `read_clipboard` | `focus_window` `close_window` `launch_app` `set_volume` `media_key` `write_clipboard` | — |

**The approval gate is structural.** `registry.execute_tool()` refuses any
non-READ tool without `approved=True`, and writes an `ActivityLog` row for
**every attempt — executed and blocked alike**. No prompt, no jailbreak, no
model can route around it, because it is an `if` statement above the call site.

**The planner** (`app/agents/planner.py`) is a LangGraph graph:
`draft_plan → reflect → execute ⇄ revise`. Its hard rules, all in code:

- **Signature-based approval.** A write step executes only when its exact
  signature (`json.dumps([tool, parameters], sort_keys=True)`) was approved.
  Replanned steps get new signatures and pause again. Approval never transfers
  to an action the user has not seen.
- **Read-before-write ordering.** READ steps run first so you approve
  *"delete C:\...\a.tmp"*, never *"delete whatever the search finds"*.
- **`PENDING:` placeholders resolve in code**, not by an LLM replan — a whole
  module (`placeholder_resolver.py`) expands "delete the files the search found"
  into concrete steps with fresh signatures.
- **Pre-flight path guard.** A write step whose source path provably does not
  exist fails into the replan loop *before* the approval pause. You are never
  asked to approve a guessed path.
- **Failures are never silently skipped**, and a revision may never repeat a
  failed step unchanged (enforced by signature comparison, because the prompt
  rule saying so was ignored live).
- **Clarifying questions are verified.** An option written as a concrete path
  must exist on disk; all-invented options are rejected with retry feedback. A
  question with no options triggers a **real `search_files` run in code** to
  answer it before the user ever sees it.

**The grounding locks** — the exfiltration bound, and the most important idea in
the codebase. Each is a *comparator*: a fact computed independently of the
model's output, that the model's output is checked against.

| Lock | Guarantee |
|---|---|
| `_recipient_violation` | A send/draft address must trace to the **user's own words** or a `lookup_contact` result *in this plan*. Read email bodies are excluded from the corpus by construction — a prompt-injected "forward this to attacker@x.com" can never ground a send |
| `_event_id_violation` | A concrete calendar event id must come from a read step in this plan |
| `_entity_id_violation` | Same, for smart-home devices — a hallucinated `light.bedroom` might be the garage door |
| `_window_handle_violation` | Same, for window handles — and binds harder, because a wrong handle is unreadable to a human |
| `_scope_violation` | On "delete **all** files", a step filtering by an extension the user never said is rejected. Memory is deliberately excluded from the grounding corpus — that was the leak |
| `_upload_path_violation` | The file attached to a form must trace to the user's words |
| `_browse_origin_violation` | The sites a browse may visit trace to the user's words, never to page content |
| `_browse_substitution` | A browser-agent plan that reaches for `read_webpage` instead of `browse` is rejected — comparator is the router's own verdict |
| `_fill_violation` | A form value must come from the curated autofill profile or the user's words |

**Deterministic recovery.** `_missing_target` keys on our own tools' error
wording and drives *ask-not-fail*: a plan about to die on "folder not found"
pauses on a code-derived question with verified options instead.

**`folder_resolver.py`** — the which-drive guard. When a step is about to scope
itself to `C:\Users\...\Downloads` for a bare "downloads" the user said with no
drive, a cheap stat probe looks for `D:\Downloads` too, and pauses to ask. It
covers reads *and writes* (writes were added after 85 PDFs went to the wrong
drive), with a `test_every_path_param_is_covered_or_exempt` invariant that walks
the registry so a new file tool is a decision, never a silent hole.

---

### 5.3 Chat routing — deciding what a message *is*

`chat_stream` runs a chain of deterministic routers before the LLM ever sees the
message, each of which defers to any already-open question:

```
restore parked state
  → reminder_router     ("remind me at 6 to call Jamil")
  → routine_router      ("run my morning routine")
  → continuation_router ("look again" — re-runs the ORIGINAL goal)
  → interrupt_router    ("stop" — pauses a live agent)
  → task_router         (the two-token classifier)
  → Phase-2 chat path   (untouched fallback)
```

**The task router is a two-stage gate** so ordinary conversation pays nothing:

1. **A deterministic regex gate, tuned for RECALL.** A strong domain noun fires
   alone with any wording ("please del all files" — users invent verbs
   endlessly, but a computer task almost always names its object). Weak signals
   need an action verb. Tiers are named and audited: `strong_domain`,
   `weak_domain`, `own_action`, `external_question`, `stored_recall`,
   `browse_intent`, `desktop_intent`, `bare_navigation`.
2. **One temperature-0 classifier call** returning `(label, mode)`:
   `TASK/EMAIL/CALENDAR/WEB/BROWSE/HOME/DESKTOP/CHAT` × `INLINE/DELEGATE`.

**Mode is a UX choice, never a safety one** — both paths share the approval
gate, so a read mis-tagged DELEGATE costs a notification and a write mis-tagged
INLINE just pauses inline.

**Everything fails OPEN to plain chat** — and because it does, the chat LLM can
fabricate the backend's own message formats. So there is a **system-voice
impersonation guard**: both chat routes scan the response for backend-owned
phrases ("Done — N step(s) completed", "Reminder set —", "has been initiated"),
cut the stream at the first marker, and append a deterministic correction.
Memory extraction is skipped on a corrected turn — a fabrication must never seed
memories.

**Routing is audited.** `routing_decisions` records every turn: which gate tier
fired, the classifier label, its latency, and a `fail_open_reason` that
distinguishes three previously-indistinguishable causes — `gate_closed`,
`classifier_chat`, `classifier_error`. For a system whose main failure mode is
"it didn't do the thing", the not-doing is now the thing that is recorded.

**No magic words.** Four prompt rules used to end by telling the model to ask the
user to rephrase. That demand is now cut before it reaches the screen and the
request is re-run through the planner instead — same registry, same approval
gate, so widening the rescue widens *recall*, never *authority*.

---

### 5.4 Proactive layer — Furi speaks first

Built in strict dependency order, each layer the foundation of the next:

- **Push channel** (`/ws`) — strictly server→client. Inbound frames are read
  only to detect disconnect; actions always go through the HTTP API and its
  gates. `push()` never raises and never blocks on a broken client.
- **Scheduler** — `JarvisScheduler` over APScheduler. **SQLite is the truth**;
  timers are rebuilt at startup, and a job due while the backend was down fires
  immediately with `late=True`. A job fires **at most once, ever** (firing
  atomically claims the row, so fire-vs-cancel can never both win).
- **Ambient presence** — close-to-tray, tray menu, `Ctrl+Shift+J` global hotkey,
  single-instance lock, native toasts created in the main process (the renderer
  still never gets Node).
- **Reminders** — deterministic time parsing, *no LLM*. Ambiguity produces a
  question, never a guess. One message can carry several reminders. A fired
  reminder both pushes *and* persists a chat message, because the push channel
  has no queue.
- **Background tasks** — plans escape the chat turn. A `Task` row wraps a plan
  running as a detached asyncio task; approval pauses park through the *same*
  plan store; startup reconciles interrupted rows.
- **Live narration + cancel/pause/steer** — one `plan_step` push per transition;
  cooperative cancel checked *between* steps (a running step always finishes,
  never killed mid-write); **pause** is cancel's sibling that leaves pending
  steps PENDING, so you can correct a running agent and it resumes with its
  completed work intact.
- **Daily briefing** — one LLM call over code-gathered data, each source
  independently best-effort, with a deterministic template fallback so a 429 or
  an outage still delivers.
- **Initiative Engine** — a throttled recurring pass over the World Model
  proposes suggestions. **The autonomy policy is code-owned**: the LLM only
  proposes; `classify_autonomy` caps every candidate at the user's ceiling
  (off/suggest/ask/act), and even "act" re-derives the plan from a goal *string*,
  so **"act" = auto-PLAN, never auto-WRITE**. A governor (daily budget, quiet
  hours, rate limit, dedupe) runs entirely *before* the LLM call.
- **Pattern mining + scheduled routines** — deterministic cadence detection over
  completed task timestamps ("you do this most Fridays around 4pm — want it
  scheduled?"). A scheduled routine stores the **goal string, never a plan**, so
  every run is replanned fresh and the approval gate re-applies.
- **Relationship continuity** — people-cadence nudges (phrased honestly:
  "haven't caught up with X", never a false "haven't messaged X", because the
  underlying data is when they last *came up*), memory callbacks, and a
  `GoalThread` store with a nudge cadence that pushes `next_check_at` out so a
  concern is never nagged every heartbeat.

---

### 5.5 The Context Layer — a private world model

A local, **write-only**, in-memory picture of what you are doing right now.
Privacy is structural: opt-in, OFF by default, local-only, **retention = NONE**
(nothing sensed ever hits SQLite), behind a master kill switch with a visible
"Sensing: On" indicator.

- Device sensing runs in Electron **main** (so it works from the tray) via one
  long-lived PowerShell/Win32 helper — zero native npm dependency.
- Signals arrive over **authed HTTP, never the WebSocket** (server→client stays
  invariant).
- Screen OCR captures a **downscaled** thumbnail, only while **per-session
  armed**, and the endpoint 403s before any decode unless both toggles are on.
  The frame is OCR'd in memory and dropped — never written to disk. The summary
  is produced **deterministically** (no LLM), so periodic capture is free and
  cannot exfiltrate your screen to a provider.
- A "What Furi currently sees" audit panel is the trust surface.

---

### 5.6 Voice — hands-free, entirely local

- **STT:** faster-whisper on the RTX 3060. Measured: a 6.4s clip went **169s →
  631ms**. Language is pinned to English (unpinned, Whisper detected Arabic for
  English input and ran 23% slower).
- **TTS:** Kokoro via `onnxruntime-gpu`. Measured **13.7s → 1.04s**.
- **`sanitize_for_speech()`** — a deterministic, tested markdown→speech cleaner.
  Code fences become "(code omitted)", links become their label, bare URLs become
  a hostname, paths become a basename, emoji are dropped. Furi never reads raw
  markdown or a full file path aloud.
- **Streaming spoken responses** — an incremental sentence segmenter emits
  clauses *while the SSE response is still streaming*, with a sequential playback
  queue (synthesis of N+1 overlaps playback of N) and instant barge-in that
  invalidates every async continuation via a generation counter.
- **Intra-sentence streaming** — raw PCM16 over `/api/voice/speak/stream`, with
  an adaptive two-render schedule (measured: a naive ladder produced 2s
  mid-sentence gaps; two renders produced **zero**). First audio 2.9s → and the
  full round measured 14.15s → 5.32s end-to-end.
- **Wake word + summon** — an on-device ONNX wake model in the renderer, plus a
  hotkey summon that starts a toggle-mode recording auto-stopping on silence.
- **Spoken approval** — you can approve a write by voice, bound by a
  **contract hash** over the ordered pending-step signatures. This *reverses* the
  codebase's own refusal to accept a typed "yes" — but satisfies its reasoning
  through another channel: the client can only hold the hash if it received the
  contract. Its consent word set is deliberately **narrower** than the typed one
  (a bare "yes" does not approve by voice, because a spoken yes may be ambient).

**The single most instructive voice bug:** compiled CUDA graphs recorded on one
thread do not replay from another, so warmup compiled on one pool thread and the
first real request landed on a different one, silently falling back to a 4×-slow
eager path. Fix: all engine work runs on **one dedicated thread**, with a
thread-affinity regression test.

---

### 5.7 The browser — the hardest part of the project

Furi drives a **real, headed, watchable Chrome** with an observe → decide → act
loop and **no per-site code**. This inverts the rest of the system's doctrine:
page content drives the action loop directly, because the page *is* the input.
So the safety story is different and stated explicitly.

**Package:** `app/browser/` — `window.py` (one shared persistent context, many
per-site tabs), `session.py` (a tab's lifecycle + interception),
`observe.py` (DOM → numbered element list), `loop.py`, `commit_flow.py`,
`grounding.py`, `extract.py`, `choice.py`, `runtime.py`, `trace.py` and more.

**What is actually gated:**

- **The submit-gesture gate.** A gesture that would act on the world — a form's
  own submit control, a send/post/upload/like/delete/buy control, Enter in a
  non-search field — **stops the run and asks**. A genuine search submit is
  reading and is never caught.
- **One approval, one gesture.** Your yes returns a `gesture_fingerprint` (kind +
  the control's role/name/href + host) consumed when that gesture fires. This
  replaced a run-wide boolean under which a yes to "send this message" also
  authorised any buy the loop chose next.
- **A form submit** goes only through `arm_commit` → `submit_commit` under a
  signature approval of the **code-read contract** — method, URL, every field
  value, any attached file — never a raw click. The permit is one-shot, and only
  *firing* consumes it.
- **CAPTCHAs are never solved or touched.** Credentials are never entered by
  Furi. A wall or challenge hands the tab **over to you in place**; escalating to
  a separate clean window is *earned* only by a repeat challenge from the same
  site within 10 minutes.

**`choice.py` — "which one did you mean?"** When several items on a page (or
several values in a size/colour control) match your words *equally* well, the run
stops and asks with the page's own labels. A user whose words single one out is
never interrupted. Sold-out items are never offered; a single buyable value is
**forced in code** rather than asked about. This is pure — no LLM — and the
comparator is your own words against the page's labels.

**Highlights of the browser work, each root-caused from real failures:**

- **Query fan-out** — an ambiguous question is enumerated into readings, searched
  in parallel and merged by Reciprocal Rank Fusion. *Cover* readings rather than
  *choose* one: the right reading need only appear, never be chosen.
- **The evidence resolver** — a search whose snippets are thin or truncated is
  escalated to a real page read **in code, zero LLM calls**, because the URL is
  already in the completed step's output.
- **The summary grounding guard** — the output is checked against the record by
  token overlap, and is **shape-independent** (it reads bullets *and* inline
  comma runs, after a fabrication laid 51 invented country names out as prose and
  the bullet-only guard scored zero).
- **One Playwright route disables Chromium's HTTP cache.** Owning the CDP `Fetch`
  domain instead took repeat-load slowdown from **3.3× → 0.9×** (faster than an
  un-intercepted browser). The obvious fix — sending `setCacheDisabled: false`
  from a second session — *succeeds and does nothing*, because Chromium ORs the
  flag across sessions. Only measurement caught that.
- **Multi-tab** — one shared context, per-site tabs, LRU eviction that never
  touches a busy tab, and an ownership decision for new tabs that lives in
  exactly one place.
- **An ad pop-under is not adopted** — a tab that has landed off-allowlist is
  somewhere Rule 3 already forbids, so adopting it was incoherent. A blank tab is
  guarded immediately but **taken over only once it says where it is going**.
- **Season/episode resolution** — "the latest season" is a fact about the
  *world* (answered by AniList/TMDb, zero LLM calls, 8/8 measured) while "which
  episode is newest" is a fact about the *site* (answered by the page).

**Three scored benches** keep this honest: `browse_bench` (6 real tasks against
real sites, scored in code against grounded evidence), `plan_bench` (the real
planner on a real disk, scoring `no_unapproved_write` and `no_escape` on every
case), and `route_bench` (reporting **three** numbers — gate recall, gate cost,
label accuracy — because a single accuracy figure would have hidden the finding
that the gate blocked 7 of 8 questions the classifier got right).

---

### 5.8 Physical reach

- **Home & IoT** (Home Assistant): 5 tools, entity-id lock, **no free-text
  service call** — a fixed (domain, desired state) → service map, because an
  arbitrary-service passthrough would reach HA's own `shell_command`. The base
  URL can only come from the user's own configuration; no tool accepts a URL.
- **Desktop control**: 9 tools via **ctypes, not a PowerShell helper** — no
  subprocess to supervise and, decisively, **no shell, therefore no injection
  surface**. `launch_app` resolves a *name* against a Start-Menu registry and has
  no path or argument parameter, which is what keeps it out of DESTRUCTIVE.
  `close_window` sends `WM_CLOSE` (it asks, it does not kill) and re-verifies the
  approved title, because Windows recycles handles.
- **`open_folder`** is READ, and what makes it safe is one line, not the gate:
  it resolves a file path to its **parent**, so what reaches the OS is always a
  directory and it can never execute anything.

### 5.9 Remote surface — approve from your phone

A **second `uvicorn.Server` in the same process**, serving a second FastAPI app
built by copying only the routes on an explicit manifest.

**The safety is that the routes are not there** — not that a check refuses them.
`/chat/stream` returns 404 on the remote port for the same reason it would on a
webserver that never heard of Furi. Tests assert **absence from the mounted route
table**, not response codes: a 403 means a check ran and worked; a 404 means
there was nothing to check.

Every route is allowed or denied with a written reason, enforced by a walk over
the real app. Device tokens are **stored hashed** (they are only ever compared,
so a leaked database is useless), expiring, revocable. Pairing routes are denied
remotely — a paired device that can pair another is a credential that cannot be
taken away.

---

## 6. The design doctrine — the ideas that generalise

These are the reusable lessons, each paid for by a live failure.

### 6.1 Structural over prompt
> *"A rule with nothing to check it is a suggestion."*

Prompt-hardening was **measured at zero three separate times** on the same rule.
Every real guarantee in this codebase is a comparator: a fact computed
independently of the model's output, that the output is checked against.
`_recipient_violation` compares an address against the user's words.
`_event_id_violation` compares an id against completed reads.
`_repeated_failure` compares a signature against failed ones. When a rule has no
comparator, it is documented as *belt*, not as the guarantee.

### 6.2 A predicate must be evaluated when it is answerable
Planner rule 16 said "follow up with `read_webpage` when the snippets don't
answer it" — and had **never once executed and could not**, because the planner
drafts every step *before* any snippet exists. The fix is a different evaluation
*time*, not a stronger prompt. Conversely, "could this question mean two things?"
*is* answerable at draft time and needed an independent *evaluator*, not a later
one. Same rule, two defects, two different fixes.

### 6.3 The record must not lie
Three separate incidents where a cap or a marker made the evidence say something
false, and the model then reasoned correctly from it:
- A 300-char snippet cap cut a source mid-list, and the prompt said "only call a
  list truncated if the results say so" — so **fabrication was the compliant
  reading**.
- `json.dumps(output)[:1200]` plus the literal `"… (truncated)"` turned a
  complete 85-file search into "8 files, truncated".
- `rendering._clip` no longer uses the word "truncated" at all: in this codebase
  that word is a *fact a tool reports about its own results*, and using it for
  "our display budget ran out" is exactly the conflation that caused the bug.

### 6.4 A second copy of a list is a hole
Recorded **seven times**: `_MUTATION_TEMPLATES` keyed on singular tool names
while the prompt steered the planner to the plural ones; `_DIR_KEY` covered
reads but not writes; `_settle` kept its own status tuple; `set_voice_config`
hand-listed its fields and silently dropped a consent setting. The fix is always
the same shape — **stop keeping the list**: read the registry
(`registry.mutates`), derive the predicate, serialize with `asdict()`. And where
a mapping genuinely cannot be derived, make it **total** with a coverage test
that walks the real registry. Those tests have found real holes on their *first
run* three times.

### 6.5 Fail toward asking, never toward guessing
`folder_resolver` asks which `downloads`; `did_you_mean` asks which domain;
`choice.py` asks which product; `lookup_contact` asks which person. Code never
picks between real equals. And an ask must never be a dead end — every
clarifying question offers verified options, and a stuck browse now asks rather
than dying.

### 6.6 Measure; do not reason about performance
- The prompt-size reorder was scoped as "the biggest latency win for tasks";
  measured, a 6× larger prompt cost the **same wall time** (network RTT is the
  floor). **Dropped.**
- A "browser got slower" report measured *worse on the old code* — the machine
  was slower that day. The only valid control is the old code, now.
- A 151-second decision turned out to be machine-wide contention, not the
  browser.
- Fuzzy-match thresholds are chosen from **printed score tables**, not intuition:
  the typo floor is 84.0 because the lowest true typo and the highest coincidence
  *touch at 83.3*.

### 6.7 A test that drives a shape the product no longer uses measures nothing
Five recorded instances. 1,578 green tests could not see a whole feature that had
never fired, because every test called the tool directly and none drove the
planner. 37 bulk-file tests all drove the singular tool after the prompt had
steered the planner to the plural one. A page fake returned the same object for
two "tabs". **A fake page is a claim about the live DOM — check it against the
real one.**

### 6.8 Falsification is the acceptance criterion
Every behavioural change is **proven to FAIL by reverting the specific line in
place**, with a regression twin that must keep passing. The harness
(`backend/scripts/_falsify_*.py`) encodes its own hard-won rules as checks:
- anchors must be unique whole lines including indentation (a partial anchor once
  produced an `IndentationError` — every test failed, for the wrong reason, which
  is indistinguishable from a passing falsification if you read only the exit
  code);
- the revert is **verified on disk** before the result is trusted (three
  falsifications have *lied*);
- pytest's **exit code** is read, not its last line (5 = nothing collected is
  never a pass; a `| tail` once returned *tail's* exit code);
- restore happens in a `finally`.

**When a falsification comes back green, suspect the test's reach, then the
fixture, then the code.** Recorded causes: the guarantee was defended in three
places and reverting one left the others holding; the test called the function
directly and never touched the wiring; the fixture made both branches give the
same answer.

### 6.9 Runtime verification is separate from tests
A hermetic test cannot tell you the wiring boots. Every significant round ends
with a `_verify_*_runtime.py` that drives the **real `main.py` lifespan** on an
isolated port with a scratch database. Those probes have found defects no test
could: a CUDA-graph thread affinity bug, an async variant `id` that resolves ~1s
after the DOM settles, a token budget consumed entirely by reasoning, a search
box whose sibling was an image-upload button.

**And the probe is wrong roughly as often as the code.** Seven recorded
instances — a selector that matched the nav bar, an assertion that "token" was
absent from a response containing `has_token`, a truncated URL whose 404 title
scored plausibly. **A probe that asserts the wrong thing manufactures defects.**
The rule that came out of it: a negative result is only evidence if a **positive
control passes in the same run**.

### 6.10 Everything degrades, nothing crashes
Google not connected drops a briefing section, never the briefing. Qdrant
missing falls back to filename search. Narration is best-effort and can never
break execution. A history write failure logs *and rolls back*, because a
poisoned session once silently killed chat persistence for a day. Every one of
these is a `try/except` with a written reason next to it.

---

## 7. Data model

23 tables. The interesting ones:

| Table | Role |
|---|---|
| `messages` | Chat history + an `embedded_at` cursor driving incremental conversation embedding |
| `semantic_memories` | Facts, with `subject` (user/shared/contact), `archived_at`, `last_used_at` |
| `contacts` / `contact_interactions` | People + an **append-only** fact log |
| `memory_conflicts` | A queue of "these may conflict" — a claim, never a verdict |
| `parked_plans` / `pending_resolutions` | Persisted un-answered questions (SQLite is the truth, memory is a cache) |
| `tasks` | Background agent runs, with `domain` naming the owning agent |
| `routines` | Procedural memory — a **goal string**, never a plan |
| `scheduled_jobs` | The scheduler's truth; timers are rebuilt from it at boot |
| `activity_log` | Every tool attempt, executed and blocked |
| `routing_decisions` | Why each message routed where — the "it didn't do the thing" audit |
| `plan_traces` | One row per planning invocation: fail class, rejections, replans, duration |
| `app_settings` | Generic k/v for all runtime settings (no migration per feature) |
| `file_index` | Per-file ledger for incremental re-embedding |

**Timestamp convention:** the DB stores naive UTC; API serializers must use
`utc_iso()`, never bare `.isoformat()` — a naive ISO string is read as *local*
by `new Date()` and every displayed time shifts by the machine's offset. Calendar
*dates* (birthdays, event dates) deliberately keep bare `.isoformat()`, because
marking them UTC would shift the displayed day.

**Migrations auto-run at startup** (`ensure_schema()`), handling three DB states,
followed by `verify_schema()` as a drift alarm — because a migration written but
never applied once silently stopped chat history persisting for a day.

---

## 8. Security & privacy posture, in one place

| Concern | Control |
|---|---|
| Local API is not authorization | Static token at `~/.jarvis/auth_token` (0600, atomic write); ASGI middleware validates `X-Jarvis-Token` on every request and `?token=` on every WS handshake, with `compare_digest`. Fails **closed** |
| Destructive actions | Structurally refused without `approved=True`, at one choke point |
| Shell | Hard blocklist + 30s timeout in code; PowerShell `-EncodedCommand` refused; the blocklist runs over *script contents* before spawning |
| Deletes | Never unlinked — moved to `~/.jarvis/trash`; if the backup move fails, the delete does not happen |
| Prompt injection | Web pages, email bodies and screen text are framed as DATA-never-instructions **and** excluded from every grounding corpus by construction |
| Secrets | Autofill secrets DPAPI-sealed at rest; the model only ever sees a `{{secret:key}}` placeholder |
| OAuth | Least-privilege frozen scopes (never `gmail.modify`); token at `~/.jarvis`, never in the DB, never logged; a missing scope reads as *not connected* |
| SSRF | http/https only; localhost, private, link-local and cloud-metadata refused; the final host re-checked after redirects |
| Sensing | Opt-in, OFF by default, retention NONE, master kill switch, visible indicator, hard 403 before any decode |
| Remote access | Separate hashed device tokens; dangerous routes **not mounted**; the local token does not work on the remote port |
| CAPTCHAs | Never solved, never auto-interacted with — structural, the loop returns before any decision |

---

## 9. Testing & quality methodology

Five distinct instruments, each answering a question the others cannot:

1. **Hermetic unit/integration suite** — ~3,900 tests, 19 autouse fixtures making
   network, GPU models, the real home directory and real Google tokens
   structurally unreachable.
2. **Falsification harness** (43 scripts) — every behavioural change proven to
   fail when its specific line is reverted in place.
3. **Runtime verification** — the real lifespan, real ports, real Chromium, real
   providers, scratch databases.
4. **Scored benches** — `route_bench` (67 cases × 3 runs, three separate
   numbers), `plan_bench` (real planner, real disk, safety invariants on every
   case), `browse_bench` (6 real websites, scored against grounded evidence).
5. **Measurement scripts** — `perf_probe`, `browse_speed`, `browse_observe_profile`,
   `bench_voice`, and one-off `_measure_*` probes that decided real design
   questions (and killed at least three planned features on evidence).

**Known-gap discipline:** a measured defect that is deliberately not fixed is
recorded with its measurement and excluded from the exit code — because a
permanently red gate stops being read, which is the same failure as a no-op that
reports success, in the other direction.

---

## 10. What is deliberately *not* built

Recorded with reasons, so they are not re-litigated:

- **Automatic conflict resolution in memory.** Deciding one fact invalidates
  another is a judgement about meaning with no substring test behind it, and
  being wrong destroys a true fact silently and permanently.
- **Episode consolidation.** Speculative — episodes are already bounded at render
  and the real database has zero rows.
- **A `kill_process` desktop tool.** That is a data-loss tool wearing a window
  tool's name.
- **A free-text Home Assistant service call.** It would reach HA's own
  `shell_command`.
- **Explicit "part N" season parsing.** In these catalogs a part is a subdivision
  of a season as often as it is one; a bare number after it does not reliably
  name an entry.
- **Vision-first browsing.** Measured: up to 12s per step returning "unusable"
  while DOM did all the real work. Kept as a *stuck-only* escalation, and the
  posture is a setting so reversing it needs no code change.

**Honest open limits:** Windows-only for desktop control and DPAPI; "non-GET =
mutation" in browser READ mode is an HTTP convention (RFC 7231), not a proof;
within an allowlisted authenticated origin a compromised loop has full user
authority — which is why the window stays headed and watchable; and the enabled
`foreign_keys` pragma is per-connection and currently only set on the `init_db`
connection.

---

## 11. The LinkedIn demo video

### 11.1 What to show — and what to leave out

The temptation is to show all 48 tools. **Don't.** The story is not breadth, it
is that this thing is *safe and real*. Pick five moments; every one must be a
live screen recording of the actual app, never a slide.

| # | Moment | Why it earns its place | Time |
|---|---|---|---|
| 1 | **Memory across sessions** | Proves it is not a chat wrapper. Close the app, reopen, ask something only a persistent memory could answer | 0:20 |
| 2 | **A real file task with the approval card** | The core safety story, visible in one screen: it names the exact files and the exact destination, and cancelling does nothing | 0:35 |
| 3 | **Voice mode, end to end** | Speak → it plans → it *speaks the contract aloud* → you approve by voice. This is the "wow" shot | 0:30 |
| 4 | **The browser filling and submitting a real form** | The single most impressive capability. Real Chrome, real site, real approval card showing method + URL + every field value | 0:45 |
| 5 | **Proactive: it speaks first** | A reminder or a suggestion arriving unprompted as a native toast with the app in the tray | 0:20 |

**Leave out:** the settings panels, the memory explorer, anything that is a list
of rows. They are good product work and they are boring on video.

### 11.2 The script (~2:30)

> **[0:00 — cold open, no talking. Screen only.]**
> *Type into the app:* `move all the pdfs in downloads to a folder called invoices`
> *Let the approval card render, and hold on it for two full seconds.*

**VO:** "This is Furi. It's a personal AI OS that runs entirely on my machine.
And before it touches a single file, it stops and shows me exactly what it's
about to do."

> **[0:12 — click Approve. Show the files move. Show the completion message.]**

**VO:** "That gate isn't a prompt asking the model to behave. It's an `if`
statement above the tool call. A write is refused unless the exact parameters —
these exact 32 file paths — were approved. Replan it, and it asks again."

> **[0:30 — cut to a *new* session. Ask something personal.]**
> `what did I say I was working on with Jamil?`

**VO:** "It remembers. Not the last thirty messages — a real memory engine.
Facts, people, preferences, open threads, all with identity resolution that asks
'which Jamil?' instead of guessing. And it decays and consolidates, so memory
that only grows doesn't slowly crowd out the facts that matter."

> **[0:50 — hit the hotkey. Voice mode opens. Speak.]**
> "Furi, what's on my calendar tomorrow, and email Ali the summary."

**VO:** "Voice is local — Whisper and Kokoro on the GPU. Six hundred milliseconds
to transcribe. And it speaks the approval contract out loud before it sends
anything: who it's going to, the subject, the whole body."

> *Let it read the contract. Say "approve".*

**VO:** "That yes is bound to a hash of the exact steps it just read me. If the
plan changed, the yes doesn't apply."

> **[1:30 — the big one. Real Chrome opens on a real site.]**
> `go to <site> and add the black kameez in large to my cart`

**VO:** "This is a real Chrome window, and there is no code in this project that
knows anything about this website. It reads the DOM, decides the next action,
searches, opens the product — and when there are three things that match what I
said equally well, it stops and asks which one, using the page's own labels."

> *Answer. Let it pick the size. Let the commit card render.*

**VO:** "Now watch this card. That's not the model describing what it's doing —
that's the actual form contract, read out of the page in code. Method, URL, every
field value. One approval, one gesture. It cannot submit anything else."

> **[2:05 — close the window to tray. Wait. A native toast appears.]**

**VO:** "And it starts things itself. Reminders, a morning briefing, and an
initiative engine that watches what I'm doing and suggests the next thing —
capped at whatever autonomy level I set, and even at the highest one it only
auto-*plans*. Every write still stops at the same gate."

> **[2:20 — final card on screen, numbers only.]**

**VO:** "One hundred and forty-seven thousand lines. Forty-eight tools. Nearly
four thousand tests. Thirty-seven days."

### 11.3 The end card

```
Furi OS
Local-first personal AI operating system

147k LOC  ·  48 tools  ·  ~3,900 tests  ·  3 scored benchmarks
Electron + React + FastAPI + LangGraph + Qdrant
Whisper & Kokoro on-device  ·  Playwright browser agent

Every action that changes the world is refused in code
until you approve the exact parameters.

37 days.
```

### 11.4 Production notes

- **Record at 1080p, crop to 1:1 or 4:5.** LinkedIn plays vertical/square far
  better in-feed.
- **Burn in captions.** Most of the feed is watched muted; your VO is the whole
  argument.
- **Do not speed up the approval cards.** They are the point; let them breathe.
- **First three seconds decide everything.** Open on the approval card, not on a
  logo and not on your face.
- **Use a real machine with real data**, blurred where needed. A demo with
  `test1.txt` and `foo@bar.com` reads as a toy.
- **The post copy should lead with the safety idea, not the feature list.**
  Something like: *"I spent a month building a personal AI that can delete my
  files, send my email and buy things on the web. The interesting part isn't what
  it can do — it's that none of it is possible without an approval, and that
  guarantee is enforced in code rather than by asking the model nicely. Here's a
  two-minute tour."*

---

## 12. Honest assessment — a rating

**8.5 / 10.**

That number is deliberately not a 10, and deliberately not a 6. Here is the case
for each side.

### Why it is not lower — what genuinely distinguishes this

AI coding assistants have made *volume* cheap. A 147k-line agent app with 48
tools is, in 2026, no longer proof of much on its own. What is still expensive —
and what this project actually demonstrates — is **judgement under evidence**:

1. **The safety architecture is real engineering, not vibes.** The distinction
   between a prompt rule and a comparator, the observation that a rule with
   nothing to check it is a suggestion, the decision to test *absence from the
   route table* rather than a 403 — these are the kinds of decisions that models
   do not volunteer and that most human-built agent projects get wrong.
2. **The verification methodology is better than most production teams'.**
   Falsifying every behavioural change by reverting the specific line, with a
   regression twin, and a harness that verifies the revert landed on disk because
   three falsifications had *lied* — I have not seen that discipline in a
   one-month personal project before. The three scored benchmarks and the
   separation of hermetic tests from runtime probes are the same instinct.
3. **The engineering log is itself an artifact.** `CLAUDE.md` records not just
   what was built but what was *tried and measured to zero* — prompt-hardening
   three times, a Google CSE migration that turned out to be impossible, a
   prompt-size optimisation that measured flat. Recording your falsified
   hypotheses is a research habit, and it is the reason the same defect class was
   caught seven times instead of shipping seven times.
4. **The failure-driven design.** Almost every guard in this codebase is named
   after the live incident that produced it. Nothing here was designed from an
   imagined threat model; it was designed from logs. That is why the guards are
   in the right places.
5. **It works end to end on real inputs.** A real browser filling a real
   storefront form behind a real approval card, GPU voice at 600ms, memory that
   survives months — measured, not claimed.

### Why it is not higher — the honest deductions

1. **Single-platform.** Windows-only for desktop control, DPAPI, and the sensing
   helper. That is a real ceiling on "operating system".
2. **Single-user, single-machine, no packaging story.** There is no signed
   installer, no update channel, no onboarding for someone who isn't you. The
   distance from "runs on my machine beautifully" to "someone else can install
   it" is not trivial and is not covered.
3. **The frontend is the weakest layer.** 17k lines against 68k of backend, no
   ESLint config, and by the doc's own admission the UI panels are functional
   rather than designed. For a demo-driven product that matters.
4. **Reliability rests on nondeterministic components in places.** The routing
   classifier, the summary writer and the browse decision loop are all LLM calls,
   and the log honestly records compounding variance (enumeration × ranking ×
   obedience). The benches make this *visible*, which is the right response, but
   it is still a ceiling.
5. **Cost and dependency surface.** Optional GPU wheels, a 6GB VRAM budget that
   the project's own audit found is already over-committed, Playwright, Qdrant,
   an LLM API key. The install story is heavy.
6. **Some scope is breadth over depth.** The home and desktop tool suites are
   thin (5 and 9 tools), and several roadmap features are specs rather than code.

### The "but AI wrote it" question, answered directly

The relevant question in 2026 is not *did a model write the lines* — it did, and
that is now unremarkable. The question is: **could someone get this result by
asking a model for it?** No. Ask any model for "a personal AI assistant" and you
get a chat wrapper with a tool loop and no approval gate. What produced this
specific system is a month of:

- reading logs and root-causing to the *line* before writing a fix;
- refusing three separate attempts to fix a problem with a better prompt because
  the previous two had been *measured* at zero;
- inventing a falsification harness and then hardening it against its own lies;
- killing your own planned features when the measurement disagreed with them
  (the section-link click, the prompt reorder, the Google CSE migration);
- and writing down every falsified hypothesis so the next round does not repeat
  it.

That is the scarce skill now, and the artifact demonstrates it more clearly than
most production codebases do. **8.5.**

To make it a 9.5: ship a signed installer, get it running on a second machine
that isn't yours, and put a real design pass on the frontend. To make it a 10:
have someone who is not you use it daily for a month and fix what that finds.

---

*End of dossier.*
