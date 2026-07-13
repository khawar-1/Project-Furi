"""
Jarvis OS — Best-effort chat-history persistence (2026-07-13)

The ONE way to write a Message row that must never take the feature down
with it. Every router/runner used to inline the same try/except-log around
`db.add(Message(...)); await db.commit()` — but WITHOUT a rollback, so a
failed INSERT left the session in a failed-transaction state and poisoned
everything after it on the same session.

Live incident 2026-07-12: the production DB was missing the Phase 6 Part 4
`messages.embedded_at` column (migration never applied), so every Message
INSERT failed. Each failure was swallowed as "non-critical" — and then
start_task()'s own commit on the SAME session died on the pending rollback:
"I couldn't start that as a background task". One missing column silently
disabled chat history AND background tasks.

Rules:
- Failure is logged and rolled back; the caller gets False, never an
  exception. The user-facing turn (stream/toast/push) must already carry
  the content — history is the durable copy, not the delivery channel.
- The rollback is the point: the session stays usable for whatever the
  caller does next.
"""
from typing import Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Message


async def persist_message_best_effort(
    db: AsyncSession,
    session_id: str,
    role: str,
    content: str,
    model: Optional[str] = None,
    tokens_used: Optional[int] = None,
    what: str = "chat message",
) -> Optional[Message]:
    """Write one Message row; on ANY failure log, ROLL BACK, return None.
    Returns the persisted Message so callers can feed follow-up hooks
    (e.g. the conversation-index embed)."""
    msg = Message(
        session_id=session_id, role=role, content=content,
        model=model, tokens_used=tokens_used,
    )
    try:
        db.add(msg)
        await db.commit()
        return msg
    except Exception as e:
        logger.warning(f"Persisting {what} failed (non-critical): {e}")
        try:
            await db.rollback()  # un-poison the session — see module docstring
        except Exception as rb:
            logger.warning(f"Rollback after failed persist also failed: {rb}")
        return None
