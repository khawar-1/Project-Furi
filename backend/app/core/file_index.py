"""
Jarvis OS — Semantic file index service (Phase 6, Part 2)

Walks the user's configured folders, extracts text (app/core/file_extract.py),
chunks it, embeds each chunk locally (fastembed, 384-dim — the same
embed_batch every other vector feature uses), and upserts the chunks into the
Qdrant "file_chunks" collection. The FileIndex SQLite table is the ledger and
the truth; Qdrant holds the vectors.

Design rules (all in code):
- INCREMENTAL by default: a file whose size AND mtime match its ledger row is
  skipped — no re-extract, no re-embed. `full=True` forces everything.
- The user's wording defines the scope — NEVER a whole drive. Configured
  folders only; the file_tools safety layer (_resolve_path / _blocked_reason /
  _PROTECTED / _SKIP_DIR_NAMES) is reused verbatim, plus per-folder exclusion
  lists. Hidden dirs and the standard skip dirs are pruned during the walk.
- Chunk point ids are DETERMINISTIC (uuid5 of file_id:index), so a changed
  file's old vectors are deleted precisely before the new ones upsert — no
  orphans, no duplicates.
- PRUNE: an active ledger row whose file was not seen this pass (deleted,
  folder de-configured, or now excluded) is soft-deleted — its row goes
  is_active=False and its vectors are removed.
- Best-effort throughout: one unreadable file or one Qdrant hiccup never aborts
  the pass. Qdrant is REQUIRED (the caller guards on get_qdrant_client()); a
  run with no vector store does nothing rather than record un-searchable rows.
- Runs entirely off the request path when triggered from the API: a detached
  asyncio task with its own session (SESSION_FACTORY, the task_runner pattern).
"""
import asyncio
import hashlib
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.app_settings import FileIndexConfig, get_file_index_config
from app.core.file_extract import extract_text, is_indexable
from app.db.models import FileIndex, utc_now
from app.db.qdrant_client import get_qdrant_client
from app.memory.embedder import embed_batch
from app.tools.file_tools import _PROTECTED, _SKIP_DIR_NAMES, _blocked_reason, _resolve_path

FILE_CHUNKS_COLLECTION = "file_chunks"

# Chunking — char windows with overlap (bge-small handles ~512 tokens; ~1000
# chars stays well inside that with headroom for multi-byte text).
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 150
MAX_CHUNKS_PER_FILE = 50
CHUNK_PAYLOAD_TEXT_CAP = 1500   # chunk text stored in the point payload (for snippets)

MAX_FILES_SCANNED = 50_000      # hard bound on files visited per pass

# Stable namespace so a file's chunk ids are reproducible across runs/processes.
_CHUNK_NS = uuid.UUID("6f9b2c1a-0d3e-4c7a-9e21-1f5b7a8c4d20")

# Injectable session factory (the memory_tools / task_runner pattern) — tests
# and the API/scheduler resolve this at call time.
SESSION_FACTORY: Optional[Callable[[], Any]] = None

# Background-run state for the API (single pass at a time; poll status).
_INDEXING = False
_RUNNING: set[asyncio.Task] = set()


@dataclass
class IndexStats:
    """What one pass did — the /api/index/rebuild + status payload."""
    scanned: int = 0        # indexable files visited
    indexed: int = 0        # new files added
    updated: int = 0        # changed files re-embedded
    skipped: int = 0        # unchanged, or unextractable-and-not-indexed
    removed: int = 0        # ledger rows soft-deleted (gone / de-scoped / cleared)
    chunks: int = 0         # vectors written this pass
    errors: int = 0         # files that raised during processing
    error: Optional[str] = None   # a pass-level failure (e.g. no vector store)
    folders: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------------ chunking

