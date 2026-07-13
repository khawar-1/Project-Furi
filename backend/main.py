"""
Jarvis OS — Backend Entry Point
Now includes memory API routes, contacts, episodes, preferences.
"""
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from app.core.config import settings
from app.db.database import init_db
from app.db.qdrant_client import init_qdrant
from app.api import health, chat, memory
from app.api import contacts, episodes, preferences
from app.api import agent, activity
from app.api import ws, schedule, reminders, tasks
from app.api import integrations, settings as settings_api
from app.api import index as index_api
from app.api import routines as routines_api
import app.core.reminders  # noqa: F401 — registers the "reminder" job handler at import time
import app.core.birthdays  # noqa: F401 — registers the "birthday" job handler at import time
import app.core.daily_briefing  # noqa: F401 — registers the "daily_briefing" job handler at import time
import app.core.reindex  # noqa: F401 — registers the "reindex" job handler at import time


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown lifecycle management."""
    logger.info("🚀 Jarvis OS backend starting...")

    # Apply schema migrations FIRST (2026-07-13): nobody runs alembic by hand
    # on a desktop app. create_all below only adds missing TABLES — a new
    # COLUMN on an existing table lands only here (live incident: a missing
    # messages.embedded_at silently killed chat history AND background tasks).
    from app.db.migrate import ensure_schema, verify_schema
    await ensure_schema()

    # Initialize SQLite database (creates tables if not exist)
    await init_db()
    logger.info("✅ SQLite database initialized")

    # The drift alarm: any ORM column missing from the live DB is a CRITICAL
    # log line at boot — this class of silent failure must never hide again.
    await verify_schema()

    # Phase 3.5: drop parked plans / pending questions whose 24h window passed.
    # Phase 4 Part 5: then reconcile background tasks against reality — a Task
    # still `running` was killed by the restart, and a paused Task whose
    # parked plan just got purged can never be answered (order matters:
    # purge first, so fail_interrupted_tasks sees the surviving rows only).
    try:
        from app.agents import fail_interrupted_tasks, purge_expired_plans
        from app.db.database import AsyncSessionLocal
        from app.memory.session_persistence import purge_expired_pending_state
        async with AsyncSessionLocal() as session:
            await purge_expired_plans(session)
            await purge_expired_pending_state(session)
            await fail_interrupted_tasks(session)
    except Exception as e:
        logger.warning(f"⚠️  Expired-state purge failed (non-critical): {e}")

    # Initialize Qdrant connection (creates all Phase 2 collections)
    try:
        await init_qdrant()
        logger.info("✅ Qdrant vector database connected")
    except Exception as e:
        logger.warning(f"⚠️  Qdrant unavailable (vector memory disabled): {e}")

    # Pre-warm the fastembed embedding model in the background.
    # Without this, the FIRST chat message would block for ~2 min while
    # the model files download from HuggingFace. After the first run the
    # model is cached locally and this takes < 1 second on subsequent boots.
    async def _prewarm_embedder():
        try:
            from app.memory.embedder import embed_text
            await embed_text("warmup")
            logger.info("✅ fastembed model pre-warmed and ready")
        except Exception as e:
            logger.warning(f"⚠️  fastembed pre-warm failed (non-critical): {e}")

    import asyncio
    asyncio.create_task(_prewarm_embedder())

    # Phase 4: start the scheduler and rebuild timers from SQLite — jobs
    # whose run_at passed while the backend was down fire immediately (late).
    from app.core.scheduler import scheduler
    try:
        rehydrated = await scheduler.start()
        logger.info(f"✅ Scheduler started ({rehydrated} pending job(s) rehydrated)")
    except Exception as e:
        logger.warning(f"⚠️  Scheduler failed to start (timed jobs disabled): {e}")

    # Phase 5 Part 4: reconcile birthday reminders against the contacts table —
    # arm missing jobs (pre-Part-4 contacts, fire/re-arm crashes) and sweep
    # orphans. After scheduler.start() so its timers are live; non-critical.
    try:
        from app.core.birthdays import ensure_birthday_jobs
        await ensure_birthday_jobs()
    except Exception as e:
        logger.warning(f"⚠️  Birthday-job reconciliation failed (non-critical): {e}")

    # Phase 5 Part 6: reconcile the daily-briefing job — arm the default-on
    # 08:00 job on first boot, heal a crash between fire and re-arm, sweep
    # strays. After scheduler.start() so its timer is live; non-critical.
    try:
        from app.core.daily_briefing import ensure_briefing_job
        await ensure_briefing_job()
    except Exception as e:
        logger.warning(f"⚠️  Daily-briefing reconciliation failed (non-critical): {e}")

    # Phase 6 Part 3: reconcile the incremental-reindex job — arm it when the
    # file index is enabled, heal a fire/re-arm crash, sweep strays. After
    # scheduler.start() so its timer is live; non-critical.
    try:
        from app.core.reindex import ensure_reindex_job
        await ensure_reindex_job()
    except Exception as e:
        logger.warning(f"⚠️  Reindex-job reconciliation failed (non-critical): {e}")

    logger.info(f"🤖 LLM Provider: {settings.LLM_PROVIDER}")
    logger.info(f"🌐 Backend ready at http://{settings.BACKEND_HOST}:{settings.BACKEND_PORT}")

    yield

    logger.info("🛑 Jarvis OS backend shutting down...")
    await scheduler.shutdown()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Jarvis OS API",
        description="Personal AI Operating System — Backend API",
        version=settings.APP_VERSION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # CORS — allow Electron renderer and Vite dev server
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Phase 1 routes
    app.include_router(health.router, prefix="/health", tags=["Health"])
    app.include_router(chat.router, prefix="/chat", tags=["Chat"])
    app.include_router(memory.router, prefix="/memory", tags=["Memory"])

    # Phase 2 routes
    app.include_router(contacts.router, prefix="/api/contacts", tags=["Contacts"])
    app.include_router(episodes.router, prefix="/api/episodes", tags=["Episodes"])
    app.include_router(preferences.router, prefix="/api/preferences", tags=["Preferences"])

    # Phase 3 routes
    app.include_router(agent.router, prefix="/api/agent", tags=["Agent"])
    app.include_router(activity.router, prefix="/api/activity", tags=["Activity"])

    # Phase 4 routes — push channel (WebSocket at /ws) + scheduler + reminders
    app.include_router(ws.router, tags=["Push"])
    app.include_router(schedule.router, prefix="/api/schedule", tags=["Schedule"])
    app.include_router(reminders.router, prefix="/api/reminders", tags=["Reminders"])
    app.include_router(tasks.router, prefix="/api/tasks", tags=["Tasks"])

    # Phase 5 routes — external integrations (Google OAuth foundation)
    app.include_router(integrations.router, prefix="/api/integrations", tags=["Integrations"])
    # Phase 5 Part 6 — runtime app settings (daily briefing)
    app.include_router(settings_api.router, prefix="/api/settings", tags=["Settings"])

    # Phase 6 Part 2 — semantic file index (config + manual rebuild)
    app.include_router(index_api.router, prefix="/api/index", tags=["File Index"])

    # Phase 6 Part 5 — teachable routines (procedural memory)
    app.include_router(routines_api.router, prefix="/api/routines", tags=["Routines"])

    return app


app = create_app()
