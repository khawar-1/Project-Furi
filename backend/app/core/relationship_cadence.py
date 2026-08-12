"""
Furi OS — Relationship & memory cadence (Phase 11, Parts 1 & 2)

Read-only signals for the Initiative Engine, derived on demand (the
file_intelligence precedent — no new table, best-effort → []):

- people_cadence (11.1): contacts you haven't caught up with in a while, from
  Contact.last_interaction. HONEST about the data — last_interaction reflects
  the last time a person CAME UP (memory-extraction activity), not a verified
  outbound message, so the nudge is phrased "haven't caught up with X in a
  while", never a false "you haven't messaged X".

- memory_callbacks (11.2): the heuristic FALLBACK for proactive follow-ups when
  there is no structured goal-thread (app/core/goal_threads.py is preferred) —
  user/shared facts from ~1-3 weeks ago that read as open concerns ("worried
  about the deadline"), by a conservative keyword filter.
"""
from datetime import datetime, timedelta
from typing import Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Contact, SemanticMemory, utc_now

# people_cadence tuning (conservative — a nudge should feel earned).
DEFAULT_WEEKS_THRESHOLD = 3       # silence this long before a reconnect nudge
DEFAULT_MIN_INTERACTIONS = 2      # only people with real history, not one mention
DEFAULT_PEOPLE_LIMIT = 5

# memory_callbacks tuning.
CALLBACK_MIN_DAYS = 5
CALLBACK_MAX_DAYS = 21
DEFAULT_CALLBACK_LIMIT = 3

_CONCERN_KEYWORDS = (
    "worried", "worry", "worrying", "deadline", "interview", "waiting",
    "hoping", "hope to", "nervous", "anxious", "stressed", "stress",
    "concern", "applied", "application", "exam", "results", "decision",
    "struggling", "hoping to hear", "follow up", "pending",
)


async def people_cadence(
    db: AsyncSession,
    *,
    weeks_threshold: int = DEFAULT_WEEKS_THRESHOLD,
    min_interactions: int = DEFAULT_MIN_INTERACTIONS,
    limit: int = DEFAULT_PEOPLE_LIMIT,
    now: Optional[datetime] = None,
) -> list[dict]:
    """Active contacts with real history not interacted with for >= the
    threshold, longest silence first. Best-effort → []."""
    now = now or utc_now()
    cutoff = now - timedelta(weeks=weeks_threshold)
    try:
        result = await db.execute(select(Contact).where(Contact.is_active == True))  # noqa: E712
        contacts = result.scalars().all()
    except Exception as e:
        logger.warning(f"people_cadence query failed (non-critical): {e}")
        return []

    out: list[dict] = []
    for c in contacts:
        if c.last_interaction is None:
            continue
        if (c.interaction_count or 0) < min_interactions:
            continue
        if c.last_interaction > cutoff:
            continue  # caught up recently enough
        out.append({
            "name": c.name,
            "contact_id": c.id,
            "weeks_since": max(1, (now - c.last_interaction).days // 7),
            "relationship_type": c.relationship_type,
        })

    out.sort(key=lambda d: d["weeks_since"], reverse=True)
    return out[:limit]


async def memory_callbacks(
    db: AsyncSession,
    *,
    min_days: int = CALLBACK_MIN_DAYS,
    max_days: int = CALLBACK_MAX_DAYS,
    limit: int = DEFAULT_CALLBACK_LIMIT,
    now: Optional[datetime] = None,
) -> list[dict]:
    """User/shared facts from the min..max-days-ago window that read as open
    concerns (keyword heuristic). The FALLBACK for goal-thread follow-ups.
    Best-effort → []."""
    now = now or utc_now()
    lower = now - timedelta(days=max_days)
    upper = now - timedelta(days=min_days)
    try:
        result = await db.execute(
            select(SemanticMemory)
            .where(
                SemanticMemory.is_active == True,  # noqa: E712
                SemanticMemory.subject.in_(("user", "shared")),
                SemanticMemory.created_at >= lower,
                SemanticMemory.created_at <= upper,
            )
            .order_by(SemanticMemory.created_at.desc())
        )
        rows = result.scalars().all()
    except Exception as e:
        logger.warning(f"memory_callbacks query failed (non-critical): {e}")
        return []

    out: list[dict] = []
    for m in rows:
        text = (m.content or "").lower()
        if any(k in text for k in _CONCERN_KEYWORDS):
            out.append({
                "content": m.content,
                "days_ago": max(1, (now - m.created_at).days),
            })
        if len(out) >= limit:
            break
    return out
