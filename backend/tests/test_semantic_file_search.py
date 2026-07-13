"""
Phase 6 Part 3 — semantic_file_search tool (app/tools/semantic_file_tools.py).

The READ tool over Part 2's file index. Uses the memory_tools test pattern: a
file-backed DB the tool's own sessions point at (SESSION_FACTORY), plus a FAKE
qdrant + a stubbed embed_text so the suite never loads fastembed or touches the
real vector store. Covers grouping/ranking, the metadata refiners (filename /
folder / ISO date), the non-ISO refusal, the empty-query failure, the no-vector-
store fallback, the empty-index note, and the rendering formatter.
"""
import time
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.tools  # noqa: F401 — registers every tool
from app.agents.rendering import _RESULT_FORMATTERS, _fmt_semantic_file_search
from app.db.database import Base
from app.db.models import FileIndex, Message, utc_now
from app.tools import semantic_file_tools
from app.tools.registry import execute_tool, registry

NOW = time.time()
DAY = 86400.0


# ================================================================= fixtures

@pytest_asyncio.fixture
async def idx_db(tmp_path_factory, monkeypatch):
    """File-backed DB the tool's own sessions point at (SESSION_FACTORY)."""
    db_dir = tmp_path_factory.mktemp("semfile-db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_dir / 'idx.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(semantic_file_tools, "SESSION_FACTORY", factory)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _seed(db) -> None:
    db.add_all([
        FileIndex(
            id="f1", path=r"C:\Users\me\Docs\langgraph_notes.pdf",
            folder_root=r"C:\Users\me\Docs", filename="langgraph_notes.pdf",
            ext=".pdf", size=100, mtime=NOW - 2 * DAY, content_hash="h1",
            chunk_count=2, is_active=True,
        ),
        FileIndex(
            id="f2", path=r"C:\Users\me\Downloads\budget_report.docx",
            folder_root=r"C:\Users\me\Downloads", filename="budget_report.docx",
            ext=".docx", size=200, mtime=NOW - 100 * DAY, content_hash="h2",
            chunk_count=1, is_active=True,
        ),
        FileIndex(
            id="f3", path=r"C:\Users\me\Docs\trip_readme.txt",
            folder_root=r"C:\Users\me\Docs", filename="trip_readme.txt",
            ext=".txt", size=50, mtime=NOW - 5 * DAY, content_hash="h3",
            chunk_count=1, is_active=True,
        ),
    ])
    await db.commit()


class _Hit:
    def __init__(self, id, score, payload):
        self.id = id
        self.score = score
        self.payload = payload


class _FakeQdrant:
    """Minimal AsyncQdrantClient stand-in — only .search is exercised."""
    def __init__(self, hits):
        self._hits = hits
        self.calls = []

    async def search(self, collection_name, query_vector, limit, score_threshold=0.0):
        self.calls.append({"collection": collection_name, "limit": limit,
                           "threshold": score_threshold})
        return list(self._hits)[:limit]


def _chunk_hit(file_id, path, filename, ext, score, text):
    return _Hit(f"{file_id}:chunk", score, {
        "file_id": file_id, "path": path, "filename": filename,
        "ext": ext, "chunk_index": 0, "text": text,
    })


def _wire_semantic(monkeypatch, hits):
    """Point the tool at a fake qdrant + a stub embedder (no fastembed load)."""
    async def _fake_embed(text):
        return [0.01] * 384
    monkeypatch.setattr("app.memory.embedder.embed_text", _fake_embed)
    fake = _FakeQdrant(hits)
    monkeypatch.setattr(semantic_file_tools, "_qdrant", lambda: fake)
    return fake


def _default_hits():
    return [
        _chunk_hit("f1", r"C:\Users\me\Docs\langgraph_notes.pdf",
                   "langgraph_notes.pdf", ".pdf", 0.90,
                   "LangGraph is a framework for building stateful agents."),
        _chunk_hit("f1", r"C:\Users\me\Docs\langgraph_notes.pdf",
                   "langgraph_notes.pdf", ".pdf", 0.70, "A weaker second chunk."),
        _chunk_hit("f2", r"C:\Users\me\Downloads\budget_report.docx",
                   "budget_report.docx", ".docx", 0.55, "Q3 budget figures."),
    ]


async def _run(**kwargs):
    return await registry.get("semantic_file_search").execute(**kwargs)


# ============================================================ semantic search

async def test_ranks_and_groups_by_file(idx_db, monkeypatch):
    await _seed(idx_db)
    _wire_semantic(monkeypatch, _default_hits())
    result = await _run(query="langgraph framework")
    assert result.success is True
    assert result.output["count"] == 2  # f1's two chunks collapse to one file
    # f1 wins: higher base score + filename token 'langgraph' + recency.
    assert result.output["matches"][0]["filename"] == "langgraph_notes.pdf"
    assert result.output["matches"][0]["score"] >= result.output["matches"][1]["score"]


async def test_best_chunk_score_and_snippet_win(idx_db, monkeypatch):
    await _seed(idx_db)
    _wire_semantic(monkeypatch, _default_hits())
    result = await _run(query="langgraph")
    top = result.output["matches"][0]
    assert "LangGraph is a framework" in top["snippet"]  # the 0.90 chunk, not 0.70
    assert top["path"] == r"C:\Users\me\Docs\langgraph_notes.pdf"  # full path


async def test_filename_contains_filters(idx_db, monkeypatch):
    await _seed(idx_db)
    _wire_semantic(monkeypatch, _default_hits())
    result = await _run(query="anything", filename_contains="budget")
    names = [m["filename"] for m in result.output["matches"]]
    assert names == ["budget_report.docx"]


async def test_folder_filters(idx_db, monkeypatch):
    await _seed(idx_db)
    _wire_semantic(monkeypatch, _default_hits())
    result = await _run(query="anything", folder=r"C:\Users\me\Downloads")
    assert [m["filename"] for m in result.output["matches"]] == ["budget_report.docx"]


async def test_modified_after_filters(idx_db, monkeypatch):
    await _seed(idx_db)
    _wire_semantic(monkeypatch, _default_hits())
    cutoff = (datetime.now() - timedelta(days=10)).date().isoformat()
    result = await _run(query="anything", modified_after=cutoff)
    # f2 is 100 days old → dropped; f1 (2 days) survives.
    assert [m["filename"] for m in result.output["matches"]] == ["langgraph_notes.pdf"]


async def test_non_iso_date_is_refused(idx_db, monkeypatch):
    await _seed(idx_db)
    _wire_semantic(monkeypatch, _default_hits())
    result = await _run(query="x", modified_after="03/04/2026")
    assert result.success is False
    assert "iso" in result.error.lower()


async def test_empty_query_fails(idx_db):
    result = await _run(query="   ")
    assert result.success is False
    assert "query" in result.error.lower()


# ------------------------------------------------------------- fallback path

async def test_no_vector_store_falls_back_to_filename(idx_db, monkeypatch):
    await _seed(idx_db)
    monkeypatch.setattr(semantic_file_tools, "_qdrant", lambda: None)
    result = await _run(query="langgraph")
    assert result.success is True
    assert [m["filename"] for m in result.output["matches"]] == ["langgraph_notes.pdf"]
    assert "unavailable" in result.output["note"].lower()


# ------------------------------------------------------------- empty index

async def test_empty_index_zero_matches_fails_with_recovery(idx_db, monkeypatch):
    """Zero matches over an EMPTY index is an unavailable capability, not an
    answer — the tool FAILS so the replan loop recovers to a search_files name
    search instead of completing on a Settings hint (live bug 2026-07-13)."""
    # No rows seeded; fake qdrant returns nothing.
    _wire_semantic(monkeypatch, [])
    result = await _run(query="whatever")
    assert result.success is False
    assert "search_files" in result.error
    assert "empty or disabled" in result.error.lower()


async def test_empty_index_fallback_fails_with_recovery(idx_db, monkeypatch):
    """The qdrant-None fallback over an empty ledger fails the same way."""
    monkeypatch.setattr(semantic_file_tools, "_qdrant", lambda: None)
    result = await _run(query="whatever")
    assert result.success is False
    assert "search_files" in result.error


async def test_healthy_index_zero_matches_stays_success(idx_db, monkeypatch):
    """A populated index with no hits is a REAL 'nothing matches' answer."""
    await _seed(idx_db)
    _wire_semantic(monkeypatch, [])
    result = await _run(query="quantum knitting")
    assert result.success is True
    assert result.output["count"] == 0


# ------------------------------------------------ read-level, no approval

async def test_read_level_runs_without_approval(idx_db, monkeypatch):
    await _seed(idx_db)
    monkeypatch.setattr(semantic_file_tools, "_qdrant", lambda: None)
    result = await execute_tool(
        "semantic_file_search", {"query": "langgraph"}, idx_db, approved=False
    )
    assert result.success is True  # READ never needs approval


# ------------------------------------------------------------ rendering

def test_formatter_is_registered():
    assert _RESULT_FORMATTERS.get("semantic_file_search") is _fmt_semantic_file_search


def test_fmt_groups_and_fences_snippets():
    out = {
        "query": "x", "count": 1,
        "matches": [{
            "path": r"C:\a\b\notes.pdf", "filename": "notes.pdf",
            "folder_root": r"C:\a\b", "score": 0.9,
            "modified": "2026-07-10T00:00:00", "snippet": "hello world",
        }],
    }
    text = _fmt_semantic_file_search(out)
    assert "notes.pdf" in text
    assert "hello world" in text
    assert "```" in text  # snippet fenced
    assert r"C:\a\b" in text


def test_fmt_empty():
    assert "No matching files" in _fmt_semantic_file_search({"matches": []})


def test_fmt_surfaces_note_when_empty():
    text = _fmt_semantic_file_search(
        {"matches": [], "note": "The file index is empty or turned off."}
    )
    assert "empty" in text.lower()


# ============================================ cross-source (Part 4) recall

class _MultiQdrant:
    """Returns different hits per collection — files vs. conversations."""
    def __init__(self, file_hits, convo_hits):
        self.file_hits = file_hits
        self.convo_hits = convo_hits

    async def search(self, collection_name, query_vector, limit, score_threshold=0.0):
        pool = self.convo_hits if collection_name == "conversation_messages" \
            else self.file_hits
        return list(pool)[:limit]


def _msg_hit(mid, score, text, role="user"):
    return _Hit(mid, score, {
        "message_id": mid, "session_id": "sess-1", "role": role,
        "text": text, "created_at": None,
    })


async def _seed_message(db, mid="m1", content="we agreed the budget was too tight"):
    m = Message(id=mid, session_id="sess-1", role="user", content=content)
    m.created_at = utc_now()
    m.embedded_at = utc_now()
    db.add(m)
    await db.commit()


def _wire_multi(monkeypatch, file_hits, convo_hits):
    async def _fake_embed(text):
        return [0.01] * 384
    monkeypatch.setattr("app.memory.embedder.embed_text", _fake_embed)
    fake = _MultiQdrant(file_hits, convo_hits)
    monkeypatch.setattr(semantic_file_tools, "_qdrant", lambda: fake)
    return fake


async def test_cross_source_returns_files_and_conversations(idx_db, monkeypatch):
    await _seed(idx_db)
    await _seed_message(idx_db)
    _wire_multi(
        monkeypatch,
        file_hits=[_chunk_hit(
            "f2", r"C:\Users\me\Downloads\budget_report.docx",
            "budget_report.docx", ".docx", 0.80, "Q3 budget figures.")],
        convo_hits=[_msg_hit("m1", 0.88, "we agreed the budget was too tight")],
    )
    result = await _run(query="the budget")
    assert result.success is True
    types = {m["type"] for m in result.output["matches"]}
    assert types == {"file", "conversation"}
    convo = next(m for m in result.output["matches"] if m["type"] == "conversation")
    assert convo["session_id"] == "sess-1"
    assert convo["role"] == "user"
    assert "budget" in convo["snippet"]


async def test_cross_source_ranked_together(idx_db, monkeypatch):
    await _seed(idx_db)
    await _seed_message(idx_db, content="budget talk")
    # Conversation scores higher than the file → it ranks first in the merged list.
    _wire_multi(
        monkeypatch,
        file_hits=[_chunk_hit(
            "f2", r"C:\Users\me\Downloads\budget_report.docx",
            "budget_report.docx", ".docx", 0.40, "Q3 budget figures.")],
        convo_hits=[_msg_hit("m1", 0.95, "budget talk")],
    )
    result = await _run(query="budget")
    assert result.output["matches"][0]["type"] == "conversation"


async def test_file_refiner_excludes_conversations(idx_db, monkeypatch):
    await _seed(idx_db)
    await _seed_message(idx_db)
    _wire_multi(
        monkeypatch,
        file_hits=[_chunk_hit(
            "f2", r"C:\Users\me\Downloads\budget_report.docx",
            "budget_report.docx", ".docx", 0.80, "Q3 budget figures.")],
        convo_hits=[_msg_hit("m1", 0.99, "we agreed the budget was too tight")],
    )
    # A folder refiner means "a file" → conversations are not searched.
    result = await _run(query="budget", folder=r"C:\Users\me\Downloads")
    assert result.success is True
    assert all(m["type"] == "file" for m in result.output["matches"])


def test_fmt_renders_conversation_matches():
    out = {
        "query": "budget", "count": 2,
        "matches": [
            {"type": "file", "path": r"C:\a\notes.pdf", "filename": "notes.pdf",
             "folder_root": r"C:\a", "score": 0.9,
             "modified": "2026-07-10T00:00:00", "snippet": "file snippet"},
            {"type": "conversation", "message_id": "m1", "session_id": "s1",
             "role": "user", "score": 0.8, "created": "2026-07-11T09:00:00+00:00",
             "snippet": "we discussed the budget"},
        ],
    }
    text = _fmt_semantic_file_search(out)
    assert "notes.pdf" in text
    assert "conversation message(s)" in text
    assert "we discussed the budget" in text
    assert "2026-07-11" in text
