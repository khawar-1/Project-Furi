---
name: verify
description: Runtime-verify a Jarvis OS backend change by driving the real FastAPI + planner surface with an isolated backend instance (scratch DB, alternate port).
---

# Verifying Jarvis OS backend changes at the runtime surface

The backend's real surface is the FastAPI API on 127.0.0.1 (the Electron
app is just a client). Planner/agent changes are observable through
`POST /api/agent/execute` without launching Electron at all.

## Isolated launch (never touch the real jarvis.db)

The production DB, scheduler jobs, and reminders live in
`backend/jarvis.db` — a second instance against it can double-fire
scheduled jobs. Always point a verification instance at a scratch DB and
an alternate port:

```powershell
cd "C:\Users\DELL\Desktop\jarvis 2.0\backend"
$env:DATABASE_URL = "sqlite+aiosqlite:///C:/path/to/scratch/verify.db"   # dir must EXIST first
venv\Scripts\python -m uvicorn main:app --port 8001 --log-level info
```

- The scratch directory must exist or startup dies with
  `sqlite3.OperationalError: unable to open database file`.
- cwd must stay `backend/` (config loads `../.env`; embedded Qdrant opens
  `./qdrant_data` — fine to share for reads while the real app is closed).
- Check the real app isn't running first: `Get-NetTCPConnection -LocalPort 8000`.
- Health: `GET http://127.0.0.1:8001/health` (shows provider + component status).

## Driving the planner

```powershell
$body = @{ goal = "..."; session_id = "verify-x" } | ConvertTo-Json
Invoke-WebRequest -Uri "http://127.0.0.1:8001/api/agent/execute" -Method POST `
  -Body $body -ContentType "application/json" -UseBasicParsing -TimeoutSec 240
```

- NEVER approve a write/destructive plan against real user files. Clean up
  every awaiting plan: `POST /api/agent/approve` with `approved = $false`.
- `GET /api/activity/{session_id}` shows the audited tool calls (including
  the question gate's own searches).
- The LLM is nondeterministic: a lazy/misbehaving path may need a baiting
  goal (e.g. "ask me for its full path first, then ...") to reproduce; the
  backend log (loguru) is where structural guards announce themselves
  ("Question gate: rejected ...", "Plan paused ...").
- Mind the LLM quota — each /execute costs 2–5 provider calls (draft,
  reflect, refine/revise rounds).

## Gotchas

- Kill the instance when done (`Get-NetTCPConnection -LocalPort 8001` →
  `Stop-Process`); it holds the Qdrant embedded-storage lock.
- Windows PowerShell 5.1: no `&&`; JSON via `ConvertTo-Json`; responses
  print with mangled unicode (— becomes â) — cosmetic only.
- Never append `2>$null` to pytest/uvicorn: PS 5.1 wraps native stderr in
  ErrorRecords and corrupts the reported exit code (a fully-green pytest
  run came back as exit 45). Let stderr flow; it is captured anyway.
- Groq quota is 100k tokens/day and each planner call is ~2-4k with the
  tool schemas — check the remaining budget before a live run; a 429
  mid-verify reads like a plan failure but is external.
