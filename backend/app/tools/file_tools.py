"""
Jarvis OS — File Tools (Phase 3, Part 2)
Tools over the local filesystem:

  search_files    READ         find files/folders by name, extension, date, size
  read_file       READ         read a text file's contents
  list_directory  READ         list a directory's entries
  move_file       WRITE        move a file to a new location
  move_files      WRITE        move MANY files into one folder (batch)
  rename_file     WRITE        rename a file or folder in place
  delete_file     DESTRUCTIVE  delete a single file (backed up to trash first)
  delete_files    DESTRUCTIVE  delete MANY files (batch, each backed up first)

The batch pair exists because bulk work was structurally unplannable
(2026-07-29): one step per file against a 30-step plan cap meant "move all
85 PDFs" could not be expressed, and the plan reported success having moved
nothing. A batch tool loops the SAME single-file primitive (_move_one /
_delete_one) as its singular twin, so no safety property is re-implemented,
and the explicit path list lives in the step's parameters — which is what
binds the approval to those exact files.

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
# Raised 100 → 1000 (2026-07-29). At 100 a folder holding 150 PDFs answered
# "all the PDFs" with 100 of them and `truncated: true`, and every consumer
# treated that as the whole set — a bulk move would have moved two thirds and
# reported success. The real bound on a search is SEARCH_MAX_SCANNED; this cap
# only exists to keep a result payload sane, and it must stay <= BATCH_MAX_FILES
# so a full search result is always actionable in one batch step (test-pinned).
SEARCH_MAX_RESULTS = 1000
SEARCH_MAX_SCANNED = 100_000  # hard bound on entries visited per search (all roots combined)
SEARCH_MAX_ROOTS = 8  # max directories per multi-root search
LIST_MAX_ENTRIES = 500
# Files per move_files / delete_files call — a bulk operation is a bounded
# one, never an unbounded one. INVARIANT: >= SEARCH_MAX_RESULTS, so a full
# search result is always actionable in ONE batch step (the same "keep the
# downstream cap above what the upstream can emit" discipline
# rendering._STEP_RESULT_CAPS documents). A test pins it.
BATCH_MAX_FILES = 1000

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


# --------------------------------------------------- single-file primitives
# The ONE implementation of each mutation. Both the singular tool and its
# batch twin call these, so the never-overwrite / trash-first / no-directory
# guarantees can never drift apart between "move one" and "move eighty-five".
# Each returns (payload, None) on success or (None, error) on failure — the
# caller decides whether a failure ends the call (singular) or is collected
# and reported alongside the successes (batch).

def _move_one(source: Path, destination: Path) -> tuple[Optional[dict], Optional[str]]:
    if not source.exists():
        return None, f"Source not found: '{source}'"
    if source.is_dir():
        return None, f"'{source}' is a directory — only single files are moved"
    # An existing directory as destination means "move into it"
    target = destination / source.name if destination.is_dir() else destination
    if target.exists():
        return None, f"Refusing to overwrite existing file: '{target}'"
    if not target.parent.exists():
        return None, f"Destination folder does not exist: '{target.parent}'"
    try:
        shutil.move(str(source), str(target))
    except OSError as e:
        return None, f"Could not move '{source}': {e}"
    return {"moved_from": str(source), "moved_to": str(target)}, None


def _delete_one(path: Path) -> tuple[Optional[dict], Optional[str]]:
    if not path.exists():
        return None, f"File not found: '{path}'"
    if path.is_dir():
        return None, f"'{path}' is a directory — only single files are deleted"
    size = path.stat().st_size
    # Safety net: the file is MOVED to the trash, never unlinked outright.
    # If the backup cannot be made, the delete does not happen at all.
    backup = _trash_target(path.name)
    try:
        TRASH_DIR.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(backup))
    except OSError as e:
        return None, f"Could not back up '{path}' to the trash before deleting: {e}"
    return {
        "deleted": str(path),
        "size_bytes": size,
        "backed_up_to": str(backup),
    }, None


# ------------------------------------------------------------ batch plumbing

def _coerce_path_list(value: Any) -> tuple[list[str], Optional[str]]:
    """(paths, error) for a batch parameter. A bare string is accepted as a
    one-item list — a model that writes the singular form is not punished for
    it — but anything else is refused rather than guessed at."""
    if isinstance(value, str):
        text = value.strip()
        return ([text], None) if text else ([], "No files were given")
    if not isinstance(value, list):
        return [], "Expected a list of file paths"
    # Dedupe preserving order: a repeated path would otherwise produce a
    # confusing second "Source not found" for a file we just moved ourselves.
    paths: list[str] = []
    seen: set[str] = set()
    for v in value:
        text = str(v or "").strip()
        if text and text not in seen:
            seen.add(text)
            paths.append(text)
    if not paths:
        return [], "No files were given"
    if len(paths) > BATCH_MAX_FILES:
        # REFUSE, never truncate. Silently acting on part of a destructive
        # batch and reporting success is the defect this whole change exists
        # to remove.
        return [], (
            f"{len(paths)} files is more than the {BATCH_MAX_FILES}-file limit "
            f"for one batch — narrow the selection (by folder, date, or size) "
            f"and run it in parts"
        )
    return paths, None


def _run_batch(raw_paths: list[str], operate) -> tuple[list[dict], list[dict], int]:
    """Apply a single-file primitive across a list, collecting BOTH halves.

    A failure is recorded and the batch CONTINUES: a bulk operation that
    stops dead on file 12 of 85 leaves the user in a state nobody chose, and
    rolling the first 11 back is more dangerous than the truth. The caller
    reports what moved and what did not, naming every failure."""
    done: list[dict] = []
    failed: list[dict] = []
    total = 0
    for raw in raw_paths:
        try:
            path = _resolve_path(raw)
        except ValueError as e:
            failed.append({"path": str(raw), "error": str(e)})
            continue
        if reason := _blocked_reason(path):
            failed.append({"path": str(path), "error": reason})
            continue
        payload, error = operate(path)
        if error or payload is None:
            failed.append({"path": str(path), "error": error or "Unknown failure"})
            continue
        done.append(payload)
        total += int(payload.get("size_bytes") or 0)
    return done, failed, total


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

        def consider(entry: os.DirEntry, is_dir: bool) -> bool:
            """Append the entry when it passes every filter. True = stop.

            The stat comes from the DirEntry, which on Windows is served out
            of the directory enumeration the scan already paid for — the old
            `Path.stat()` here was a fresh syscall per candidate and was most
            of the 26.9s a real Downloads search took (2026-07-29). Symlinks
            are still FOLLOWED for the filter stat, matching the previous
            behaviour exactly; only the descend decision below refuses to."""
            nonlocal scanned, truncated
            scanned += 1
            if scanned > SEARCH_MAX_SCANNED:
                truncated = True
                return True
            name = entry.name
            lower = name.lower()
            if query and query not in lower:
                return False
            if file_type and (is_dir or not lower.endswith(f".{file_type}")):
                return False  # the extension filter never matches folders
            try:
                stat = entry.stat()
            except OSError:
                return False
            if not filters.matches(stat, is_dir):
                return False
            matches.append({
                "path": entry.path,
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
            # Depth-first, pre-order — the os.walk(topdown=True) traversal this
            # replaced, so which entries a truncated search returns does not
            # change. Reversed pushes keep sibling order.
            stack: list[Path] = [root]
            while stack and not truncated:
                current = stack.pop()
                try:
                    with os.scandir(current) as it:
                        entries = list(it)
                except OSError:
                    continue  # unreadable directory — os.walk swallowed these
                                # too (onerror=None); one locked folder must
                                # never kill a search that otherwise works
                subdirs: list[os.DirEntry] = []
                files: list[os.DirEntry] = []
                for entry in entries:
                    try:
                        # follow_symlinks=False for the DESCEND decision only,
                        # matching os.walk(followlinks=False): a junction
                        # pointing at an ancestor would otherwise walk forever.
                        if entry.is_dir(follow_symlinks=False):
                            name = entry.name
                            if (
                                name.startswith(".")
                                or name.lower() in _SKIP_DIR_NAMES
                                or Path(entry.path) in _PROTECTED
                            ):
                                continue  # pruned, exactly as before
                            subdirs.append(entry)
                        else:
                            files.append(entry)
                    except OSError:
                        continue
                if include_folders:
                    for entry in subdirs:
                        if consider(entry, is_dir=True):
                            break
                if not truncated:
                    for entry in files:
                        if consider(entry, is_dir=False):
                            break
                if truncated:
                    break
                stack.extend(Path(e.path) for e in reversed(subdirs))
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
class CreateFolderTool(BaseTool):
    """Create a folder (and any missing parents). Live bug 2026-07-12:
    with no folder tool, the planner faked "create a folder called
    jarvis_test" with a 0-byte create_file — a FILE named jarvis_test —
    and every file created inside it then failed."""

    @property
    def name(self) -> str:
        return "create_folder"

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.WRITE

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            path = _resolve_path(kwargs.get("path"))
        except ValueError as e:
            return _fail(self, str(e))
        if reason := _blocked_reason(path):
            return _fail(self, reason)
        return await asyncio.to_thread(self._create, path)

    def _create(self, path: Path) -> ToolResult:
        if path.is_dir():
            # Idempotent: "make sure the folder exists" is the user's intent,
            # and a replan re-running this step must not die on it.
            return _ok(self, {"created": str(path), "already_existed": True})
        if path.exists():
            return _fail(
                self,
                f"A FILE named '{path}' already exists — a folder cannot be "
                f"created over it. Delete or rename the file first."
            )
        path.mkdir(parents=True)
        return _ok(self, {"created": str(path), "already_existed": False})

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Create a folder (missing parent folders are created too). "
                "Succeeds if the folder already exists. This is the ONLY way "
                "to create a folder — never use create_file (that makes a "
                "text FILE) and never mkdir through the shell."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path of the folder to create"},
                },
                "required": ["path"],
            },
            permission_level=self.permission_level,
        )


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
        payload, error = _move_one(source, destination)
        return _fail(self, error) if error else _ok(self, payload)

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
class MoveFilesTool(BaseTool):
    """Move MANY files into one folder as a single approved action.

    Why this exists (live failure 2026-07-29): "move all the PDFs in Downloads
    into this folder" used to become one move_file step PER FILE, which a
    30-step plan cap turned into a hard ~28-file ceiling — an 85-file move
    could not be planned at all, and the plan reported success having moved
    nothing. One step carrying the explicit list has no such ceiling, and the
    approval binds to that exact list (it is in the parameters, so it is in
    the step's signature) — the user still approves concrete, named files."""

    @property
    def name(self) -> str:
        return "move_files"

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.WRITE

    async def execute(self, **kwargs: Any) -> ToolResult:
        sources, error = _coerce_path_list(kwargs.get("sources"))
        if error:
            return _fail(self, error)
        try:
            destination = _resolve_path(kwargs.get("destination"))
        except ValueError as e:
            return _fail(self, str(e))
        if reason := _blocked_reason(destination):
            return _fail(self, reason)
        if not destination.exists():
            return _fail(
                self,
                f"Destination folder does not exist: '{destination}' — "
                f"create it first with create_folder",
            )
        if not destination.is_dir():
            return _fail(
                self,
                f"'{destination}' is a FILE, not a folder — moving several "
                f"files needs a destination folder",
            )
        return await asyncio.to_thread(self._move_all, sources, destination)

    def _move_all(self, sources: list[str], destination: Path) -> ToolResult:
        def operate(path: Path) -> tuple[Optional[dict], Optional[str]]:
            try:
                size = path.stat().st_size if path.is_file() else 0
            except OSError:
                size = 0
            payload, error = _move_one(path, destination)
            if payload is not None:
                payload["size_bytes"] = size
            return payload, error

        moved, failed, total = _run_batch(sources, operate)
        # SCALARS FIRST — load-bearing, not style. The ActivityLog row stores
        # json.dumps(output) clipped at registry._RESULT_SUMMARY_MAX_LEN, so
        # the counts and destination must be serialized before the long lists
        # or the audit record of a bulk move says nothing at all.
        output = {
            "moved_count": len(moved),
            "failed_count": len(failed),
            "destination": str(destination),
            "total_bytes": total,
            "moved": moved,
            "failed": failed,
        }
        if not moved:
            # Carry the record even on total failure: a failed step with
            # structured output still renders (rendering._render_step), so the
            # user reads WHICH files failed and why, not just "it failed".
            first = failed[0]["error"] if failed else "nothing to move"
            return ToolResult(
                success=False,
                output=output,
                error=f"No files were moved — all {len(failed)} failed. First: {first}",
                permission_level=self.permission_level,
            )
        return _ok(self, output)

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Move MANY files into one folder in a single step. Use this "
                "whenever more than one file is being moved (e.g. 'move all the "
                "PDFs in Downloads into that folder') — never one move_file step "
                "per file. Pass the paths as a list; a 'PENDING: ...' placeholder "
                "on 'sources' is filled in code from an earlier search's results. "
                "The destination must be an existing folder. Never overwrites: a "
                "file whose name is already taken is reported as failed and the "
                "rest still move."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "sources": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Files to move (full paths)",
                    },
                    "destination": {"type": "string", "description": "Existing target folder"},
                },
                "required": ["sources", "destination"],
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
        payload, error = _delete_one(path)
        return _fail(self, error) if error else _ok(self, payload)

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


