"""
Jarvis OS — Suggestions domain (Phase 9, the Initiative Engine)

The ONE accessor for the `suggestions` table and the initiative feedback
signal (the reminders.py rule: the router/handler orchestrate, this module owns
the domain). Everything the initiative heartbeat, the API, and the tests touch
goes through here.

Two safety-critical properties live here:
- Accepting/acting a suggestion NEVER runs a frozen plan. We stored a GOAL
  STRING (`Suggestion.goal`); accept re-derives the plan through `start_task`,
  so the structural approval gate + path/recipient/event-id locks all re-apply
  (the Routine.goal_template principle). A suggestion can never smuggle a
  pre-approved destructive action past the gate.
- Accept/dismiss tune a Preference-backed affinity per CATEGORY
  (`initiative_affinity:<category>`), a clamped bipolar counter. Those rows are
  namespaced and filtered out of the chat MEMORY CONTEXT (MemoryEngine.
  get_preferences) so an internal score never leaks into a chat prompt.
"""
import re
from datetime import timedelta
from typing import Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Preference, Suggestion, utc_now

# Namespace for the internal accept/dismiss affinity signal — MUST match
# MemoryEngine.INTERNAL_PREFERENCE_PREFIX so these rows stay out of chat context.
AFFINITY_KEY_PREFIX = "initiative_affinity:"
AFFINITY_MIN = -5
AFFINITY_MAX = 5

# How long a fresh suggestion stays actionable before it auto-expires. A nudge
# that sat unanswered for a day has usually gone stale (the meeting passed, the
# email was read) — better to expire it than surface a lie.
DEFAULT_TTL_HOURS = 24

# Dedupe cooldown: a suggestion whose dedupe_key matched one created within this
# window (in ANY status) is a repeat and is suppressed.
DEDUPE_COOLDOWN_HOURS = 48

_PUNCT_RE = re.compile(r"[^a-z0-9\s]+")
_WS_RE = re.compile(r"\s+")


# ------------------------------------------------------------------ dedupe key

def make_dedupe_key(category: str, title: str, goal: Optional[str]) -> str:
    """A normalized signature (category + the goal, or the title when there is
    no goal) so the same nudge does not surface twice. Lowercased, punctuation-
    stripped, whitespace-collapsed, capped at the column width."""
    basis = (goal or title or "").lower()
    basis = _PUNCT_RE.sub(" ", basis)
    basis = _WS_RE.sub(" ", basis).strip()
    key = f"{(category or 'general').strip().lower()}:{basis}"
    return key[:128]


# ------------------------------------------------------------------ CRUD

