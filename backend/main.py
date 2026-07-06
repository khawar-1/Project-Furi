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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown lifecycle management."""
    logger.info("🚀 Jarvis OS backend starting...")

    # Initialize SQLite database (creates tables if not exist)
    await init_db()
    logger.info("✅ SQLite database initialized")

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

    logger.info(f"🤖 LLM Provider: {settings.LLM_PROVIDER}")
    logger.info(f"🌐 Backend ready at http://{settings.BACKEND_HOST}:{settings.BACKEND_PORT}")

    yield

    logger.info("🛑 Jarvis OS backend shutting down...")


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

    return app


app = create_app()
