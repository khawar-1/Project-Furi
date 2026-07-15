"""
Phase 8 Part 3 — screen OCR: the deterministic condenser + the injectable
engine seam. No test loads a real OCR model (the autouse _hermetic_screen_ocr
fixture refuses; these swap in a fake returning canned text).
"""
import pytest

from app.core import screen_ocr
from app.core.screen_ocr import condense_ocr_text, run_ocr


# --------------------------------------------------------------- condense

def test_condense_empty():
    assert condense_ocr_text("") == ""
    assert condense_ocr_text("   \n  \n") == ""


def test_condense_strips_noise_lines():
    # Single glyphs / punctuation are OCR noise (below the word-char floor).
    text = "Inbox — 3 unread\n|\n.\nx\nCompose a message"
    out = condense_ocr_text(text)
    assert "Inbox" in out
    assert "Compose a message" in out
    assert "|" not in out


def test_condense_dedupes_case_insensitive():
    text = "Save File\nsave file\nSAVE FILE\nOpen Folder"
    out = condense_ocr_text(text)
    assert out.count("ave") == 1 or out.lower().count("save file") == 1
    assert "Open Folder" in out


def test_condense_keeps_most_informative_and_preserves_order():
    # More lines than the cap → the richest survive, in on-screen order.
    lines = [
        "a b",                              # short, likely dropped
        "The quarterly revenue report",     # rich
        "cd",
        "Meeting notes for the product sync",  # rich
        "ef",
    ]
    out = condense_ocr_text("\n".join(lines), max_lines=2)
    assert "quarterly revenue report" in out
    assert "product sync" in out
    # order preserved: revenue appears before product sync
    assert out.index("quarterly") < out.index("product")


def test_condense_caps_length():
    long_line = "word " * 500
    out = condense_ocr_text(long_line, max_chars=100)
    assert len(out) <= 101  # 100 + the ellipsis
    assert out.endswith("…")


# --------------------------------------------------------------- engine seam

async def test_run_ocr_uses_injected_engine():
    screen_ocr.OCR_ENGINE_FACTORY = lambda: (lambda data: "hello from a fake screen")
    text = await run_ocr(b"\x89PNG-not-really")
    assert text == "hello from a fake screen"


async def test_run_ocr_engine_built_once():
    builds = {"n": 0}

    def factory():
        builds["n"] += 1
        return lambda data: "x"

    screen_ocr.OCR_ENGINE_FACTORY = factory
    await run_ocr(b"1")
    await run_ocr(b"2")
    assert builds["n"] == 1  # lazily built, then reused


async def test_run_ocr_empty_result():
    screen_ocr.OCR_ENGINE_FACTORY = lambda: (lambda data: "")
    assert await run_ocr(b"1") == ""