async def create_suggestion(
    db: AsyncSession,
    *,
    category: str,
    title: str,
    body: str,
    rationale: str = "",
    autonomy: str = "suggest",
    priority: str = "normal",
    goal: Optional[str] = None,
    session_id: Optional[str] = None,
    status: str = "pending",
    dedupe_key: str = "",
    ttl_hours: int = DEFAULT_TTL_HOURS,
) -> Suggestion:
    """Persist a suggestion row. `status` is "pending" for suggest/ask items and
    "acted" when the heartbeat already started the task (autonomy level act)."""
    if not dedupe_key:
        dedupe_key = make_dedupe_key(category, title, goal)
    expires_at = utc_now() + timedelta(hours=max(1, int(ttl_hours)))
    row = Suggestion(
        category=(category or "general").strip() or "general",
        title=title.strip(),
        body=body.strip(),
        rationale=(rationale or "").strip(),
        autonomy=autonomy,
        priority=priority,
        goal=(goal.strip() if goal else None),
        session_id=session_id,
        status=status,
        dedupe_key=dedupe_key,
        expires_at=expires_at,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def get_suggestion(db: AsyncSession, suggestion_id: str) -> Optional[Suggestion]:
    return await db.get(Suggestion, suggestion_id)


async def list_suggestions(
    db: AsyncSession, status: Optional[str] = None, limit: int = 50
) -> list[Suggestion]:
    """Newest first — the feed shows what Jarvis most recently volunteered."""
    query = select(Suggestion).order_by(Suggestion.created_at.desc()).limit(limit)
    if status is not None:
        query = query.where(Suggestion.status == status)
    result = await db.execute(query)
    return list(result.scalars().all())


async def has_recent_duplicate(db: AsyncSession, dedupe_key: str) -> bool:
    """True if a suggestion with this dedupe_key was created within the cooldown
    window (any status). Blocks the heartbeat from re-surfacing the same nudge."""
    if not dedupe_key:
        return False
    cutoff = utc_now() - timedelta(hours=DEDUPE_COOLDOWN_HOURS)
    row = (
        await db.execute(
            select(Suggestion.id)
            .where(Suggestion.dedupe_key == dedupe_key)
            .where(Suggestion.created_at >= cutoff)
            .limit(1)
        )
    ).first()
    return row is not None


async def count_created_since(db: AsyncSession, since) -> int:
    """How many suggestions were created at/after `since` (for the daily
    budget). Counts every surfaced row — suggest, ask, and acted alike."""
    rows = await db.execute(
        select(Suggestion.id).where(Suggestion.created_at >= since)
    )
    return len(rows.all())


async def most_recent_created_at(db: AsyncSession):
    """The created_at of the most recent suggestion, or None (for the rate
    limiter). Excludes nothing — any surfaced item counts as 'Jarvis spoke'."""
    row = (
        await db.execute(
            select(Suggestion.created_at).order_by(Suggestion.created_at.desc()).limit(1)
        )
    ).first()
    return row[0] if row else None


# --------------------------------------------------------------- state changes

async def expire_stale(db: AsyncSession) -> int:
    """Flip every pending suggestion past its expires_at to "expired". Swept at
    startup and at the top of each heartbeat. Returns how many were expired."""
    now = utc_now()
    rows = (
        await db.execute(
            select(Suggestion)
            .where(Suggestion.status == "pending")
            .where(Suggestion.expires_at.isnot(None))
            .where(Suggestion.expires_at < now)
        )
    ).scalars().all()
    for row in rows:
        row.status = "expired"
    if rows:
        await db.commit()
    return len(rows)


async def accept_suggestion(
    db: AsyncSession, suggestion_id: str, provider=None
) -> Optional[Suggestion]:
    """Accept a pending suggestion. If it carries a goal, start an approval-
    gated background Task (start_task → the planner re-derives the plan; every
    write still pauses at the gate) and stamp its task_id. Tunes the category's
    affinity positive. Returns the updated row, or None if not found/not pending.

    Idempotent-safe: a suggestion already settled returns None (no double-run)."""
    row = await db.get(Suggestion, suggestion_id)
    if row is None or row.status != "pending":
        return None

    if row.goal:
        # Deferred import — app.agents pulls in the planner graph; keeping it
        # lazy avoids an import cycle (suggestions ← initiative ← agents).
        from app.agents import planner_memory_context, start_task
        try:
            memory = await planner_memory_context(db, row.goal)
        except Exception:
            memory = ""
        task = await start_task(
            db, row.goal, row.session_id,
            conversation="", memory=memory, provider=provider,
        )
        row.task_id = task.id

    row.status = "accepted"
    row.updated_at = utc_now()
    await db.commit()
    await db.refresh(row)

    await apply_initiative_feedback(db, row.category, accepted=True)
    return row


async def dismiss_suggestion(db: AsyncSession, suggestion_id: str) -> Optional[Suggestion]:
    """Dismiss a pending suggestion and tune the category's affinity negative.
    Returns the updated row, or None if not found/not pending."""
    row = await db.get(Suggestion, suggestion_id)
    if row is None or row.status != "pending":
        return None
    row.status = "dismissed"
    row.updated_at = utc_now()
    await db.commit()
    await db.refresh(row)

    await apply_initiative_feedback(db, row.category, accepted=False)
    return row


# ------------------------------------------------------------- feedback signal

def affinity_key(category: str) -> str:
    return f"{AFFINITY_KEY_PREFIX}{(category or 'general').strip().lower()}"


async def apply_initiative_feedback(
    db: AsyncSession, category: str, *, accepted: bool
) -> None:
    """Read-modify-write the category's affinity Preference (a clamped signed
    counter: accept +1, dismiss -1). Direct row write rather than
    upsert_preference, whose +0.05-confidence / value-overwrite semantics don't
    model a bipolar counter. Best-effort — feedback tuning must never break an
    accept/dismiss. The row is namespaced (initiative_affinity:*) and filtered
    out of the chat MEMORY CONTEXT."""
    try:
        key = affinity_key(category)
        pref = (
            await db.execute(select(Preference).where(Preference.key == key))
        ).scalar_one_or_none()
        delta = 1 if accepted else -1
        if pref is None:
            score = max(AFFINITY_MIN, min(AFFINITY_MAX, delta))
            db.add(Preference(
                key=key,
                value=str(score),
                description=_affinity_stance(score),
                source="inferred",
                confidence=1.0,
            ))
        else:
            try:
                current = int(pref.value)
            except (TypeError, ValueError):
                current = 0
            score = max(AFFINITY_MIN, min(AFFINITY_MAX, current + delta))
            pref.value = str(score)
            pref.description = _affinity_stance(score)
            pref.occurrence_count += 1
            pref.updated_at = utc_now()
        await db.commit()
    except Exception as e:  # never let feedback break the accept/dismiss
        logger.warning(f"Initiative feedback tuning failed (non-critical): {e}")
        await db.rollback()


def _affinity_stance(score: int) -> str:
    if score >= 2:
        return "often accepts these suggestions"
    if score <= -2:
        return "often dismisses these suggestions"
    return "neutral on these suggestions"


async def get_affinities(db: AsyncSession) -> dict[str, int]:
    """The current per-category affinity scores ({category: score}), for the
    heartbeat to feed back into its prompt so the pass self-tunes."""
    rows = (
        await db.execute(
            select(Preference).where(Preference.key.like(f"{AFFINITY_KEY_PREFIX}%"))
        )
    ).scalars().all()
    out: dict[str, int] = {}
    for p in rows:
        category = (p.key or "")[len(AFFINITY_KEY_PREFIX):]
        try:
            out[category] = int(p.value)
        except (TypeError, ValueError):
            continue
    return out
