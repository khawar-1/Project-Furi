"""
Jarvis OS — SQLite Database Setup
Async SQLAlchemy engine and session factory.
"""
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""
    pass


# Async engine — SQLite with WAL mode for better concurrent access
engine: AsyncEngine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    connect_args={
        "check_same_thread": False,
        "timeout": 30,
    },
)

# Session factory used throughout the application
AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def init_db() -> None:
    """
    Create all tables defined via ORM models.
    Safe to call multiple times — uses CREATE TABLE IF NOT EXISTS.
    """
    # Import models so SQLAlchemy registers them before create_all
    import app.db.models  # noqa: F401

    async with engine.begin() as conn:
        # Enable WAL mode for SQLite (better read/write concurrency)
        await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    """Dispose engine connections on shutdown."""
    await engine.dispose()
