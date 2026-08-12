# Furi OS

> A personal AI operating system — your intelligent digital extension.

## Overview

Furi OS is a local-first desktop AI agent that remembers you, learns how you
work, and carries out real tasks: it searches and organises your files, handles
your mail and calendar, drives a real browser, controls your desktop and smart
home, speaks and listens, and volunteers things before you ask.

**This is not a chatbot. This is a personal AI operating system.**

Everything that can change the world goes through one structural approval gate,
and everything it does is written to an audit trail you can read.

> **Platform:** developed and verified on **Windows**. The Electron shell, the
> backend and the browser stack are cross-platform, but desktop control,
> device/screen sensing and encrypted secret storage (DPAPI) are Windows-only
> today and fail cleanly elsewhere rather than pretending to work.

## What it does

| | |
|---|---|
| **Memory** | Semantic facts, people (append-only fact logs with fuzzy identity resolution), episodes and inferred preferences. Extraction runs in the background after every turn; old, unused facts are summarised and archived rather than deleted, and contradictions are queued for you — never auto-resolved. |
| **Agent + tools** | A LangGraph planner drafts a plan, runs the READ steps, then pauses for approval on anything that writes. **48 tools** (23 read · 17 write · 8 destructive) across files, terminal, memory, email, calendar, web, browser, home and desktop. |
| **Files** | Search by name/date/size, or by MEANING — a local vector index over your folders *and* your past conversations. Bulk moves and deletes default to a folder's own files and say what they left behind. Deleted files go to `~/.jarvis/trash`, never straight to unlink. |
| **Email & calendar** | Gmail and Google Calendar. Recipients must trace to your own words or your contacts — never to the content of a page or an email — and a send shows the complete message before it leaves. |
| **Browser** | A real Chromium doing real work: search, read, fill forms, choose a size, add to cart, sign in by hand when a site asks. One approval per submitted form, bound to that exact form's contents. CAPTCHAs are handed to you, never solved. |
| **Voice** | Local Whisper (STT) and Kokoro (TTS), an on-device wake word, and a hands-free voice mode with a live transcript and in-place approval cards. Audio never leaves the machine. |
| **Home & desktop** | Lights, climate and scenes through Home Assistant; windows, volume, media keys, clipboard, screenshots and app launching on this machine. |
| **Proactive** | Reminders, a morning briefing, teachable routines (with schedules), pattern mining, relationship nudges, open-thread tracking, and an initiative engine that suggests work under an autonomy ceiling you set. |
| **Phone** | An optional second listener on your LAN serving a small read-and-approve page, so you can answer an approval from your phone. It physically cannot start anything — the routes that could are not mounted on it. |

## Safety model

The interesting part, and it is structural rather than prompted:

- **No write runs without approval.** `registry.execute_tool()` refuses any
  WRITE/DESTRUCTIVE tool call that is not approved, and every attempt —
  executed *and* blocked — is written to `ActivityLog`.
- **Approval is bound to exact parameters.** A replan produces a new signature
  and asks again. Approval never transfers to an action you have not seen.
- **Grounding locks.** A recipient, a calendar event id, a browse origin, an
  upload path or a form value must trace to *your* words or your own data.
  Page and email content are excluded from that corpus by construction, so a
  prompt injection buried in a web page cannot address an email or navigate you
  somewhere you never named.
- **Path guards.** A step whose target provably does not exist fails into the
  replan loop *before* you are asked to approve a guessed path.
- **Everything proactive re-derives its plan.** A routine, a schedule and an
  accepted suggestion all store a goal *string*, never a plan — so the approval
  gate re-applies every time. "Automatic" means auto-PLAN, never auto-WRITE.

## Tech stack

| Layer | Technology |
|-------|-----------|
| Desktop shell | Electron 28 |
| Frontend | React 18 + TypeScript + Tailwind CSS + Vite + Zustand |
| Backend | Python 3.11 + FastAPI + Uvicorn |
| Agent planning | LangGraph |
| Database | SQLite (async, SQLAlchemy 2) + Alembic (22 migrations, auto-applied at startup) |
| Vector DB | Qdrant (local, embedded) + fastembed (`bge-small`, local) |
| Scheduling | APScheduler |
| Browser | Playwright 1.48 (real Chromium, headed) |
| Voice | faster-whisper (STT) · kokoro-onnx (TTS) · openWakeWord |
| LLM | Any of DeepSeek · Gemini · Groq · OpenRouter · Ollama |

## Prerequisites

