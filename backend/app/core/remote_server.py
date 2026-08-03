"""
Jarvis OS — Running the remote listener inside the main process (2026-08-03)

A second `uvicorn.Server` as a lifespan-owned asyncio task, beside
`start_housekeeping()`. It inherits the process, its database, its scheduler and
its shutdown — there is no second backend to install, package or keep alive, and
nothing about the Electron build changes.

⚠️ NOT A SCHEDULER JOB, for the same reason housekeeping is not: SQLite is the
truth for FEATURES that must survive a restart. A listener is not one of those —
it exists exactly as long as the process does.

Default OFF (`settings.REMOTE_ENABLED`), the convention every reaching-outward
capability in this codebase follows.

⚠️ THE FLAG IS THE SWITCH, NOT THE SAFETY. What makes this safe to expose is
that the app it serves has only the manifest's routes on it — see
`remote_app.create_remote_app`. Turning the flag on cannot widen that.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from loguru import logger

_server = None
_task: Optional[asyncio.Task] = None


async def start_remote_listener(source_app) -> None:
    """Arm the remote listener if it is enabled. Best-effort: a failure to bind
    logs and leaves the main app entirely unaffected."""
    global _server, _task
    from app.core.config import settings

    if not settings.REMOTE_ENABLED:
        return
    if _task is not None and not _task.done():
        return  # idempotent, like start_housekeeping
    try:
        import uvicorn

        from app.core.remote_app import create_remote_app

        config = uvicorn.Config(
            create_remote_app(source_app),
            host=settings.REMOTE_HOST,
            port=settings.REMOTE_PORT,
            log_level="warning",
            # This app is mounted into a process that has ALREADY started up —
            # running a lifespan again would re-run migrations, re-arm the
            # scheduler and re-warm the models.
            lifespan="off",
        )
        _server = uvicorn.Server(config)
        _task = asyncio.get_running_loop().create_task(_server.serve())
        logger.info(
            f"📱 Remote surface listening on {settings.REMOTE_HOST}:{settings.REMOTE_PORT} "
            "(read + approve + answer only)"
        )
    except Exception as e:
        logger.warning(f"⚠️  Remote listener could not start (non-critical): {e}")
        _server, _task = None, None


async def stop_remote_listener() -> None:
    """Ask it to exit and WAIT, so shutdown never leaves a task pending on a
    loop that is about to close — the stop_housekeeping rule."""
    global _server, _task
    server, task = _server, _task
    _server, _task = None, None
    if server is not None:
        server.should_exit = True
    if task is None or task.done():
        return
    try:
        await asyncio.wait_for(task, timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
        task.cancel()
