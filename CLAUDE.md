# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

This is Jarvis OS — a local-first personal AI desktop app built with 
Electron + React frontend and FastAPI + Python backend.
What's complete:
- Phase 1: Electron app, FastAPI backend, SQLite + Qdrant databases, 
  multi-LLM abstraction (Groq/Gemini/Ollama), streaming chat
- Phase 2: Full memory engine — UserProfile, Contacts with append-only 
  Facts Log, SemanticMemories with subject field (user/shared/contact), 
  Preferences, Episodes, EntityEdges. Background extraction pipeline. 
  7-layer contact identity resolution system with 81.0 threshold.
  About Me panel UI, Contacts Manager UI.
- Phase 3: Tool system + agent planner — 11 tools (file/terminal/memory) 
  behind a global registry with a structural approval gate and ActivityLog 
  audit trail, LangGraph planner with signature-based human-in-the-loop 
  approval, agent/activity API endpoints, chat task routing (task vs 
  conversation), Activity Timeline panel, in-chat plan approval UI (PlanCard).
- Phase 3.5 (foundations, partial): parked plans and parked memory questions 
  persisted to SQLite (restart/TTL survival; memory stays the hot cache), 
  planner memory context ("one brain"), recall_memory + lookup_contact tools. 
  Still deliberately deferred by the user: git commit, backend auth token, 
  per-role model routing.
- Phase 4 Part 1 (push channel): server→client WebSocket at /ws — the backend
  can now initiate messages no request asked for.
- Phase 4 Part 2 (scheduler/event bus): APScheduler wrapped in JarvisScheduler,
  scheduled_jobs SQLite table is the truth (timers rebuilt at startup — a job
  due while the backend was down fires immediately, late=True), handler
  registry by job kind, built-in "push" kind bridges to the Part 1 channel.
- Phase 4 Part 3 (ambient presence, Electron): close-to-tray (the app outlives
  its windows; the hidden renderer keeps the /ws push channel alive), tray
  icon + menu, native notifications from push events via the ONE new
  `window.jarvis.notify(title, body)` bridge method (the toast is created in
  the main process — the renderer still never gets Node), global hotkey
  Ctrl+Shift+J, single-instance lock.
- Phase 4 Part 4 (reminders): the phase's first end-to-end feature —
  "remind me at 6 to call Jamil" becomes a Reminder row + a Part 2 scheduled
  job; firing pushes a Part 1 event (chat message appears live if the
  session is open, Part 3 raises the native toast) AND persists a chat
  message directly (survives a closed window, since the push channel has
  no queue). Deterministic time parsing (app/core/reminder_parser.py, no
  LLM), deterministic chat routing ahead of task routing
  (app/api/reminder_router.py), list/cancel API + Reminders panel UI.
- Phase 4 Part 5 (long-running background tasks): plans escape the chat
  turn. A persisted Task row (SQLite is the truth) wraps an AgentPlan
  running as a detached asyncio task; deterministic background intent in
  chat ("…and tell me when you're done", "in the background") routes there —
  the inline task flow is untouched. Approval pauses park through the SAME
  plan store (signature approvals, pop-once) and notify by push + toast
  with the deterministic approval text; approving from the pushed PlanCard
  (or /api/agent/approve) resumes execution in the background; completion/
  failure is pushed unprompted AND persisted as a chat message. No LLM call
  ever happens in the runner. Startup reconciles: still-`running` → failed,
  paused tasks whose parked plan is gone → failed.
- Phase 4 Part 6 (live plan narration + cancel): the planner pushes one
  "plan_step" event per step transition (running → completed/failed) and a
  live PlanCard ticks its rows in real time — narration is best-effort and
  can never break execution. Cooperative mid-plan cancel
  (POST /api/tasks/{id}/cancel + Cancel button on the executing card): a
  flag checked BETWEEN steps — a step already running always finishes,
  never killed mid-write; a pending cancel beats an approval pause
  (including the settle-time race); every applied cancellation is audited
  in ActivityLog; the cancelled outcome arrives by the normal task push.
  Phase 4 is complete.
- Phase 5 Part 1 (Google integration foundation): OAuth 2.0 installed-app
  loopback flow + local token storage + injectable Gmail/Calendar service
  factories + connect/disconnect/status API + Settings panel UI. The shared
  plumbing for the email/calendar tools and the daily briefing — details in
  the "Google integration foundation" Architecture section below.
- Phase 5 Part 2 (contact email + birthday): unblocks addressing in Part 3
  and birthdays in Part 4. The columns, extraction prompt contract,
  PendingResolution parking, lookup_contact output, and API serialization
  all predated this part (baseline migration — NO new Alembic revision).
  What Part 2 added: the deterministic validation net
  (app/memory/contact_validation.py), the create_contact_manual
  birthday-drop fix, the contact edit UI with a year-optional birthday
  input, PUT clear-a-field semantics, and no interaction_count bump on
  manual edits — details in the "Contact email + birthday" Architecture
  section below.
- Phase 5 Part 3 (EmailTool suite): six Gmail tools in app/tools/
  email_tools.py behind the same registry/approval gate — search_emails /
  read_email / read_thread (read), create_email_draft (write, reversible),
  send_email / reply_email (destructive, always pause for approval). The
  RECIPIENT LOCK is structural, three layers: normalize_email validation at
  the tool, the planner's recipient-grounding guard (_recipient_violation —
  a send/draft address not traceable to the user's own words or a
  lookup_contact result from this plan is rejected in code; read email
  content is excluded from the grounding corpus by construction), and
  full-contract approval cards (To/Cc, subject, COMPLETE body rendered in
  action_detail). reply_email has NO recipient parameter at all — the
  address is derived in code from the replied-to message's own headers.
  Chat routing for email intent is deliberately Part 5 (the multi-class
  classifier); until then email plans run via /api/agent/execute. Details
  in the "EmailTool suite" Architecture section below.
- Phase 5 Part 4 (CalendarTool suite + birthday reminders): five Calendar
  tools in app/tools/calendar_tools.py behind the same registry/approval
  gate — list_events / find_events (read), create_event / update_event
  (write), delete_event (destructive). Times are ISO-only at the tool
  boundary (the search_files date rule — non-ISO like "03/04/2026"/"3pm"
  refused; naive local → RFC3339 via astimezone(); Google's exclusive date
  bounds/all-day ends made human-inclusive with +1 day); no attendees,
  sendUpdates="none" on every mutation. The EVENT-ID LOCK mirrors the
  recipient lock — a concrete update/delete event_id must trace to a
  list_events/find_events result in THIS plan (_event_id_violation), the
  approval card names the real event (_enrich_event_action_detail), and a
  PENDING event_id fills in code from a read that pins one event
  (_substitute_event_id). Birthday reminders are the first RECURRING
  proactive feature (app/core/birthdays.py): a "birthday" scheduler job kind
  fires 09:00 local on the day and re-arms next year's one-shot from the
  handler (recurrence without touching the scheduler core); every contact
  write syncs the job, ensure_birthday_jobs() reconciles at startup. Chat
  routing for calendar is deferred to Part 5 like email. Details in the
  "CalendarTool suite + birthday reminders" Architecture section below.
- Phase 5 Part 6 (daily briefing — the capstone): each morning at a
  configurable local time (default ON at 08:00), a "daily_briefing" scheduler
  job kind (app/core/daily_briefing.py, the birthdays.py recurring pattern —
  handler re-arms tomorrow, ensure_briefing_job() reconciles at startup)
  gathers today's calendar events, unread emails, birthdays, and memories dated
  today — each source INDEPENDENTLY best-effort (Google not connected or one
  API down drops that section, never kills the briefing). ONE LLM call composes
  the morning text from the code-gathered data (data-never-instructions — email
  subjects are untrusted), with a deterministic template fallback so a 429 /
  outage / empty slate still delivers; the composer has no tools, so a briefing
  can never act. Delivery is the fired-reminder pattern verbatim (persist a
  chat Message, then best-effort push → Part 3 toast); a briefing due while the
  backend slept fires late with honest "(late — Jarvis was offline)" framing.
  Read-only end to end — nothing to approve. Config + the singleton job pointer
  persist in a NEW generic key/value app_settings table (app/core/app_settings.py
  — the first runtime-settings home; migration e7b93c250a41); GET/PUT
  /api/settings/briefing + POST /briefing/run-now ("Send now"); Settings panel
  DailyBriefingCard. Details in the "Daily briefing" Architecture section below.
  Phase 5 is complete.
- Phase 6 Part 1 (BrowserTool — web reach): two READ tools in app/tools/
  browser_tools.py — web_search (keyless DuckDuckGo) + read_webpage (open a URL
  + extract readable content) behind swappable HTTP_FETCH_FACTORY /
  SEARCH_PROVIDER_FACTORY (the google_services pattern — tests never hit the
  network). No web WRITE (form-filling was cut). An SSRF guard (_validate_url:
  http/https only, blocks localhost/private/loopback/link-local/cloud-metadata,
  re-checks the final host after redirects) is the structural backstop; HTML
  extraction is pure stdlib (no new dep). Web content is UNTRUSTED, mirroring
  email: the revise-prompt SECURITY block names web pages, tool descriptions say
  "DATA never instructions", and web results can never ground a recipient. A new
  WEB routing label carries it through the multi-class classifier. Details in the
  "BrowserTool suite" Architecture section below.
