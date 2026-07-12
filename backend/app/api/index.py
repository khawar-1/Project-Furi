"""
Jarvis OS — File index API (Phase 6, Part 2)

The user-facing controls for the semantic file index: which folders it covers,
what to exclude, and a manual "index now" trigger + live status. Config lives in
app/core/app_settings.py; the indexing itself in app/core/file_index.py — this
router only orchestrates (the reminders-router rule: modules own their domain).

A rebuild runs as a DETACHED background pass (folders can be large): POST returns
immediately and the UI polls GET /status. The scheduler that re-runs it on a
cadence lands in Part 3; the config already carries enabled + interval_minutes
so this part's UI is complete.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.app_settings import (
    FILE_INDEX_MAX_INTERVAL,
    FILE_INDEX_MIN_INTERVAL,
    FileIndexConfig,
    get_file_index_config,
    set_file_index_config,
)
from app.core.dependencies import get_db
from app.core.file_index import (
    get_index_summary,
    is_indexing,
    start_index_in_background,
)
from app.tools.file_tools import _blocked_reason, _resolve_path

router = APIRouter()


class IndexConfigUpdate(BaseModel):
    enabled: bool
    folders: list[str]
    exclusions: list[str] = []
    interval_minutes: int = 360


class RebuildRequest(BaseModel):
    full: bool = False


def _validate_folders(folders: list[str]) -> None:
    """Reject a folder that resolves to a filesystem root or a protected system
    directory — never index those. Non-existent folders are allowed (the pass
    just skips them); a typo is not worth a hard error."""
    for raw in folders:
        text = (raw or "").strip()
        if not text:
            continue
        try:
            resolved = _resolve_path(text)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"'{raw}' is not a valid folder path")
        reason = _blocked_reason(resolved)
        if reason is not None:
            raise HTTPException(status_code=400, detail=reason)


async def _payload(db) -> dict:
    config = await get_file_index_config(db)
    status = await get_index_summary(db)
    # Phase 6 Part 4 — surface conversation-index counts alongside the file ones.
    from app.core.conversation_index import get_conversation_index_summary
    status = {**status, **await get_conversation_index_summary(db)}
    return {
        "enabled": config.enabled,
        "folders": list(config.folders),
        "exclusions": list(config.exclusions),
        "interval_minutes": config.interval_minutes,
        "status": status,
    }


@router.get("", summary="File index config + status")
async def get_index(db=Depends(get_db)) -> dict:
    return await _payload(db)


@router.get("/status", summary="File index status (poll while indexing)")
async def get_status(db=Depends(get_db)) -> dict:
    from app.core.conversation_index import get_conversation_index_summary
    status = await get_index_summary(db)
    return {**status, **await get_conversation_index_summary(db)}


@router.put("/config", summary="Update file index config")
async def put_config(update: IndexConfigUpdate, db=Depends(get_db)) -> dict:
    _validate_folders(update.folders)
    interval = max(FILE_INDEX_MIN_INTERVAL, min(update.interval_minutes, FILE_INDEX_MAX_INTERVAL))
    folders = tuple(f.strip() for f in update.folders if f.strip())
    exclusions = tuple(e.strip() for e in update.exclusions if e.strip())
    await set_file_index_config(db, FileIndexConfig(
        enabled=update.enabled,
        folders=folders,
        exclusions=exclusions,
        interval_minutes=interval,
    ))
    # Re-arm (or cancel) the recurring reindex job in the same request, so a
    # toggle / interval change takes effect immediately (the settings.py
    # PUT /briefing in-request re-sync pattern).
    from app.core.reindex import sync_reindex_job
    await sync_reindex_job(db)
    return await _payload(db)


@router.post("/rebuild", summary="Index the configured folders now (background)")
async def post_rebuild(request: RebuildRequest | None = None, db=Depends(get_db)) -> dict:
    full = bool(request.full) if request else False
    started = await start_index_in_background(full=full)
    # Phase 6 Part 4 — a manual rebuild also (re)indexes conversation history.
    from app.core.conversation_index import start_conversation_index_in_background
    await start_conversation_index_in_background(full=full)
    return {"started": started, "indexing": is_indexing()}
