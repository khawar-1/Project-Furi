"""
Jarvis OS — File Tools (Phase 3, Part 2)
Six single-action tools over the local filesystem:

  search_files    READ         find files/folders by name, extension, date, size
  read_file       READ         read a text file's contents
  list_directory  READ         list a directory's entries
  move_file       WRITE        move a file to a new location
  rename_file     WRITE        rename a file or folder in place
  delete_file     DESTRUCTIVE  delete a single file (backed up to trash first)

Safety model (enforced here, before any filesystem access):
- Paths are expanded (~, env vars) and resolved to absolute; a relative path
  resolves against the user's HOME, never the backend's working directory.
- System directories (Windows, Program Files, /usr, ...) and filesystem
  roots are refused for EVERY operation, read included.
- delete_file only ever deletes a single file — never a directory.
- All blocking I/O runs in a thread (asyncio.to_thread); expected failures
  return a clean failed ToolResult, never an exception.
"""
import asyncio
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from app.core.base_tool import BaseTool, PermissionLevel, ToolDefinition, ToolResult
from app.tools.registry import register_tool

# ------------------------------------------------------------------ limits
READ_MAX_BYTES = 1_048_576  # 1 MB cap for read_file
SEARCH_MAX_RESULTS = 100
SEARCH_MAX_SCANNED = 100_000  # hard bound on entries visited per search (all roots combined)
SEARCH_MAX_ROOTS = 8  # max directories per multi-root search
LIST_MAX_ENTRIES = 500

# Directory names never descended into during search (lowercase)
_SKIP_DIR_NAMES = {"appdata", "node_modules", "__pycache__", "venv", ".git"}


def _protected_roots() -> list[Path]:
    """System locations no tool may touch, resolved per-platform."""
    roots: list[Path] = []
    if os.name == "nt":
        for env in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
            value = os.environ.get(env)
            if value:
                roots.append(Path(value).resolve())
    else:
        for p in ("/bin", "/sbin", "/usr", "/etc", "/boot", "/sys", "/proc", "/dev", "/lib"):
            roots.append(Path(p))
    return roots


_PROTECTED = _protected_roots()


def _resolve_path(raw: Any) -> Path:
    """Expand ~ and env vars, then resolve to an absolute path. A relative
    path is resolved against HOME (the backend's CWD would surprise users)."""
    text = str(raw or "").strip()
    if not text:
        raise ValueError("Path is empty")
    path = Path(os.path.expandvars(os.path.expanduser(text)))
    if not path.is_absolute():
        path = Path.home() / path
    return path.resolve()


def _blocked_reason(path: Path, allow_root: bool = False) -> Optional[str]:
    """Non-None when the path is protected. Checked BEFORE any fs access.
    allow_root is granted ONLY to search_files ("anywhere on my PC" needs to
    start at a drive root) — the walk itself prunes protected directories.
    Every other operation, read included, still refuses roots."""
    if path.parent == path and not allow_root:
        return f"'{path}' is a filesystem root — refusing to operate on it"
    for root in _PROTECTED:
        if path == root or root in path.parents:
            return f"'{path}' is inside the protected system directory '{root}'"
    return None


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).isoformat(timespec="seconds")


def _created_iso(stat: os.stat_result) -> str:
    # st_birthtime where the OS has real creation time (macOS/BSD); on
    # Windows st_ctime IS creation time; on Linux it's inode-change time —
    # the closest available answer.
    return _iso(getattr(stat, "st_birthtime", stat.st_ctime))


# ------------------------------------------------------- search filter parsing

