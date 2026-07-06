# Jarvis OS

> A personal AI operating system for Windows/macOS/Linux — your intelligent digital extension.

## Overview

Jarvis OS is a desktop AI agent that remembers you, learns your preferences, and executes real-world tasks using tools across files, email, calendar, browser, and terminal.

**This is not a chatbot. This is a personal AI operating system.**

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Desktop Shell | Electron 28 |
| Frontend | React 18 + TypeScript + Tailwind CSS + Vite |
| Backend | Python 3.11 + FastAPI |
| Agent Planning | LangGraph |
| Database | SQLite (local) |
| Vector DB | Qdrant (local Docker) |
| LLM (default) | Gemini 2.0 Flash |
| LLM (alternatives) | Groq, OpenRouter, Ollama |

## Prerequisites

- [Node.js 20+](https://nodejs.org/)
- [Python 3.11+](https://www.python.org/)
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (for Qdrant)

## Quick Start

### 1. Clone and configure

```bash
git clone <repo-url>
cd jarvis-os
cp .env.example .env
# Edit .env and fill in your GEMINI_API_KEY
```

### 2. Start Qdrant (vector database)

```bash
docker-compose up -d
# Verify: http://localhost:6333/dashboard
```

### 3. Set up Python backend

```bash
cd backend
python -m venv venv

# Windows
venv\Scripts\activate

# macOS/Linux
source venv/bin/activate

pip install -r requirements.txt
```

### 4. Set up Node.js dependencies

```bash
cd ..  # back to jarvis-os root
npm run install:all
```

### 5. Run everything

```bash
# Terminal 1 — Backend
cd backend && uvicorn main:app --reload --port 8000

# Terminal 2 — Frontend + Electron
npm run dev
```

Or use the combined script (requires all prerequisites):

```bash
npm run dev
```

### 6. Verify

- Backend health: `http://localhost:8000/health`
- Qdrant dashboard: `http://localhost:6333/dashboard`
- Electron window opens with the Jarvis OS chat interface

## Switching LLM Providers

Edit `.env`:

```env
# Use Gemini (default)
LLM_PROVIDER=gemini
GEMINI_API_KEY=your_key

# Use Groq
LLM_PROVIDER=groq
GROQ_API_KEY=your_key

# Use local Ollama
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.2
```

Restart the backend. No code changes needed.

## Architecture

```
jarvis-os/
├── electron/        # Electron main process + IPC bridge
├── frontend/        # React + Vite UI
│   └── src/
│       ├── components/  # Chat, Memory, Contacts, Timeline, Voice
│       ├── stores/      # Zustand state management
│       ├── lib/         # Typed API client
│       └── types/       # Shared TypeScript types
├── backend/         # FastAPI Python backend
│   └── app/
│       ├── api/         # Route handlers
│       ├── agents/      # LangGraph agent definitions (Phase 3)
│       ├── tools/       # Tool implementations (Phase 3)
│       ├── memory/      # Memory engine (Phase 2)
│       ├── providers/   # LLM provider abstraction
│       ├── db/          # SQLite models + Qdrant client
│       └── core/        # Config, DI, base classes
└── docker-compose.yml
```

## Build Phases

| Phase | Status | Description |
|-------|--------|-------------|
| Phase 1 | ✅ Complete | Foundation: Electron + React + FastAPI + SQLite + Qdrant + Streaming Chat |
| Phase 2 | 🚧 In progress | Memory Engine: Semantic, Relational, Episodic + dual-perspective shared facts, identity resolution |
| Phase 3 | 🔜 Planned | Tool System + LangGraph Planner |
| Phase 4 | 🔜 Planned | Email + Calendar (Communication Layer) |
| Phase 5 | 🔜 Planned | Browser Automation + File Intelligence |
| Phase 6 | 🔜 Planned | Voice Layer (Whisper + Piper) |
| Phase 7 | 🔜 Planned | Polish, Tests, Docker, Production Build |

## Environment Variables

See [`.env.example`](.env.example) for the full list with descriptions.

## License

Private — All rights reserved.
