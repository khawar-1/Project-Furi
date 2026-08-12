"""
Furi OS — Screen OCR (Phase 8, Part 3)

The opt-in, hard-gated on-screen-context reader. Electron captures a downscaled
screen frame (desktopCapturer) and POSTs it here ONLY while screen OCR is armed;
this module OCRs it locally and condenses the text to a short summary. The raw
image never touches disk and is dropped the moment OCR returns — retention is
the rolling summary only (held in app/core/context_store.py, in memory).

OCR runs on RapidOCR (onnxruntime) behind an injectable factory — the
STT_MODEL_FACTORY seam: tests swap OCR_ENGINE_FACTORY for a fake returning
canned text, so the suite never loads a real model or touches onnxruntime. The
default factory lazy-builds the engine once (a slow first load is fine for a
periodic, best-effort feature) and runs off the event loop.

The summary is produced DETERMINISTICALLY (condense_ocr_text) — no LLM call, so
periodic capture is free and cannot exfiltrate the screen to a provider.
"""
import asyncio
import re
from typing import Callable, Optional

from loguru import logger

#: The injectable seam. A zero-arg factory returning a callable that maps image
#: bytes → recognized text (newline-separated). Tests replace it with a fake;
#: nothing else in the codebase constructs an OCR engine any other way.
def _default_ocr_factory() -> Callable[[bytes], str]:
    """Build a RapidOCR-backed reader. Imported lazily so merely importing this
    module (the API router does) never pays for onnxruntime — only an actual
    capture, on an opted-in machine, loads the model."""
    from rapidocr_onnxruntime import RapidOCR  # heavy: onnxruntime + models

    from app.core.gpu_bootstrap import cpu_worker_threads

    # RapidOCR was built with NO thread arguments, so onnxruntime applied its
    # default of one intra-op thread per core — across THREE graphs (detection,
    # classification, recognition). A screen capture fires every 30s and again
    # on every foreground-window change, so that is a repeated full-core burst
    # on a machine the user is trying to work on. RapidOCR exposes per-graph
    # thread counts as constructor kwargs; they are passed defensively because
    # the names are version-dependent and a capture must never fail over a
    # tuning knob (the lazy-import discipline this factory already follows).
    threads = cpu_worker_threads()
    try:
        engine = RapidOCR(
            det_intra_op_num_threads=threads,
            cls_intra_op_num_threads=threads,
            rec_intra_op_num_threads=threads,
        )
    except TypeError:
        logger.debug("RapidOCR does not accept thread kwargs; using its defaults.")
        engine = RapidOCR()

    def _run(image_bytes: bytes) -> str:
        # RapidOCR accepts encoded image bytes directly; result is a list of
        # [box, text, score] (or None when nothing is found).
        result, _elapsed = engine(image_bytes)
        if not result:
            return ""
        return "\n".join(
            str(line[1]) for line in result if isinstance(line, (list, tuple)) and len(line) > 1
        )

    return _run


OCR_ENGINE_FACTORY: Callable[[], Callable[[bytes], str]] = _default_ocr_factory

# The lazily-built singleton reader (retention=none: this is a model, not data).
_engine: Optional[Callable[[bytes], str]] = None


def reset_screen_ocr() -> None:
    """Test hook: drop the cached engine and restore the default factory."""
    global _engine, OCR_ENGINE_FACTORY
    _engine = None
    OCR_ENGINE_FACTORY = _default_ocr_factory


def _get_engine() -> Callable[[bytes], str]:
    global _engine
    if _engine is None:
        _engine = OCR_ENGINE_FACTORY()
    return _engine


def _run_sync(image_bytes: bytes) -> str:
    return _get_engine()(image_bytes) or ""


async def run_ocr(image_bytes: bytes) -> str:
    """OCR one captured frame → recognized text (newline-separated), or "".
    Runs off the event loop (the first call may build the model). Raises on a
    genuine engine failure — the API layer degrades it to a clean error."""
    return await asyncio.to_thread(_run_sync, image_bytes)


# ----------------------------------------------------- deterministic condense

#: Caps on the rolling summary — a short on-screen-context digest, not a
#: transcript of the screen.
CONDENSE_MAX_LINES = 8
CONDENSE_MAX_CHARS = 600

#: A line is noise unless it carries at least this many word characters — OCR
#: routinely emits stray glyphs, single punctuation marks, and UI chrome.
_MIN_WORD_CHARS = 3

_WORD_CHARS_RE = re.compile(r"\w")


def _word_char_count(line: str) -> int:
    return len(_WORD_CHARS_RE.findall(line))


def condense_ocr_text(text: str, *, max_lines: int = CONDENSE_MAX_LINES,
                      max_chars: int = CONDENSE_MAX_CHARS) -> str:
    """Reduce raw OCR text to a short, deterministic on-screen-context summary.

    Strips blank/noise lines, de-duplicates (case-insensitive, order-preserving),
    keeps the most informative `max_lines` by word-char count (re-ordered back to
    their on-screen order for readability), and caps the total length. Pure and
    tested — never an LLM call."""
    if not text:
        return ""

    seen: set[str] = set()
    kept: list[tuple[int, str]] = []  # (original index, line)
    for idx, raw in enumerate(text.splitlines()):
        line = " ".join(raw.split())  # collapse internal whitespace
        if _word_char_count(line) < _MIN_WORD_CHARS:
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        kept.append((idx, line))

    if not kept:
        return ""

    # Most informative lines first, then restore on-screen order for reading.
    top = sorted(kept, key=lambda p: _word_char_count(p[1]), reverse=True)[:max_lines]
    top.sort(key=lambda p: p[0])
    summary = " · ".join(line for _idx, line in top)

    if len(summary) > max_chars:
        summary = summary[:max_chars].rstrip(" ·") + "…"
    return summary