def _parse_date_bound(value: Any, key: str) -> Optional[datetime]:
    """Parse an ISO date/datetime filter bound. A BARE DATE names a whole
    day on either bound — human ranges are inclusive at both ends:
      *_after  2026-07-01 → on/after that day    (>= 2026-07-01 00:00)
      *_before 2026-07-31 → through that ENTIRE day (< 2026-08-01 00:00)
    so after=2026-06-09 + before=<today> is exactly "9 June to today",
    today's files included (live gap 2026-07-10: the old strictly-before
    date bound silently dropped the named day, and expecting the LLM to
    pass day+1 is LLM date arithmetic — banned here). A full DATETIME
    keeps exact semantics: >= for after, strictly < for before.
    Anything non-ISO (e.g. '03/04/2026') is REFUSED — ambiguous day/month
    ordering must be resolved by the planner asking the user, never here."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(
            f"{key} must be an ISO date like 2026-07-01 (year-month-day) or a "
            f"full ISO datetime — got '{text}'"
        )
    is_date_only = "T" not in text and " " not in text
    if is_date_only and key.endswith("_before"):
        parsed += timedelta(days=1)
    return parsed


def _parse_size_bound(value: Any, key: str) -> Optional[int]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        size = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number of bytes — got '{value}'")
    if size < 0:
        raise ValueError(f"{key} cannot be negative")
    return size


class _SearchFilters:
    """Validated date/size bounds applied in code — never in the LLM's head."""

    def __init__(self, kwargs: dict[str, Any]) -> None:
        self.created_after = _parse_date_bound(kwargs.get("created_after"), "created_after")
        self.created_before = _parse_date_bound(kwargs.get("created_before"), "created_before")
        self.modified_after = _parse_date_bound(kwargs.get("modified_after"), "modified_after")
        self.modified_before = _parse_date_bound(kwargs.get("modified_before"), "modified_before")
        self.min_size = _parse_size_bound(kwargs.get("min_size"), "min_size")
        self.max_size = _parse_size_bound(kwargs.get("max_size"), "max_size")

    @property
    def any_active(self) -> bool:
        return any(v is not None for v in (
            self.created_after, self.created_before,
            self.modified_after, self.modified_before,
            self.min_size, self.max_size,
        ))

    def matches(self, stat: os.stat_result, is_dir: bool) -> bool:
        created = datetime.fromtimestamp(getattr(stat, "st_birthtime", stat.st_ctime))
        modified = datetime.fromtimestamp(stat.st_mtime)
        if self.created_after is not None and created < self.created_after:
            return False
        if self.created_before is not None and created >= self.created_before:
            return False
        if self.modified_after is not None and modified < self.modified_after:
            return False
        if self.modified_before is not None and modified >= self.modified_before:
            return False
        if not is_dir:  # size bounds never exclude folders
            if self.min_size is not None and stat.st_size < self.min_size:
                return False
            if self.max_size is not None and stat.st_size > self.max_size:
                return False
        return True


def _fail(tool: "BaseTool", message: str) -> ToolResult:
    return ToolResult(
        success=False, output=None, error=message,
        permission_level=tool.permission_level,
    )


def _ok(tool: "BaseTool", output: Any) -> ToolResult:
    return ToolResult(
        success=True, output=output, permission_level=tool.permission_level,
    )


# ============================================================== READ tools

