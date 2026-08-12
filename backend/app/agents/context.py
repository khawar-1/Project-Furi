"""
Furi OS — Planner Memory Context (Phase 3.5)

Renders the same long-term memory the chat path sees into a string for the
agent planner, so task turns are not amnesiac about people, preferences, and
facts ("email Jamil about the trip" knows who Jamil is).

Deliberate choices:
- session_id is NOT passed to retrieve_context: the planner must never
  create conversation sessions as a side effect, and the parked memory
  questions ("which jamil?") belong to the chat path, not the planner.
- Failures return "" — memory context is an enhancement; planning must
  never fail because retrieval did.
- Output is capped: contact fact logs can get long, and the planner prompt
  already carries tool schemas + conversation context.
"""
from typing import Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

MEMORY_CONTEXT_MAX_CHARS = 4000


async def planner_memory_context(db: AsyncSession, goal: str, qdrant=None) -> str:
    """Rendered MEMORY CONTEXT block for a planner run; "" when there is
    nothing relevant or retrieval fails."""
    goal = (goal or "").strip()
    if not goal:
        return ""
    try:
        from app.memory.engine import MemoryEngine

        if qdrant is None:
            try:
                from app.db.qdrant_client import get_qdrant_client
                qdrant = get_qdrant_client()
            except Exception:
                qdrant = None

        engine = MemoryEngine(db=db, qdrant=qdrant)
        bundle = await engine.retrieve_context(goal, session_id=None)
        context = await engine.format_context(bundle)
        if len(context) > MEMORY_CONTEXT_MAX_CHARS:
            context = context[:MEMORY_CONTEXT_MAX_CHARS] + "… (truncated)"
        return context
    except Exception as e:
        logger.warning(f"Planner memory context failed (non-critical): {e}")
        return ""
