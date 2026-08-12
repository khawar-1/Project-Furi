"""
Furi OS — File Intelligence (Phase 6, Part 6 — the phase capstone)

Learns the user's folder HABITS so the planner can suggest a save/move
destination when the user names none ("save these notes", "organize this
screenshot"). This is the smallest, most heuristic piece of the Intelligence
phase: a read-only signal derived ON DEMAND from the ActivityLog audit trail —
no new table, no background job, and (crucially) nothing that can act.

The signal: the destination FOLDER of every SUCCESSFUL move_file / create_file
/ rename_file, ranked by how often it was used, recency breaking ties. It rides
into the planner as background DATA (the planner_memory_context pattern) with a
rule that scopes its use to destination-less create/move goals — and because a
create_file/move_file step is a WRITE step, any suggested location still passes
through the structural approval gate. A learned folder can never bypass it, and
web/email/memory content can never plant one (the aggregation reads only our own
file tools' audited outcomes).

Design notes:
- We prefer the tool RESULT's real final path (`moved_to` / `renamed_to` /
  `created`) over the requested parameter — move_file's `destination` may be a
  folder the file landed INSIDE, so its parent would be wrong; the result's
  parent is always the true destination folder.
- Folder keys are OS-normalized (case-insensitive on Windows) so "C:\\Users\\x"
  and "c:\\users\\X" count as one; the first-seen casing is displayed.
- Furi's own trash (~/.jarvis/trash) is never a suggestion.
"""
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ActivityLog

# Tools whose SUCCESSFUL runs reveal a destination folder the user chose.
# move_files counts ONCE per call, not once per file: a habit is a DECISION,
# one approval is one decision, and counting 85 would let a single bulk move
# permanently dominate the ranking.
_DESTINATION_TOOLS = ("create_file", "move_file", "rename_file", "move_files")

# Never suggest Furi's own trash as a place to save things.
_TRASH_DIR = Path.home() / ".jarvis" / "trash"

DEFAULT_LIMIT = 5


@dataclass(frozen=True)
class FolderUsage:
    """One destination folder and how the user has used it. `folder` keeps the
    first-seen display casing; ranking uses the normalized key internally."""
    folder: str
    count: int
    last_used: Optional[datetime]


def _loads(raw: Optional[str]) -> Optional[dict]:
    """A stored JSON dict, or None on absent/invalid/truncated data (the
    result_summary is capped and may end in '…')."""
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _folder(path: Any) -> Optional[str]:
    """The value AS the folder (no .parent) — for tools whose destination is
    required to be a directory."""
    text = str(path or "").strip()
    return text or None


def _parent(path: Any) -> Optional[str]:
    text = str(path or "").strip()
    if not text:
        return None
    try:
        folder = str(Path(text).parent)
    except (ValueError, OSError):
        return None
    return folder or None


def _normkey(folder: str) -> str:
    """Case- and separator-normalized dedup key (case-insensitive on Windows)."""
    return os.path.normcase(os.path.normpath(folder))


def _is_dir(folder: str) -> bool:
    try:
        return Path(folder).is_dir()
    except OSError:
        return False


def _destination_folder(
    tool_name: str, params: Optional[dict], result: Optional[dict]
) -> Optional[str]:
    """The folder a successful file operation put a file into — from the tool's
    real result first, its requested parameter only as a fallback."""
    if tool_name == "move_file":
        if result and result.get("moved_to"):
            return _parent(result["moved_to"])
        # Fallback only: `destination` may be the folder itself or a full path;
        # the parent is the best guess when no result is available.
        return _parent(params.get("destination")) if params else None
    if tool_name == "move_files":
        # NOT _parent(): move_files REQUIRES its destination to be an existing
        # folder, so the destination IS the learned folder. (move_file needs
        # the parent because its destination may be a full target path — that
        # asymmetry is why this is a branch of its own.) The parameter is a
        # reliable fallback here: _sanitize_params caps values at 300 chars,
        # which a folder path survives, while the batch RESULT is clipped by
        # _summarize_result and often will not parse.
        if result and result.get("destination"):
            return _folder(result["destination"])
        return _folder(params.get("destination")) if params else None
    if tool_name == "rename_file":
        if result and result.get("renamed_to"):
            return _parent(result["renamed_to"])
        return _parent(params.get("path")) if params else None
    if tool_name == "create_file":
        if result and result.get("created"):
            return _parent(result["created"])
        return _parent(params.get("path")) if params else None
    return None


async def frequent_folders(
    db: AsyncSession, *, limit: int = DEFAULT_LIMIT, existing_only: bool = False
) -> list[FolderUsage]:
    """The user's most-used save/move destinations, most-frequent first
    (recency breaks ties). Best-effort — a query or parse failure yields [].

    existing_only=True drops folders that no longer exist on disk (a stale
    suggestion is worse than none) — the planner passes this; the raw API
    exposes everything the audit trail recorded.
    """
    try:
        rows = (
            await db.execute(
                select(ActivityLog)
                .where(ActivityLog.success.is_(True))
                .where(ActivityLog.tool_name.in_(_DESTINATION_TOOLS))
                .order_by(ActivityLog.created_at.asc())
            )
        ).scalars().all()
    except Exception as e:  # pragma: no cover — defensive, never break planning
        logger.warning(f"frequent_folders query failed (non-critical): {e}")
        return []

    trash_key = _normkey(str(_TRASH_DIR))
    # key -> mutable [display, count, last_used]
    agg: dict[str, list] = {}
    for row in rows:
        folder = _destination_folder(
            row.tool_name, _loads(row.parameters), _loads(row.result_summary)
        )
        if not folder:
            continue
        key = _normkey(folder)
        if key == trash_key:
            continue
        entry = agg.get(key)
        if entry is None:
            agg[key] = [folder, 1, row.created_at]
        else:
            entry[1] += 1
            if row.created_at is not None and (
                entry[2] is None or row.created_at >= entry[2]
            ):
                entry[2] = row.created_at

    usages = [FolderUsage(folder=d, count=c, last_used=t) for d, c, t in agg.values()]
    if existing_only:
        usages = [u for u in usages if _is_dir(u.folder)]
    usages.sort(
        key=lambda u: (u.count, u.last_used or datetime.min), reverse=True
    )
    return usages[: max(0, limit)]


def format_frequent_folders(folders: list[FolderUsage]) -> str:
    """Render the ranked folders as a plain-text list for the planner DATA
    block. "" when there is nothing to suggest (the block is then omitted)."""
    if not folders:
        return ""
    lines = [
        f"- {u.folder}  (used {u.count} time{'s' if u.count != 1 else ''})"
        for u in folders
    ]
    return (
        "The folders the user saves or moves files into most often, "
        "most-used first:\n" + "\n".join(lines)
    )