@register_tool
class DeleteFilesTool(BaseTool):
    """Delete MANY files as a single approved action — the batch twin of
    delete_file, and the destructive sibling of move_files.

    Every file still goes to the trash FIRST, one at a time, through the same
    _delete_one primitive delete_file uses: a file whose backup cannot be made
    is not deleted, and it is reported rather than skipped silently."""

    @property
    def name(self) -> str:
        return "delete_files"

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.DESTRUCTIVE

    async def execute(self, **kwargs: Any) -> ToolResult:
        paths, error = _coerce_path_list(kwargs.get("paths"))
        if error:
            return _fail(self, error)
        return await asyncio.to_thread(self._delete_all, paths)

    def _delete_all(self, paths: list[str]) -> ToolResult:
        deleted, failed, total = _run_batch(paths, _delete_one)
        # Scalars first — see MoveFilesTool._move_all.
        output = {
            "deleted_count": len(deleted),
            "failed_count": len(failed),
            "total_bytes": total,
            "trash": str(TRASH_DIR),
            "deleted": deleted,
            "failed": failed,
        }
        if not deleted:
            first = failed[0]["error"] if failed else "nothing to delete"
            return ToolResult(
                success=False,
                output=output,
                error=f"No files were deleted — all {len(failed)} failed. First: {first}",
                permission_level=self.permission_level,
            )
        return _ok(self, output)

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Delete MANY files in a single step. Use this whenever more "
                "than one file is being deleted (e.g. 'delete all the .tmp "
                "files in that folder') — never one delete_file step per file. "
                "Pass the paths as a list; a 'PENDING: ...' placeholder on "
                "'paths' is filled in code from an earlier search's results. "
                f"Every file is moved to Jarvis's trash ({TRASH_DIR}) first, so "
                "it can be recovered by hand. Directories are refused."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Files to delete (full paths)",
                    },
                },
                "required": ["paths"],
            },
            permission_level=self.permission_level,
        )