@register_tool
class SearchFilesTool(BaseTool):
    """Find files (and optionally folders) by name substring, extension,
    creation/modification date, and size — recursively, across one or more
    roots. Every filter is applied in code; the LLM never filters."""

    @property
    def name(self) -> str:
        return "search_files"

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.READ

    async def execute(self, **kwargs: Any) -> ToolResult:
        query = str(kwargs.get("query") or "").strip().lower()
        file_type = str(kwargs.get("file_type") or "").strip().lower().lstrip(".")
        include_folders = bool(kwargs.get("include_folders"))
        try:
            filters = _SearchFilters(kwargs)
        except ValueError as e:
            return _fail(self, str(e))

        # One directory, or several ("directories") for multi-drive searches.
        # Whether the caller SCOPED the search matters below: an explicit
        # folder makes a criterion-less "everything in here" search valid.
        raw_roots = kwargs.get("directories")
        if not (isinstance(raw_roots, list) and raw_roots):
            explicit_dir = str(kwargs.get("directory") or "").strip()
            raw_roots = [explicit_dir] if explicit_dir else None
        scoped = raw_roots is not None
        if raw_roots is None:
            raw_roots = [Path.home()]
        if len(raw_roots) > SEARCH_MAX_ROOTS:
            return _fail(self, f"At most {SEARCH_MAX_ROOTS} directories per search")
        roots: list[Path] = []
        for raw in raw_roots:
            try:
                root = _resolve_path(raw)
            except ValueError as e:
                return _fail(self, str(e))
            # Roots (C:\, D:\) are allowed for searching only; the walk below
            # prunes protected system directories instead.
            if reason := _blocked_reason(root, allow_root=True):
                return _fail(self, reason)
            if not root.is_dir():
                return _fail(self, f"'{root}' is not a directory")
            if root not in roots:
                roots.append(root)

        if not query and not file_type and not filters.any_active:
            # "Everything inside <folder>" is a bounded, legitimate ask when
            # the caller names the folder (live failure 2026-07-10: "delete
            # all files in phase3test" has no criterion to give — the search
            # scoped to the folder was refused three times and the plan
            # died). The refusal only protects against an UNBOUNDED
            # match-all: no scope (defaults to the whole home directory) or
            # a scope that is an entire drive.
            if not scoped:
                return _fail(
                    self,
                    "Provide at least one criterion (a query, a file_type, "
                    "or a date/size filter) — or pass the folder whose "
                    "entire contents you want as 'directory'",
                )
            for root in roots:
                if root.parent == root:
                    return _fail(
                        self,
                        f"'{root}' is an entire drive — matching every file "
                        "on it needs at least one criterion (a query, a "
                        "file_type, or a date/size filter)",
                    )

        return await asyncio.to_thread(
            self._search, roots, query, file_type, include_folders, filters,
        )

    def _search(
        self,
        roots: list[Path],
        query: str,
        file_type: str,
        include_folders: bool,
        filters: _SearchFilters,
    ) -> ToolResult:
        matches: list[dict] = []
        scanned = 0
        truncated = False

        def consider(full: Path, name: str, is_dir: bool) -> bool:
            """Append the entry when it passes every filter. True = stop."""
            nonlocal scanned, truncated
            scanned += 1
            if scanned > SEARCH_MAX_SCANNED:
                truncated = True
                return True
            lower = name.lower()
            if query and query not in lower:
                return False
            if file_type and (is_dir or not lower.endswith(f".{file_type}")):
                return False  # the extension filter never matches folders
            try:
                stat = full.stat()
            except OSError:
                return False
            if not filters.matches(stat, is_dir):
                return False
            matches.append({
                "path": str(full),
                "type": "folder" if is_dir else "file",
                "size_bytes": None if is_dir else stat.st_size,
                "created": _created_iso(stat),
                "modified": _iso(stat.st_mtime),
            })
            if len(matches) >= SEARCH_MAX_RESULTS:
                truncated = True
                return True
            return False

        for root in roots:
            if truncated:
                break
            for dirpath, dirnames, filenames in os.walk(root):
                base = Path(dirpath)
                # Prune hidden, known-noise, and protected system directories
                dirnames[:] = [
                    d for d in dirnames
                    if not d.startswith(".")
                    and d.lower() not in _SKIP_DIR_NAMES
                    and (base / d) not in _PROTECTED
                ]
                if include_folders:
                    for dirname in dirnames:
                        if consider(base / dirname, dirname, is_dir=True):
                            break
                if not truncated:
                    for filename in filenames:
                        if consider(base / filename, filename, is_dir=False):
                            break
                if truncated:
                    break
        return _ok(self, {
            "matches": matches,
            "count": len(matches),
            "truncated": truncated,
            "searched_in": [str(r) for r in roots],
        })

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Search for files (and optionally folders) by name, extension, "
                "date, and size, recursively under one or more directories "
                "(defaults to the user's home; pass drive roots like 'C:\\' in "
                "'directories' to search a whole PC). With ONLY a 'directory' "
                "and no other filter it returns every file under that folder, "
                "recursively — the right call for 'all files in <folder>'. "
                "Returns paths with size, creation time, and modified time. "
                "All filtering happens in code, so results are exact."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Case-insensitive substring of the file/folder name"},
                    "directory": {"type": "string", "description": "Directory to search in (default: home)"},
                    "directories": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Several directories/drives to search in one call (overrides 'directory')",
                    },
                    "file_type": {"type": "string", "description": "Extension filter, e.g. 'pdf' or '.txt' (never matches folders)"},
                    "include_folders": {"type": "boolean", "description": "Also match folder names (default false: files only)"},
                    "created_after": {"type": "string", "description": "Only entries created ON or AFTER this ISO date (YYYY-MM-DD) or datetime. Dates must be ISO — convert the user's wording first"},
                    "created_before": {"type": "string", "description": "Only entries created ON or BEFORE this ISO date (YYYY-MM-DD — the whole day is included, so 'until today' is today's date). A full datetime is exclusive (strictly before)"},
                    "modified_after": {"type": "string", "description": "Only entries modified ON or AFTER this ISO date or datetime"},
                    "modified_before": {"type": "string", "description": "Only entries modified ON or BEFORE this ISO date (the whole day is included, so 'until today' is today's date). A full datetime is exclusive (strictly before)"},
                    "min_size": {"type": "integer", "description": "Only files at least this many bytes"},
                    "max_size": {"type": "integer", "description": "Only files at most this many bytes"},
                },
                "required": [],
            },
            permission_level=self.permission_level,
        )


