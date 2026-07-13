"""
Jarvis OS — Conversation index service (Phase 6, Part 4)

Messages are the one memory surface that was SQLite-only — searchable by
session, never by meaning. This module embeds each chat Message into the Qdrant
"conversation_messages" collection (one point per message, point id = message
id), so semantic_file_search can return ranked matches from files AND prior
chats in a single ask.

Design rules (all in code, mirroring app/core/file_index.py):
- INCREMENTAL by default: only messages whose `embedded_at` is NULL are
  embedded; `full=True` re-embeds every message. The Message row is the ledger
  (embedded_at is the cursor) — no separate table.
- ONE point per message: id = message.id (a uuid string). A re-embed upserts
  the same id, so there are never duplicates.
- Only real conversation turns are indexed: role in {user, assistant} with
  non-empty content. System/scaffolding messages are skipped.
- PRIVACY: gated on the SAME toggle as the file index (FileIndexConfig.enabled).
  Disabled → nothing is embedded. Embedding is local (fastembed) — message text
  never leaves the machine.
- Best-effort throughout: a dead vector store, one embed failure, or one bad
  row never raises to a caller. The write-path hook and the reindex pass both
  degrade to no-ops rather than breaking a chat turn or a scheduled job.
"""
import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.app_settings import get_file_index_config
from app.db.models import Message, utc_iso, utc_now
from app.db.qdrant_client import get_qdrant_client
from app.memory.embedder import embed_batch, embed_text

CONVERSATION_MESSAGES_COLLECTION = "conversation_messages"

INDEXABLE_ROLES = ("user", "assistant")
PAYLOAD_TEXT_CAP = 2000        # message text stored in the point payload (snippets)
BATCH_SIZE = 128               # messages embedded per batch in a backfill pass
MAX_MESSAGES_PER_PASS = 100_000

# Injectable session factory (the memory_tools / file_index pattern) — tests and
# the API/scheduler resolve this at call time, never at import.
SESSION_FACTORY: Optional[Callable[[], Any]] = None

# Background-run state for the API rebuild (single pass at a time; poll status).
_INDEXING = False
_RUNNING: set[asyncio.Task] = set()


@dataclass
class ConversationIndexStats:
    """What one pass did — for the rebuild/status payload and tests."""
    scanned: int = 0        # messages considered
    embedded: int = 0       # messages embedded this pass
    skipped: int = 0        # already-embedded / not a real turn (full=False path)
    errors: int = 0         # batches that raised during embed/upsert
    error: Optional[str] = None   # a pass-level failure (disabled / no vector store)

    def as_dict(self) -> dict:
        return asdict(self)


def _session_factory() -> Callable[[], Any]:
    if SESSION_FACTORY is not None:
        return SESSION_FACTORY
    from app.db.database import AsyncSessionLocal
    return AsyncSessionLocal


def _is_indexable(msg: Message) -> bool:
    return bool(msg.role in INDEXABLE_ROLES and (msg.content or "").strip())


def _message_payload(msg: Message) -> dict:
    return {
        "message_id": msg.id,
        "session_id": msg.session_id,
        "role": msg.role,
        "text": (msg.content or "")[:PAYLOAD_TEXT_CAP],
        "created_at": utc_iso(msg.created_at),
    }


# ------------------------------------------------------------ Qdrant upsert

async def _upsert_messages(
    qdrant: Any,
    rows: list[Message],
    vectors: list[list[float]],
) -> int:
    if qdrant is None or not rows:
        return 0
    from qdrant_client.http import models as qm

    points = [
        qm.PointStruct(id=msg.id, vector=vector, payload=_message_payload(msg))
        for msg, vector in zip(rows, vectors)
    ]
    await qdrant.upsert(
        collection_name=CONVERSATION_MESSAGES_COLLECTION, points=points
    )
    return len(points)


# -------------------------------------------------------------- core pass

async def index_conversations(
    db: AsyncSession,
    qdrant: Any,
    *,
    full: bool = False,
    embed: Optional[Callable[[list[str]], Awaitable[list[list[float]]]]] = None,
) -> ConversationIndexStats:
    """Embed every message that needs it. INCREMENTAL unless full=True. Assumes
    a live qdrant client (the runner guards on None). Best-effort per batch — a
    failed batch is counted and the pass continues."""
    if embed is None:
        embed = embed_batch  # resolved at call time (tests monkeypatch this)
    stats = ConversationIndexStats()

    query = select(Message).where(Message.role.in_(INDEXABLE_ROLES))
    if not full:
        query = query.where(Message.embedded_at.is_(None))
    query = query.order_by(Message.created_at).limit(MAX_MESSAGES_PER_PASS)

    result = await db.execute(query)
    rows = [m for m in result.scalars().all() if _is_indexable(m)]
    stats.scanned = len(rows)

    now = utc_now()
    for start in range(0, len(rows), BATCH_SIZE):
        batch = rows[start:start + BATCH_SIZE]
        try:
            vectors = await embed([m.content for m in batch])
            written = await _upsert_messages(qdrant, batch, vectors)
        except Exception as e:
            logger.warning(f"conversation_index: batch embed/upsert failed: {e}")
            stats.errors += 1
            continue
        for m in batch:
            m.embedded_at = now
        stats.embedded += written
        # COMMIT PER BATCH, never once per pass (the file_index 2026-07-13
        # lesson): a pass-wide transaction holds SQLite's write lock for the
        # whole backfill, starving every concurrent chat/audit write — and one
        # lock collision at the end used to roll back the ENTIRE pass's
        # embedded_at cursor (this pass ran during the file build, lost the
        # race, and 0 of 456 messages were marked embedded).
        await db.commit()
    logger.info(
        f"conversation_index pass: scanned={stats.scanned} "
        f"embedded={stats.embedded} errors={stats.errors}"
    )
    return stats