- [Node.js 20+](https://nodejs.org/)
- [Python 3.11+](https://www.python.org/)
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) — for Qdrant.
  Furi runs without it and falls back to non-vector search, but memory and file
  search are much weaker.

## Quick start

### 1. Clone and configure

```bash
git clone <repo-url>
cd "jarvis 2.0"
cp .env.example .env
# Edit .env: set LLM_PROVIDER and the matching API key (see below)
```

### 2. Start Qdrant

```bash
docker-compose up -d
# Verify: http://localhost:6333/dashboard
```

### 3. Python backend

```bash
cd backend
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # macOS/Linux
pip install -r requirements.txt
```

### 4. Node dependencies

```bash
cd ..
npm run install:all
```

### 5. Run

```bash
npm run dev
```

That starts the backend, the Vite dev server and Electron together, and waits
for the backend's health check before opening the window.

| Command | When |
|---|---|
| `npm run dev` | Day to day. Uvicorn runs **without** `--reload`. |
| `npm run dev:watch` | Only while editing Python. `--reload` costs a second process and reloads ~1.5 GB of voice models on every file touch. |
| `npm run stop` | After `Ctrl+C`. Sweeps stranded processes, then **re-checks the ports** and exits non-zero if one is still held. |
| `npm run typecheck` | Frontend gate. |
| `npm run build` | Production build (Vite + electron-builder). |

> `npm run lint` currently fails — there is no ESLint config in `frontend/`.
> The real frontend gates are `npm run typecheck` and `npx vite build`.

### 6. Verify

- Backend health: `http://localhost:8000/health`
- Qdrant dashboard: `http://localhost:6333/dashboard`
- The Electron window opens on the Furi chat interface

> **API auth:** every endpoint except `/health` requires a static token
> (`X-Jarvis-Token` header; `?token=` on the WebSocket). The backend generates
> it at first startup in `~/.jarvis/auth_token` and Electron injects it
> automatically. For plain-browser dev (no Electron), copy that file's value
> into `frontend/.env.local` as `VITE_JARVIS_TOKEN=...`.
>
> The header and path still say *jarvis*: the rename to Furi was deliberately
> cosmetic, so no existing install loses its token, its Google session or its
> data. See the note at the top of `CLAUDE.md`.

## Choosing an LLM provider

Edit `.env` and restart the backend. No code changes.

```env
# DeepSeek — what this build is developed and measured against
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=your_key
DEEPSEEK_MODEL=deepseek-v4-flash

# Gemini (the built-in default in config.py)
LLM_PROVIDER=gemini
GEMINI_API_KEY=your_key

# Groq
LLM_PROVIDER=groq
GROQ_API_KEY=your_key

# Local Ollama — nothing leaves the machine
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.2
```

The status bar shows the provider and model actually in use, read from
`/health` — not from a setting that might disagree with it.

## Optional extras

All off by default. Each degrades cleanly when it is missing.

| Feature | What it needs |
|---|---|
| Web search | `TAVILY_API_KEY` in `.env`. Without it, search falls back to keyless DuckDuckGo scraping, which is much thinner. |
| Google (mail + calendar) | `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`, then **Settings → Google account → Connect**. Least-privilege scopes: read, compose, send, calendar events. Never delete or relabel. |
| Home Assistant | Hub URL + a long-lived token in **Settings → Home & devices**. The token is stored outside the database. |
| Voice on GPU | `onnxruntime-gpu` + `nvidia-cudnn-cu12` + `nvidia-cublas-cu12`, installed `--no-deps` (see the notes in `requirements.txt`). Measured on an RTX 3060: STT 169 s → 631 ms, TTS 13.7 s → 1.0 s. Plain CPU install works and falls back automatically. |
| Screen sensing (OCR) | `pip install rapidocr-onnxruntime`, then enable it in **Settings → Context & sensing**. Nothing sensed is ever written to disk. |
| Phone surface | `REMOTE_ENABLED=true` in `.env`, then pair the device from **Settings** by scanning the QR. |

## Repo layout

```
jarvis 2.0/
├── electron/            # Main process, preload bridge, tray, sensing helper
├── frontend/            # React + Vite UI
│   └── src/
│       ├── components/   # chat, voice, memory, contacts, timeline, settings, …
│       ├── stores/       # Zustand state
│       ├── lib/          # typed API client, voice pipeline, push channel
│       └── types/        # shared TypeScript types
├── backend/
│   ├── app/
│   │   ├── api/          # route handlers + the chat router chain
│   │   ├── agents/       # LangGraph planner, approval gate, traces
│   │   ├── browser/      # session, DOM observation, commit flow, grounding
│   │   ├── tools/        # the 48 tool implementations
│   │   ├── memory/       # extraction, identity resolution, budget, archive
│   │   ├── integrations/ # Google, Home Assistant
│   │   ├── providers/    # LLM provider abstraction
│   │   ├── core/         # config, scheduler, push, voice, sensing, housekeeping
│   │   └── db/           # SQLAlchemy models, Qdrant client, migrations
│   ├── alembic/          # 22 migrations, applied automatically at startup
│   ├── scripts/          # scored benches, measurement probes, runtime verifiers
│   └── tests/            # 3,966 tests
└── docker-compose.yml
```

## Testing

```bash
cd backend
venv\Scripts\python -m pytest tests/ -q            # full suite
venv\Scripts\python -m pytest tests/test_memory.py -v
```

`pytest.ini` sets `asyncio_mode = auto` and collects `backend/tests/` only.

Beyond the unit suite there are **scored benches** that run the real components
against real inputs, because a green unit suite has more than once failed to see
a feature that never fired:

```bash
cd backend
venv\Scripts\python -u scripts\route_bench.py --gate-only   # routing, free + deterministic
venv\Scripts\python -u scripts\plan_bench.py                # real planner on a real sandbox
venv\Scripts\python -u scripts\browse_bench.py              # real browser on real sites
```

`plan_bench` checks two things on every case that no mock can: that no write
ever succeeded before approval (in the audit log *and* on the filesystem at the
moment the card appears), and that nothing escaped the sandbox.

## Schema migrations

```bash
cd backend
venv\Scripts\python -m alembic revision --autogenerate -m "describe change"
venv\Scripts\python -m alembic upgrade head
```

Migrations run automatically at startup — nobody runs Alembic by hand on a
desktop app — and a drift check logs a CRITICAL line if any ORM column is
missing from the live database.

## Environment variables

See [`.env.example`](.env.example) for the full list with descriptions.

## License

Private — All rights reserved.
