"""
Furi OS — Episodes API (Phase 2)
Read-only access to episodic memory (auto-populated by extraction pipeline).
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc

from app.core.dependencies import get_db, get_qdrant
from app.db.models import Episode
from app.memory.engine import MemoryEngine

router = APIRouter()


def _episode_to_dict(ep: Episode) -> dict:
    return {
        "id": ep.id,
        "title": ep.title,
        "summary": ep.summary,
        "episode_type": ep.episode_type,
        "occurred_at": ep.occurred_at.isoformat(),
        "created_at": ep.created_at.isoformat(),
    }


@router.get("", summary="List recent episodes")
async def list_episodes(
    limit: int = 30,
    db: AsyncSession = Depends(get_db),
) -> list:
    result = await db.execute(
        select(Episode).order_by(desc(Episode.created_at)).limit(limit)
    )
    return [_episode_to_dict(ep) for ep in result.scalars().all()]


@router.get("/search", summary="Search episodes semantically")
async def search_episodes(
    q: str = Query(..., min_length=1),
    limit: int = 10,
    db: AsyncSession = Depends(get_db),
    qdrant=Depends(get_qdrant),
) -> list:
    engine = MemoryEngine(db=db, qdrant=qdrant)
    episodes = await engine.search_episodes(q, limit=limit)
    return [_episode_to_dict(ep) for ep in episodes]
