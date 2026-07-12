"""
Phase 6 Part 2 — the semantic file index service.

Exercised against a REAL in-memory Qdrant (AsyncQdrantClient(":memory:")) so the
PointStruct / PointIdsList / upsert / delete API shapes are validated, with a
fake 384-dim embedder (no fastembed download, deterministic vectors). The
FileIndex ledger is the conftest in-memory SQLite.

Covered: new-file indexing, incremental skip, changed-file re-embed, full
force, prune-on-delete, exclusion lists, non-indexable + binary skipping,
payload shape, and the config store round-trip.
"""
import os

import pytest
import pytest_asyncio
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qm
from sqlalchemy import select

from app.core import file_index
from app.core.app_settings import (
    FileIndexConfig,
    default_file_index_config,
    get_file_index_config,
    set_file_index_config,
)
from app.core.file_index import chunk_text, get_index_summary, index_folders
from app.db.models import FileIndex


# ------------------------------------------------------------- fixtures

@pytest_asyncio.fixture
async def qmem():
    client = AsyncQdrantClient(location=":memory:")
    await client.create_collection(
        collection_name="file_chunks",
        vectors_config=qm.VectorParams(size=384, distance=qm.Distance.COSINE),
    )
    yield client
    await client.close()


async def fake_embed(texts):
    """Deterministic non-zero 384-dim vectors — no fastembed needed."""
    return [[0.01 * (i + 1)] + [0.5] * 383 for i, _ in enumerate(texts)]


def cfg(folders, exclusions=()):
    return FileIndexConfig(
        enabled=True,
        folders=tuple(str(f) for f in folders),
        exclusions=tuple(str(e) for e in exclusions),
        interval_minutes=360,
    )


async def _count_points(qmem) -> int:
    return (await qmem.count(collection_name="file_chunks", exact=True)).count


async def _active_rows(db):
    result = await db.execute(select(FileIndex).where(FileIndex.is_active.is_(True)))
    return result.scalars().all()


async def _run(db, qmem, config, *, full=False):
    return await index_folders(db, qmem, config, full=full, embed=fake_embed)


# ------------------------------------------------------------- chunking

def test_chunk_text_small_is_one_chunk():
    assert chunk_text("hello world") == ["hello world"]


def test_chunk_text_splits_with_overlap():
    chunks = chunk_text("word " * 800)  # ~4000 chars
    assert len(chunks) > 1
    assert all(len(c) <= file_index.CHUNK_SIZE + 5 for c in chunks)


def test_chunk_text_empty():
    assert chunk_text("   ") == []


# ------------------------------------------------------------- indexing

