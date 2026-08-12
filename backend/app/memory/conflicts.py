"""
Furi OS — Memory conflict capture (2026-08-04)

*(Tier 2, item 7 of `suhhestionsfromclaude.txt` — "conflict handling is
supersede-only")*

WHAT WAS ACTUALLY WRONG, AND IT WAS NOT THE SUPERSEDE RULE
----------------------------------------------------------
`engine.supersede_is_covered` requires a replacement to contain EVERY word of
the fact it replaces, so *"moved to Lahore"* never supersedes *"lives in
Karachi"*. That rule is CORRECT and is not touched here — it is what stops a
"merged" replacement silently dropping participants, and it was written from two
observed data-loss incidents.

The defect was what happened next:

    else:
        logger.info(f"Supersede blocked (new fact does not cover it): ...")

The extractor's judgement that two facts collide was DETECTED and then thrown
away. Both facts lived forever, the user was never told, and nothing could read
the disagreement back. Same shape as the defect `plan_traces` fixed one layer
up: **the symptom persisted, the diagnosis died with the run.**

⚠️ WHY NOTHING HERE RESOLVES ITSELF
------------------------------------
Deciding that "moved to Lahore" invalidates "lives in Karachi" is a judgement
about MEANING. There is no substring test for it, no comparator to check it
against, and this codebase has measured prompt-only rules with nothing to check
them at ZERO three separate times. Worse, the failure is silent and permanent:
an automatic resolution that gets it wrong destroys a true fact and leaves no
trace that it ever existed.

So a row here is a **claim**, not a verdict — what an LLM extractor thought. It
is a queue entry for the one party who can actually settle it. Every surface
says "these may conflict", never "this one is wrong", and RESOLVING is a user
click that routes through `delete_semantic_memory`, the hard-delete path that
has been user-initiated-only since Phase 2.

WHAT THE PROMPT SEES: NOTHING NEW
---------------------------------
Deliberately. MEMORY CONTEXT already renders both facts and the MEMORY RULES
block already tells the model the most recent fact in a category is the current
truth. Adding a "possibly superseded" marker would be an unverified LLM claim
leaking into the context block, which is the one place this codebase works
hardest to keep grounded.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MemoryConflict, SemanticMemory, utc_now

# Settled rows (resolved or dismissed) age out; OPEN ones never do. Ageing out
# an unanswered question would silently drop it, which is the failure this
# module exists to stop.
CONFLICT_RETENTION_DAYS = 90


async def record_conflict(
    db: AsyncSession, *, old_content: str, new_content: str
) -> Optional[MemoryConflict]:
    """Capture a supersede request that was refused for not covering its target.

    Best-effort and NEVER raises: it is called from the extraction write path,
    where a bookkeeping failure must not cost the user the fact that was just
    written. Returns the row when one was created, None otherwise (nothing to
    conflict with, already queued, or a write failure)."""
    try:
        old_content = (old_content or "").strip()
        new_content = (new_content or "").strip()
        if not old_content or not new_content or old_content == new_content:
            return None

        # Only worth recording if the older fact is actually still live. A row
        # pointing at a fact that is already gone is a question with no answer.
        old = (
            await db.execute(
                select(SemanticMemory)
                .where(SemanticMemory.content == old_content)
                .where(SemanticMemory.is_active.is_(True))
                .limit(1)
            )
        ).scalar_one_or_none()
        if old is None:
            return None

        # One open row per (old fact, replacement). Re-extraction of the same
        # conversation is normal and must not queue the same question twice.
        existing = (
            await db.execute(
                select(MemoryConflict)
                .where(MemoryConflict.memory_id == old.id)
                .where(MemoryConflict.new_content == new_content)
                .where(MemoryConflict.status == "open")
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return None

        conflict = MemoryConflict(
            memory_id=old.id,
            old_content=old_content,
            new_content=new_content,
            status="open",
        )
        db.add(conflict)
        await db.commit()
        logger.info(
            f"Memory conflict queued for review: '{old_content[:50]}' vs "
            f"'{new_content[:50]}' (both kept)"
        )
        return conflict
    except Exception as e:
        logger.warning(f"Could not record memory conflict (non-critical): {e}")
        try:
            await db.rollback()
        except Exception:
            pass
        return None


async def list_open_conflicts(
    db: AsyncSession, *, limit: int = 50
) -> list[MemoryConflict]:
    """The review queue, newest first.

    ⚠️ ONLY QUESTIONS THAT STILL HAVE AN ANSWER. The older fact can leave by
    another route between capture and review — the user deletes it in About Me
    (hard), or a LATER extraction supersedes it with a replacement that does
    cover it (soft). Either way the disagreement is settled and asking again is
    noise. `record_conflict` already refuses to queue a question about a fact
    that is not live; this is the same rule at the other end, and the two must
    agree or the queue slowly fills with things the user has already dealt
    with."""
    return list(
        (
            await db.execute(
                select(MemoryConflict)
                .join(
                    SemanticMemory,
                    SemanticMemory.id == MemoryConflict.memory_id,
                )
                .where(MemoryConflict.status == "open")
                .where(SemanticMemory.is_active.is_(True))
                .order_by(MemoryConflict.detected_at.desc())
                .limit(limit)
            )
        ).scalars().all()
    )


async def _settle(
    db: AsyncSession, conflict_id: str, status: str
) -> Optional[MemoryConflict]:
    conflict = (
        await db.execute(
            select(MemoryConflict)
            .where(MemoryConflict.id == conflict_id)
            .where(MemoryConflict.status == "open")
        )
    ).scalar_one_or_none()
    if conflict is None:
        return None
    conflict.status = status
    conflict.resolved_at = utc_now()
    await db.commit()
    return conflict


async def dismiss_conflict(db: AsyncSession, conflict_id: str) -> bool:
    """"They do not conflict" — both facts stay exactly as they are."""
    return await _settle(db, conflict_id, "dismissed") is not None


async def resolve_conflict(db: AsyncSession, conflict_id: str, engine) -> bool:
    """"The newer one is right" — hard-delete the OLDER fact.

    ⚠️ This is the only destructive thing in the whole item-7 body of work, and
    it happens only when a human clicks it. It routes through
    `MemoryEngine.delete_semantic_memory`, the same path About Me's trash button
    uses — SQLite row AND Qdrant point, so the fact is gone rather than hidden.
    Nothing automatic can reach this function.

    The row is settled even when the memory has already vanished by another
    route: the question is answered either way, and leaving it open would
    re-ask something the user has dealt with."""
    conflict = (
        await db.execute(
            select(MemoryConflict)
            .where(MemoryConflict.id == conflict_id)
            .where(MemoryConflict.status == "open")
        )
    ).scalar_one_or_none()
    if conflict is None:
        return False

    memory_id = conflict.memory_id
    await engine.delete_semantic_memory(memory_id)
    # Re-fetch: delete_semantic_memory commits, which can expire the instance.
    return await _settle(db, conflict_id, "resolved") is not None


async def purge_settled_conflicts(db: AsyncSession) -> int:
    """Housekeeping entry point, matching the `step(db) -> int` shape.

    Only ever removes rows the user has already answered."""
    cutoff = utc_now() - timedelta(days=CONFLICT_RETENTION_DAYS)
    result = await db.execute(
        delete(MemoryConflict)
        .where(MemoryConflict.status != "open")
        .where(MemoryConflict.resolved_at.is_not(None))
        .where(MemoryConflict.resolved_at < cutoff)
    )
    await db.commit()
    return int(result.rowcount or 0)
