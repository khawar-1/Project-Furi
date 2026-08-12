"""
Furi OS — Routines (Phase 6, Part 5 — teachable procedural memory)

The ONE accessor for the `routines` table (modules own their domain; the
routine router and the API never touch the table directly — the reminders
rule). A routine is a NAMED, REPEATABLE goal string the user taught once and
invokes by name later. We store the goal STRING (goal_template), never a
frozen plan: every run is replanned fresh through the agent planner, so the
structural approval gate, path guards, and recipient/event-id locks all
re-apply automatically.

Also home to the "offer-to-save" logic: when the same goal has completed
enough times (ROUTINE_OFFER_THRESHOLD), Furi proactively offers to save it
as a routine — spelling out the exact teach phrase so confirmation reuses the
normal TEACH trigger (no fragile yes/no state machine). The offer is
throttled to once per goal via the app_settings k/v store, and delivered the
fired-reminder way: persist a chat Message (durable copy), then best-effort
push (→ native toast).
"""
import re
from typing import Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.app_settings import get_setting, set_setting
from app.core.push import push
from app.db.models import Message, Routine, Task

# The recurrence count at which the same completed goal earns an offer to be
# saved as a routine. Mirrors Preference.occurrence_count thresholding.
ROUTINE_OFFER_THRESHOLD = 3

# app_settings key holding the JSON list of goals already offered — an offer
# fires at most once per goal, ever.
OFFERED_KEY = "routines.offered"

_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")
# Straight and curly quotes that may wrap a name the user typed.
_QUOTE_CHARS = "\"'‘’“”`"


# ---------------------------------------------------------------- normalization

def normalize_name(s: str) -> str:
    """The single normalization used for BOTH the routine lookup key and
    goal-recurrence matching (one source of truth): strip surrounding quotes,
    lowercase, replace punctuation with spaces, collapse whitespace. So
    "Clean Desktop", "clean-desktop!", and "clean  desktop" all map to the
    same key."""
    if not s:
        return ""
    text = s.strip().strip(_QUOTE_CHARS).strip()
    text = text.lower()
    text = _PUNCT_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text


def normalize_goal(goal: str) -> str:
    """Goal-recurrence matching uses the same normalization as routine names."""
    return normalize_name(goal)


# ------------------------------------------------------------------- CRUD

async def create_routine(db: AsyncSession, name: str, goal_template: str) -> Routine:
    """Upsert on normalized_name: teaching an existing name REPLACES its
    goal_template (re-teach, not duplicate) and reactivates it. Returns the
    live row."""
    key = normalize_name(name)
    display = name.strip().strip(_QUOTE_CHARS).strip() or key
    goal_template = goal_template.strip()

    existing = await _get_row_by_key(db, key, active_only=False)
    if existing is not None:
        existing.name = display
        existing.goal_template = goal_template
        existing.is_active = True
        await db.commit()
        await db.refresh(existing)
        logger.info(f"Routine re-taught: '{display}' (key '{key}')")
        return existing

    routine = Routine(name=display, normalized_name=key, goal_template=goal_template)
    db.add(routine)
    await db.commit()
    await db.refresh(routine)
    logger.info(f"Routine saved: '{display}' (key '{key}')")
    return routine


async def _get_row_by_key(
    db: AsyncSession, key: str, active_only: bool = True
) -> Optional[Routine]:
    if not key:
        return None
    query = select(Routine).where(Routine.normalized_name == key)
    if active_only:
        query = query.where(Routine.is_active.is_(True))
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def get_routine_by_name(db: AsyncSession, name: str) -> Optional[Routine]:
    """Normalized lookup of an ACTIVE routine, or None."""
    return await _get_row_by_key(db, normalize_name(name), active_only=True)


async def list_routines(db: AsyncSession) -> list[Routine]:
    """Active routines, newest first."""
    result = await db.execute(
        select(Routine).where(Routine.is_active.is_(True)).order_by(Routine.created_at.desc())
    )
    return list(result.scalars().all())


async def delete_routine(db: AsyncSession, routine_id: str) -> bool:
    """Hard delete (user-initiated, like delete_reminder). True if it existed."""
    routine = await db.get(Routine, routine_id)
    if routine is None:
        return False
    await db.delete(routine)
    await db.commit()
    return True


# ---------------------------------------------------------- offer-to-save

