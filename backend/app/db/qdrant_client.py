"""
Furi OS — Qdrant Client
Manages the local Qdrant vector database in embedded (disk) mode.
Creates all Phase 2 collections at startup.
"""
from typing import Optional

from loguru import logger
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qdrant_models

from app.core.config import settings

# Module-level client instance — None if Qdrant is unavailable
_qdrant_client: Optional[AsyncQdrantClient] = None

# fastembed BAAI/bge-small-en-v1.5 outputs 384 dimensions
EMBEDDING_DIM = 384

# All collections used by the memory engine
COLLECTIONS = [
    "semantic_memory",
    "contacts",
    "episodes",
    "file_chunks",  # Phase 6 Part 2 — semantic file index (one point per doc chunk)
    "conversation_messages",  # Phase 6 Part 4 — past-chat search (one point per message)
]


async def _ensure_collection(client: AsyncQdrantClient, name: str) -> None:
    """Create a Qdrant collection if it does not already exist."""
    existing = await client.get_collections()
    names = {c.name for c in existing.collections}
    if name not in names:
        await client.create_collection(
            collection_name=name,
            vectors_config=qdrant_models.VectorParams(
                size=EMBEDDING_DIM,
                distance=qdrant_models.Distance.COSINE,
            ),
        )
        logger.info(f"Created Qdrant collection: {name}")
    else:
        logger.debug(f"Qdrant collection exists: {name}")


async def init_qdrant() -> None:
    """
    Initialize Qdrant in local disk mode and create all Phase 2 collections.
    Called once during application startup.
    """
    global _qdrant_client

    client = AsyncQdrantClient(
        path="./qdrant_data",
        timeout=10,
    )

    for collection_name in COLLECTIONS:
        await _ensure_collection(client, collection_name)

    _qdrant_client = client
    logger.info(f"Qdrant ready with {len(COLLECTIONS)} collections")


def get_qdrant_client() -> Optional[AsyncQdrantClient]:
    """Return the initialized Qdrant client, or None if not available."""
    return _qdrant_client


async def close_qdrant() -> None:
    """Close the Qdrant client connection on shutdown."""
    global _qdrant_client
    if _qdrant_client is not None:
        await _qdrant_client.close()
        _qdrant_client = None