# -------------------------------------------------------- write-path hook

async def embed_message_best_effort(db: AsyncSession, message: Message) -> bool:
    """Embed a SINGLE freshly-persisted message so a just-said turn is searchable
    immediately (semantic_file_search also queries chats). Called right after a
    chat message is committed. Returns True iff a vector was written.

    Best-effort and NEVER raises: it no-ops when the index is disabled (the
    privacy toggle — the default) or the vector store is offline, and swallows
    any error (the backfill pass will pick the message up later). Uses the
    request's own session, so no cross-session concurrency."""
    try:
        if not _is_indexable(message):
            return False
        config = await get_file_index_config(db)
        if not config.enabled:
            return False
        qdrant = get_qdrant_client()
        if qdrant is None:
            return False
        vector = await embed_text(message.content)
        await _upsert_messages(qdrant, [message], [vector])
        message.embedded_at = utc_now()
        await db.commit()
        return True
    except Exception as e:
        logger.debug(f"conversation_index: on-write embed skipped ({e})")
        return False


def schedule_message_embed(message_id: str) -> None:
    """Fire-and-forget embed of ONE freshly-persisted message, OFF the chat
    turn's critical path (latency, 2026-07-13: the on-write embed + vector
    upsert used to be awaited in chat._persist_message BEFORE the SSE stream
    started). Opens its OWN session — the request session closes when the
    response ends and must never be handed to a detached task. Never raises;
    anything skipped here (no loop, embed failure) is picked up by the
    reindex pass's backfill, exactly like the other message writers."""
    async def _embed_one() -> None:
        try:
            factory = _session_factory()
            async with factory() as db:
                msg = await db.get(Message, message_id)
                if msg is not None:
                    await embed_message_best_effort(db, msg)
        except Exception as e:
            logger.debug(f"conversation_index: deferred on-write embed skipped ({e})")

    try:
        task = asyncio.create_task(_embed_one())
        _RUNNING.add(task)  # referenced like every detached task; awaited by
        task.add_done_callback(_RUNNING.discard)  # wait_for_conversation_index
    except RuntimeError:
        pass  # no running loop — backfill covers it


# ------------------------------------------------------- runner + status

async def run_conversation_index(*, full: bool = False) -> ConversationIndexStats:
    """One pass with its own session, resolving config + qdrant. Safe to await
    directly (the reindex scheduler handler does). Gated on the file-index
    enable toggle (privacy) and a live vector store — either off is a no-op."""
    factory = _session_factory()
    async with factory() as db:
        config = await get_file_index_config(db)
        if not config.enabled:
            return ConversationIndexStats(error="File index is disabled.")
        qdrant = get_qdrant_client()
        if qdrant is None:
            logger.warning("conversation_index: no vector store — skipping pass")
            return ConversationIndexStats(error="Vector store (Qdrant) is unavailable.")
        return await index_conversations(db, qdrant, full=full)


def is_indexing() -> bool:
    return _INDEXING


async def start_conversation_index_in_background(*, full: bool = False) -> bool:
    """Kick off a detached conversation-index pass (the rebuild endpoint). Returns
    False if one is already running (single pass at a time)."""
    global _INDEXING
    if _INDEXING:
        return False
    _INDEXING = True

    async def _runner() -> None:
        global _INDEXING
        try:
            await run_conversation_index(full=full)
        except Exception as e:
            logger.error(f"conversation_index background pass failed: {e}")
        finally:
            _INDEXING = False

    task = asyncio.create_task(_runner())
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)
    return True


async def wait_for_conversation_index() -> None:
    """Await any in-flight background pass (tests / shutdown)."""
    while _RUNNING:
        await asyncio.gather(*list(_RUNNING), return_exceptions=True)


async def get_conversation_index_summary(db: AsyncSession) -> dict:
    """Counts for the status endpoint — how many turns are indexed vs. pending."""
    embedded = await db.scalar(
        select(func.count()).select_from(Message).where(
            Message.role.in_(INDEXABLE_ROLES),
            Message.embedded_at.is_not(None),
        )
    )
    pending = await db.scalar(
        select(func.count()).select_from(Message).where(
            Message.role.in_(INDEXABLE_ROLES),
            Message.embedded_at.is_(None),
        )
    )
    return {
        "indexed_messages": int(embedded or 0),
        "unindexed_messages": int(pending or 0),
    }
