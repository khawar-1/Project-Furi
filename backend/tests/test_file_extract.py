"""
Phase 6 Part 2 — file content extraction.

Best-effort text extraction from txt/md/pdf/docx: readable text out, None on
anything unreadable (binary, corrupt, unsupported), capped, never raising.
"""
import pytest

from app.core.file_extract import (
    INDEXABLE_EXTS,
    MAX_EXTRACT_CHARS,
    extract_text,
    is_indexable,
)


def test_is_indexable_matrix(tmp_path):
    from pathlib import Path
    assert is_indexable(Path("notes.txt"))
    assert is_indexable(Path("readme.MD"))       # case-insensitive
    assert is_indexable(Path("paper.pdf"))
    assert is_indexable(Path("letter.docx"))
    assert not is_indexable(Path("photo.png"))
    assert not is_indexable(Path("archive.zip"))
    assert not is_indexable(Path("legacy.doc"))  # old binary .doc unsupported


async def test_extract_plain_text(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("Hello world\nsecond line", encoding="utf-8")
    assert await extract_text(f) == "Hello world\nsecond line"


async def test_extract_markdown(tmp_path):
    f = tmp_path / "n.md"
    f.write_text("# Title\n\nBody text about vector databases.", encoding="utf-8")
    text = await extract_text(f)
    assert "vector databases" in text


async def test_binary_disguised_as_text_returns_none(tmp_path):
    f = tmp_path / "bin.txt"
    f.write_bytes(b"PK\x03\x04\x00\x00binary garbage")
    assert await extract_text(f) is None


async def test_empty_file_returns_none(tmp_path):
    f = tmp_path / "empty.txt"
    f.write_text("   \n  ", encoding="utf-8")
    assert await extract_text(f) is None


async def test_unsupported_extension_returns_none(tmp_path):
    f = tmp_path / "x.png"
    f.write_bytes(b"\x89PNG\r\n")
    assert await extract_text(f) is None


async def test_output_is_capped(tmp_path):
    f = tmp_path / "big.txt"
    f.write_text("a" * (MAX_EXTRACT_CHARS + 5000), encoding="utf-8")
    text = await extract_text(f)
    assert len(text) == MAX_EXTRACT_CHARS


async def test_docx_extraction(tmp_path):
    docx = pytest.importorskip("docx")
    path = tmp_path / "letter.docx"
    doc = docx.Document()
    doc.add_paragraph("Dear team,")
    doc.add_paragraph("The LangGraph migration is complete.")
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Task"
    table.rows[0].cells[1].text = "Done"
    doc.save(str(path))

    text = await extract_text(path)
    assert "LangGraph migration is complete" in text
    assert "Task" in text and "Done" in text


async def test_corrupt_pdf_returns_none(tmp_path):
    f = tmp_path / "broken.pdf"
    f.write_bytes(b"%PDF-1.4 this is not a real pdf body")
    assert await extract_text(f) is None  # best-effort: garbage → None, no raise


def test_indexable_exts_frozen():
    # txt/md families + pdf + docx; no surprises.
    assert ".txt" in INDEXABLE_EXTS and ".pdf" in INDEXABLE_EXTS and ".docx" in INDEXABLE_EXTS
    assert ".exe" not in INDEXABLE_EXTS