def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping windows, breaking on whitespace near each
    boundary so a word/sentence is rarely cut. Deterministic and bounded."""
    text = re.sub(r"[ \t]+", " ", text).strip()
    if not text:
        return []
    chunks: list[str] = []
    start, n = 0, len(text)
    while start < n and len(chunks) < MAX_CHUNKS_PER_FILE:
        end = min(start + size, n)
        if end < n:
            window = text[start:end]
            brk = max(window.rfind("\n"), window.rfind(". "), window.rfind(" "))
            if brk > size * 0.5:
                end = start + brk + 1
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return chunks


def _chunk_point_id(file_id: str, index: int) -> str:
    return str(uuid.uuid5(_CHUNK_NS, f"{file_id}:{index}"))


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


# ----------------------------------------------------------- Qdrant helpers

async def _delete_points(qdrant: Any, file_id: str, chunk_count: int) -> None:
    if qdrant is None or chunk_count <= 0:
        return
    from qdrant_client.http import models as qm

    ids = [_chunk_point_id(file_id, i) for i in range(chunk_count)]
    try:
        await qdrant.delete(
            collection_name=FILE_CHUNKS_COLLECTION,
            points_selector=qm.PointIdsList(points=ids),
        )
    except Exception as e:
        logger.warning(f"file_index: deleting old chunks for {file_id} failed: {e}")


async def _upsert_points(
    qdrant: Any, row: FileIndex, chunks: list[str], vectors: list[list[float]]
) -> int:
    if qdrant is None or not chunks:
        return 0
    from qdrant_client.http import models as qm

    points = []
    for i, (chunk, vector) in enumerate(zip(chunks, vectors)):
        points.append(qm.PointStruct(
            id=_chunk_point_id(row.id, i),
            vector=vector,
            payload={
                "file_id": row.id,
                "path": row.path,
                "filename": row.filename,
                "ext": row.ext,
                "chunk_index": i,
                "text": chunk[:CHUNK_PAYLOAD_TEXT_CAP],
            },
        ))
    try:
        await qdrant.upsert(collection_name=FILE_CHUNKS_COLLECTION, points=points)
        return len(points)
    except Exception as e:
        logger.warning(f"file_index: upserting chunks for {row.path} failed: {e}")
        return 0


# ---------------------------------------------------------------- the walk

def _resolved_exclusions(exclusions) -> list[Path]:
    out: list[Path] = []
    for raw in exclusions or ():
        try:
            out.append(_resolve_path(raw))
        except ValueError:
            continue
    return out


def _is_excluded(path: Path, exclusions: list[Path]) -> bool:
    for ex in exclusions:
        if path == ex or ex in path.parents:
            return True
    return False


def _iter_indexable_files(folders, exclusions: list[Path]):
    """Yield (folder_root, Path) for every indexable file under the configured
    folders, honoring the reused safety pruning + exclusion list. A generator
    so the caller can bound total work."""
    for raw in folders or ():
        try:
            root = _resolve_path(raw)
        except ValueError:
            continue
        if _blocked_reason(root) is not None or not root.is_dir():
            continue
        if _is_excluded(root, exclusions):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            here = Path(dirpath)
            # Prune protected / skip / hidden / excluded subtrees in place.
            kept = []
            for d in dirnames:
                child = here / d
                low = d.lower()
                if low in _SKIP_DIR_NAMES or d.startswith("."):
                    continue
                if any(child == p or p in child.parents for p in _PROTECTED):
                    continue
                if _is_excluded(child, exclusions):
                    continue
                kept.append(d)
            dirnames[:] = kept
            for name in filenames:
                fpath = here / name
                if not is_indexable(fpath):
                    continue
                if _is_excluded(fpath, exclusions):
                    continue
                yield str(root), fpath


# -------------------------------------------------------------- core pass

async def _load_row(db: AsyncSession, path_str: str) -> Optional[FileIndex]:
    result = await db.execute(select(FileIndex).where(FileIndex.path == path_str))
    return result.scalar_one_or_none()


async def _clear_row(db: AsyncSession, qdrant: Any, row: FileIndex) -> None:
    """Soft-delete a ledger row and remove its vectors."""
    await _delete_points(qdrant, row.id, row.chunk_count)
    row.is_active = False
    row.chunk_count = 0
    row.indexed_at = utc_now()


async def index_folders(
    db: AsyncSession,
    qdrant: Any,
    config: FileIndexConfig,
    *,
    full: bool = False,
    embed: Callable[[list[str]], Awaitable[list[list[float]]]] = embed_batch,
) -> IndexStats:
    """Index (or incrementally re-index) every configured folder. Assumes a
    live qdrant client (the runner guards on None)."""
    stats = IndexStats(folders=list(config.folders))
    exclusions = _resolved_exclusions(config.exclusions)
    seen: set[str] = set()

    for folder_root, fpath in _iter_indexable_files(config.folders, exclusions):
        if stats.scanned >= MAX_FILES_SCANNED:
            logger.warning("file_index: hit MAX_FILES_SCANNED — stopping this pass")
            break
        try:
            stat = fpath.stat()
        except OSError:
            continue
        path_str = str(fpath)
        seen.add(path_str)
        stats.scanned += 1

        row = await _load_row(db, path_str)
        if (
            row is not None and row.is_active and not full
            and row.size == stat.st_size and row.mtime == stat.st_mtime
        ):
            stats.skipped += 1
            continue

        try:
            text = await extract_text(fpath)
        except Exception as e:
            logger.debug(f"file_index: extract raised for {path_str}: {e}")
            stats.errors += 1
            continue

        if not text:
            # Unreadable/empty now: clear any prior index; otherwise nothing.
            if row is not None and row.is_active:
                await _clear_row(db, qdrant, row)
                stats.removed += 1
            else:
                stats.skipped += 1
            continue

        chunks = chunk_text(text)
        try:
            vectors = await embed(chunks) if chunks else []
        except Exception as e:
            logger.warning(f"file_index: embedding failed for {path_str}: {e}")
            stats.errors += 1
            continue

        new_row = row is None
        if new_row:
            # folder_root is NOT NULL — set it before the flush that inserts.
            row = FileIndex(path=path_str, folder_root=folder_root)
            db.add(row)
            await db.flush()  # assign row.id for deterministic chunk ids
        else:
            await _delete_points(qdrant, row.id, row.chunk_count)

        row.folder_root = folder_root
        row.filename = fpath.name
        row.ext = fpath.suffix.lower()
        row.size = stat.st_size
        row.mtime = stat.st_mtime
        row.content_hash = _text_hash(text)
        row.is_active = True
        row.indexed_at = utc_now()

        written = await _upsert_points(qdrant, row, chunks, vectors)
        row.chunk_count = written
        stats.chunks += written
        stats.indexed += 1 if new_row else 0
        stats.updated += 0 if new_row else 1

    # Prune: active rows we did not encounter this pass are gone / de-scoped.
    active = await db.execute(select(FileIndex).where(FileIndex.is_active.is_(True)))
    for row in active.scalars().all():
        if row.path not in seen:
            await _clear_row(db, qdrant, row)
            stats.removed += 1

    await db.commit()
    logger.info(
        f"file_index pass: scanned={stats.scanned} indexed={stats.indexed} "
        f"updated={stats.updated} skipped={stats.skipped} removed={stats.removed} "
        f"chunks={stats.chunks} errors={stats.errors}"
    )
    return stats


# ------------------------------------------------------- runner + status

def _session_factory() -> Callable[[], Any]:
    if SESSION_FACTORY is not None:
        return SESSION_FACTORY
    from app.db.database import AsyncSessionLocal
    return AsyncSessionLocal


async def run_index(*, full: bool = False) -> IndexStats:
    """One pass with its own session, resolving qdrant + config. Safe to await
    directly (the scheduler handler in Part 3 does)."""
    qdrant = get_qdrant_client()
    if qdrant is None:
        logger.warning("file_index: no vector store — skipping index pass")
        return IndexStats(error="Vector store (Qdrant) is unavailable.")
    factory = _session_factory()
    async with factory() as db:
        config = await get_file_index_config(db)
        stats = await index_folders(db, qdrant, config, full=full)
    return stats


def is_indexing() -> bool:
    return _INDEXING


async def start_index_in_background(*, full: bool = False) -> bool:
    """Kick off a detached index pass. Returns False if one is already running
    (single pass at a time — poll status)."""
    global _INDEXING
    if _INDEXING:
        return False
    _INDEXING = True

    async def _runner() -> None:
        global _INDEXING
        try:
            await run_index(full=full)
        except Exception as e:
            logger.error(f"file_index background pass failed: {e}")
        finally:
            _INDEXING = False

    task = asyncio.create_task(_runner())
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)
    return True


async def wait_for_index() -> None:
    """Await any in-flight background pass (tests / shutdown)."""
    while _RUNNING:
        await asyncio.gather(*list(_RUNNING), return_exceptions=True)


async def get_index_summary(db: AsyncSession) -> dict:
    """Counts for the status endpoint — active files, total vectors, freshness."""
    file_count = await db.scalar(
        select(func.count()).select_from(FileIndex).where(FileIndex.is_active.is_(True))
    )
    chunk_total = await db.scalar(
        select(func.coalesce(func.sum(FileIndex.chunk_count), 0)).where(
            FileIndex.is_active.is_(True)
        )
    )
    last_indexed = await db.scalar(
        select(func.max(FileIndex.indexed_at)).where(FileIndex.is_active.is_(True))
    )
    from app.db.models import utc_iso
    return {
        "indexed_files": int(file_count or 0),
        "indexed_chunks": int(chunk_total or 0),
        "last_indexed_at": utc_iso(last_indexed) if last_indexed else None,
        "indexing": _INDEXING,
    }