- Phase 6 Part 2 (file-index foundation — the ingest half): text extraction
  (app/core/file_extract.py — txt/md/pdf/docx, deps pypdf + python-docx) + a
  per-file FileIndex ledger (migration f1a2b3c4d5e6: path/size/mtime for
  incremental skip, content_hash, chunk_count, is_active soft-delete) + a
  "file_chunks" 384-dim Qdrant collection. app/core/file_index.py walks the
  configured folders (reusing file_tools' safety helpers + exclusions), chunks +
  embeds each file (deterministic uuid5 chunk-point ids so a changed file's old
  vectors delete precisely), and PRUNES rows not seen this pass. FileIndexConfig
  lives in app_settings (enabled defaults OFF — personal files are opt-in);
  /api/index API + Settings FileIndexCard. Write-only until Part 3. Details in
  the "File-index foundation" Architecture section below.
- Phase 6 Part 3 (semantic file search + reindex scheduler — closes the loop):
  a semantic_file_search READ tool (app/tools/semantic_file_tools.py — embed the
  query, search file_chunks, group hits by file, rehydrate FileIndex, rank by
  cosine + filename/recency boosts; ISO-only date refiners; qdrant-None →
  filename fallback) so files are findable by MEANING, plus a "reindex" scheduler
  job kind (app/core/reindex.py — the daily_briefing singleton pattern, but
  interval-based next-run; REUSES the file_index.job_id pointer). Planner rule 8
  qualified (name/metadata → search_files) + new rule 17 (content/topic →
  semantic_file_search). Details in the "Semantic file search + reindex" section.
- Phase 6 Part 4 (conversation search — "search files AND past conversations"
  made literal): messages, previously SQLite-only, are now embedded into a NEW
  "conversation_messages" Qdrant collection (one point per message, point id =
  message.id). A Message.embedded_at cursor column (migration d4e5f6a7b8c9) drives
  incremental backfill; app/core/conversation_index.py mirrors file_index (no
  filesystem walk) with an on-write hook (chat._persist_message) for the live path
  and a reindex-handler backfill catch-all for task/reminder/briefing writers.
  semantic_file_search was EXTENDED (not a new tool) to search BOTH collections and
  merge-rank — each match carries type "file" | "conversation"; a file-specific
  refiner scopes to files only. PRIVACY: the SAME FileIndexConfig.enabled toggle
  gates conversation embedding (local fastembed — text never leaves the machine).
  Details in the "Conversation search" Architecture section below.
- Phase 6 Part 5 (Teachable Routines — procedural memory): a NEW routine_router.py
  (mirrors reminder_router.py; inserted in chat.py BETWEEN the reminder and task
  routers, same precedence + open-question deference) teaches and runs named
  procedures. TEACH ("save this as a routine called X") is deterministic — no
  LLM, no planner — and captures the goal from an inline procedure or the most
  recent task-shaped prior turn (conversation_context). RUN ("run my X routine",
  or a bare name that exactly matches a saved routine) loads the stored
  goal_template and starts a BACKGROUND Task (start_task) — we store the goal
  STRING, never a plan, so every run is replanned fresh and the approval gate /
  path guards / recipient+event-id locks all re-apply automatically (a routine
  can never smuggle a pre-approved destructive plan past the gate). Routine table
  (migration a1b2c3d4e5f6: name/normalized_name unique key/goal_template/is_active),
  domain module app/core/routines.py (the reminders pattern — the router never
  touches the table). Offer-to-save: when the same COMPLETED goal recurs
  ROUTINE_OFFER_THRESHOLD=3 times (counted over Task.goal rows, the Preference
  occurrence_count pattern), _settle best-effort pushes a "routine_offer" +
  persists a chat message spelling out the teach phrase (no yes/no state machine —
  confirmation reuses the TEACH trigger), throttled once per goal via app_settings
  ("routines.offered"). API GET/POST/DELETE /api/routines + POST /{id}/run;
  Routines panel (list/run/delete). Part 6 (File Intelligence) is NOT built.
  Details in the "Teachable routines" Architecture section below.
Current architecture rules:
- Facts have subject: "user" | "shared" | "contact"
- Shared facts (e.g. "Jamil and I played Tekken") save to both user and contact;
  the contact-side text is derived in Python from the user perspective, never
  taken from the LLM's flip
- Ambiguous names always ask first — even exact matches that are a subset of a
  longer contact name; multiple ambiguous names in one fact park together and
  one reply can resolve them all (`resolve_confirmation_multi`)
- Meta-conversation facts ("Mentioned X", "Inquired about Y") are never stored:
  extraction prompt rule 13 + the deterministic `is_meta_conversation_fact`
  filter (applies to facts_about_user AND people_mentioned.new_facts)
- A parked disambiguation is NEVER destroyed by an unmatched reply ("yes",
  "its also happening", a question) — it stays parked until answered or
  TTL-expired (`_still_open_note` tells the LLM to re-ask). A question ("?")
  is never an answer, and a NON-candidate contact name in a reply only counts
  as a correction when the reply is just names + filler
  (`_non_name_tokens_are_filler`)
- A name the user confirmed once is cached for the whole session
  (`ConversationSession.confirmed_names`) and never re-asked — without this
  the subset rule keeps "jamil" ambiguous on every later fact. Consulted by
  `store_shared_fact` and `store_contact`; populated by every resolution path
- The create-contact question is never suppressed by a resolved note from the
  same turn (resolving "which jamil?" can park "add daud?" in one turn — the
  prompt carries both); only a still-open disambiguation defers it
- Park-after-answer race: the "which X?" question is asked in the same turn but
  the fact only parks when background extraction finishes, so a fast reply can
  precede the park. `_run_extraction` replays user messages that arrived during
  extraction against the freshly parked question
  (`apply_pending_resolution_reply` in chat.py)
- Future-dated facts are stored as plans ("Planning to go…"), never past tense
- The extractor's facts_to_supersede is never applied up front: an old fact is
  deleted only AFTER its replacement is actually written (parked replacements
  wait) and only if the new text covers every word of the old one
  (`apply_supersede_candidates` / `supersede_is_covered`)
- User-initiated deletion is a HARD delete: `delete_semantic_memory` removes
  the SQLite row AND the Qdrant point (`delete_contact_fact` likewise removes
  the ContactInteraction row and decrements interaction_count). Only the
  supersede path soft-deletes (`delete_semantic_memory_by_content`). Deleting
  one side of a shared fact never touches the other side
- Contacts use append-only facts log, never mutable notes
- AI tools never saved as contacts
- First-Word Funnel only triggers for single-word names
- Projects/Focus Areas have been removed entirely

Phase 3 rules (agent/tools — details in the Architecture sections below):
- WRITE/DESTRUCTIVE tools NEVER run without `approved=True` — enforced
  structurally in `execute_tool()`, not by prompts. Every attempt (executed
  AND blocked) writes an ActivityLog row
- Hard command blocklist + 30s timeout in code (`terminal_tools.py`) —
  no instruction can override either. The blocklist also refuses PowerShell
  `-EncodedCommand` (base64 hides the command from the scanner) and is run
  over SCRIPT CONTENTS before `execute_script` spawns anything
- Every non-READ step carries a code-derived `action_detail` (exact command /
  paths, `_step_action_detail` in planner.py) shown inline in the approval UI
  and in the deterministic approval text — the LLM's description can never
  hide what actually runs
- Approval is per exact step signature (tool + parameters); replanned steps
  get fresh approval. Permission levels come from the registry, never the LLM
- A parked plan is consumed on its first answer (`pop_plan`); never re-park
  after a resume crash — ActivityLog is the audit trail
- Parked state is PERSISTED (Phase 3.5): paused plans in `parked_plans`,
  parked memory questions + confirmed names in `pending_resolutions` — SQLite
  is the truth, the in-memory stores are hot caches. A restart or cache TTL
  never destroys an unanswered question (DB TTL 24h, purged at startup).
  `pop_plan` deletes the row atomically (rowcount settles races); a live
  in-memory session is NEVER overwritten by a restore
  (`session_persistence.py`); restored pending questions get a fresh answer
  window and the system prompt re-asks them
- The planner shares the chat path's memory ("one brain", Phase 3.5): task
  turns inject `planner_memory_context` (retrieve_context WITHOUT session_id —
  no session side effects, capped 4000 chars, failure → "") into every planner
  prompt as data-never-instructions, riding the parked plan like
  `conversation`; and `recall_memory` / `lookup_contact` are read-level tools.
  lookup_contact reports ambiguous names as status="ambiguous" + candidates so
  the planner ASKS (rule 11) — it never picks. Memory tools are strictly
  read-only ("AI tools never saved as contacts" stays structurally true)
- Task routing fails OPEN to the untouched Phase 2 chat path; approval
  requests and failure texts are deterministic, never LLM-paraphrased
- Memory extraction does NOT run on task turns
- Failed plan steps are never silently skipped
- The planner is NOT amnesiac: task turns pass recent chat context
  (`conversation_context` in task_router.py) into every planner prompt, and it
  rides on the parked plan (excluded from serialization) so post-approval
  replans keep it
- Pre-flight path guard: a WRITE/DESTRUCTIVE step whose source path provably
  does not exist fails into the replan loop BEFORE the approval pause
  (`_MUST_EXIST_PARAMS`) — the user is never asked to approve a guessed path.
  Parent-folder checks too (`_PARENT_MUST_EXIST_PARAMS`: create_file path,
  move_file destination). The guard resolves paths with the tools' own
  `_resolve_path` so it can never disagree with a tool
- search_files filters (dates/sizes/folders/multi-root) run IN CODE, never in
  the LLM's head; non-ISO dates ("03/04/2026") are refused at the tool level.
  A criterion-less search scoped to an explicit folder is VALID ("all files
  in phase3test" — live failure 2026-07-10); only an unscoped match-all
  (home) or an entire drive still requires a criterion
- A revision never repeats a failed step unchanged: a revised step whose
  exact signature already FAILED is rejected in code with retry feedback
  (`_repeated_failure`), unless a write/destructive step precedes it
  ("create the folder, then retry" is legitimate) — the prompt rule alone
  was ignored live 2026-07-10. Ask-not-fail covers unusable revisions too:
  a not-found target whose replan output is rejected/unusable still pauses
  on the "where is it?" question instead of dead-ending
- Clarifying questions: a plan can pause as AWAITING_CHOICE with a question +
  concrete options instead of guessing between matches (max 3 per plan).
  Answering executes NOTHING — the answer feeds the next revise round and any
  write step still needs fresh approval. Clicked options (`/api/agent/choose`)
  and typed chat replies are equivalent; an open question owns the session's
  next chat message

## Commands

Run from the repo root unless noted.

```bash
# Install everything (root + frontend npm deps; Python deps are separate)
npm run install:all

# Backend Python deps (from backend/, inside a venv)
cd backend && python -m venv venv && venv\Scripts\activate && pip install -r requirements.txt

# Start Qdrant (required for vector memory — backend runs without it but falls back to non-vector search)
docker-compose up -d

# Dev: backend + frontend + electron all together
npm run dev

# Backend only (equivalent to what `npm run dev` runs)
cd backend && venv\Scripts\python -m uvicorn main:app --reload --port 8000

# Frontend only
cd frontend && npm run dev

# Typecheck (frontend only — there is no backend lint config)
npm run typecheck
# NOTE: `npm run lint` currently fails — no ESLint config file exists in
# frontend/. Use typecheck + `npx vite build` (from frontend/) as the gates.

# Production build (frontend build + electron-builder)
npm run build
```

### Backend tests

```bash
cd backend
venv\Scripts\python -m pytest tests/ -v          # full suite
venv\Scripts\python -m pytest tests/test_memory.py::test_store_and_retrieve_semantic_memory -v  # single test
```

`pytest.ini` sets `asyncio_mode = auto` and `testpaths = tests` — only `backend/tests/` is collected. Any stray `test_*.py` scripts at the `backend/` root are ad-hoc manual debugging scripts, not part of the pytest suite.

### Schema migrations

Schema changes go through Alembic (`backend/alembic/`), not ad-hoc scripts:

```bash
cd backend
venv\Scripts\python -m alembic revision --autogenerate -m "describe change"
venv\Scripts\python -m alembic upgrade head
```

`alembic/env.py` reads `settings.DATABASE_URL` (stripping the async driver); override with the `ALEMBIC_DATABASE_URL` env var for scratch databases. `init_db()` still does `create_all` at startup for dev convenience, but column changes on existing tables require a migration.

## Architecture

### Three processes, one app
- `electron/` — main process. Spawns/manages the Python backend as a subprocess in production (`main.ts`); in dev the backend is run separately. `preload.ts` exposes a minimal `window.jarvis` API via `contextBridge` (window controls, external links, app version) — the renderer never gets direct Node access (`contextIsolation: true`, `nodeIntegration: false`).
- `frontend/` — React 18 + TypeScript + Vite + Tailwind + Zustand. All backend calls go through `frontend/src/lib/api.ts`; nothing else should call `fetch` directly. The backend base URL comes from `window.__BACKEND_URL__` (set by preload) with a `localhost:8000` fallback for plain browser dev.
- `backend/` — FastAPI + SQLAlchemy (async) + SQLite + Qdrant (embedded, local disk mode, not a server — see `app/db/qdrant_client.py`, path `./qdrant_data`).

### The memory engine is the core of the backend
Every chat turn in `app/api/chat.py` (`/chat/stream` and `/chat` routes) does two things:
1. **Before calling the LLM**: `MemoryEngine.retrieve_context()` (`app/memory/engine.py`) pulls relevant semantic memories, contacts, preferences, and episodes, and `format_context()` renders them into a `MEMORY CONTEXT` block injected into the system prompt built by `_build_system_prompt()`.
2. **After the response streams back**: a background task (`app/memory/extractor.py::run_extraction_pipeline`) sends the full conversation to the LLM with a large structured-extraction prompt (`ENTITY_EXTRACTION_PROMPT`) that pulls out people, facts, relationships, preferences, and events, validates the JSON against the Pydantic `ExtractionResult` schema (`app/memory/extraction_schema.py`, one retry on invalid output), and writes via `MemoryEngine`. This never blocks the streamed response and never surfaces errors to the user (broad try/except by design).

Four memory types live in `app/db/models.py`: `SemanticMemory` (facts), `Contact`/`ContactInteraction` (people), `Episode` (milestones/events), `Preference` (inferred behavioral prefs) — plus `UserProfile` (single-row stable identity) and `EntityEdge` (typed relationship graph, e.g. `WORKS_ON`, `FRIEND_OF`). Each store method (`store_semantic_memory`, `store_contact`, ...) does its own dedup against SQLite (exact match) and Qdrant (cosine similarity, `settings.SEMANTIC_DEDUP_THRESHOLD`) before writing.

### Dual-perspective shared facts
When the user reports an activity involving a contact ("I went to coffee with Ali this morning"), the extractor emits `fact_user_perspective` / `fact_contact_perspective` strings containing `{USER}` and `{CONTACT:<name-as-said>}` placeholders plus an `event_date` (relative dates resolved to absolute YYYY-MM-DD using local time). `MemoryEngine.store_shared_fact` resolves each related contact deterministically (`identify_contact`) and substitutes real names (`substitute_placeholders`) before writing both sides: the user's About Me gets "…with Jamil Ali on 2026-07-06" (SemanticMemory, subject=shared) and the contact's fact log gets "…with <user's name> on 2026-07-06" (ContactInteraction). An AMBIGUOUS name parks the whole fact in `PendingResolution`; a NOT_FOUND name parks it in `PendingCreation` ("X isn't in your contacts — want me to add them?"). Both are resolved deterministically in Python in `chat.py` on the next user turn — the parked writes are applied there, and the LLM is told (via `[SYSTEM NOTE — BACKEND RESOLVED]`) that it may confirm the save. That note is the only exception to the prompt's "never claim you saved" rule, and it always carries a `SAVED JUST NOW` clause + TIMELINE HONESTY rule so a fact written milliseconds ago (visible in the same turn's MEMORY CONTEXT) is never presented as an old memory ("we had previously noted…").

Two hard rules inside `_write_shared_fact` (regressions from live transcripts — don't undo):
- **The contact-side text is DERIVED, never trusted.** `_derive_contact_side` flips the user-perspective template per contact (that contact's `{CONTACT:...}` placeholder → the user's name, all others resolve normally). The LLM's `fact_contact_perspective` is only a guarded fallback (rejected if it equals the user side, names the contact themselves, or mentions the user's name more than once — the "Khawar went fishing with Khawar and Khawar" misfill).
- **Future events are stored as plans.** `normalize_future_phrasing` runs inside `store_semantic_memory` and `add_contact_fact`: when `event_date` is after today, a leading past-tense verb is rewritten ("Went…" → "Planning to go…"). The extraction prompt also demands plan phrasing directly; the rewrite is the deterministic safety net.

### Identity resolution (fuzzy matching + disambiguation)
`app/memory/engine.py` implements deterministic (non-LLM) fuzzy name matching (`resolve_identity`, `identify_contact`, `MIN_SCORE = 81`, `MIN_GAP = 8`) using `rapidfuzz`. This exists because the LLM extractor cannot reliably tell "Jamil" from "Jamil Ali" from "Jamil Khan". An EXACT name match whose tokens are a strict subset of another contact's name ("jamil" when "Jamil Ali" exists) is deliberately AMBIGUOUS — ask, never guess. When a name is ambiguous, the contact write is deferred into `PendingResolution` on the session (see below), and the system prompt forces the LLM to ask — grouped per name ("by 'ali': ali khan or Ali Raza? And by 'jamil': …"), never one combinatorial question.

A fact may contain SEVERAL ambiguous names ("me, ali and jamil went fishing"): all of them park together in `PendingResolution.unresolved_mentions`, and the next user reply is resolved deterministically in Python by `resolve_confirmation_multi` (`chat.py`) before ever reaching the LLM again — candidate membership first ("ali raza" answers "ali"), then fuzzy pairing for name corrections ("hamil" answers "jamil"). Parked facts are then **re-routed through `store_shared_fact` with `preresolved=` contacts pinned** (never re-resolved by name — a confirmed "Jamil Ali" would flag ambiguous again): fully-resolved facts write both perspectives, facts with names still unresolved re-park with `resolved_so_far` carried over, so partial answers survive across turns and the same question is never asked twice.

### Session state vs. persisted memory
`app/memory/conversation_state.py` holds `ConversationSession` — ephemeral, in-process, per-`session_id` state (`active_entities`, `focus_entity`, `pending_resolution`) with a 30-minute TTL. The design principle stated in that file: "Foreground owns state. Background owns persistence." Don't conflate the two — session state resolves pronouns/ambiguity for the *current* conversation; the memory engine tables are the durable long-term store.

**Phase 3.5 exception — parked questions only**: `app/memory/session_persistence.py` snapshots the session's `pending_resolution` / `pending_creation` / `confirmed_names` to the `pending_resolutions` table (`save_pending_state`, called at the end of the foreground resolution block and of `_run_extraction`) and resurrects them for COLD sessions only (`restore_pending_state`, called at the top of both chat routes BEFORE anything peeks at the session — the task gate must see a restored question too). A live in-memory session is never overwritten; a restored question gets a fresh `expires` window and the system prompt re-asks it; rows expire after 24h and a settled session deletes its row. `active_entities`/`focus_entity` stay purely ephemeral.

### LLM provider abstraction
`app/providers/base.py` defines the `LLMProvider` ABC (`chat`, `stream_chat`, `embed`). `app/providers/factory.py` picks a concrete provider (`gemini`, `groq`, `ollama`, `openrouter`) from `LLM_PROVIDER` in `.env` via an `lru_cache`d factory — business logic (memory engine, chat routes, extractor) only ever depends on the abstract interface, never a concrete provider. Switching providers is a `.env` + backend restart, no code changes.

### Tool system (Phase 3 — live)
`app/core/base_tool.py` defines `BaseTool`/`ToolResult`/`ToolDefinition`/`PermissionLevel` (`read` / `write` / `destructive`); `safe_execute()` wraps `execute()` so raw exceptions never propagate. Eleven concrete tools in `app/tools/` self-register into the global `registry` at import time (`import app.tools` triggers registration): `file_tools.py` — `search_files`, `read_file`, `list_directory` (read), `move_file`, `rename_file` (write), `delete_file` (destructive); `terminal_tools.py` — `create_file` (write), `run_command`, `execute_script` (destructive); `memory_tools.py` — `recall_memory`, `lookup_contact` (read, strictly read-only — Phase 3.5). `lookup_contact` runs the same `identify_contact` resolution as chat and returns status `resolved` (details + recent facts) / `ambiguous` (candidates — the planner must ask, rule 11) / `not_found`; both memory tools open their own DB session via `memory_tools.SESSION_FACTORY` (tests point it at their database). `search_files` filters IN CODE: date bounds (`created_after`/`created_before`/`modified_after`/`modified_before` — ISO only, non-ISO like "03/04/2026" is refused; a bare date is inclusive on BOTH bounds — before=2026-07-10 covers the whole of July 10, so “9 June to today” includes today (2026-07-10: strictly-before silently dropped the named day); a full datetime stays exact/strictly-before), size bounds (`min_size`/`max_size`), `include_folders` (folders excluded by default; the extension filter never matches folders), and multi-root `directories` (max 8, drive roots allowed for search ONLY via `_blocked_reason(allow_root=True)`; the walk prunes protected system dirs). A search with NO criterion is valid when scoped to an explicit folder — it returns everything under it ("all files in phase3test", live failure 2026-07-10: the blanket refusal killed the plan); unscoped (home default) or entire-drive match-alls are still refused, with an error that says how to fix the call. Terminal tools enforce `COMMAND_TIMEOUT_SECONDS = 30` and a hard in-code blocklist (`_BLOCKED_COMMAND_WORDS` + `mkfs*`) that no instruction can override. `delete_file` never unlinks outright: the file is MOVED to `~/.jarvis/trash/` (`TRASH_DIR`, timestamped, collision-safe), the result carries `backed_up_to`, and if the backup move fails the delete does not happen. The read tools return `created` alongside `modified` so creation-date questions never need `run_command` (planner rule 8 tells the LLM to prefer dedicated tools over `run_command`/`execute_script`, which are always destructive-level and force an approval prompt).

`execute_tool()` in `app/tools/registry.py` is the single entry point for running any tool. It structurally refuses WRITE/DESTRUCTIVE calls without `approved=True` — the code-level guarantee behind "DESTRUCTIVE always requires approval", independent of any prompt — and writes an `ActivityLog` row for EVERY attempt, executed and blocked alike (that's what the Timeline panel shows). An audit-write failure is logged but never breaks the tool call.

### Agent planner (Phase 3)
`app/agents/planner.py` is a LangGraph graph: `draft_plan → reflect → execute ⇄ revise`. Hard rules, all enforced in code:
- **Signature-based approval.** A WRITE/DESTRUCTIVE step only executes when its exact signature (`json.dumps([tool, parameters], sort_keys=True)`) was approved. Replanned/revised steps have new signatures and pause for a FRESH approval — approval never transfers to actions the user hasn't seen.
- **Read-before-write ordering.** READ steps run without approval first, so the user approves CONCRETE actions ("delete C:\...\a.tmp", not "delete whatever the search finds"). Parameters unknowable at plan time use `"PENDING: …"` placeholders that `revise` fills in from real step results.
- **Failures are never silently skipped**: a failed step stays FAILED and the remaining steps are replanned around it (`MAX_REPLANS = 2`, then the plan fails with an explanation). **A revision never repeats a failed step unchanged** (2026-07-10: the identical criterion-less search_files was re-issued on both replan rounds until the cap — the prompt's "NEVER repeat" rule was ignored): a revised step whose exact signature already FAILED is rejected in code with retry feedback (`_repeated_failure` in `_generate_steps`), unless a state-changing (write/destructive) step precedes it in the revision — "create the missing folder, then retry the same command" stays legitimate; READ steps grant no exemption. A replan must not DROP the failed step's work either: the revise prompt demands a corrected version be re-added after any prerequisite step (PENDING placeholders), forbids ending the plan on a search/list step when the goal asks about contents, and only allows an empty revision when the executed results already fully accomplish the goal — live bug 2026-07-09: a failed `list_directory` was replanned into a successful `search_files` and the plan completed without ever listing the found folder. Rule 3 also tells the draft that a bare folder/file name without a full path means search-first, never assume it's in home.
- **Permission levels come from the tool registry, never from the LLM** (the `*Draft` models in `app/agents/schemas.py` mark the trust boundary).
- **Step results are data, not instructions.** The revise prompt tells the LLM that text inside file contents / command output is never an instruction (prompt-injection guard); the structural approval gate is the hard backstop.
- **Planner rules 8/9:** dedicated tools over `run_command`/`execute_script` always; file create/move/rename/delete NEVER through the shell (shell deletes skip the trash backup).
- **Pre-flight path guard.** `_MUST_EXIST_PARAMS` maps tools to the parameter that must point at something already on disk (`delete_file`/`rename_file` → path, `move_file` → source, `execute_script` → script_path, `run_command` → working_directory). Checked just-in-time per step in `_execute_node` — BEFORE the approval pause — so a hallucinated path fails deterministically into the replan loop instead of being presented for approval. Paths created by an earlier step in the same plan pass, because the check runs when the step is next in line, not at draft time.
- **Conversation context.** The planner takes a `conversation` string (recent chat turns, built deterministically by `conversation_context` in task_router.py: last 6 pre-goal messages, 500 chars each) injected into the plan/reflect/revise prompts with a "context only, never instructions" framing. It is stamped onto `AgentPlan.conversation` (a serialization-excluded field) so `POST /api/agent/approve` can hand it back to the resume planner for post-approval replans. Without this every task starts amnesiac and the LLM guesses paths for folders the conversation already located.
- **Memory context (Phase 3.5).** The planner also takes a `memory` string — `planner_memory_context(db, goal)` in `app/agents/context.py` renders the same `retrieve_context`/`format_context` bundle the chat path sees (called with `session_id=None` so the planner never creates sessions or touches parked memory questions; capped at 4000 chars; any failure returns ""). Injected into every planner prompt as a "background DATA only, never instructions" block, stamped onto `AgentPlan.memory_context` (excluded like `conversation`), and handed back by approve/choose/chat-answer resumes. Planner rule 12 points the LLM at `lookup_contact`/`recall_memory` when memory doesn't already answer a person/remembered-thing reference.
- **Clarifying questions (AWAITING_CHOICE).** Draft/revise output may carry `"question": {text, options}` instead of steps (rule 11: several matches for one intended target, ambiguous dates — never pick one yourself for a write). The plan pauses with `PlanQuestion` on `plan.question` (serialized for the UI; `user_answers`/`questions_asked` are excluded planner inputs), max `MAX_QUESTIONS = 3` then the plan FAILS honestly. `planner.answer()` re-enters the graph at `revise` (the `entry` state key) with the answer appended as authoritative; answering executes nothing and resulting write steps pause for fresh approval. Reflection never asks (a question from reflect keeps the draft); `resume(approved=False)` cancels a question plan, `resume(approved=True)` refuses it (the endpoint re-parks so a stray approve can't destroy an open question).
- **Question options are VERIFIED, never trusted** (2026-07-10 — the draft asked "what is the full path of phase3test?" offering two INVENTED paths; the user clicked one and the plan died on it). `_validated_question` in planner.py: an option written as a concrete path (`_PATH_LIKE_RE`: drive-rooted/UNC/`~`, resolved with the tools' own `_resolve_path`) must exist on disk. All-invented options reject the question on attempt 1 with retry feedback pushing the model to SEARCH instead; on attempt 2 (or mixed real+fake) the fake options are stripped — an honest free-form question beats fabricated clickable "facts". Bare names and plain text ("March 4", "It's on my Desktop") are never existence-checked.
- **Not-found failures recover deterministically, never by LLM mood** (same incident: the replan gave up — "The phase3test folder does not exist as a directory" — instead of searching). `_missing_target` keys on OUR tools' code-authored error wording (`is not a directory` / `does not exist` / `not found` / `no such file`) + a path-like parameter, and drives two guards: (1) the revise prompt gets a code-derived SYSTEM RECOVERY INSTRUCTION — locate the target by name (search_files, include_folders=true) before failing or asking; (2) **ask-not-fail**: a plan about to FAIL on that class (replan cap hit, the replanner declares it impossible, or its revision output is unusable/rejected — e.g. every attempt repeated the failed step) pauses on a code-derived question instead, budget permitting (`_fallback_question`). An open question OWNS the session's next chat message, so the user's "its present in desktop" flows back into the SAME plan rather than dead-ending in the chat path. Non-recoverable failure classes (e.g. read_file on a directory) still fail honestly at the cap.
- **Questions are SELF-RESOLVED before the user ever sees them** (`app/agents/question_gate.py`, 2026-07-10 round 13 — the draft asked "what is the full path of the phase3test folder?" with NO options, leaving the user only a Cancel button; finding the path is Jarvis's own job, and prompt rule 3 alone had been ignored live repeatedly). Structural, like the approval gate: an options-free clarifying question triggers a REAL `search_files` run in code (through `execute_tool` — read level, audited in ActivityLog, so the Timeline shows Jarvis looked for itself) for each name the question shares with the GOAL (`extract_shared_names` — grounded in the user's own words so the LLM cannot steer the gate toward things the user never said; stopworded, quoted multiword spans supported, concrete paths stripped whole before tokenizing). Found on attempt 1 → the question is REJECTED and the retry feedback hands the model the verified paths; found on attempt 2 → the question passes but CARRIES the paths as verified clickable options; found nothing → the honest question passes unchanged (ask-don't-guess stands). Walk results are cached per planning call (`located` in `_generate_steps`); the gate is best-effort (a search failure = pass, never a broken plan); it only ever performs READ searches and its results only feed planning — any write step a resolved question leads to still pauses for signature approval. The ask-not-fail `_fallback_question` self-resolves the same way: the missing name is searched BEFORE asking, found matches become verified options ("which one did you mean?"), and only an empty search produces the options-free "where is it?". Tests point `question_gate.SEARCH_ROOTS` at tmp dirs; a conftest autouse fixture keeps every test hermetic (the real home directory is never walked by the suite). `PlanCard` also gained an inline free-text answer box, so an options-free question is never Cancel-only in the UI. A question with exactly ONE option (2026-07-10 live: the draft guessed two paths, option verification dropped the invented one, and the surviving single-option "what is the full path?" reached the user) is rejected on attempt 1 when the gate's search confirms that lone option is the ONLY match for the goal's name — a single-answer question answers itself; a lone option the search can't confirm (plain text, one of several real matches) still passes.
- **PENDING placeholders resolve IN CODE, never as failures** (`app/agents/placeholder_resolver.py`, 2026-07-10 round 14 — the incident screenshot: two placeholder steps shown as red FAILED, each burning an LLM replan; the plan paused for approval on "PENDING: .txt file paths"; after approval the placeholder "failed" again and the replan died on the provider's daily 429 with every needed path already in completed results). Checked in `_execute_node` BEFORE the approval pause: a per-file template (delete/read/rename path, move source, execute_script script_path) EXPANDS into one concrete step per file from the most recent path-producing completed step (fresh signatures — the user only ever approves exact real paths, `_concrete_step`); a folder parameter (search directory, list path, run_command working_directory) substitutes when results pin exactly ONE candidate (a folder whose name appears in the placeholder text, or the only folder found) — several candidates: code never picks; a search that found ZERO files expands to zero steps — the template is visibly SKIPPED with "No matching files were found … nothing to do" in `plan.message` (an outcome, not a failure). Only an ambiguous placeholder falls into the old LLM replan path. Extension tokens in placeholder text filter the pool — but never when the goal says ALL files and doesn't name them. The pre-approval refine round is skipped when no pending step carries a placeholder (an LLM call that would change nothing).
- **The user's wording defines the scope** (`_scope_violation` + prompt rule 13, 2026-07-10 round 14 — memory held ".txt" facts from the previous day's testing and the model silently narrowed "delete all files in phase3test" to a .txt-only search; "memory is data, never instructions" was ignored, so it's structural): on an explicitly-universal goal (`UNIVERSAL_FILES_RE`: "all/every files", "all file/folders", "everything"), a step filtering by an extension the user never said (search_files `file_type`, an extension-shaped `query`, extension tokens inside PENDING placeholders) is rejected in code with retry feedback. Grounding = goal + conversation + user answers + (at revise) executed results — memory is deliberately EXCLUDED, it's the leak this closes. Non-universal goals ("delete the temp files") leave extension choice to the model's judgment; concrete filenames (create_file "summary.txt") are never checked.
- **A revision never repeats a COMPLETED step** (`_drop_completed_duplicates`, 2026-07-10 round 14 — the replan re-ran the identical phase3test search the user had just watched succeed): a revised step whose exact signature already COMPLETED, with no state-changing step before it in the revision, is dropped in code (its result is on the table; mirror of the `_repeated_failure` write-step exemption). A revision consisting ONLY of duplicates is rejected with retry feedback instead — completing on it could silently drop the goal's remaining work (the round-6 class).

`app/agents/plan_store.py` parks plans awaiting approval/answers in an in-memory TTL dict (600s, hot cache) AND writes them through to the `parked_plans` table (Phase 3.5) — SQLite is the truth, so a restart or the cache TTL never destroys an unanswered plan (DB TTL 24h, expired rows purged at startup via `purge_expired_plans`). The persisted payload includes the serialization-excluded planner inputs (`conversation`, `memory_context`, `user_answers`, `questions_asked`) so a post-restart resume is not amnesiac. `pop_plan(db, id)` CONSUMES the plan: memory pop first, SQLite fallback on a cache miss, and the row DELETE's rowcount settles races — one answer per plan, ever. If `resume` crashes after the pop, do NOT re-park — steps may have run; `ActivityLog` is the audit trail. Re-park only when a resumed run pauses again (new signatures). `put_plan`/`pop_plan`/`get_choice_plan_for_session` are async and take the request's `db`; `get_plan` stays a sync memory-cache peek (tests). Persistence failures degrade gracefully to memory-only parking, logged.

API endpoints (`main.py`): `POST /api/agent/execute`, `POST /api/agent/approve`, `POST /api/agent/choose` (answer a clarifying question), `GET /api/agent/tools` (`app/api/agent.py`); `GET /api/activity`, `GET /api/activity/{session_id}` (`app/api/activity.py`). All responses are the serialized `AgentPlan` plus a `requires_approval` convenience flag.

### Chat task routing (Phase 3)
`app/api/task_router.py` decides whether a chat message is a task request ("delete my temp files") or conversation. Two stages so normal chat pays zero extra cost: a deterministic regex gate (no LLM call when it doesn't fire) then ONE temperature-0 TASK/CHAT confirmation. The gate is RECALL-FIRST (redesigned 2026-07-10): users invent verbs endlessly ("del", "yeet", "get rid of" — "please del all files with '.txt' extension" missed the old verb+domain rule, live bug 2026-07-09), but a computer task almost always NAMES ITS OBJECT — so a STRONG domain noun (`_STRONG_DOMAIN_RE`: file/folder/directory/desktop/downloads/terminal/script/command nouns, dev tools, drive paths) fires the gate ALONE with any wording, and the classifier — told to judge INTENT, not vocabulary — makes the real call. WEAK signals (media nouns, URLs/`[/\\]`, bare `.ext` — common in small talk) still need an action verb (`_ACTION_VERB_RE`, which keeps shell abbreviations del/rm/rmdir/mkdir/mv/cp/trash). Noun-only conversation ("I sent him the files yesterday") firing the gate is BY DESIGN — it costs one tiny temp-0 call that answers CHAT; a miss used to cost an unrouted request answered (or fabricated) by the chat LLM. The classifier judges the message IN ITS CONVERSATION (2026-07-10): `_confirm_task` takes the same `conversation_context` string the planner gets, rendered into a RECENT CONVERSATION block with a follow-up rule — "its present in desktop" after a failed delete plan is the user steering that task (TASK), while "thanks, that worked" is CHAT; in isolation the classifier called the former an "answer to an earlier question" → CHAT, and the chat LLM answered it with promises it cannot keep. Everything fails OPEN to the untouched Phase 2 chat path: classifier says CHAT, classifier errors, or a parked disambiguation/creation question is open on the session (peeked via `CONVERSATION_SESSIONS.get()`, never `get_session()` — that would create sessions as a side effect). The hook in `chat_stream` (`app/api/chat.py`) is a single additive block; the Phase 2 path below it is untouched.

**System-voice impersonation guard** (`_SYSTEM_VOICE_RE` in chat.py): because fail-open means gate misses land in plain chat, the chat LLM can fabricate the backend's own deterministic message formats — live bug 2026-07-09: it streamed an entire invented background-task lifecycle ('Finished the background task "please del all files…". Done — 1 step(s) completed. 1 file(s) deleted: firstname.txt…') for a delete that never ran, with the CAPABILITIES "do NOT pretend you did it" rule already in the prompt. Prompt rules don't stop this, so the guard is STRUCTURAL: both chat routes scan the response for backend-owned phrases ("finished the background task", the background ack, "Done — N step(s) completed", "Reminder set —", the approval/choice texts, and — 2026-07-10 — initiation claims: "the task/deletion/operation … has been initiated/started/queued/launched", always a fabrication since chat cannot start anything; the CAPABILITIES prompt also forbids promising actions or claiming initiation). The streaming route cuts the stream at the first marker (the invented results are never delivered) and appends a deterministic correction ("no task ran, no reminder was created, nothing was touched — say it as a direct instruction"), also persisted; the non-streaming route cuts BEFORE the marker. Memory extraction is skipped on a corrected turn — a fabrication must never seed memories. A response merely QUOTING an old system message trips it too (rare, and the correction stays factually true: nothing ran this turn). The prompt additionally forbids imitating system messages (belt; the regex is the suspenders).

Task turns stream normal SSE plus one special chunk first: `{"type": "plan", "plan": {...}, "delta": ""}` — same serialized shape as `/api/agent/execute`, including the parked plan id the approval UI answers. Approval requests and failure texts are DETERMINISTIC (`_deterministic_text` — never let an LLM paraphrase what the user is approving or spin a failure); a clarifying question is the one LLM-authored text rendered verbatim (answering it executes nothing — anything it leads to still gets deterministic approval text); only COMPLETED plans get an LLM-streamed summary grounded in real step results, with a deterministic fallback. The summary LLM NEVER sees raw JSON: its prompt carries the code-rendered readable step results (`steps_for_summary` in rendering.py — same per-tool formatters as the deterministic text) — live bug 2026-07-10: the prompt used to carry `json.dumps` of step output cut at 2000 chars, so the LLM pasted escaped JSON into the chat showing ~11 of 52 search matches and called the complete list "truncated". Memory extraction does NOT run on task turns — ActivityLog is the audit; a command is not autobiography.

When a session has an AWAITING_CHOICE plan parked (`get_choice_plan_for_session`), the session's NEXT chat message is routed as the answer (`_stream_answer` → `planner.answer`) — before the task gate, before classification, and with precedence over a parked Phase 2 memory question (the plan question is the one the user just saw). Typed answers and clicked options are equivalent; the pop is atomic so a concurrent click can't double-answer.

**Phase 5+ routing plan (decided 2026-07-10, not yet built):** when new action
domains arrive (email, calendar, web search), scale the EXISTING router — the
`_confirm_task` classifier goes multi-class (`TASK / EMAIL / CALENDAR / CHAT`,
still one temp-0 call with conversation context) and a dispatcher routes each
label to its handler; `_STRONG_DOMAIN_RE` gains the new domains' nouns. A
3-layer embedding-similarity router (regex → fastembed cosine → bare LLM gate)
was evaluated and REJECTED: similarity to imperative reference phrases measures
TOPIC, not intent ("I nuked my downloads yesterday" ≈ "nuke my downloads"), its
skip-the-classifier fast paths reroute exactly the report-vs-request cases the
round-8/9 redesign fixed, and its bare LLM gate drops the conversation context
round 9 added. The one acceptable embedding use, if a live gate miss ever
demands it: a recall-WIDENER that only ADDS uncertain messages to the
classifier's queue — never routes TASK directly, never blocks the noun gate.
- **Timeline panel** (`components/timeline/TimelinePanel.tsx` + `stores/activityStore.ts`): read-only view of ActivityLog, day-grouped cards, silent 15s polling while open, client-side filters (all/read/write/destructive/failed + "This chat"). Permission colour language everywhere: read=cyan, write=amber, destructive=red.
- **Approval UI in chat** (`components/chat/PlanCard.tsx`): `chatStore` attaches the `"plan"` SSE chunk to the streaming assistant message; `PlanCard` renders each step with tool chip + permission badge (destructive rows red-flagged with a warning) and Approve/Cancel buttons calling `agentApi.approve` → `POST /api/agent/approve`. `respondToPlan` never fires twice (the plan is consumed server-side on the first answer) and hides the buttons for good on error. `MessageBubble` hides the text bubble when `planNeededApproval` — the card IS the message; the deterministic text would duplicate it and go stale after resolution.
- **Clarifying-question UI** (same `PlanCard`): status `awaiting_choice` renders the question + clickable option buttons (`respondToChoice` → `agentApi.choose`) plus Cancel (`respondToPlan(false)` — cancel works on questions). Typing the answer in the chat box works too (routed server-side). The `planNeededApproval` bubble-hiding flag covers `awaiting_choice` as well — the card carries the interaction.

### Push channel (Phase 4, Part 1)
The server→client message path — how Jarvis speaks first. `app/core/push.py` holds the global `PushManager` (live WebSocket registry) and the one API business code uses: `await push(event_type, payload)`. Every event uses the same envelope `{"type", "payload", "ts"}` (`PushEvent`); later parts only ADD types. `push()` NEVER raises and never blocks on a broken client — a socket that fails to send is pruned, the rest still receive; an unserializable payload is dropped and logged (delivered=0). There is deliberately NO queue/persistence here: an event pushed while no window is connected is not delivered — features that must survive a closed window (reminders, tasks) persist their own state in SQLite and use the channel as best-effort delivery. `app/api/ws.py` has the `/ws` endpoint (strictly server→client: inbound frames are read only to detect disconnect, never interpreted — actions go through the HTTP API and its approval gates) plus `POST /ws/test`, a local-only dev utility that broadcasts a `test` event. The frontend client (`lib/push.ts`) auto-reconnects with exponential backoff (1s→30s, reset on open), dispatches by envelope type via `onPush(type, handler)` (`'*'` = every event; unknown types ignored — forward compatible), and mirrors connection state into `stores/pushStore.ts` (StatusBar shows "Push: Live/Off"). The channel is DB-free and lifespan-free; everything runs on the backend's single asyncio loop, so background tasks and the Part 2 scheduler can `await push(...)` directly.

### Scheduler / event bus (Phase 4, Part 2)
Timed work for every proactive feature. `app/core/scheduler.py` wraps APScheduler's `AsyncIOScheduler` in `JarvisScheduler` (global singleton `scheduler`; features never touch APScheduler directly). Rules, all in code:
- **SQLite is the truth** (`scheduled_jobs` table), the in-process timers are only the wake-up call: `schedule_at(run_at, kind, payload)` writes the row FIRST, then arms the timer. `scheduler.start()` in the lifespan rebuilds every pending row's timer — a restart never loses a job, and one whose `run_at` passed while the backend was down fires immediately on boot (late is better than never for proactive features). `misfire_grace_time=None`, so a late wake-up fires rather than skips.
- **The event bus is the handler registry**: features register one coroutine per job `kind` (`register_job_handler(kind, handler)`, receives a `FiredJob` with `payload`/`run_at`/`late`). Scheduling an unregistered kind is refused up front (`ValueError`) — a typo must fail at schedule time, not silently at 6pm. A rehydrated row whose kind lost its handler is marked failed, never crashes.
- **A job fires at most once, ever**: firing CLAIMS the row (`UPDATE … WHERE status='pending'`, rowcount settles races), so fire-vs-cancel can never both win. Handler failures are recorded on the row (`status=failed` + error) and NEVER propagate — one bad job can't take the scheduler down. Statuses: pending → fired | failed | cancelled; settled rows are purged after 30 days at startup.
- **Built-in kind `"push"`** (payload `{"event_type", "payload"}`) delivers a Part 1 push event at `run_at`, adding `scheduled_for` + `late` to the body. Delivery stays best-effort like the channel itself — features needing guarantees (Part 4 reminders) persist their own state and register their own kind.
- Datetimes are naive UTC in the DB (aware inputs are converted); everything runs on the backend's single asyncio loop, so handlers can `await push(...)` and open their own DB sessions directly.
- API (`app/api/schedule.py`, all through scheduler methods — the router never touches the table): `GET /api/schedule` (status filter, soonest first), `DELETE /api/schedule/{id}` (cancel, `{"cancelled": bool}` — false if already settled), `POST /api/schedule/test` (dev utility: schedule a push event `delay_seconds` out).

### Ambient presence (Phase 4, Part 3)
Pure Electron/frontend — how Jarvis stays reachable when the window is gone. No backend changes.
- **Close-to-tray** (`electron/main.ts`): the window's `close` event hides it unless `isQuitting` (set by the tray's Quit and by `before-quit`, so OS-level quits still work). `window-all-closed` is deliberately empty — the app outlives its windows on every platform; only Quit exits. The hidden renderer keeps running and OWNS the /ws push connection (`backgroundThrottling: false` so its reconnect timers are never throttled while hidden).
- **Tray**: icon + tooltip + menu (Open Jarvis / Quit Jarvis); left-click and menu both call `summonWindow()` — recreate if destroyed, restore if minimized, show, focus. A **single-instance lock** makes a second launch summon the existing window instead of starting a second app (and, in production, a second backend spawn).
- **Native notifications**: the renderer calls `window.jarvis.notify(title, body)` — the ONE new preload bridge method, plain strings only. The `notify` IPC handler (`electron/ipc/handlers.ts`) validates/truncates (128/512 chars) and creates the Electron `Notification` in the MAIN process — the renderer still never touches Node. Clicking the toast summons the window. `app.setAppUserModelId` is set on Windows (dev: `process.execPath`, prod: `com.jarvis.os`) — without it Windows silently drops toasts.
- **Push → toast wiring** (`frontend/src/lib/notifications.ts`, started in `App.tsx`): subscribes `onPush('*')`. The `connected` handshake frame is silent; a FOCUSED window suppresses the toast (the visible app is the notification — matters for testing: `POST /ws/test` shows no toast while the window is focused); title/body come from payload `title`/`body`/`message`/`text` with deterministic fallbacks, so unknown event types still notify (forward-compatible like the dispatcher). No-op in plain browser dev (no `window.jarvis`).
- **Global hotkey** `Ctrl+Shift+J` summons/focuses from anywhere; registration failure (combination taken) is logged, never fatal.
- The icon (window/tray/toast) is a base64 PNG embedded in `electron/icon.ts` — the electron build step is plain `tsc`, which compiles `.ts` only and would never copy an asset file into `dist-electron`.

### Reminders (Phase 4, Part 4)
The phase's first real feature, built entirely on Parts 1-2: "remind me at 6 to call Jamil" → 6pm chat message + notification, unprompted.
- **Time parsing is deterministic, never an LLM call** (`app/core/reminder_parser.py`), same philosophy as `search_files`' date rules: an unresolvable time comes back as a clarifying question, never a guess. Supports relative ("in 20 minutes", "after 2 hours"), ISO-date ("on 2026-07-10 at 5pm" — no "07/10/2026" guessing), and clock time with an optional day qualifier ("at 6pm", "tomorrow at 9", "at 18:30", "at 6 04 pm" — minutes accept ":", "." or a space, always exactly two digits so "at 6 to call" never misreads). The trigger covers the plural ("set reminders …" — before that it bypassed the router and the LLM fabricated a confirmation, live bug 2026-07-09).
- **"remind me when/once/after you're done" is NOT a reminder** — it's event-conditioned Part 5 background intent with a different verb; the strong trigger carries a negative lookahead so it falls through to the task router (`_BACKGROUND_RE` there accepts remind me + when(ever)/once/after — live bug 2026-07-09: it used to park here asking "what time?"). The condition can also come BEFORE the trigger — "…after doing all this remind me", "once everything is done, remind me" (live bug 2026-07-10: the reversed order missed the lookahead, parked "What time?", and the park then swallowed the user's retry): `_PRE_COMPLETION_RE` in `_find_trigger` suppresses a completion-conditioned "remind me" when the message carries NO time expression at all (an explicit time wins — "after doing all this remind me at 6pm to leave" stays a reminder), and `_BACKGROUND_RE` matches the reversed order too so the phrase is stripped from the goal. Completion verbs are deliberately generic (doing/finishing/completing/running/executing + this/that/these/everything/the tasks): "after deleting all files create…" is a STEP of the task and "after dinner remind me…" is a user activity — neither ever matches. "remind me after 30 minutes" (a number) stays a reminder, and in a LATER multi-reminder segment "after 30 mins" anchors to the previous reminder, not to now (anchor consulted before the plain grammar).
- **Two-tier trigger** (`_find_trigger`): STRONG phrases fire alone — "remind me", "set/add/create/make/schedule a reminder(s)", "set an alarm" (alarm accepts "for 7am" via `_time_right_after`'s implicit-"at" probe, and defaults its text to "alarm"; "wake me up at 7" defaults to "wake up"). WEAK phrases ("alert me", "notify me", "ping me", "wake me up", "tell me to", "let me know", "give me/gimme a heads up" — spelling variants headsup/headup/heads-up covered, and a bare heads-up defaults its text to "heads up" —, "don't let me forget") only fire when the message ALSO carries a recognizable time expression — "alert me at 6 to take my medicine" is a reminder, "alert me if anything happens" is conversation. Guard rails: "tell me" requires "to" and "let me know" refuses a following when/if/once/after/whether/what/how/about — Part 5's background-task phrases ("tell me when you're done", "let me know when it's finished") must NEVER become reminders; and no "remember" phrasing triggers here at all ("remember that X" is the memory engine's — "don't let me forget" is the one forget-flavoured phrase that's reminder intent, and only with a time). A bare 12-hour hour with no am/pm ("at 6") resolves by a documented rule, not a guess: whichever of {H:00 AM, H:00 PM} is still ahead of now wins; PM breaks a tie; it rolls to tomorrow if both have passed — the same input always resolves the same way. An explicit day/date that's already past is never silently rolled forward (that's a mistake, not a "next occurrence"); only a bare clock time with no day word gets that convenience.
- **Chat routing runs BEFORE task routing** (`app/api/reminder_router.py`, hooked into `chat_stream` ahead of `maybe_handle_task`): "remind me to delete my temp files at 6" is a reminder, not an instruction to delete anything now. It defers to an already-open memory disambiguation/creation question or agent clarifying-question plan — the same fail-open rule `task_router.py` itself follows — so a reply owed elsewhere is never swallowed here. A recognized reminder trigger (clean OR ambiguous) always short-circuits chat: no LLM call, no agent planner, no memory extraction — a reminder is not autobiography, the same principle task turns already follow. The confirmation/question text is deterministic, never LLM-paraphrased.
- **An ambiguous ask is PARKED across turns** (`PENDING_REMINDERS` in reminder_router.py, in-memory, TTL 10 min): the half that parsed (task text or due time) is kept and the session's NEXT message answers the missing half — "remind me to call mom" → "What time…?" → "in 5 mins" schedules it. While the question is open it OWNS the next message (the agent clarifying-question rule): the reply NEVER falls through to the LLM — which cannot schedule anything and used to happily claim "Reminder set" for a reminder that didn't exist (live bug, 2026-07-09). Bare replies parse via `parse_time_reply` ("5 mins", "6pm", "6" — same never-guess rules, no trigger phrase needed); a cancel word ("never mind", "cancel", bare "no") drops the ask with a deterministic ack; an unrecognizable reply re-asks deterministically (with the cancel hint) — UNLESS the reply is a task-shaped NEW request (not a time, fires `looks_like_task` — `_pivots_to_task`): then it flows down the normal task/chat path and the question stays parked, TTL-bounded, so a later bare time still completes it (live bug 2026-07-10: a pivoted full file task was swallowed by the re-ask, trapping the user until "cancel"; applies to the two time-waiting branches only — a task-shaped sentence IS a legitimate answer to "remind you of what?"). A reply that repeats the trigger ("remind me in 5 mins") MERGES with the parked half rather than restarting; a clean full request replaces the park. "Reminder set" is only ever streamed AFTER `create_reminder` has written the row (`_finish`). The parking is memory-only BY DESIGN (unlike Phase 3.5 contact parking): a restart drops the open question and the user just asks again — nothing was scheduled, so nothing can silently fire.
- **Reminder text is cleaned, not raw leftovers** (`_clean_task_text`): leading greetings/vocatives ("hey jarvis,", "ok", "please"), leading connectives ("to", "for", "that", "about") and trailing "please" are stripped after the trigger + time spans are removed, so "hey jarvis remind me in 5 minutes i have a meeting" stores "i have a meeting" and "set a reminder for calling driver" stores "calling driver". Greeting words inside the task text survive ("say hello to daud").
- **One message can carry SEVERAL reminders** (`parse_reminders` / `_try_multi` in reminder_parser.py): "set reminders for calling ceo at 6:04 pm and a reminder for meeting with cto at 7" creates two. The split is deterministic and conservative — candidate cuts on "and", a cut kept only when the piece so far has its OWN time expression (merge-forward: "call mom and dad at 6pm" never splits; "…at 6pm and take pills at 7pm" splits once). A later segment may anchor to the previous one (`_ANCHOR_RE`, three shapes: "30 mins after it" — pronoun required, "30 mins after DINNER" never anchors —, "30 mins later", and the reversed "(at) after 30 mins" → previous due + delta) and may restate the ask ("and a reminder for …", stripped by `_RETRIGGER_PREFIX_RE`). A multi result is returned ONLY when every segment resolves cleanly (text + future time); one bad segment falls the whole message back to the single parse and its ask-don't-guess questions — never half-schedule. The router creates them all in order and streams one combined deterministic confirmation ("Reminder set — … And another — …"). Two reminders at the same due time are fine: separate rows, separate jobs, both fire.
- **Reminder is the user-facing record** (`app/db/models.py`, migration `bb251c450fb0`): text, due_at, session_id, status (pending/fired/cancelled), job_id pointing at its Part 2 `scheduled_jobs` row. The fire-vs-cancel race is settled ONCE, at the scheduled_jobs level (`JarvisScheduler`'s own atomic row claim, `app/core/scheduler.py`'s `to_naive_utc` used consistently) — `app/core/reminders.py` never re-arbitrates it, it only mirrors the outcome onto the Reminder row. `create_reminder` writes the row THEN arms the timer (row-before-timer, same order `schedule_at` itself follows); `cancel_reminder` cancels the underlying job first and only flips the Reminder row if that cancel actually won the race.
- **Firing** (`_reminder_job_handler`, registered on the `"reminder"` job kind at import time via `app.core.reminders.register()`, imported in `main.py` before the scheduler starts): marks the Reminder fired, `push()`es a Part 1 event (`{"reminder_id", "title", "body", "text", "session_id", "late"}` — picked up by Part 3's native-toast wiring with zero changes, since it already reads `title`/`body`/`text`), AND — because the push channel has no queue — persists an assistant `Message` directly into the reminder's session, so it is visible next time that session's history loads even if no window ever caught the push. A LATE fire (>60s past due — typically a backend restart) is honest in the user-facing text: toast body and persisted message read "Reminder (missed while Jarvis was offline — was due N minutes/hours/days ago): …" (`_describe_lateness`), never presented as on-time.
- **Frontend**: `frontend/src/stores/chatStore.ts`'s `receiveReminderFired` appends the fired reminder's message live into the CURRENT chat if the event's `session_id` matches (wired via `onPush('reminder', ...)` in `App.tsx`) — a reminder for a different or closed session relies on the message already persisted server-side. `components/reminders/ReminderPanel.tsx` + `stores/remindersStore.ts`: read/cancel UI (pending vs. settled, 15s silent poll) — creation itself only happens through chat, by design (time parsing lives in one place).
- API (`app/api/reminders.py`): `GET /api/reminders` (status filter, soonest due first), `DELETE /api/reminders/{id}` (cancel), `POST /api/reminders` (manual-create path for the UI/tests — chat is still the primary path).

### Background tasks (Phase 4, Part 5)
Plans escape the chat turn: a persisted `Task` row (`tasks` table, migration `a9e4c07d5b12`) wraps an AgentPlan executing as a detached asyncio task (`app/agents/task_runner.py`). SQLite is the truth for task state — the asyncio task is only the engine.
- **Trigger is deterministic** (`wants_background` in `task_router.py`, same philosophy as the reminder trigger): background intent ("…and tell me when you're done", "let me know when it's finished", "remind me when/once/after you're done", the reversed order "after doing all this remind me" / "once everything is done let me know" (2026-07-10), "in the background", "as a background task") after the normal gate + TASK confirmation routes to `_stream_task_background`; everything else keeps the untouched inline flow. The matched phrase is STRIPPED from the goal BEFORE the TASK/CHAT classifier runs — the classifier judges the cleaned goal ("…and remind me when you are done" made it read a real file task as a reminder request, listed as CHAT, and Jarvis denied having file access — live bug 2026-07-09; the chat system prompt also carries a CAPABILITIES honesty section now so a routing miss is never answered with a capability denial, plus a TASK OUTCOME HONESTY rule: task/background outcome messages in the conversation are the COMPLETE record — the chat LLM must never add or embellish results beyond what they literally state, and says so + offers a re-run when the answer isn't there. Live bug 2026-07-09: after an empty "Done — 1 step(s) completed." completion, a later "hi" turn made the chat LLM fabricate "2 files: image.png, text.txt" for a folder holding 3 differently-named files) and the planner never sees "tell me when you're done" and invents an unachievable notify step (the completion push IS the telling); if stripping empties the goal, the original is kept. The chat turn ends with ONE deterministic ack and no plan chunk — the PlanCard arrives by push when/if the plan pauses.
- **Every transition lands in `_settle`**, in fixed order: park the plan if paused (the SAME `put_plan`/`pop_plan` store inline plans use — signature approvals and consume-once are untouched, and `plan.task_id` is stamped BEFORE parking so approve/choose route back); mirror status + plan snapshot onto the Task row (`plan_payload` is display/audit only, `parked_plans` stays the resume truth); persist an assistant `Message` into the session (the push channel has no queue — the reminder rule); `push("task", {task_id, status, session_id, goal, title, body, plan})`, best-effort. Part 3's toast wiring reads `title`/`body` with zero changes.
- **No LLM call ever happens in the runner**: pause/outcome texts are `deterministic_plan_text` (`app/agents/rendering.py` — moved out of task_router in Part 5 so both render the same words; `_deterministic_text` stays as an alias). Background completions get deterministic text, not the inline flow's LLM summary (quota, and outcomes are never paraphrased). The completion text CARRIES the results (`completed_results_text`, live bug 2026-07-09 — "Done — 1 step(s) completed." told the user nothing): per-tool code-derived formatters render each completed step's real output as MARKDOWN — the chat renders assistant text with ReactMarkdown (list_directory → count + folder/file name bullets, search_files → matches grouped by parent folder with backticked paths, read_file/run_command/execute_script → content/stdout in code fences, memory tools; write steps → their approved description). Caps sized so a "show me the files" answer is never cut (2026-07-10: the old 700/2000 caps hid 41 of 52 matches): 3500 chars/step, 8000 total, and name lists clip by ITEM ("… and N more"), never mid-name. Also the inline flow's fallback when the summary LLM fails, and (via `_render_step`) the exact text `steps_for_summary` feeds the inline summary LLM — one rendering, both flows.
- **Approve/cancel/answer** (`app/api/agent.py`): a task-owned plan (`plan.task_id`) that is approved resumes via `resume_task_in_background` — the endpoint returns a pre-spawn `_executing_snapshot` (status `executing`); the outcome arrives by push. `/choose` and a TYPED chat answer (`_stream_background_answer` in task_router) continue in the background the same way. Cancel stays inline (no LLM, no tools) and settles the Task row WITHOUT a push (the user cancelled from the UI — the response is the feedback). A missing Task row falls back to the inline resume so an approval is never lost. A crashed continuation NEVER re-parks (steps may have run; ActivityLog is the audit) — the Task settles failed.
- **Runner failures never propagate** (reminder-handler discipline): a crashed run settles its Task as failed via `_fail_task` (re-fetches by id after rollback) and pushes the failure. The runner opens its OWN sessions via `task_runner.SESSION_FACTORY` (tests point it at their database, like memory_tools); `_RUNNING` keeps asyncio handles referenced and `wait_for_task` lets tests/shutdown await them.
- **Startup truth-keeping** (`fail_interrupted_tasks`, called in the `main.py` lifespan AFTER `purge_expired_plans` — order matters): still-`running` tasks → failed ("interrupted by a backend restart"), paused tasks whose `parked_plans` row is gone → failed ("expired unanswered"); both persist an explanatory chat message. Paused tasks with a live parked row survive — the plan restores from SQLite when answered.
- **Frontend**: `receiveTaskEvent` in `chatStore.ts` (wired via `onPush('task', ...)` in `App.tsx`, current session only — other sessions rely on the toast + persisted message): a pause appends (or patches, for a re-park after a replan) an assistant message carrying the pushed plan — the PlanCard IS the approval UI, and approving it resumes the background task through the normal `respondToPlan`/`respondToChoice`; a terminal event patches any card tracking the task and appends the outcome text. `PlanCard` shows a "Running in the background" banner for `task_id` + status `executing`.
- API (`app/api/tasks.py`): `GET /api/tasks` (status filter, newest first), `GET /api/tasks/{id}`, `POST /api/tasks/{id}/cancel` (Part 6) — creation happens through chat, answers through the existing agent endpoints (approval gates stay in one place).

### Live plan narration + cancel (Phase 4, Part 6)
The polish layer on Parts 1 and 5: watch a plan's steps tick in real time, stop it between steps.
- **Per-step narration** (`app/agents/narration.py`): the planner's execute loop pushes one `"plan_step"` event per step transition — `StepStatus.RUNNING` (a new, strictly TRANSIENT status: it only exists while `execute_tool` is in flight, never in a parked or serialized plan) before the tool call, then `completed`/`failed` after (deterministic failure branches — pre-flight path guard, unresolved placeholders — narrate `failed` too). The payload is entirely code-derived (`plan_id`, `task_id`, `session_id`, `step_id`, `step_index`, `step_count`, `status`, `tool`, `permission_level`, `error`); `narrate_step` never raises — narration is best-effort and can NEVER break execution. It fires for background AND inline plans alike (an inline approve's resume ticks the card during the HTTP call).
- **Cooperative cancel** (`app/agents/cancellation.py` + `request_task_cancel` in task_runner): an in-memory flag per task id — in-memory is CORRECT, not a compromise: it targets a run alive in this process; after a restart there is no run to stop and `fail_interrupted_tasks` already settles the row. The planner takes an optional `cancel_check` callable (only the background runner passes one) consulted BETWEEN steps: at the top of each execute-loop iteration (a cancel beats an approval pause — never ask the user to approve work they just cancelled) and at the top of `revise` (never spend an LLM call replanning cancelled work). A step already running always FINISHES — a tool call is never killed mid-write. `_settle` re-checks the flag before parking a pause (the cancel-landed-during-the-final-planning-round race) and converts it to CANCELLED. `apply_cancellation` marks every pending step SKIPPED with a deterministic message stating how far the plan got; `log_cancellation` writes an ActivityLog row (`tool_name="cancel_plan"`, completed/skipped counts) — every applied cancellation is audited; audit failure is logged, never raised. The flag is cleared when the run's asyncio task ends (`_spawn`'s done callback), so an unconsumed cancel never leaks into a later resume.
- **API** (`POST /api/tasks/{id}/cancel`): only a `running` task is cancellable here (paused tasks cancel from their approval card via `/api/agent/approve` — the approval gates stay in one place; that inline path still settles WITHOUT a push, unchanged). The response is honest about cooperation: `accepted=true` means the flag was set on a live run — the cancelled outcome arrives later as a normal `"task"` push event; `accepted=false` carries the reason (settled already / paused / no live run in this process).
- **Frontend**: `receiveStepEvent` in `chatStore.ts` (wired via `onPush('plan_step', ...)` in `App.tsx`, current session only) patches the matching step row on any message whose plan id matches — `running` renders a spinner in `PlanCard`'s `StepStatusIcon`, a `failed` event carries the error before the authoritative final plan arrives. `plan_step` is in `notifications.ts`' `SILENT_TYPES` (a plan can emit dozens of ticks; the plan-level `task` events are the toast-worthy ones). The "Running in the background" banner gains a Cancel button → `cancelBackgroundTask` → `tasksApi.cancel`; while the request is pending the banner says the running step will finish first, and the cancelled `task` push event resolves the card (terminal patches clear `planCancelRequested`). A not-accepted cancel re-enables the button and surfaces the backend's reason as the card error.

### Google integration foundation (Phase 5, Part 1)
The shared plumbing for email/calendar tools and the daily briefing. `app/integrations/google_auth.py` is the ONLY module that reads or writes Google credentials; `app/integrations/google_services.py` is the ONE way business code gets a Gmail/Calendar client. Rules, all in code:
- **OAuth 2.0 installed-app loopback flow** (`GoogleAuthManager.start_connect` → `_run_flow_sync`): the consent page opens in the system browser and the redirect lands on an ephemeral 127.0.0.1 port (loopback only — the backend's own bind rule). `run_local_server` BLOCKS, so the flow runs in a worker thread (`asyncio.to_thread`, task kept referenced like task_runner's `_RUNNING`) with a hard `OAUTH_FLOW_TIMEOUT_SECONDS = 300` — an abandoned browser tab can never wedge connect forever; one flow at a time (`_connecting` under `_state_lock`; a second POST /connect returns `in_progress`). POST /connect returns immediately (`pending`) and the UI polls GET /status.
- **Scopes are least-privilege and FROZEN at module level** (`SCOPES`: gmail readonly/compose/send + calendar.events — never gmail.modify or full mail: Jarvis reads, drafts, and sends; it does not delete or relabel mail). A stored token missing ANY required scope counts as NOT connected — re-consent, never silent partial capability. Adding a scope in a later part deliberately forces every user to reconnect.
- **Token hygiene**: the token lives at `~/.jarvis/google_token.json` (`GOOGLE_TOKEN_PATH` overrides; same `~/.jarvis` home as the delete-file trash) — outside the repo, never in .env, NEVER logged (log lines carry exception class names and the account email at most). Writes are atomic (temp + `os.replace`, best-effort 0o600). The account email is fetched ONCE at connect time (via gmail.readonly `getProfile`) and stored in the token file under `_account_email`, so `status()` is purely local — config + token file, no network — and the UI can poll it freely.
- **Degradation contract**: a missing/revoked/unrefreshable token raises `GoogleNotConnectedError` (stable user-facing message) from `get_credentials()` — the single choke point every Google call goes through; refresh runs silently off the event loop and re-persists. Dependent features catch it and fail clean ("Google account not connected"), never crash: no Google account is a normal state, not an error state. Never destructive on ambiguity: a failed refresh raises but does NOT delete the token file (the failure may be transient network trouble); a corrupt token file reads as not-connected, never a crash.
- **Disconnect** revokes at Google (best-effort, `_revoke_token` — module-level so tests stub the network away) and ALWAYS deletes the local token — a revoke outage must not trap the user in a connected state they asked to leave.
- **Injectable service factories** (`get_gmail_service()` / `get_calendar_service()` in google_services.py, `GMAIL_SERVICE_FACTORY` / `CALENDAR_SERVICE_FACTORY` module-level, sync-or-async callables): the SESSION_FACTORY pattern — tests swap them and the suite NEVER touches the real Google API. Likewise `AUTH_MANAGER` in google_auth.py (resolved via `auth_manager()` at call time, never import time); a conftest autouse fixture points every test's manager at an empty scratch token path, so the suite never reads the real `~/.jarvis` token.
- **API** (`app/api/integrations.py`, `/api/integrations` — routers orchestrate, modules own their domain): `GET /google/status` (`{configured, connected, connecting, account_email, scopes, detail}`), `POST /google/connect` (400 with the .env fix when unconfigured; else `{"status": "pending"|"already_connected"|"in_progress"}`), `POST /google/disconnect` (`{disconnected, revoked}`). OAuth client id/secret come from Settings (`GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` — for installed apps Google documents the secret as non-confidential, but it still only lives in .env).
- **Frontend**: `SettingsPanel` (`components/settings/SettingsPanel.tsx`, new `settings` panel in Sidebar/App) hosts the Google account card — status badge, plain-language scope summary, Connect (disabled with the .env hint when unconfigured) / Disconnect; polls status slow (15s) idle and fast (2s) while a consent flow is open. `integrationsApi` in lib/api.ts.
- **Deliberately rejected**: a `/plugins` directory with dynamic discovery (auto-loading dropped-in .py files is a code-execution vector against the structural approval gate; explicit self-registering modules per domain is the pattern), and two-tier LLM "skill" routing for tool injection (an extra LLM call per task against a 20/day quota + a worse failure mode — a plan missing a capability it needs; revisit ONLY past ~40 tools, and then as deterministic filtering off the Part 5 multi-class classifier label, never a second LLM call).

### Contact email + birthday (Phase 5, Part 2)
The fields themselves predate this part end-to-end (baseline migration, extraction prompt, `PersonMentioned`, `update_contact` merge, `PendingResolution.pending_update` parking, `lookup_contact`, API serialization, frontend type) — Part 2 is the hardening layer. Rules, all in code:
- **Deterministic validation net** (`app/memory/contact_validation.py` — the `normalize_future_phrasing` philosophy: the prompt states the contract, Python enforces it). `normalize_email` (regex + dot rules, domain lowercased, local part preserved, `mailto:`/angle-bracket stripping, prompt-placeholder echoes like `"null or string"` → None) and `normalize_birthday` (canonical `MM-DD` year-unknown / `YYYY-MM-DD` year-known — people say "Jamil's birthday is March 4" without a year; accepts slash separators, zero-pads, parses English month names + ordinals deterministically; rejects calendar-invalid days, month>12 — **never DD-MM-flipped**, the net's job is safety, not repair —, future dates, years <1900; Feb 29 valid year-less and in real leap years; `today` injectable for tests). Applied THREE times: `PersonMentioned` field validators (belt — junk never reaches `store_contact`, so it never parks a resolution or triggers a create question), `update_contact`/`create_contact_manual` (suspenders — a value that fails normalization is SKIPPED, never overwrites good data; covers every write path including parked-resolution merges), and `_validate_contact_payload` in `app/api/contacts.py` (manual edits get an explicit 400 with the format hint — a silent drop is right for the LLM, wrong for a human whose edit quietly didn't save). The extraction prompt's email line also demands verbatim copying — never invent/complete an address.
- **Identity resolution reused as-is**: an ambiguous "Jamil's email is…" parks the details dict in `PendingResolution.pending_update` exactly like any contact attribute — which-Jamil flow, `confirmed_names` cache, multi-name resolution, `apply_pending_resolution_reply` all apply with zero new resolution code.
- **Manual-edit semantics on `update_contact`**: `clear_empty=False` (only the API PUT passes True — an explicit `""` erases email/phone/organization/birthday; extraction can never clear because post-validation fields are None, never "") and `touch_interaction=True` (the API PUT passes False — correcting a field is not an interaction with the person, so no `interaction_count` bump). PUT also 404s properly on a missing contact. `create_contact_manual` now persists `birthday` (it used to silently drop it).
- **Frontend**: `ContactFormModal` (`components/contacts/ContactFormModal.tsx`, replaces the old AddContactModal) serves create AND edit (Pencil button in ContactDetail; name is read-only in edit mode — the engine has no rename path). Birthday input is year-optional (month select + day + optional year), composed/validated by `lib/birthday.ts`, which mirrors the server rules and does manual string math only — `new Date('YYYY-MM-DD')` parses as UTC and shifts the displayed day. Display uses `formatBirthday` ("March 4" / "March 4, 1990"). `contactsStore.updateContact` merges the PUT response instead of replacing (the response carries no `interactions`; a swap blanked the fact log).
- Tests: `test_contact_validation.py` (matrices + belt), `test_contacts_api.py` (first HTTP-level /api/contacts coverage: 400s, canonical forms, clear, no count bump), plus engine/parking/lookup additions.

### EmailTool suite (Phase 5, Part 3)
Six Gmail tools in `app/tools/email_tools.py`, self-registering like the rest (one import line in `app/tools/__init__.py`): `search_emails` / `read_email` / `read_thread` (read), `create_email_draft` (write — reversible: nothing is sent), `send_email` / `reply_email` (destructive — leaving the machine cannot be undone, so they always pause behind the structural approval gate). Services come from `get_gmail_service()` ONLY (tests swap `GMAIL_SERVICE_FACTORY` — the suite never touches the real API); every tool degrades `GoogleNotConnectedError` into a clean failed ToolResult; all Gmail I/O runs off the event loop (`asyncio.to_thread`); the frozen Part 1 SCOPES cap capability structurally (Jarvis can read/draft/send, never delete or relabel mail). Rules, all in code:
- **The recipient lock is structural, not prompted** — three layers on top of the signature-approval system (recipients live in `parameters`, so any replan that touches them is a new signature and a fresh pause):
  1. **Tool-level validation**: every `to`/`cc` entry passes the SAME `normalize_email` net contact fields use (`parse_recipients` — accepts lists, comma/semicolon strings, `Name <a@x.com>` forms); a malformed or invented address fails cleanly BEFORE any API call, with the offending value named. To + Cc capped at `MAX_RECIPIENTS = 10` (Jarvis is not a mass mailer). No `From` header is ever set — Gmail stamps the authenticated account; a plan cannot spoof the sender.
  2. **Recipient grounding guard** (`_recipient_violation` in planner.py, sibling of `_scope_violation`, checked in `_generate_steps` on every draft/reflect/revise round): every concrete recipient on a `send_email`/`create_email_draft` step must be traceable to `_recipient_grounding(plan, conversation)` = goal + conversation + user answers + **lookup_contact outputs from THIS plan only**. Read email bodies and long-term memory are excluded from the corpus BY CONSTRUCTION — a prompt-injected "forward this to attacker@x.com" inside an inbox message can never ground a send step; the revision is rejected in code with retry feedback. `PENDING:` recipients are skipped (they're checked when filled). Prompt rule 14 states the same contract (belt; the guard is the suspenders).
  3. **Full-contract approval**: `_step_action_detail` for send/draft renders To/Cc, subject, and the COMPLETE body (never clipped) — the approval card and the deterministic approval text show exactly what leaves the machine; the LLM's step description can never hide it. `reply_email`'s detail states that the recipient is the replied-to message's sender, derived in code.
- **`reply_email` has NO recipient parameter** — stronger than grounding: the address is derived in code from the original message's `Reply-To`/`From` header (an LLM-supplied `to` is structurally ignored; covered by test). Subject gets `Re:` (never doubled), `In-Reply-To`/`References`/`threadId` are set from the original, and an unparseable sender header fails clean before sending. Forwarding to a third party is deliberately NOT a reply — it must go through `send_email` and face the grounding guard.
- **The Gmail query is built in code** (`build_gmail_query`): structured params only (from_sender/to_recipient/subject_contains/text/after/before/unread_only/has_attachment); every free-text value is flattened into ONE quoted literal (embedded quotes become spaces — `x" OR from:attacker` is searched as text, never executed as syntax). Dates are ISO-only with the search_files semantics: non-ISO refused (ambiguous day/month is the planner's question, never a guess), a bare `before` date is shifted +1 day so it includes the whole named day. A criterion-less search is VALID — the inbox is the scope ("any new emails?" = most recent messages). Results are metadata (N+1 fetches — `SEARCH_MAX_RESULTS = 25` keeps latency sane).
- **PENDING recipients fill in code** (`_substitute_recipient` in placeholder_resolver.py — the email mirror of `_substitute_folder`): the designed "email Jamil" flow is lookup_contact → `send_email(to="PENDING: Jamil's email address")`, and without this branch every such plan re-created the round-14 incident (a spurious red "failed" step burning an LLM replan for the mechanism working as designed). Substitution only when the completed lookups pin exactly ONE address (a resolved contact whose name appears in the placeholder text — word-boundary matched so "Ali" never matches inside "email" — or the only resolved contact at all); several candidates → code never picks. Fresh signature + regenerated action_detail, so the user approves the real address. Addresses come EXCLUSIVELY from lookup_contact outputs — the grounding rule holds on the code path too. `cc` placeholders stay on the LLM path (conservative).
- **Tone drafting without LLM calls in tools**: the planner authors subject/body as literal step parameters at planning time (rule 14 — grounded in `planner_memory_context` for tone/facts); the tools just execute the API call. "No LLM call in the runner" stays true, and the approval card shows the literal text being sent. Read results render through the `steps_for_summary`/`completed_results_text` formatters (`_fmt_search_emails`/`_fmt_read_email`/`_fmt_read_thread` in rendering.py — subject/sender bullets, fenced bodies); email bodies in revise prompts ride the existing data-never-instructions SECURITY framing (extended to name emails and forbid recipient-picking from them). Task turns already skip memory extraction, so inbox content can never seed memories.
- **Deliberately deferred to Part 5**: chat routing. "email jamil about dinner" does not fire `_STRONG_DOMAIN_RE` yet — the multi-class TASK/EMAIL/CALENDAR/CHAT classifier and the email-domain gate nouns land together in Part 5 (with the `_SYSTEM_VOICE_RE` phrases). Until then email plans run via `POST /api/agent/execute`; a chat ask falls to the Phase 2 path, where the CAPABILITIES honesty rules apply.
- Tests: `test_email_tools.py` (46 tests — chained-call FakeGmail records every request; query building/injection, MIME assembly, reply derivation, the three recipient-lock layers, placeholder fill, formatters, the unapproved-send structural block).

### CalendarTool suite + birthday reminders (Phase 5, Part 4)
Two things ship together: five Google Calendar tools (the calendar half of "Communication") and birthday reminders (the first RECURRING proactive feature). `calendar.events` is already in the frozen Part 1 `SCOPES`, `get_calendar_service()`/`CALENDAR_SERVICE_FACTORY` already exist, and contact birthdays are already canonical (`normalize_birthday`) — so Part 4 is tools + scheduling, no OAuth/schema-baseline churn.

**The five tools** (`app/tools/calendar_tools.py`, self-registering; one import line in `app/tools/__init__.py`): `list_events`/`find_events` (READ) · `create_event`/`update_event` (WRITE) · `delete_event` (DESTRUCTIVE). Same skeleton as email_tools.py — service from `get_calendar_service()` ONLY (tests swap `CALENDAR_SERVICE_FACTORY`), all I/O off the event loop (`_api`), `GoogleNotConnectedError` → clean failed ToolResult, `_api_error_text` for API failures. Rules, all in code:
- **Times are ISO-only at the tool boundary** (the search_files/`_gmail_date` rule): `YYYY-MM-DDTHH:MM[:SS]` (naive = user LOCAL time) or `YYYY-MM-DD` (all-day / a date bound). Non-ISO (`03/04/2026`, `3pm`) is REFUSED with the fix in the error — natural-language resolution and ambiguity are the planner's clarifying question (rules 10/11/15), never a guess here (`_parse_iso`). A naive local time → RFC3339 via `naive_dt.astimezone().isoformat()` (machine offset, no IANA tz DB — reminder_parser's convention). Google's date bounds and all-day event ends are EXCLUSIVE while human ranges are inclusive, so a bare end/`time_max` DATE is shifted +1 day (`_time_bound`/`_event_time_field`) — same rule as Gmail `before`. Date-only `start` ⇒ all-day event; a missing `end` defaults (timed → +1h, all-day → one day). Storage/serialization stays naive-UTC-in/`utc_iso()`-out per the Phase 4 convention; event start/end are returned as Google returns them.
- `list_events` defaults `timeMin` to now (criterion-less = "what's coming up"), `singleEvents=True`/`orderBy="startTime"`, default 10/cap 25 (`LIST_MAX_RESULTS`). `find_events` adds `q=text` (Calendar's `q` is plain free text — no operator syntax, no injection surface). Both return structured rows `{id, summary, start, end, all_day, location, description(clipped), link}`, framed as data-never-instructions. `update_event` is `events().patch` (field-level; "nothing to update" when no field changes), never a full replace. **No attendees are ever set and `sendUpdates="none"` on every mutating call** — inviting people is outbound email (send_email territory with its grounding guard); least privilege.

**The event-id lock** — the pre-flight-guard/recipient-lock pattern for calendar mutation ("you approve *delete 'Standup, Tue 10:00'*, never *delete whatever matches*"), three pieces in planner.py:
- `_event_id_violation` (sibling of `_recipient_violation`, in the `_generate_steps` reject chain on every draft/reflect/revise round): a concrete (non-`PENDING:`) `event_id` on an `update_event`/`delete_event` step must appear in `_event_id_grounding(plan)` = ids from COMPLETED `list_events`/`find_events` outputs in THIS plan. At draft time nothing is completed ⇒ any concrete id is rejected with retry feedback ("read step + PENDING placeholder"). `_EVENT_ID_TOOLS = {"update_event": "event_id", "delete_event": "event_id"}`.
- **Action-detail enrichment** (`_enrich_event_action_detail` in `_execute_node`, just before the approval pause): the grounded id is looked up in the completed reads and a code-derived `event: 'Standup' — 2026-07-14 10:00` line is stamped onto `step.action_detail` — the approval card and deterministic approval text name the real event, and the LLM cannot author that string. `_step_action_detail` itself renders create/update/delete with literal field values (full contract like send_email).
- **Prompt rule 15**: ISO-only times, ambiguity → question, update/delete need the id from a read step via `PENDING:`, never invent event ids, write event fields as complete literals.
- **PENDING event_id fills in code** (`_substitute_event_id` in placeholder_resolver.py — the calendar mirror of `_substitute_recipient`): the designed "delete the standup" flow is `find_events` → `delete_event(event_id="PENDING: the standup")`; without it every such plan re-created the round-14 spurious-failure/replan burn. Substitution only when the completed calendar reads pin exactly ONE event (a summary whose words match the placeholder text — word-boundary — or the only event found); several candidates → code never picks. Fresh signature + regenerated action_detail. Ids come EXCLUSIVELY from read-step outputs, so the grounding rule holds on the code path.
- **Read results** render through `_fmt_calendar_events` (registered for both read tools in `_RESULT_FORMATTERS`): count + one bullet per event (`• Standup — 2026-07-14 10:00–10:30, Room 3`, via `format_event_when`), item-level clipping. Write/destructive steps render their approved description unchanged.

**Birthday reminders** (`app/core/birthdays.py`, `app/core/reminders.py` as the template): a `"birthday"` scheduler job kind, registered at import (`import app.core.birthdays` in main.py). Recurrence WITHOUT touching the scheduler's one-shot core — the handler re-arms next year after firing; SQLite stays the truth (a restart on the birthday fires late-but-fires). Rules, all in code:
- **`next_birthday_run_at(birthday, now=None)`** — pure, injectable clock (the `normalize_birthday(today=)` pattern): parse canonical `MM-DD`/`YYYY-MM-DD`, next 09:00-LOCAL occurrence strictly after `now` (this year if still ahead, else next year), **Feb 29 → Feb 28 in non-leap years** (early beats missed for a "wish them" nudge), returned as naive UTC via `to_naive_utc`.
- **`sync_contact_birthday_job(db, contact)`** — the single choke point every hook calls: cancel the contact's current job (if any), then iff active with a valid birthday arm the next occurrence and store its id on `Contact.birthday_job_id` (new column, `String(36)`, mirrors `Reminder.job_id`; migration `c1a4e8f60b23` chained on head `a9e4c07d5b12`; excluded from the API serializer). Best-effort — a scheduler failure never breaks the contact save (logged, like narration). Hooked into `create_contact_manual` (the ONLY place `Contact(` is constructed — extraction and the parked-creation confirm both funnel here), `update_contact` (only when the birthday actually changed — no churn on unrelated edits), `delete_contact_by_name` (hard delete — cancel before the row is gone), and the `/api/contacts` DELETE soft-delete (sync cancels for an inactive contact).
- **`_birthday_job_handler`** — opens its own `AsyncSessionLocal` session. Guards, in order: contact missing/inactive/birthday-cleared → return (no re-arm); `contact.birthday_job_id != job.id` → return (a stale job never forks a second recurrence chain); payload `birthday != contact.birthday` → return (the sync path owns the new value). Then: build the body ("Today is Jamil Ali's birthday 🎂"; "…they turn 36 today" when the year is known; a fire on a LATER day — backend was offline past it, `_fired_on_later_day` by calendar day, not the 60s `job.late` — says "missed while Jarvis was offline — … was on July 14"), `push("birthday", {title, body, text, contact_id, late})` (Part 3's toast reads title/body unchanged), persist an assistant `Message` into the MOST RECENT chat session (newest Message row's session_id; skipped if none), and re-arm next year via `sync_contact_birthday_job`. Handler failures land on the job row per the scheduler contract — never propagate.
- **`ensure_birthday_jobs()`** — startup reconciliation (main.py lifespan, after `scheduler.start()`, non-critical try/except): a pending `"birthday"` job is VALID only if it is the current pointer of an active contact with a schedulable birthday — everything else is swept (cancelled), and every active contact with a valid birthday but no live pending job is armed (covers pre-Part-4 contacts, fire/re-arm crashes, and stray/duplicate jobs). Enumerating pending birthday jobs needed an optional `kind` filter on `JarvisScheduler.list_jobs` (backwards-compatible; the router still never touches the table).
- **No frontend in Part 4** (confirmed): toast + persisted chat message + the existing Schedule/Timeline panels cover it; a Calendar UI panel and chat routing (the multi-class TASK/EMAIL/CALENDAR/CHAT classifier) both land in Part 5. Until then calendar plans run via `POST /api/agent/execute`.
- Tests: `test_calendar_tools.py` (33 — chained-call FakeCalendar; ISO refusal, RFC3339 offsets, inclusive/exclusive date bounds, patch-not-replace, `sendUpdates="none"`, not-connected degradation, the unapproved-write structural block, the event-id guard/enrichment/fill, `_fmt_calendar_events`), `test_birthdays.py` (26 — real JarvisScheduler on a shared in-memory DB: occurrence-math matrix, sync on every contact-write path, handler fire → push + Message + re-arm, the three no-op guards, `ensure_birthday_jobs` arm/sweep, late/off-day wording), plus `test_contacts_api.py` additions (PUT schedules a job, DELETE cancels it — with an autouse fixture isolating the app-wide scheduler so no contacts-API test touches the real jarvis.db).

### Daily briefing (Phase 5, Part 6 — the capstone)
The "Jarvis moment", assembled from every prior part. Each morning at a
configurable local time Jarvis composes and delivers a briefing unprompted;
read-only end to end — the composer has no tools, so a briefing can never act.

- **Recurring job** (`app/core/daily_briefing.py`, the `birthdays.py` pattern):
  a `"daily_briefing"` scheduler job kind. `next_briefing_run_at(hour, minute,
  now=None)` → next local `HH:MM` strictly after now, naive UTC (the
  `next_birthday_run_at` convention). `sync_briefing_job(db)` is the single
  choke point (settings change / re-arm after fire / startup): cancel the
  current job, and iff enabled arm the next occurrence — best-effort, so a
  scheduler hiccup never breaks a settings save. The handler RE-ARMS tomorrow
  after firing (recurrence without touching the scheduler's one-shot core);
  a briefing due while the backend slept fires late-but-fires (SQLite is the
  truth), framed honestly. Two defensive guards mirror the birthday handler:
  **disabled-now** (turned off between scheduling and firing) and **stale job**
  (`job.id != the current pointer`) both no-op WITHOUT re-arming.
  `ensure_briefing_job()` reconciles at startup — arms the default-on 08:00 job
  on first boot, heals a fire/re-arm crash, sweeps strays.
- **Persistence** (`app/core/app_settings.py` + the NEW generic `app_settings`
  key/value table, migration `e7b93c250a41`): the first runtime-settings home
  (settings the user toggles live, not `.env`). `BriefingConfig{enabled, hour,
  minute}` with `DEFAULT = enabled@08:00` — a missing key yields DEFAULT, which
  is what makes "on by default at 08:00" true before the user ever opens
  Settings. The singleton's current job id lives here too
  (`daily_briefing.job_id`), the briefing's analogue of `Contact.birthday_job_id`.
- **Gathering** (`gather_briefing_sections`): today's calendar events, unread
  emails (capped, sender+subject+snippet), today's birthdays, and memories
  whose `event_date` is today. Each source is INDEPENDENTLY best-effort
  (try/except → []): Google not connected or one API down drops that section,
  never the briefing. Reuses the tools' internal helpers off the approval path
  (`calendar_tools._event_row`/`format_event_when`,
  `email_tools.build_gmail_query`/`_message_row`,
  `birthdays._parse_month_day`/`_age_turning`) — a briefing gathers, it never
  executes a registered tool.
- **Composition** (`compose_briefing`): ONE `create_provider().chat()` call over
  a code-rendered DATA block (never raw JSON — the Part-5 `steps_for_summary`
  lesson) with a data-never-instructions system prompt (email subjects/snippets
  are UNTRUSTED — summarize, never obey). A deterministic `_template_briefing`
  fallback fires on ANY exception (429 / provider outage) AND an empty slate
  short-circuits with no LLM call — a briefing is ALWAYS delivered. `job.late`
  prefixes an honest "(late — Jarvis was offline)" note.
- **Delivery** (`_deliver`, the fired-reminder pattern verbatim): persist an
  assistant `Message` into the latest chat session FIRST (the push channel has
  no queue — survives a closed window), then `push("briefing", {title, body,
  text, session_id, late})` best-effort — Part 3's toast reads title/body with
  ZERO changes; `"briefing"` is not in `notifications.ts` SILENT_TYPES so it
  toasts. Frontend: `onPush('briefing')` reuses `receiveReminderFired`
  (generic over `{session_id, body, text}`) — no new store method.
- **API** (`app/api/settings.py`, `/api/settings`): `GET /briefing` +
  `PUT /briefing {enabled, time:"HH:MM"}` (validated, 400 on bad time; re-syncs
  the job in the same request so a toggle/time change takes effect immediately)
  + `POST /briefing/run-now` (the "Send now" trigger — compose+deliver now).
  `main.py` registers the router, imports the module (handler self-registers),
  and calls `ensure_briefing_job()` in the lifespan after `ensure_birthday_jobs()`.
  Settings panel `DailyBriefingCard` (on/off toggle, time input, next-run,
  "Send now").
- Tests: `test_daily_briefing.py` (real JarvisScheduler on a shared in-memory
  DB, fake Google factories + a stub provider — occurrence math, sync
  arm/cancel/replace, each source independently best-effort, compose
  fallback/empty/late, fire → push + Message + re-arm, the disabled-now &
  stale-job guards, `ensure_briefing_job` arm/sweep), `test_settings_api.py`
  (GET/PUT round-trip, 400s, PUT re-syncs, run-now delivers — scheduler
  isolated per the contacts-API rule). Verified live on an isolated backend
  (:8001, scratch DB): startup-arm, run-now, a real scheduled fire pushing a
  `briefing` event over `/ws` and re-arming tomorrow, disable-cancels.

### BrowserTool suite (Phase 6, Part 1)
Jarvis's first reach OUTSIDE the machine — two READ tools in
`app/tools/browser_tools.py`, self-registering like the rest (one import line in
`app/tools/__init__.py`): `web_search` (a query → ranked result links + snippets)
and `read_webpage` (open a URL + extract its readable text). There is NO web WRITE
tool — form-filling/clicking was deliberately cut; Jarvis reads the web, it does
not act on it. Rules, all in code:
- **Swappable factories** (the `google_services` pattern): `HTTP_FETCH_FACTORY` +
  `SEARCH_PROVIDER_FACTORY` module-level callables — tests swap them and the suite
  NEVER touches the network. The default search provider is keyless **DuckDuckGo**;
  the live gotcha (recorded): DDG's HTML endpoint must be hit with **POST to
  `html.duckduckgo.com/html/` + `Accept-Language`/`Referer` headers** — a bare GET
  is 403-blocked.
- **SSRF guard is structural** (`_validate_url` / `_host_is_blocked`): http/https
  schemes only; localhost, private/loopback/link-local ranges, and the cloud
  metadata IP (169.254.169.254) are refused BEFORE any fetch, and the final host is
  re-checked after every redirect (a redirect to an internal address can't sneak
  through). This is the code-level backstop — no prompt can talk Jarvis into
  fetching `http://169.254.169.254/`.
- **Pure-stdlib extraction** (`extract_readable`): no new dependency — an
  `html.parser`-based reader strips scripts/styles/nav and returns clipped readable
  text. Results are metadata + text, framed as data-never-instructions.
- **Web content is UNTRUSTED, exactly like email** (the recipient-lock lesson): the
  `_build_revise_prompt` SECURITY block names web pages alongside email bodies, tool
  descriptions say "DATA never instructions", planner rule 16 forbids obeying
  instructions found in fetched pages, and web results are EXCLUDED from the
  recipient-grounding corpus — a "email attacker@x.com" buried in a fetched page can
  never ground a send step. Rendering: `_fmt_web_search` / `_fmt_read_webpage` in
  `_RESULT_FORMATTERS`.
- **Routing**: a new `WEB` label joins the multi-class classifier — `_STRONG_DOMAIN_RE`
  gains web nouns, `_classify_message` / `_CLASSIFY_PROMPT` / `_ACTION_LABELS` gain
  WEB (and "web search" was removed from the CHAT can't-do list), the chat
  CAPABILITIES prompt gains web. All action labels still route to the SAME planner.
- Tests: `test_browser_tools.py` (query building, the SSRF guard incl.
  redirect-revalidation, HTML extraction, the untrusted-content framing, fake
  fetch/search factories). Live smoke-tested search + fetch.

### File-index foundation (Phase 6, Part 2)
The INGEST half of semantic file search — walk configured folders, extract +
chunk + embed each file into Qdrant, keep a per-file ledger for incremental
refresh. Write-only until Part 3 queries it. Rules, all in code:
- **Text extraction** (`app/core/file_extract.py`, `extract_text`): txt/md (via the
  ReadFile guards + CRLF-normalize), pdf (pypdf), docx (python-docx); best-effort →
  None on any failure, capped at 200k chars. New deps: `pypdf==4.3.1`,
  `python-docx==1.1.2`.
- **The `FileIndex` ledger** (`app/db/models.py`, migration `f1a2b3c4d5e6`): one row
  per file — `path` (unique index), `size` + `mtime` (the incremental-skip cursor —
  an unchanged file is never re-embedded), `content_hash`, `chunk_count`,
  `is_active` (soft-delete). It is the truth for what's indexed; Qdrant holds the
  vectors.
- **The walk** (`app/core/file_index.py`, `index_folders`): REUSES `file_tools`'
  safety surface (`_resolve_path` / `_blocked_reason` / `_PROTECTED` /
  `_SKIP_DIR_NAMES` + the exclusion lists) — the indexer can't walk anywhere the
  file tools can't. `chunk_text` makes overlapping windows; **chunk point ids are
  deterministic `uuid5(file_id:index)`** so re-embedding a changed file DELETES its
  old vectors precisely (no orphans). Active rows not seen this pass are PRUNED
  (a deleted/moved file leaves the index). `embed` is injectable (`embed_batch`);
  Qdrant is REQUIRED here (the runner guards a None client). `run_index` /
  `start_index_in_background` run a single detached pass (`SESSION_FACTORY` pattern,
  `_INDEXING` flag).
- **The `file_chunks` collection** (384-dim COSINE) is added to
  `qdrant_client.COLLECTIONS` (auto-created at startup).
- **Config** (`FileIndexConfig` in `app_settings`): **`enabled` defaults OFF** —
  indexing personal files is strictly opt-in; `folders` prefilled
  Desktop/Documents/Downloads, `exclusions`, `interval_minutes` clamped 15..10080.
- **API** (`app/api/index.py`, `/api/index`): GET, GET `/status`, PUT `/config`
  (400 on a root/protected folder), POST `/rebuild` (background). Frontend
  `FileIndexCard` in `SettingsPanel` (folder/exclusion editors, enable toggle,
  interval, Index-now + status poll) + `indexApi`.
- Tests: `test_file_extract.py`, `test_file_index.py` (real `:memory:` Qdrant + a
  fake 384-dim embed), `test_index_api.py`. Live-verified that REAL fastembed →
  Qdrant ranking is meaningful. NO search tool + NO scheduler yet (Part 3).

### Semantic file search + reindex scheduler (Phase 6, Part 3)
Closes the Part 2 loop: a READ tool to FIND files by meaning, and a recurring job
to keep the index fresh. Rules, all in code:
- **`semantic_file_search`** (`app/tools/semantic_file_tools.py`, the `memory_tools`
  READ-tool seam — `SESSION_FACTORY` + `_qdrant()` resolved at call time): embed the
  query → `qdrant.search("file_chunks")` → **group chunk hits by `payload["file_id"]`**
  (the point id is the uuid5 CHUNK id, never a row id — rehydrate `FileIndex` via the
  payload) → keep the best chunk score + snippet per file → **combined rank** =
  cosine base + boosts (a query token in the filename, recency ≤30d). Optional
  refiners `filename_contains` / `folder` / `modified_after|before` HARD-filter
  (reusing `file_tools`' ISO-only `_parse_date_bound` — non-ISO refused, the planner
  asks on ambiguity, never guesses). **Qdrant None → filename fallback** over the
  `FileIndex` rows (the memory-engine "degraded but useful" philosophy); an empty
  index sets an explanatory `note`. Full paths are returned so the question_gate can
  offer them as VERIFIED options. Strictly READ (no approval).
- **The `"reindex"` scheduler job** (`app/core/reindex.py`): the `daily_briefing`
  singleton pattern verbatim EXCEPT interval-based — `next_reindex_run_at(interval,
  now) = now + timedelta(minutes=interval)` (a pure interval needs no wall-clock
  dance). It **REUSES Part 2's `get/set_file_index_job_id` pointer** (key
  `file_index.job_id`) — no new app_settings key. `sync_reindex_job(db)` is the single
  choke point (cancel current → iff enabled arm next → store id); the handler has the
  disabled-now + stale-job guards, calls `run_index(full=False)` (a None qdrant is a
  safe no-op), then re-arms; `ensure_reindex_job()` reconciles at startup (arm/heal/
  sweep). Wiring: `main.py` imports the module + calls `ensure_reindex_job()` after
  `ensure_briefing_job()`; `index.py` PUT `/config` calls `sync_reindex_job` so a
  toggle/interval change re-arms immediately.
- **Planner routing**: rule 8 qualified (name/metadata → `search_files`), new rule 17
  (a file by its CONTENT / topic / description → `semantic_file_search`; read-level,
  feed a chosen path into later steps via `PENDING:`, ask via rule 11 when several
  match and a write must target one). Rendering: `_fmt_semantic_file_search`
  (group-by-folder + fenced snippets). No migration, no frontend (Part 2's
  `FileIndexCard` already exposes enabled/folders/interval — the interval now
  actually drives the scheduler). Multi-match disambiguation reuses the EXISTING
  question_gate / AWAITING_CHOICE machinery — no new disambiguation code. Also fixed
  a stale `task_router._CLASSIFY_PROMPT` closing line (it omitted WEB).
- Tests: `test_semantic_file_search.py` (fake qdrant + stub embed), `test_reindex.py`
  (real `JarvisScheduler` on `:memory:`; `run_index` is a no-op without qdrant).

### Conversation search (Phase 6, Part 4)
Makes "search files AND past conversations" literally true. Messages were the one
memory surface that was SQLite-only — searchable by session, never by MEANING. Now
each chat `Message` is embedded into a NEW `"conversation_messages"` Qdrant
collection (384-dim, **one point per message, point id = `message.id`**), and
`semantic_file_search` returns ranked matches from files AND prior chats in one ask.
Rules, all in code:
- **The cursor** (`Message.embedded_at`, migration `d4e5f6a7b8c9`, down_revision
  `f1a2b3c4d5e6`): NULL = not yet embedded — the Message row IS the ledger (no
  separate table), mirroring `FileIndex`'s size/mtime skip. A re-embed upserts the
  same id, so there are never duplicates.
- **The service** (`app/core/conversation_index.py`, the `file_index` pattern but no
  filesystem walk): `index_conversations(db, qdrant, *, full, embed)` — INCREMENTAL
  (only `embedded_at IS NULL` unless `full`), only role ∈ {user, assistant} with
  non-empty content (system/scaffolding skipped), batched embed + upsert
  `PointStruct(id=msg.id, payload={message_id, session_id, role, text≤2000,
  created_at})`, best-effort per batch. `embed_message_best_effort(db, msg)` is the
  **ON-WRITE hook** (a single just-said turn, best-effort, no-op when disabled /
  no-qdrant, NEVER raises — `expire_on_commit=False` makes reading `msg.content`
  post-commit safe). `run_conversation_index(*, full)` opens its own session and is
  gated on the enable toggle + a live qdrant. Plus `start_..._in_background` /
  `get_conversation_index_summary`.
- **Two-tier coverage** (there is no single choke point for Message creation — ~10
  write sites): the on-write hook covers the main chat path (`chat._persist_message`
  after commit); the **reindex handler ALSO runs `run_conversation_index(full=False)`**
  each interval as the BACKFILL catch-all for task/reminder/briefing turns the hook
  doesn't touch. `index.py` `/rebuild` also reindexes chats; GET `/status` merges
  `indexed_messages` / `unindexed_messages`.
- **Unified recall** — `semantic_file_search` was **EXTENDED, not renamed** (no new
  tool → no manifest churn): embed the query ONCE, search BOTH `file_chunks` +
  `conversation_messages`, merge + rank together — each match tagged
  `type: "file" | "conversation"`. `include_conversations = not filename_contains and
  folder is None` (a file-specific refiner ⇒ files only; date bounds apply to both);
  `_recency_boost` now takes `now` (files=local, chats=naive-UTC). Conversation hits
  aren't path-like, so the question_gate treats them as free-text options —
  cross-source disambiguation reuses the EXISTING AWAITING_CHOICE flow with zero new
  code. `_fmt_semantic_file_search` renders a "matching conversation message(s)"
  section (role + date + fenced snippet); planner rule 17 broadened to "what did we
  discuss about X" / "the chat where I mentioned Y".
- **PRIVACY**: conversation embedding is gated on the SAME `FileIndexConfig.enabled`
  toggle (opt-in) and is local (fastembed) — message text never leaves the machine.
- Tests: `test_conversation_index.py` (fake qdrant capturing upserts + stub embed,
  `StaticPool :memory:` — embed-on-write, disabled/system/empty/no-qdrant no-ops,
  incremental vs full backfill, run enable/qdrant gates, summary), `test_semantic_
  file_search.py` +4 (cross-source files+chats, ranked-together, file-refiner-
  excludes-chats, conv formatter). Part 6 (File Intelligence) is NOT built.

### Teachable routines (Phase 6, Part 5 — procedural memory)
The user teaches a repeatable procedure once and invokes it by name. The core
safety property: we store the goal STRING (`Routine.goal_template`), NEVER a
plan — every run is replanned fresh through the agent planner, so the approval
gate, path guards, and recipient/event-id locks all re-apply automatically. A
routine can never smuggle a pre-approved destructive plan past the gate. Rules,
all in code:
- **The router** (`app/api/routine_router.py`, mirrors `reminder_router.py`):
  `maybe_handle_routine(request, session_id, db, provider)` hooked into
  `chat_stream` BETWEEN the reminder and task routers (precedence: reminder →
  routine → task → Phase 2 chat). It MUST sit ahead of the task router because a
  bare routine name ("clean my desktop") would otherwise be caught by the task
  gate's strong-noun rule and re-planned as a one-off. Deterministic (no LLM for
  teach; the RUN ack/failure texts are literal), short-circuits the turn (no
  memory extraction — a routine command is not autobiography), and defers to an
  already-open memory/agent question exactly like the reminder router (peek
  `CONVERSATION_SESSIONS.get()` + `get_choice_plan_for_session`).
- **TEACH** — tight literal anchors (`_TEACH_PATTERNS`): "save/remember this [as
  a] routine [called|named|as] X" and the quoted "remember this as 'X'" form.
  Requires the literal word "routine" OR a quoted name, so "remember that meeting
  is at 3" matches neither. The goal_template is an inline procedure split off the
  name span (`_INLINE_SPLIT_RE`: "… that: <steps>") or, more commonly, the most
  recent task-shaped prior USER turn (`_capture_prior_goal`, reusing
  `task_router.looks_like_task`). No capturable goal → deterministic clarifying
  text, creates nothing.
- **RUN** — explicit ("run my X routine" / "run routine X", `_RUN_PATTERNS`,
  requires the word "routine") or an exact normalized-name match against a saved
  routine. Loads `goal_template` → `planner_memory_context` + `conversation_context`
  → `start_task(...)` (BACKGROUND Task path) → deterministic ack. An explicit
  "run …" that names no saved routine returns None (falls through — may still be a
  real task).
- **Domain module** (`app/core/routines.py` — the reminders pattern; the router
  and API never touch the table). `normalize_name` (strip quotes/punctuation,
  lowercase, collapse ws) is the ONE normalization for BOTH the lookup key and
  goal-recurrence matching. `create_routine` UPSERTS on `normalized_name`
  (re-teach replaces goal_template, never duplicates). `Routine` table (migration
  `a1b2c3d4e5f6`, down_revision `d4e5f6a7b8c9`: `name` display / `normalized_name`
  unique key / `goal_template` Text / `is_active`).
- **Offer-to-save** (`maybe_offer_routine`, best-effort, called from
  `task_runner._settle` on terminal COMPLETED only, in its own try/except): when a
  goal has completed `ROUTINE_OFFER_THRESHOLD = 3` times (counted over `Task.goal`
  rows — the cleaner signal than raw ActivityLog, mirroring
  `Preference.occurrence_count`), and it is not already a routine and not already
  offered, persist an assistant `Message` (durable copy) + `push("routine_offer",
  …)` (fired-reminder delivery; `routine_offer` is NOT in `notifications.ts`
  SILENT_TYPES so it toasts, and `App.tsx` reuses `receiveReminderFired`). The
  offer SPELLS OUT the teach phrase — confirmation reuses the TEACH trigger, so
  there is no new yes/no state machine. Throttled once per goal via app_settings
  key `routines.offered`.
- **API** (`app/api/routines.py`, `/api/routines`): GET (list active), POST
  (`{name, goal_template}` — manual/UI create; chat is primary), DELETE (hard
  delete), POST `/{id}/run` (deps `get_db` + `get_llm_provider`; optional
  `session_id`; starts a background Task, returns `{task_id, status}`; 404 on
  missing/inactive). `utc_iso` timestamps per the API convention.
- **Frontend**: `Routine` type, `routinesApi`, `routinesStore`, `RoutinesPanel`
  (list / Run / Delete, 15s poll), Sidebar nav item, `onPush('routine_offer')`.
- Tests: `test_routines.py` (normalization matrix; create/upsert/get/delete; TEACH
  & RUN parse; goal capture from history; the replan-fresh-keeps-approval
  invariant via `start_task`; offer threshold/throttle/suppression),
  `test_routines_api.py` (CRUD round-trip, upsert, run starts a Task, 404).

### Timestamp serialization (API convention)
The DB stores naive UTC (`utc_now()` in models.py). API serializers MUST use `utc_iso()` (models.py), never bare `.isoformat()`: a naive ISO string has no timezone marker, so the frontend's `new Date(iso)` reads it as LOCAL time and every displayed timestamp shifts by the machine's UTC offset (the "reminder set for 6 PM shows 1 PM" bug, fixed 2026-07-09). Applied to reminders, activity, tasks, chat messages, and schedule serializers. Extraction-derived date-semantics fields (`event_date`, `interaction_date`, `occurred_at` in contacts/episodes/memory) deliberately keep bare `.isoformat()` — they are calendar dates, not UTC moments, and marking them UTC would shift the displayed day.

### Config
`app/core/config.py` is the single Pydantic `Settings` source of truth, loaded from `../.env` relative to `backend/` (i.e. the repo-root `.env`). Access settings via the `settings` singleton, never re-read env vars elsewhere. `BACKEND_HOST` must stay `127.0.0.1`: the API is unauthenticated and can run tools on the machine — never bind it to `0.0.0.0`.
