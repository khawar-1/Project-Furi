"""
Jarvis OS — Memory API
Full CRUD for semantic memories plus search and stats endpoints.
The 'subject' filter allows the frontend to fetch only user-facing facts (user | shared)
for the About Me panel, or contact-specific facts when browsing a contact.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.core.dependencies import get_db, get_qdrant
from app.db.models import SemanticMemory, Contact, Episode, Preference
from app.db.schemas import MemorySearchResult, SemanticMemoryCreate, SemanticMemoryResponse
from app.memory.engine import MemoryEngine

router = APIRouter()


@router.get("", response_model=MemorySearchResult, summary="List all memories")
async def list_memories(
    limit: int = 50,
    category: Optional[str] = None,
    subject: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
) -> MemorySearchResult:
    """Return stored semantic memories with optional category and subject filters.

    subject param:
      - 'user'    → only user-specific facts (About Me tab)
      - 'contact' → only contact-linked facts
      - 'shared'  → only shared (user+contact) facts
      - omitted   → returns all user + shared facts (default for About Me)
    """
    query = select(SemanticMemory).where(SemanticMemory.is_active == True)

    if category:
        query = query.where(SemanticMemory.category == category)

    if subject:
        query = query.where(SemanticMemory.subject == subject)
    else:
        # Default: show user-facing facts (user + shared) for the About Me panel
        query = query.where(SemanticMemory.subject.in_(["user", "shared"]))

    query = query.order_by(SemanticMemory.created_at.desc()).limit(limit)

    result = await db.execute(query)
    memories = result.scalars().all()

    return MemorySearchResult(
        memories=[SemanticMemoryResponse.model_validate(m) for m in memories],
        total=len(memories),
    )


@router.post("", response_model=SemanticMemoryResponse, summary="Store a memory")
async def create_memory(
    payload: SemanticMemoryCreate,
    db: AsyncSession = Depends(get_db),
    qdrant=Depends(get_qdrant),
) -> SemanticMemoryResponse:
    """Manually store a semantic memory fact. Always stored as subject='user'."""
    engine = MemoryEngine(db=db, qdrant=qdrant)
    memory = await engine.store_semantic_memory(
        content=payload.content,
        category=payload.category or "fact",
        source="explicit",
        confidence=payload.confidence,
        subject="user",  # Manual additions are always user facts
    )
    return SemanticMemoryResponse.model_validate(memory)


@router.delete("/{memory_id}", summary="Delete a memory")
async def delete_memory(
    memory_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Soft-delete a memory by marking it inactive."""
    result = await db.execute(
        select(SemanticMemory).where(SemanticMemory.id == memory_id)
    )
    memory = result.scalar_one_or_none()
    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")

    memory.is_active = False
    await db.commit()
    return {"deleted": memory_id}


@router.get("/search", summary="Search all memory types")
async def search_memory(
    q: str = Query(..., min_length=1),
    limit: int = 10,
    db: AsyncSession = Depends(get_db),
    qdrant=Depends(get_qdrant),
) -> dict:
    """Semantic search across memories and episodes."""
    engine = MemoryEngine(db=db, qdrant=qdrant)
    memories = await engine.search_semantic_memory(q, limit=limit)
    episodes = await engine.search_episodes(q, limit=5)

    return {
        "query": q,
        "memories": [SemanticMemoryResponse.model_validate(m) for m in memories],
        "episodes": [
            {
                "id": ep.id,
                "title": ep.title,
                "summary": ep.summary,
                "created_at": ep.created_at.isoformat(),
            }
            for ep in episodes
        ],
    }


@router.get("/stats", summary="Memory statistics")
async def memory_stats(db: AsyncSession = Depends(get_db)) -> dict:
    """Return counts for all memory types."""
    async def count(model):
        result = await db.execute(select(func.count()).select_from(model))
        return result.scalar_one()

    return {
        "semantic_memories": await count(SemanticMemory),
        "contacts": await count(Contact),
        "episodes": await count(Episode),
        "preferences": await count(Preference),
    }
