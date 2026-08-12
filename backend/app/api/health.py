"""
Furi OS — Health Check API
Returns backend status, database connection, Qdrant availability, and active LLM provider.
"""
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.core.config import settings
from app.core.dependencies import get_db, get_qdrant, get_llm_provider
from app.db.schemas import ComponentHealth, HealthResponse, HealthStatus
from app.providers.base import LLMProvider

router = APIRouter()


@router.get("", response_model=HealthResponse, summary="System health check")
async def health_check(
    db: AsyncSession = Depends(get_db),
    qdrant=Depends(get_qdrant),
    provider: LLMProvider = Depends(get_llm_provider),
) -> HealthResponse:
    """
    Returns the health status of all backend components.
    Used by the Electron status bar to show connection state.
    """
    components: dict[str, ComponentHealth] = {}

    # ---- SQLite health
    try:
        await db.execute(text("SELECT 1"))
        components["database"] = ComponentHealth(
            status=HealthStatus.OK,
            detail="SQLite connected",
        )
    except Exception as e:
        components["database"] = ComponentHealth(
            status=HealthStatus.ERROR,
            detail=str(e),
        )

    # ---- Qdrant health
    if qdrant is not None:
        try:
            await qdrant.get_collections()
            components["qdrant"] = ComponentHealth(
                status=HealthStatus.OK,
                detail=f"Collection: {settings.QDRANT_COLLECTION}",
            )
        except Exception as e:
            components["qdrant"] = ComponentHealth(
                status=HealthStatus.DEGRADED,
                detail=str(e),
            )
    else:
        components["qdrant"] = ComponentHealth(
            status=HealthStatus.DEGRADED,
            detail="Qdrant not initialized (vector memory disabled)",
        )

    # ---- LLM provider health
    components["llm"] = ComponentHealth(
        status=HealthStatus.OK,
        detail=f"{provider.provider_name}/{provider.model_name}",
    )

    # Overall status: OK if all critical components are OK
    overall = (
        HealthStatus.OK
        if components["database"].status == HealthStatus.OK
        else HealthStatus.DEGRADED
    )

    return HealthResponse(
        status=overall,
        version=settings.APP_VERSION,
        provider=provider.provider_name,
        model=provider.model_name,
        components=components,
        timestamp=datetime.utcnow(),
    )
