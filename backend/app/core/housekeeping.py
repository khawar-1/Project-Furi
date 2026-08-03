"""
Jarvis OS — Periodic housekeeping sweep (2026-08-03)

Expired state was reconciled ONLY at startup. On a desktop app that is a real
gap, because the backend is meant to stay up: a background task left waiting on
an approval card or a pause, and abandoned past its parked plan's 24h TTL, kept
its row forever. Un-continuable — its Continue button leads to an error — but
still counted as an active worker in the Agents panel and the status bar. The
database said one thing and the world said another, and nothing looked again
until the next restart.

This is the "look again" — the same three purges the lifespan already runs,
on a timer.

WHY A PLAIN ASYNCIO TASK AND NOT A SCHEDULER JOB
------------------------------------------------
Every recurring FEATURE in this codebase goes through JarvisScheduler, and for
good reason: SQLite is the truth and a job due while the backend slept must
still fire. None of that applies here. This sweep is meaningless across a
restart — startup already does the same work, more thoroughly (it also settles
runs the restart killed). Persisting a wake-up for it would buy nothing and
cost a job kind, an app_settings pointer and a reconcile of its own.

So: no persistence, no new table, no migration. Lifespan-owned, cancelled on
shutdown, and every pass is best-effort — a failed sweep logs and the next one
tries again. Housekeeping must never be able to take the backend down.

⚠️ THE ONE THING THIS MUST NOT DO is call fail_interrupted_tasks(), whose
`running` → `failed` half is true ONLY at startup. On a timer it would kill
every live agent mid-flight. reconcile_expired_task_plans() is the half that is
safe at any time, split out for exactly this reason.
"""
import asyncio
from typing import Optional

from loguru import logger

# 15 minutes: far below the 24h TTL it reconciles, far above anything that
# would matter for load (three indexed queries that normally match nothing).
SWEEP_INTERVAL_SECONDS = 900

_sweeper: Optional[asyncio.Task] = None


async def run_housekeeping_pass() -> None:
    """One sweep: drop expired parked plans and pending questions, settle any
    task row left pointing at a plan that no longer exists, and age out the
    three audit trails past their retention windows.

    Ordered like the lifespan's startup block — purge first, so the reconcile
    sees the surviving rows only. Each step is independently best-effort (the
    gather_briefing_sections rule): one failing source must not skip the
    others."""
    from app.agents import purge_expired_plans
    from app.agents.task_runner import reconcile_expired_task_plans
    from app.core.plan_trace import purge_old_plan_traces
    from app.core.routing_trace import purge_old_decisions
    from app.db.database import AsyncSessionLocal
    from app.memory.archive import sweep_memory_archive
    from app.memory.session_persistence import purge_expired_pending_state
    from app.tools.registry import purge_old_activity

    async with AsyncSessionLocal() as db:
        for name, step in (
            ("parked plans", purge_expired_plans),
            ("pending questions", purge_expired_pending_state),
            ("task rows", reconcile_expired_task_plans),
            # The routing audit trail writes one row per chat turn (2026-08-03).
            # Small, but unbounded without this — and retention is the price of
            # storing a copy of what the user typed.
            ("routing decisions", purge_old_decisions),
            # Its sibling one layer down: one row per planner invocation, saying
            # why a plan gave up (2026-08-03).
            ("plan traces", purge_old_plan_traces),
            # And the oldest unbounded table of the three. Note its window is a
            # YEAR, not 30 days — file_intelligence ranks folder habits over all
            # of it; see ACTIVITY_RETENTION_DAYS in app/tools/registry.py.
            ("activity log", purge_old_activity),
            # ⚠️ THE ONLY STEP HERE THAT TOUCHES THE USER'S OWN MEMORIES, and it
            # is the only one that DELETES NOTHING: it sets `archived_at`, which
            # hides a long-unused fact from retrieval and is undone by one click
            # in About Me. See app/memory/archive.py for the four conditions.
            ("memory archive", sweep_memory_archive),
        ):
            try:
                await step(db)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Housekeeping sweep of {name} failed (non-critical): {e}")
                try:
                    await db.rollback()
                except Exception:
                    # A poisoned session must not carry into the next step —
                    # the persist_message_best_effort lesson (2026-07-13).
                    return


async def _loop() -> None:
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        try:
            await run_housekeeping_pass()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # belt: run_housekeeping_pass swallows its own
            logger.warning(f"Housekeeping pass failed (non-critical): {e}")


def start_housekeeping() -> None:
    """Arm the sweep. Idempotent — a second call while one is running is a
    no-op, so a re-entered lifespan (tests) never spawns two."""
    global _sweeper
    if _sweeper is not None and not _sweeper.done():
        return
    _sweeper = asyncio.get_running_loop().create_task(_loop())
    logger.info(f"🧹 Housekeeping sweep armed (every {SWEEP_INTERVAL_SECONDS // 60} min)")


async def stop_housekeeping() -> None:
    """Cancel the sweep and WAIT for it to actually stop, so shutdown never
    leaves a task pending on a loop that is about to close."""
    global _sweeper
    task, _sweeper = _sweeper, None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
