"""
Furi OS — Local Embedder
Uses fastembed (BAAI/bge-small-en-v1.5) to generate 384-dim vectors locally.
No API key, no extra services, no Docker — runs inside the Python process.
"""
import asyncio
from functools import lru_cache
from pathlib import Path
from typing import List

from loguru import logger

# Model weights live under ~/.jarvis/fastembed — a STABLE home, the ~/.jarvis
# convention shared with whisper/kokoro/voices. fastembed's DEFAULT cache is the
# system Temp dir (AppData\Local\Temp\fastembed_cache on Windows), which Windows
# and disk-cleanup tools periodically WIPE — and once the model is gone it must
# be re-downloaded, so every offline startup after a Temp sweep silently loses
# memory/semantic search (live incident 2026-07-24: the Temp copy was left a
# corrupted `.incomplete` download and, with no network that session, contact /
# memory / episode / file search all fell back to non-vector search). Pinning the
# cache to ~/.jarvis means the model is downloaded ONCE and survives Temp sweeps
# and reboots. Override via FASTEMBED_CACHE_DIR for scripted/hermetic use.
import os

FASTEMBED_DIR = Path(
    os.getenv("FASTEMBED_CACHE_DIR", str(Path.home() / ".jarvis" / "fastembed"))
)


@lru_cache(maxsize=1)
def _get_text_embedding_model():
    """
    Lazy-load the fastembed model as a singleton.
    First call downloads the model (~23 MB) into ~/.jarvis/fastembed.
    Subsequent calls return the cached instance instantly.
    """
    try:
        from fastembed import TextEmbedding
        FASTEMBED_DIR.mkdir(parents=True, exist_ok=True)
        model = TextEmbedding(
            model_name="BAAI/bge-small-en-v1.5",
            cache_dir=str(FASTEMBED_DIR),
        )
        logger.info(
            f"fastembed model loaded: BAAI/bge-small-en-v1.5 (384 dims) "
            f"[cache: {FASTEMBED_DIR}]"
        )
        return model
    except ImportError:
        raise ImportError(
            "fastembed not installed. Run: pip install fastembed"
        )


async def embed_text(text: str) -> List[float]:
    """
    Embed a single text string and return a 384-dim float vector.
    Runs synchronously in a thread pool to avoid blocking the event loop.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _embed_sync, text)


def _embed_sync(text: str) -> List[float]:
    """Synchronous embedding call (runs in thread pool)."""
    model = _get_text_embedding_model()
    # fastembed returns a generator of numpy arrays
    embeddings = list(model.embed([text]))
    return embeddings[0].tolist()


async def embed_batch(texts: List[str]) -> List[List[float]]:
    """Embed multiple texts in one batch call."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _embed_batch_sync, texts)


def _embed_batch_sync(texts: List[str]) -> List[List[float]]:
    model = _get_text_embedding_model()
    return [emb.tolist() for emb in model.embed(texts)]
