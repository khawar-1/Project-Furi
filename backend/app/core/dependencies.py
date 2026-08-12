"""
Furi OS — Dependency Injection
FastAPI dependency providers for database sessions, Qdrant, and LLM providers.
"""
from typing import AsyncIterator, Optional

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import AsyncSessionLocal
from app.db.qdrant_client import get_qdrant_client
from app.providers.factory import create_provider
from app.providers.base import LLMProvider


async def get_db() -> AsyncIterator[AsyncSession]:
    """Provide an async SQLAlchemy session, auto-closing on exit."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def get_qdrant():
    """Provide the Qdrant async client (may be None if unavailable)."""
    return get_qdrant_client()


async def get_llm_provider() -> LLMProvider:
    """Provide the configured LLM provider instance."""
    return create_provider()
