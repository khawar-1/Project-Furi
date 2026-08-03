"""
Jarvis OS — Backend Entry Point
Now includes memory API routes, contacts, episodes, preferences.
"""
import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from app.core.auth import AuthMiddleware, get_or_create_token
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
from app.api import voice as voice_api
from app.api import context as context_api
from app.api import initiative as initiative_api
from app.api import threads as threads_api
from app.api import browser as browser_api
from app.api import autofill as autofill_api
import app.core.reminders  # noqa: F401 — registers the "reminder" job handler at import time
import app.core.birthdays  # noqa: F401 — registers the "birthday" job handler at import time
import app.core.daily_briefing  # noqa: F401 — registers the "daily_briefing" job handler at import time
import app.core.reindex  # noqa: F401 — registers the "reindex" job handler at import time
import app.core.initiative  # noqa: F401 — registers the "initiative" job handler at import time
import app.core.scheduled_routines  # noqa: F401 — registers the "routine" job handler at import time

# Persistent log file (2026-07-19): the dev terminal was the only record, and two
# browser-flow diagnoses in a row died on lost scrollback — a backend that dies
# mid-turn must leave evidence that survives it. Added at IMPORT time (not the
# lifespan) so even a crash during startup is captured. enqueue=True keeps the
# sink thread-safe across the browser/TTS worker threads. Best-effort: failing
# to open the file must never block the app.
try:
    from pathlib import Path as _Path
    _LOG_DIR = _Path.home() / ".jarvis" / "logs"
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger.add(
        _LOG_DIR / "backend.log",
        rotation="10 MB",
        retention=5,
        level="INFO",
        enqueue=True,
        backtrace=False,
        diagnose=False,
    )