async def test_indexes_new_files(db_session, qmem, tmp_path):
    (tmp_path / "a.txt").write_text("about vector databases", encoding="utf-8")
    (tmp_path / "b.md").write_text("# notes\nlanggraph agents", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.txt").write_text("nested content here", encoding="utf-8")

    stats = await _run(db_session, qmem, cfg([tmp_path]))
    assert stats.indexed == 3
    assert stats.chunks >= 3
    assert len(await _active_rows(db_session)) == 3
    assert await _count_points(qmem) == stats.chunks


async def test_incremental_skip_unchanged(db_session, qmem, tmp_path):
    (tmp_path / "a.txt").write_text("stable content", encoding="utf-8")
    await _run(db_session, qmem, cfg([tmp_path]))
    before = await _count_points(qmem)

    stats = await _run(db_session, qmem, cfg([tmp_path]))
    assert stats.skipped == 1
    assert stats.indexed == 0 and stats.updated == 0
    assert await _count_points(qmem) == before  # no churn


async def test_changed_file_is_reembedded(db_session, qmem, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("original", encoding="utf-8")
    await _run(db_session, qmem, cfg([tmp_path]))
    row = (await _active_rows(db_session))[0]
    old_mtime = row.mtime

    f.write_text("completely different and much longer content " * 40, encoding="utf-8")
    os.utime(f, (old_mtime + 100, old_mtime + 100))
    stats = await _run(db_session, qmem, cfg([tmp_path]))
    assert stats.updated == 1 and stats.indexed == 0
    # Old chunks were deleted, new ones written — count equals this file's chunks.
    assert await _count_points(qmem) == stats.chunks


async def test_full_forces_reembed(db_session, qmem, tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    await _run(db_session, qmem, cfg([tmp_path]))
    stats = await _run(db_session, qmem, cfg([tmp_path]), full=True)
    assert stats.skipped == 0
    assert stats.updated == 1


async def test_prune_deleted_file(db_session, qmem, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("temporary", encoding="utf-8")
    (tmp_path / "b.txt").write_text("keeper", encoding="utf-8")
    await _run(db_session, qmem, cfg([tmp_path]))
    assert len(await _active_rows(db_session)) == 2

    f.unlink()
    stats = await _run(db_session, qmem, cfg([tmp_path]))
    assert stats.removed == 1
    rows = await _active_rows(db_session)
    assert len(rows) == 1 and rows[0].filename == "b.txt"
    # The pruned file's vectors are gone: points == remaining active chunks.
    assert await _count_points(qmem) == sum(r.chunk_count for r in rows)


async def test_exclusions_skip_subtree(db_session, qmem, tmp_path):
    (tmp_path / "a.txt").write_text("top", encoding="utf-8")
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "s.txt").write_text("private", encoding="utf-8")

    stats = await _run(db_session, qmem, cfg([tmp_path], exclusions=[secret]))
    names = {r.filename for r in await _active_rows(db_session)}
    assert names == {"a.txt"}
    assert stats.indexed == 1


async def test_non_indexable_and_binary_skipped(db_session, qmem, tmp_path):
    (tmp_path / "a.txt").write_text("real", encoding="utf-8")
    (tmp_path / "img.png").write_bytes(b"\x89PNG\r\n binary")
    (tmp_path / "bin.txt").write_bytes(b"\x00\x01\x02 not text")  # indexable ext, binary content

    stats = await _run(db_session, qmem, cfg([tmp_path]))
    names = {r.filename for r in await _active_rows(db_session)}
    assert names == {"a.txt"}
    # png never scanned (not indexable); bin.txt scanned but unextractable → skipped
    assert stats.scanned == 2
    assert stats.indexed == 1


async def test_point_payload_shape(db_session, qmem, tmp_path):
    (tmp_path / "a.txt").write_text("searchable snippet text", encoding="utf-8")
    await _run(db_session, qmem, cfg([tmp_path]))
    points, _ = await qmem.scroll(collection_name="file_chunks", limit=10, with_payload=True)
    assert points
    payload = points[0].payload
    assert set(payload) >= {"file_id", "path", "filename", "ext", "chunk_index", "text"}
    assert payload["ext"] == ".txt"
    assert "searchable snippet" in payload["text"]


async def test_summary_reports_counts(db_session, qmem, tmp_path):
    (tmp_path / "a.txt").write_text("content", encoding="utf-8")
    await _run(db_session, qmem, cfg([tmp_path]))
    summary = await get_index_summary(db_session)
    assert summary["indexed_files"] == 1
    assert summary["indexed_chunks"] >= 1
    assert summary["last_indexed_at"] is not None
    assert summary["indexing"] is False


# --------------------------------------------------------------- config

async def test_config_default_is_opt_in(db_session):
    config = await get_file_index_config(db_session)
    assert config.enabled is False       # personal files are opt-in
    assert config.interval_minutes == 360


async def test_config_round_trip(db_session, tmp_path):
    await set_file_index_config(db_session, cfg([tmp_path], exclusions=[tmp_path / "x"]))
    loaded = await get_file_index_config(db_session)
    assert loaded.enabled is True
    assert loaded.folders == (str(tmp_path),)
    assert loaded.exclusions == (str(tmp_path / "x"),)


async def test_config_clamps_bad_interval(db_session):
    await set_file_index_config(db_session, FileIndexConfig(
        enabled=True, folders=("x",), exclusions=(), interval_minutes=1,
    ))
    loaded = await get_file_index_config(db_session)
    assert loaded.interval_minutes == file_index_min()


def file_index_min():
    from app.core.app_settings import FILE_INDEX_MIN_INTERVAL
    return FILE_INDEX_MIN_INTERVAL


async def test_config_corrupt_row_falls_back_to_default(db_session):
    from app.core.app_settings import FILE_INDEX_CONFIG_KEY, set_setting
    await set_setting(db_session, FILE_INDEX_CONFIG_KEY, "not-a-dict")
    config = await get_file_index_config(db_session)
    assert config.enabled == default_file_index_config().enabled