async def count_matching_goals(db: AsyncSession, goal: str) -> int:
    """How many COMPLETED Task rows share this goal (normalized). The cleaner
    signal than raw ActivityLog — a recurring *goal*, not per-tool churn.
    Normalization lives in Python, so we compare in Python."""
    key = normalize_goal(goal)
    if not key:
        return 0
    result = await db.execute(select(Task.goal).where(Task.status == "completed"))
    return sum(1 for (g,) in result.all() if normalize_goal(g or "") == key)


def _suggest_name(goal: str) -> str:
    """A short, typeable routine name suggested from the goal — the user can
    say it back verbatim in the teach phrase."""
    cleaned = normalize_name(goal)
    if not cleaned:
        return "my routine"
    words = cleaned.split()
    short = " ".join(words[:5])
    return short if len(short) <= 40 else short[:40].rstrip()


def _offer_body(goal: str, suggested: str, cadence=None) -> str:
    """The offer text. When a temporal cadence was mined, name it and spell out
    a SCHEDULED teach phrase so saying it back both saves the routine and sets
    its schedule (parse_routine_recurrence understands the phrase)."""
    goal_short = " ".join(goal.split())
    if len(goal_short) > 100:
        goal_short = goal_short[:99] + "…"
    if cadence is not None:
        from app.core.pattern_mining import describe_cadence, teach_phrase_cadence
        when = describe_cadence(cadence)
        phrase = teach_phrase_cadence(cadence)
        return (
            f'You\'ve had me do "{goal_short}" a few times now — usually {when}. '
            f'Want me to save it as a routine that runs on that schedule? Just '
            f'say: save this as a routine called "{suggested}" that runs {phrase} '
            f'— or drop the schedule part to just save it by name.'
        )
    return (
        f'You\'ve had me do "{goal_short}" a few times now. Want me to save it '
        f'as a reusable routine? Just say: save this as a routine called '
        f'"{suggested}" — then you can run it any time by name.'
    )


async def maybe_offer_routine(
    db: AsyncSession, goal: str, session_id: Optional[str]
) -> bool:
    """Best-effort (never raises): if this completed goal has recurred at least
    ROUTINE_OFFER_THRESHOLD times, is not already a routine, and hasn't been
    offered before, deliver an offer (persist a chat Message + push). Returns
    True only when an offer was actually delivered. Throttled once per goal."""
    try:
        if not session_id:
            return False  # nowhere to deliver / no chat to persist into
        key = normalize_goal(goal)
        if not key:
            return False

        if await count_matching_goals(db, goal) < ROUTINE_OFFER_THRESHOLD:
            return False
        if await _get_row_by_key(db, key, active_only=True) is not None:
            return False  # already saved as a routine

        offered = await get_setting(db, OFFERED_KEY, default=[])
        if not isinstance(offered, list):
            offered = []
        if key in offered:
            return False  # offer at most once per goal, ever

        # Mine a temporal cadence so a recurring-at-a-time goal is offered as a
        # SCHEDULED routine (best-effort — never blocks the offer).
        cadence = None
        try:
            from app.core.pattern_mining import cadence_for_goal
            cadence = await cadence_for_goal(db, goal)
        except Exception as e:
            logger.debug(f"Routine offer cadence mining skipped: {e}")

        suggested = _suggest_name(goal)
        body = _offer_body(goal, suggested, cadence=cadence)

        # Durable copy first (the push channel has no queue — reminder rule).
        db.add(Message(session_id=session_id, role="assistant", content=body))
        await set_setting(db, OFFERED_KEY, offered + [key])  # commits
        await db.commit()

        suggested_schedule = None
        cadence_text = ""
        if cadence is not None:
            from app.core.pattern_mining import cadence_to_schedule, describe_cadence
            suggested_schedule = cadence_to_schedule(cadence)
            cadence_text = describe_cadence(cadence)

        await push("routine_offer", {
            "session_id": session_id,
            "title": "Furi",
            "body": body,
            "text": body,
            "goal": goal,
            "suggested_name": suggested,
            "suggested_schedule": suggested_schedule,
            "cadence_text": cadence_text,
        })
        logger.info(f"Offered to save recurring goal as a routine: '{goal[:60]}'")
        return True
    except Exception as e:
        logger.warning(f"Routine offer-to-save failed (non-critical): {e}")
        try:
            await db.rollback()  # never leave the caller's session poisoned
        except Exception:
            pass
        return False