except Exception:  # pragma: no cover — logging must never take the app down
    pass


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown lifecycle management."""
    logger.info("🚀 Jarvis OS backend starting...")

    # Register CUDA DLL directories FIRST, before anything imports onnxruntime
    # or the voice engines — the GPU voice path (ctranslate2 STT / onnxruntime
    # TTS) resolves cuDNN/cuBLAS/cudart from here. Best-effort, no-op without a
    # GPU (the CPU path stays the fallback). See app/core/gpu_bootstrap.py.
    try:
        from app.core.gpu_bootstrap import register_cuda_dll_dirs
        register_cuda_dll_dirs()
    except Exception as e:
        logger.warning(f"⚠️  CUDA DLL registration failed (voice uses CPU): {e}")

    # Ensure the API auth token exists BEFORE /health goes green — Electron
    # reads ~/.jarvis/auth_token right after its health poll succeeds. A
    # failure here never blocks startup: the middleware fails closed and the
    # 401s make the problem visible.
    try:
        get_or_create_token()
    except Exception as e:
        logger.error(f"⚠️  Auth token setup failed (requests will be denied): {e}")

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

    # Phase 7 (browser refactor): encrypt any autofill SECRET still stored as
    # plaintext at rest. Idempotent — a no-op once every secret carries the
    # dpapi: prefix, and a no-op on a host without DPAPI. Best-effort.
    try:
        from app.core.autofill import encrypt_plaintext_secrets
        from app.db.database import AsyncSessionLocal
        async with AsyncSessionLocal() as session:
            migrated = await encrypt_plaintext_secrets(session)
        if migrated:
            logger.info(f"🔒 Encrypted {migrated} plaintext autofill secret(s) at rest")
    except Exception as e:
        logger.warning(f"⚠️  Autofill secret encryption pass failed (non-critical): {e}")

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
    # Reference kept on app.state: an unreferenced asyncio task may be
    # garbage-collected mid-flight, silently cancelling the warmup.
    app.state.embedder_warmup = asyncio.create_task(_prewarm_embedder())

    # Pre-warm the browser stack's heavy imports. Measured live 2026-07-21: the
    # FIRST browse paid ~56s importing google.generativeai (the vision provider's
    # grpc/protobuf tree, cold) plus the Playwright import INSIDE its own browse
    # time budget — enough, on a loaded machine, to blow the whole budget before
    # Chrome ever opened. Imports are process-wide and cached, so paying them
    # here (a worker thread — module import is mostly disk I/O, which releases
    # the GIL) makes the first browse start as fast as every later one.
    # Best-effort; gated so a box without the deps or without a vision key pays
    # nothing.
    async def _prewarm_browser_stack():
        def _import_heavy():
            try:
                import playwright.async_api  # noqa: F401
            except Exception:
                pass
            if settings.VISION_API_KEY or settings.GEMINI_API_KEY:
                try:
                    import google.generativeai  # noqa: F401
                except Exception:
                    pass
        try:
            await asyncio.to_thread(_import_heavy)
            logger.info("✅ browser stack imports pre-warmed")
            # Actually START Playwright's Node driver ONCE, on the dedicated
            # browser loop, so the cold subprocess spawn (and any stall) happens
            # HERE in the background — never inside a user's first browse chat turn
            # (the 2026-07-22 "processing forever" incident). ensure_playwright_driver
            # is bounded + logs its own timing; every later launch reuses this driver.
            from app.core import browser_runtime, browser_session
            await browser_runtime.run_browser(
                browser_session.ensure_playwright_driver()
            )
            logger.info("✅ browser driver pre-warmed")
        except Exception as e:
            logger.warning(f"⚠️  browser stack pre-warm failed (non-critical): {e}")

    app.state.browser_prewarm = asyncio.create_task(_prewarm_browser_stack())

    # Pre-warm the LLM — ONLY on Ollama. A local model cold-loads into VRAM on
    # the first request (many seconds on a laptop GPU), so without this the
    # first real chat/classify call absorbs that latency. Gated on provider so
    # a cloud provider never spends quota on boot — the whole reason we moved
    # to local. Best-effort; a 1-token chat is enough to trigger the load.
    async def _prewarm_llm():
        try:
            from app.providers.base import LLMMessage
            from app.providers.factory import create_provider
            provider = create_provider()
            await provider.chat([LLMMessage(role="user", content="hi")], max_tokens=1)
            logger.info(f"✅ Ollama model '{provider.model_name}' pre-warmed and ready")
        except Exception as e:
            logger.warning(f"⚠️  LLM pre-warm failed (non-critical): {e}")

    if settings.LLM_PROVIDER.strip().lower() == "ollama":
        app.state.llm_warmup = asyncio.create_task(_prewarm_llm())

    # Phase 7 Parts 1+3: warm-load the local voice models when voice is
    # enabled, so the first push-to-talk (and the first spoken reply) after a
    # restart never waits for a model load. Both ensure_* calls kick a
    # background task and return immediately (each module keeps its task
    # ref); best-effort like every warmup here.
    try:
        from app.core.app_settings import get_voice_config
        from app.core.voice_stt import ensure_model_loaded
        from app.core.voice_tts import ensure_engine_loaded
        from app.db.database import AsyncSessionLocal as _VoiceSessionLocal
        async with _VoiceSessionLocal() as session:
            voice_config = await get_voice_config(session)
        if voice_config.enabled:
            await ensure_model_loaded(
                voice_config.stt_model,
                voice_config.stt_device,
                voice_config.stt_compute_type,
            )
            logger.info(
                f"🎙️ Voice STT model '{voice_config.stt_model}' loading in background "
                f"(device={voice_config.stt_device})"
            )
            if voice_config.output_enabled:
                await ensure_engine_loaded(voice_config.tts_device)
                logger.info(
                    f"🔊 Voice TTS engine (Kokoro) loading in background "
                    f"(device={voice_config.tts_device})"
                )
    except Exception as e:
        logger.warning(f"⚠️  Voice warm-load failed (non-critical): {e}")

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

    # Phase 9: reconcile the initiative heartbeat — arm it when the engine is
    # enabled, heal a fire/re-arm crash, sweep strays, expire stale suggestions.
    # After scheduler.start() so its timer is live; non-critical.
    try:
        from app.core.initiative import ensure_initiative_job
        await ensure_initiative_job()
    except Exception as e:
        logger.warning(f"⚠️  Initiative-job reconciliation failed (non-critical): {e}")

    # Phase 10.2: reconcile scheduled-routine jobs — arm scheduled routines,
    # heal a fire/re-arm crash, sweep orphans. After scheduler.start(); non-critical.
    try:
        from app.core.scheduled_routines import ensure_routine_schedule_jobs
        await ensure_routine_schedule_jobs()
    except Exception as e:
        logger.warning(f"⚠️  Routine-schedule reconciliation failed (non-critical): {e}")

    # Phase 14: clear any Chromium a PRIOR backend left holding the shared
    # ~/.jarvis/browser profile lock (a sign-in window not closed on shutdown, a
    # context leaked by a crash) — else the first browse's launch hangs on it
    # (2026-07-20 incident). Scoped to processes whose command line names that
    # exact profile, so the user's everyday Chrome is never touched. Pure
    # subprocess work → off the loop; best-effort, non-critical.
    try:
        from app.core.browser_session import reclaim_orphaned_profile
        await asyncio.to_thread(reclaim_orphaned_profile)
    except Exception as e:
        logger.debug(f"browser profile reclaim (startup): {e}")

    # The startup purge above runs ONCE; this keeps it true while the backend
    # stays up (2026-08-03). A desktop backend can run for days, and an
    # abandoned approval/pause left its Task row counted as an active worker
    # long after its parked plan expired. Best-effort, cancelled on shutdown.
    try:
        from app.core.housekeeping import start_housekeeping
        start_housekeeping()
    except Exception as e:
        logger.warning(f"⚠️  Housekeeping sweep could not start (non-critical): {e}")

    logger.info(f"🤖 LLM Provider: {settings.LLM_PROVIDER}")
    logger.info(f"🌐 Backend ready at http://{settings.BACKEND_HOST}:{settings.BACKEND_PORT}")

    yield

    logger.info("🛑 Jarvis OS backend shutting down...")
    # Stop the sweep FIRST — it opens DB sessions, and a pass starting while
    # the rest of shutdown runs would race the teardown it cannot see.
    try:
        from app.core.housekeeping import stop_housekeeping
        await stop_housekeeping()
    except Exception as e:
        logger.debug(f"housekeeping shutdown: {e}")
    await scheduler.shutdown()

    # Phase 14: close every browser window Jarvis has open BEFORE stopping the
    # loop, so a clean shutdown never orphans a Chromium holding the profile lock
    # (the orphan that hangs the next run's launch). Order: close windows (on the
    # browser loop) → reclaim as a guaranteed sweep (covers the clean-subprocess
    # sign-in windows + any leaked context) → stop the loop. Two independent
    # best-effort steps so a stuck window-close never skips the reclaim sweep, and
    # the close is time-bounded so it can never wedge process exit.
    try:
        from app.core import browser_runtime, browser_session
        if browser_runtime.is_running():
            await browser_runtime.run_browser(
                browser_session.shutdown_browser_windows(), timeout=30.0
            )
            # Stop the SHARED Playwright driver (warmed once at startup) on the
            # browser loop before the loop is torn down — it is loop-bound.
            await browser_runtime.run_browser(
                browser_session.stop_playwright_driver(), timeout=15.0
            )
    except Exception as e:
        logger.debug(f"browser window close (shutdown): {e}")
    try:
        from app.core.browser_session import reclaim_orphaned_profile
        await asyncio.to_thread(reclaim_orphaned_profile)
    except Exception as e:
        logger.debug(f"browser profile reclaim (shutdown): {e}")

    # Then stop the dedicated browser loop thread (best-effort; it is a daemon, so
    # a missed stop never blocks exit).
    try:
        from app.core.browser_runtime import shutdown_browser_runtime
        shutdown_browser_runtime()
    except Exception as e:
        logger.debug(f"browser runtime shutdown: {e}")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Jarvis OS API",
        description="Personal AI Operating System — Backend API",
        version=settings.APP_VERSION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # Static-token auth gate (2026-07-15) — added BEFORE CORS in code so CORS
    # is the OUTERMOST middleware (add_middleware prepends): a browser-dev 401
    # still carries CORS headers and is readable by the renderer. /health and
    # OPTIONS preflight are exempt inside the middleware itself.
    app.add_middleware(AuthMiddleware)

    # CORS — allow Electron renderer and Vite dev server
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # /api/voice/speak/stream carries its PCM sample rate in a custom
        # header — without exposing it the cross-origin Vite renderer reads
        # null and would guess the rate.
        expose_headers=["X-Sample-Rate", "X-Audio-Format"],
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

    # Phase 7 Part 1 — local voice (push-to-talk transcription)
    app.include_router(voice_api.router, prefix="/api/voice", tags=["Voice"])

    # Phase 8 — the Context Layer (device/screen sensing + world model)
    app.include_router(context_api.router, prefix="/api/context", tags=["Context"])

    # Phase 9 — the Initiative Engine (proactive suggestion feed + settings)
    app.include_router(initiative_api.router, prefix="/api/initiative", tags=["Initiative"])

    # Phase 11 — goal threads (ongoing-concern tracking)
    app.include_router(threads_api.router, prefix="/api/threads", tags=["Threads"])

    # Phase 14 — browser media control (stop a browse window left playing)
    app.include_router(browser_api.router, prefix="/api/browser", tags=["Browser"])

    # Autofill profile — the grounded data source for browser form-filling (15.2)
    app.include_router(autofill_api.router, prefix="/api/autofill", tags=["Autofill"])

    return app


app = create_app()
