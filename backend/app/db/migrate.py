"""
Jarvis OS — Startup schema migration + drift alarm (2026-07-13)

Live incident 2026-07-12: the production jarvis.db was missing the Phase 6
Part 4 `messages.embedded_at` column — the migration existed but was never
applied. init_db()'s create_all adds missing TABLES but never new COLUMNS,
so the drift was invisible: every Message INSERT failed "non-critically" and
chat history silently stopped persisting (and, via the poisoned session,
background tasks died too). Nobody runs `alembic upgrade head` by hand on a
desktop app — the app must migrate its own database.

Startup order (main.py lifespan):
1. ensure_schema()  — BEFORE init_db: bring an existing DB to alembic head.
2. init_db()        — create_all for brand-new tables (dev convenience, kept).
3. verify_schema()  — the alarm: compare live columns against the ORM models
   and log CRITICAL on any mismatch. This is what makes the silent-loss class
   impossible: even if a future migration is forgotten, boot says so loudly.

Three DB states, handled explicitly:
- Fresh/empty DB (no `messages` table): skip upgrade; create_all builds the
  full current schema, then we STAMP head so future upgrades apply cleanly.
- Existing DB, no alembic_version (a create_all-managed dev DB): stamp head —
  create_all has kept it current by contract; verify_schema() checks that.
- Existing DB with alembic_version: `alembic upgrade head`. Migrations must
  tolerate create_all racing ahead (guards inside the migration files).

Everything is best-effort: a migration failure logs CRITICAL and startup
continues — a degraded-but-running Jarvis beats a dead one, and the drift
alarm names exactly what is broken.
"""
import asyncio
from pathlib import Path

from loguru import logger
from sqlalchemy import text

_BACKEND_DIR = Path(__file__).resolve().parents[2]  # app/db/migrate.py → backend/


def _sync_db_url() -> str:
    from app.core.config import settings

    return settings.DATABASE_URL.replace("sqlite+aiosqlite", "sqlite")


def _alembic_config():
    from alembic.config import Config

    cfg = Config(str(_BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND_DIR / "alembic"))
    return cfg


def _db_state() -> str:
    """"fresh" | "unstamped" | "stamped" — decided with a short-lived sync
    engine so this works before the app's async engine sees any traffic."""
    from sqlalchemy import create_engine, inspect

    engine = create_engine(_sync_db_url())
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
    finally:
        engine.dispose()
    if "messages" not in tables:
        return "fresh"
    return "stamped" if "alembic_version" in tables else "unstamped"


def _upgrade_sync() -> str:
    """The blocking half, run in a worker thread. Returns what was done."""
    from alembic import command

    state = _db_state()
    cfg = _alembic_config()
    if state == "fresh":
        # create_all (init_db, next in the lifespan) builds the full current
        # schema; stamping head now means future boots take the upgrade path.
        command.stamp(cfg, "head")
        return "fresh database — stamped head; create_all builds the schema"
    if state == "unstamped":
        # A dev DB create_all has managed since before alembic. Its schema is
        # assumed current (verify_schema is the check on that assumption).
        command.stamp(cfg, "head")
        return "unstamped database — stamped head (create_all-managed)"
    command.upgrade(cfg, "head")
    return "upgraded to head"


async def ensure_schema() -> None:
    """Bring the database to the current migration head. Never raises."""
    try:
        outcome = await asyncio.to_thread(_upgrade_sync)
        logger.info(f"✅ Schema migrations: {outcome}")
    except Exception as e:
        logger.critical(
            f"Schema migration FAILED — the app will run against a possibly "
            f"outdated schema and features may silently degrade. Fix with "
            f"`alembic upgrade head` from backend/. Error: {e}"
        )


async def verify_schema(engine=None) -> list[str]:
    """The drift alarm: every ORM column must exist in the live database.
    Returns the missing ones as 'table.column' strings (and logs CRITICAL) —
    an empty list is the healthy case. Never raises. `engine` is a test seam;
    the app default is the shared async engine."""
    missing: list[str] = []
    try:
        import app.db.models  # noqa: F401 — register every model
        from app.db.database import Base
        if engine is None:
            from app.db.database import engine

        async with engine.connect() as conn:
            for table in Base.metadata.sorted_tables:
                rows = await conn.execute(
                    text(f'PRAGMA table_info("{table.name}")')
                )
                live_columns = {r[1] for r in rows}
                if not live_columns:
                    missing.append(f"{table.name} (entire table)")
                    continue
                for column in table.columns:
                    if column.name not in live_columns:
                        missing.append(f"{table.name}.{column.name}")
    except Exception as e:
        logger.warning(f"Schema verification failed (non-critical): {e}")
        return []
    if missing:
        logger.critical(
            f"SCHEMA DRIFT — the database is missing: {', '.join(missing)}. "
            f"Writes touching these will fail. Run `alembic upgrade head` "
            f"from backend/ (startup auto-migration should have done this — "
            f"check the log above for its error)."
        )
    return missing