@register_tool
class ReadFileTool(BaseTool):
    """Read a text file's contents (capped, binary files refused)."""

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.READ

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            path = _resolve_path(kwargs.get("path"))
        except ValueError as e:
            return _fail(self, str(e))
        if reason := _blocked_reason(path):
            return _fail(self, reason)
        return await asyncio.to_thread(self._read, path)

    def _read(self, path: Path) -> ToolResult:
        if not path.exists():
            return _fail(self, f"File not found: '{path}'")
        if path.is_dir():
            return _fail(self, f"'{path}' is a directory — use list_directory instead")
        size = path.stat().st_size
        if size > READ_MAX_BYTES:
            return _fail(
                self,
                f"'{path}' is {size} bytes — larger than the {READ_MAX_BYTES} byte read limit",
            )
        raw = path.read_bytes()
        if b"\x00" in raw[:8192]:
            return _fail(self, f"'{path}' looks like a binary file ({size} bytes) — cannot display as text")
        return _ok(self, {
            "path": str(path),
            "size_bytes": size,
            "content": raw.decode("utf-8", errors="replace"),
        })

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="Read the contents of a text file (up to 1 MB). Binary files are refused.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path of the file to read"},
                },
                "required": ["path"],
            },
            permission_level=self.permission_level,
        )


@register_tool
class ListDirectoryTool(BaseTool):
    """List a directory's entries with type, size, and modified time."""

    @property
    def name(self) -> str:
        return "list_directory"

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.READ

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            path = _resolve_path(kwargs.get("path") or Path.home())
        except ValueError as e:
            return _fail(self, str(e))
        if reason := _blocked_reason(path):
            return _fail(self, reason)
        return await asyncio.to_thread(self._list, path)

    def _list(self, path: Path) -> ToolResult:
        if not path.exists():
            return _fail(self, f"Directory not found: '{path}'")
        if not path.is_dir():
            return _fail(self, f"'{path}' is a file — use read_file instead")
        entries: list[dict] = []
        truncated = False
        for entry in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            if len(entries) >= LIST_MAX_ENTRIES:
                truncated = True
                break
            try:
                stat = entry.stat()
            except OSError:
                continue
            entries.append({
                "name": entry.name,
                "type": "directory" if entry.is_dir() else "file",
                "size_bytes": stat.st_size if entry.is_file() else None,
                "created": _created_iso(stat),
                "modified": _iso(stat.st_mtime),
            })
        return _ok(self, {
            "path": str(path),
            "entries": entries,
            "count": len(entries),
            "truncated": truncated,
        })

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "List the files and folders inside a directory (defaults to the "
                "user's home), with size, creation time, and modified time."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory to list (default: home)"},
                },
                "required": [],
            },
            permission_level=self.permission_level,
        )


# ============================================================== WRITE tools

