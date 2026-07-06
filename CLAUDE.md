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

# Lint / typecheck (frontend only — there is no backend lint config)
npm run lint
npm run typecheck

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
`app/memory/conversation_state.py` holds `ConversationSession` — ephemeral, in-process, per-`session_id` state (`active_entities`, `focus_entity`, `pending_resolution`) with a 30-minute TTL. This is **never** written to SQLite/Qdrant. The design principle stated in that file: "Foreground owns state. Background owns persistence." Don't conflate the two — session state resolves pronouns/ambiguity for the *current* conversation; the memory engine tables are the durable long-term store.

### LLM provider abstraction
`app/providers/base.py` defines the `LLMProvider` ABC (`chat`, `stream_chat`, `embed`). `app/providers/factory.py` picks a concrete provider (`gemini`, `groq`, `ollama`, `openrouter`) from `LLM_PROVIDER` in `.env` via an `lru_cache`d factory — business logic (memory engine, chat routes, extractor) only ever depends on the abstract interface, never a concrete provider. Switching providers is a `.env` + backend restart, no code changes.

### Tool system (scaffolded, not yet wired up)
`app/core/base_tool.py` defines `BaseTool`/`ToolResult`/`ToolDefinition`/`PermissionLevel` (`read` / `write` / `destructive`) for a planned plugin-style tool architecture (Phase 3, LangGraph-based planner). `ActivityLog` in `app/db/models.py` is the audit-trail table for tool executions. No concrete tools exist yet — `app/tools/` and `app/agents/` referenced in the README are future work.

### Config
`app/core/config.py` is the single Pydantic `Settings` source of truth, loaded from `../.env` relative to `backend/` (i.e. the repo-root `.env`). Access settings via the `settings` singleton, never re-read env vars elsewhere.
