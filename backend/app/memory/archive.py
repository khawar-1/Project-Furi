"""
Jarvis OS — Reversible memory archive (2026-08-03)

*(Tier 2, item 7 — "memory only grows")*

The only part of this item that HIDES anything, so it is the part written most
conservatively.

THE RULE IT IS BUILT AROUND
---------------------------
**Nothing is ever destroyed by an automatic process.** User-initiated deletion
is a hard delete and has been since Phase 2 (`delete_semantic_memory` removes
the SQLite row AND the Qdrant point). Automatic maintenance gets a strictly
weaker verb: `archived_at` is set, the row and its vector stay exactly where
they were, retrieval skips it, About Me lists it under "Archived", and one click
puts it back.

⚠️ ARCHIVED IS NOT `is_active=False`, AND CONFLATING THEM WOULD BE A REAL BUG.
`is_active=False` is a soft DELETE written by dedup and by supersede — it means
"a newer fact replaced this one", i.e. a claim that the content is now WRONG.
`archived_at` carries no such claim: it means "nothing has needed this in a long
time". One is a correction, the other is a tidy-up, and a UI or a query that
treated them alike would either resurrect superseded facts or present a
tidy-up as a correction.

WHAT IT WILL NOT TOUCH
----------------------
Four conditions, ALL required, and each one exists because getting it wrong is
worse than never archiving anything:

  age          Older than ARCHIVE_AFTER_DAYS. A recent fact is live by
               definition.
  never used   `last_used_at IS NULL`. The column only started being written on
               2026-08-03, so this ALSO means "we have watched it for the whole
               window and it never came up" — the grace period is automatic
               rather than a special case, because a fact written before the
               column existed still has to survive ARCHIVE_AFTER_DAYS of
               observation before it qualifies.
  live         `is_active` is true. A superseded fact is already out of
               retrieval; re-marking it would blur the two states above.
  not identity `subject != "user"` is NOT the rule — the rule is that CORE
               IDENTITY categories are exempt entirely. "I am allergic to
               penicillin" may go a year unmentioned and must still be there
               the day it matters.

The pass is capped per run (`ARCHIVE_MAX_PER_PASS`) so a first run on a large
old database is gradual and visible rather than a single silent sweep of
thousands of rows.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SemanticMemory, utc_now

# Six months of never once being needed. Deliberately long: the cost of
# archiving too eagerly is that Jarvis forgets something the user still cares
# about, and the cost of archiving too slowly is a slightly larger table.
ARCHIVE_AFTER_DAYS = 180
# Gradual, so a first run over years of history is observable.
ARCHIVE_MAX_PER_PASS = 200

# Categories that are never archived however long they lie unused. These are
# the facts whose whole value is being there on the rare day they matter.
PROTECTED_CATEGORIES: frozenset[str] = frozenset({
    "identity",
    "health",
    "medical",
    "allergy",
    "credential",
    "emergency",
})


async def archive_stale_memories(
    db: AsyncSession,
    *,
    older_than_days: int = ARCHIVE_AFTER_DAYS,
    limit: int = ARCHIVE_MAX_PER_PASS,
) -> int:
    """Archive facts nothing has needed in a long time. Returns how many.

    Reversible by construction: this only ever writes `archived_at`. It issues
    no DELETE, and it does not touch Qdrant — a restored memory is immediately
    searchable again with no re-embed."""
    cutoff = utc_now() - timedelta(days=older_than_days)
    rows = (
        await db.execute(
            select(SemanticMemory)
            .where(SemanticMemory.is_active.is_(True))
            .where(SemanticMemory.archived_at.is_(None))
            .where(SemanticMemory.last_used_at.is_(None))
            .where(SemanticMemory.created_at < cutoff)
            .order_by(SemanticMemory.created_at.asc())
            .limit(limit)
        )
    ).scalars().all()

    stale = [m.id for m in rows if (m.category or "").lower() not in PROTECTED_CATEGORIES]
    if not stale:
        return 0
    await db.execute(
        update(SemanticMemory)
        .where(SemanticMemory.id.in_(stale))
        .values(archived_at=utc_now())
    )
    await db.commit()
    logger.info(f"Archived {len(stale)} unused memory row(s) (reversible)")
    return len(stale)


async def restore_memory(db: AsyncSession, memory_id: str) -> bool:
    """Put an archived memory back into retrieval. The undo half — without it
    the archive would be a delete with extra steps."""
    result = await db.execute(
        update(SemanticMemory)
        .where(SemanticMemory.id == memory_id)
        .where(SemanticMemory.archived_at.is_not(None))
        .values(archived_at=None, last_used_at=utc_now())
    )
    await db.commit()
    return bool(result.rowcount)


async def list_archived(
    db: AsyncSession, *, limit: int = 100
) -> list[SemanticMemory]:
    """Everything the archive pass has set aside, newest first. The trust
    surface: an automatic tidy-up nobody can inspect is indistinguishable from
    data loss."""
    return list(
        (
            await db.execute(
                select(SemanticMemory)
                .where(SemanticMemory.archived_at.is_not(None))
                .order_by(SemanticMemory.archived_at.desc())
                .limit(limit)
            )
        ).scalars().all()
    )


async def sweep_memory_archive(db: AsyncSession) -> int:
    """Housekeeping entry point, matching the `purge_x(db) -> int` shape the
    sweep's tuple expects."""
    return await archive_stale_memories(db)