@register_tool
class MoveFileTool(BaseTool):
    """Move a single file to a new location. Never overwrites."""

    @property
    def name(self) -> str:
        return "move_file"

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.WRITE

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            source = _resolve_path(kwargs.get("source"))
            destination = _resolve_path(kwargs.get("destination"))
        except ValueError as e:
            return _fail(self, str(e))
        for p in (source, destination):
            if reason := _blocked_reason(p):
                return _fail(self, reason)
        return await asyncio.to_thread(self._move, source, destination)

    def _move(self, source: Path, destination: Path) -> ToolResult:
        if not source.exists():
            return _fail(self, f"Source not found: '{source}'")
        if source.is_dir():
            return _fail(self, f"'{source}' is a directory — move_file only moves single files")
        # An existing directory as destination means "move into it"
        target = destination / source.name if destination.is_dir() else destination
        if target.exists():
            return _fail(self, f"Refusing to overwrite existing file: '{target}'")
        if not target.parent.exists():
            return _fail(self, f"Destination folder does not exist: '{target.parent}'")
        shutil.move(str(source), str(target))
        return _ok(self, {"moved_from": str(source), "moved_to": str(target)})

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Move a single file to a new location. If the destination is an "
                "existing folder, the file moves into it. Never overwrites."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "File to move"},
                    "destination": {"type": "string", "description": "Target folder or full target path"},
                },
                "required": ["source", "destination"],
            },
            permission_level=self.permission_level,
        )


@register_tool
class RenameFileTool(BaseTool):
    """Rename a file or folder in place. Never overwrites."""

    @property
    def name(self) -> str:
        return "rename_file"

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.WRITE

    async def execute(self, **kwargs: Any) -> ToolResult:
        new_name = str(kwargs.get("new_name") or "").strip()
        try:
            path = _resolve_path(kwargs.get("path"))
        except ValueError as e:
            return _fail(self, str(e))
        if reason := _blocked_reason(path):
            return _fail(self, reason)
        if not new_name or new_name in (".", ".."):
            return _fail(self, "new_name is empty or invalid")
        if any(sep in new_name for sep in ("/", "\\")):
            return _fail(self, "new_name must be a plain name, not a path — use move_file to relocate")
        return await asyncio.to_thread(self._rename, path, new_name)

    def _rename(self, path: Path, new_name: str) -> ToolResult:
        if not path.exists():
            return _fail(self, f"Not found: '{path}'")
        target = path.parent / new_name
        if target.exists():
            return _fail(self, f"Refusing to overwrite: '{target}' already exists")
        path.rename(target)
        return _ok(self, {"renamed_from": str(path), "renamed_to": str(target)})

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="Rename a file or folder in its current location. Never overwrites.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File or folder to rename"},
                    "new_name": {"type": "string", "description": "New name (plain name, no path separators)"},
                },
                "required": ["path", "new_name"],
            },
            permission_level=self.permission_level,
        )


# ======================================================== DESTRUCTIVE tools

TRASH_DIR = Path.home() / ".jarvis" / "trash"


def _trash_target(name: str) -> Path:
    """A collision-free path inside the trash, stamped with the delete time."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = TRASH_DIR / f"{stamp}_{name}"
    counter = 1
    while target.exists():
        target = TRASH_DIR / f"{stamp}_{counter}_{name}"
        counter += 1
    return target


@register_tool
class DeleteFileTool(BaseTool):
    """Delete a single file — moved to Jarvis's trash first, so it is
    recoverable by hand. Never deletes directories."""

    @property
    def name(self) -> str:
        return "delete_file"

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.DESTRUCTIVE

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            path = _resolve_path(kwargs.get("path"))
        except ValueError as e:
            return _fail(self, str(e))
        if reason := _blocked_reason(path):
            return _fail(self, reason)
        return await asyncio.to_thread(self._delete, path)

    def _delete(self, path: Path) -> ToolResult:
        if not path.exists():
            return _fail(self, f"File not found: '{path}'")
        if path.is_dir():
            return _fail(self, f"'{path}' is a directory — delete_file only deletes single files")
        size = path.stat().st_size
        # Safety net: the file is MOVED to the trash, never unlinked outright.
        # If the backup cannot be made, the delete does not happen at all.
        backup = _trash_target(path.name)
        try:
            TRASH_DIR.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(backup))
        except OSError as e:
            return _fail(self, f"Could not back up '{path}' to the trash before deleting: {e}")
        return _ok(self, {
            "deleted": str(path),
            "size_bytes": size,
            "backed_up_to": str(backup),
        })

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Delete a single file. The file is first moved to Jarvis's "
                f"trash folder ({TRASH_DIR}) so it can be recovered by hand. "
                "Directories are refused."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File to delete permanently"},
                },
                "required": ["path"],
            },
            permission_level=self.permission_level,
        )
