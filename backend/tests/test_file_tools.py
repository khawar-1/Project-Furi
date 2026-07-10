"""
Part 2 — File tools.
Each action is exercised against a tmp directory, including every rejection
path: protected system dirs, overwrites, directories where files are
expected, binary/oversize reads, and path-separator smuggling in renames.
"""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import app.tools  # noqa: F401 — importing the package registers the tools
from app.core.base_tool import PermissionLevel
from app.tools import file_tools
from app.tools.file_tools import (
    DeleteFileTool,
    ListDirectoryTool,
    MoveFileTool,
    ReadFileTool,
    RenameFileTool,
    SearchFilesTool,
    _blocked_reason,
    _resolve_path,
)
from app.tools.registry import execute_tool, registry


# ---------------------------------------------------------------- registration

def test_all_six_tools_registered_globally():
    for name in (
        "search_files", "read_file", "list_directory",
        "move_file", "rename_file", "delete_file",
    ):
        assert registry.get(name) is not None, f"'{name}' missing from registry"


def test_permission_levels_match_spec():
    assert registry.get("search_files").permission_level == PermissionLevel.READ
    assert registry.get("read_file").permission_level == PermissionLevel.READ
    assert registry.get("list_directory").permission_level == PermissionLevel.READ
    assert registry.get("move_file").permission_level == PermissionLevel.WRITE
    assert registry.get("rename_file").permission_level == PermissionLevel.WRITE
    assert registry.get("delete_file").permission_level == PermissionLevel.DESTRUCTIVE


# ---------------------------------------------------------------- path safety

def test_resolve_expands_home():
    assert _resolve_path("~") == file_tools.Path.home().resolve()


def test_resolve_relative_lands_in_home():
    resolved = _resolve_path("some_folder/file.txt")
    assert file_tools.Path.home().resolve() in resolved.parents


def test_resolve_rejects_empty():
    with pytest.raises(ValueError):
        _resolve_path("   ")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows system paths")
async def test_system_directory_blocked_for_read():
    result = await ReadFileTool().execute(path=r"C:\Windows\System32\drivers\etc\hosts")
    assert result.success is False
    assert "protected system directory" in result.error


@pytest.mark.skipif(sys.platform != "win32", reason="Windows system paths")
async def test_system_directory_blocked_for_delete():
    result = await DeleteFileTool().execute(path=r"C:\Windows\notepad.exe")
    assert result.success is False
    assert "protected system directory" in result.error


async def test_filesystem_root_blocked():
    root = "C:\\" if sys.platform == "win32" else "/"
    result = await ListDirectoryTool().execute(path=root)
    assert result.success is False
    assert "filesystem root" in result.error


# ---------------------------------------------------------------- search_files

@pytest.fixture
def tree(tmp_path):
    (tmp_path / "a_report.pdf").write_text("pdf-ish")
    (tmp_path / "notes.txt").write_text("notes")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "data_report.csv").write_text("csv")
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    (hidden / "secret_report.txt").write_text("secret")
    return tmp_path


async def test_search_by_query(tree):
    result = await SearchFilesTool().execute(query="report", directory=str(tree))
    assert result.success is True
    paths = {m["path"] for m in result.output["matches"]}
    assert len(paths) == 2  # hidden dir pruned
    assert any("a_report.pdf" in p for p in paths)
    assert any("data_report.csv" in p for p in paths)


async def test_search_with_file_type_filter(tree):
    result = await SearchFilesTool().execute(
        query="report", directory=str(tree), file_type=".pdf",
    )
    assert result.success is True
    assert result.output["count"] == 1
    assert result.output["matches"][0]["path"].endswith("a_report.pdf")


async def test_search_by_extension_only(tree):
    result = await SearchFilesTool().execute(directory=str(tree), file_type="txt")
    assert result.success is True
    assert result.output["count"] == 1  # only notes.txt; hidden dir pruned


async def test_search_no_criterion_scoped_to_folder_returns_everything(tree):
    """'All files in <folder>' is a bounded, legitimate ask — live failure
    2026-07-10: 'delete all files in phase3test' has no criterion to give,
    and the old refusal killed the plan after three identical rejections.
    A criterion-less search scoped to an explicit folder matches every file
    under it (recursively; hidden dirs still pruned)."""
    result = await SearchFilesTool().execute(directory=str(tree))
    assert result.success is True
    names = {Path(m["path"]).name for m in result.output["matches"]}
    assert names == {"a_report.pdf", "notes.txt", "data_report.csv"}


async def test_search_no_criterion_unscoped_still_refused():
    """Without an explicit directory a criterion-less search would sweep the
    whole home directory — that refusal stays, and the error now says how to
    fix it (scope it with 'directory')."""
    result = await SearchFilesTool().execute()
    assert result.success is False
    assert "at least one criterion" in result.error
    assert "directory" in result.error


async def test_search_no_criterion_drive_root_still_refused():
    """A whole drive is not a bounded folder — match-all over it stays
    refused even though the scope is explicit."""
    root = "C:\\" if sys.platform == "win32" else "/"
    result = await SearchFilesTool().execute(directory=root)
    assert result.success is False
    assert "entire drive" in result.error


async def test_search_nonexistent_directory(tmp_path):
    result = await SearchFilesTool().execute(query="x", directory=str(tmp_path / "nope"))
    assert result.success is False


# ------------------------------------------------- search filters (in code)

async def test_search_modified_date_range(tmp_path):
    """A bare date is inclusive on BOTH bounds — human ranges name whole
    days ("9 June to today" includes today)."""
    old = tmp_path / "old.txt"
    new = tmp_path / "new.txt"
    old.write_text("o")
    new.write_text("n")
    long_ago = datetime(2020, 3, 15, 12, 0).timestamp()
    os.utime(old, (long_ago, long_ago))

    result = await SearchFilesTool().execute(
        directory=str(tmp_path), file_type="txt", modified_before="2021-01-01",
    )
    assert [m["path"] for m in result.output["matches"]] == [str(old)]

    result = await SearchFilesTool().execute(
        directory=str(tmp_path), file_type="txt", modified_after="2021-01-01",
    )
    assert [m["path"] for m in result.output["matches"]] == [str(new)]

    # Inclusive "after": the file's own modification day matches
    result = await SearchFilesTool().execute(
        directory=str(tmp_path), file_type="txt", modified_after="2020-03-15",
        modified_before="2020-03-16",
    )
    assert [m["path"] for m in result.output["matches"]] == [str(old)]


async def test_search_before_date_includes_the_named_day(tmp_path):
    """Live gap 2026-07-10: "modification date 9 june to todays date" filled
    modified_before=<today>, and the old strictly-before bound silently
    dropped every file modified today. A bare end date now covers that whole
    day; a full datetime keeps exact strictly-before semantics."""
    target = tmp_path / "noon.txt"
    target.write_text("x")
    noon = datetime(2020, 3, 15, 12, 0).timestamp()
    os.utime(target, (noon, noon))

    # The named day itself matches…
    hit = await SearchFilesTool().execute(
        directory=str(tmp_path), file_type="txt",
        modified_after="2020-03-01", modified_before="2020-03-15",
    )
    assert hit.output["count"] == 1

    # …but only that day: the day before still excludes it
    miss = await SearchFilesTool().execute(
        directory=str(tmp_path), file_type="txt", modified_before="2020-03-14",
    )
    assert miss.output["count"] == 0

    # A full DATETIME stays exact: strictly before noon excludes a noon file
    miss = await SearchFilesTool().execute(
        directory=str(tmp_path), file_type="txt",
        modified_before="2020-03-15T12:00:00",
    )
    assert miss.output["count"] == 0
    hit = await SearchFilesTool().execute(
        directory=str(tmp_path), file_type="txt",
        modified_before="2020-03-15T12:00:01",
    )
    assert hit.output["count"] == 1


async def test_search_created_date_bounds(tmp_path):
    """Creation time can't be faked portably — bracket 'now' instead."""
    target = tmp_path / "fresh.txt"
    target.write_text("x")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    hit = await SearchFilesTool().execute(
        directory=str(tmp_path), file_type="txt", created_after=yesterday,
    )
    assert hit.output["count"] == 1

    miss = await SearchFilesTool().execute(
        directory=str(tmp_path), file_type="txt", created_after=tomorrow,
    )
    assert miss.output["count"] == 0

    miss = await SearchFilesTool().execute(
        directory=str(tmp_path), file_type="txt", created_before=yesterday,
    )
    assert miss.output["count"] == 0

    # created_before follows the same whole-day rule: "until today" works
    today = datetime.now().strftime("%Y-%m-%d")
    hit = await SearchFilesTool().execute(
        directory=str(tmp_path), file_type="txt", created_before=today,
    )
    assert hit.output["count"] == 1


async def test_search_rejects_non_iso_dates(tmp_path):
    """'03/04/2026' is ambiguous (March 4 vs April 3) — refused in code, so
    the planner has to normalize or ask, never guess."""
    result = await SearchFilesTool().execute(
        directory=str(tmp_path), query="x", created_after="03/04/2026",
    )
    assert result.success is False
    assert "ISO" in result.error
    assert "year-month-day" in result.error


async def test_search_size_bounds(tmp_path):
    small = tmp_path / "small.txt"
    big = tmp_path / "big.txt"
    small.write_text("x" * 10)
    big.write_text("x" * 5000)

    result = await SearchFilesTool().execute(
        directory=str(tmp_path), file_type="txt", min_size=100,
    )
    assert [m["path"] for m in result.output["matches"]] == [str(big)]

    result = await SearchFilesTool().execute(
        directory=str(tmp_path), file_type="txt", max_size=100,
    )
    assert [m["path"] for m in result.output["matches"]] == [str(small)]

    result = await SearchFilesTool().execute(
        directory=str(tmp_path), file_type="txt", min_size="not-a-number",
    )
    assert result.success is False
    assert "bytes" in result.error


async def test_search_filter_only_needs_no_query(tmp_path):
    """'files created after july 1' has no name to search for — a date or
    size filter alone is a valid criterion."""
    (tmp_path / "a.txt").write_text("x")
    result = await SearchFilesTool().execute(
        directory=str(tmp_path), modified_after="2000-01-01",
    )
    assert result.success is True
    assert result.output["count"] == 1


async def test_search_include_folders(tmp_path):
    (tmp_path / "project_alpha").mkdir()
    (tmp_path / "project_notes.txt").write_text("x")

    # Default: files only
    result = await SearchFilesTool().execute(query="project", directory=str(tmp_path))
    assert [m["type"] for m in result.output["matches"]] == ["file"]

    # include_folders: both, folder carries type + no size
    result = await SearchFilesTool().execute(
        query="project", directory=str(tmp_path), include_folders=True,
    )
    by_type = {m["type"]: m for m in result.output["matches"]}
    assert set(by_type) == {"file", "folder"}
    assert by_type["folder"]["size_bytes"] is None
    assert by_type["folder"]["path"] == str(tmp_path / "project_alpha")

    # The extension filter never matches folders
    result = await SearchFilesTool().execute(
        query="project", directory=str(tmp_path), include_folders=True, file_type="txt",
    )
    assert [m["type"] for m in result.output["matches"]] == ["file"]


async def test_search_multiple_directories(tmp_path):
    root_a = tmp_path / "drive_a"
    root_b = tmp_path / "drive_b"
    root_a.mkdir()
    root_b.mkdir()
    (root_a / "report_a.txt").write_text("a")
    (root_b / "report_b.txt").write_text("b")

    result = await SearchFilesTool().execute(
        query="report", directories=[str(root_a), str(root_b)],
    )
    assert result.success is True
    assert result.output["count"] == 2
    assert result.output["searched_in"] == [str(root_a), str(root_b)]


async def test_search_too_many_roots_refused(tmp_path):
    result = await SearchFilesTool().execute(
        query="x", directories=[str(tmp_path)] * 9,
    )
    assert result.success is False
    assert "8" in result.error


def test_drive_roots_allowed_for_search_only():
    """search_files may start at a drive root ('anywhere on my PC'); every
    other operation still refuses roots."""
    root = Path("C:\\") if sys.platform == "win32" else Path("/")
    assert _blocked_reason(root, allow_root=True) is None
    assert "filesystem root" in _blocked_reason(root)


# ---------------------------------------------------------------- read_file

async def test_read_text_file(tmp_path):
    target = tmp_path / "hello.txt"
    target.write_text("hello jarvis", encoding="utf-8")
    result = await ReadFileTool().execute(path=str(target))
    assert result.success is True
    assert result.output["content"] == "hello jarvis"
    assert result.output["size_bytes"] == 12


async def test_read_missing_file(tmp_path):
    result = await ReadFileTool().execute(path=str(tmp_path / "ghost.txt"))
    assert result.success is False
    assert "not found" in result.error.lower()


async def test_read_directory_refused(tmp_path):
    result = await ReadFileTool().execute(path=str(tmp_path))
    assert result.success is False
    assert "list_directory" in result.error


async def test_read_binary_refused(tmp_path):
    target = tmp_path / "blob.bin"
    target.write_bytes(b"abc\x00def")
    result = await ReadFileTool().execute(path=str(target))
    assert result.success is False
    assert "binary" in result.error


async def test_read_oversize_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(file_tools, "READ_MAX_BYTES", 10)
    target = tmp_path / "big.txt"
    target.write_text("x" * 50)
    result = await ReadFileTool().execute(path=str(target))
    assert result.success is False
    assert "read limit" in result.error


# ------------------------------------------------------------- list_directory

async def test_list_directory(tmp_path):
    (tmp_path / "b.txt").write_text("b")
    (tmp_path / "folder").mkdir()
    result = await ListDirectoryTool().execute(path=str(tmp_path))
    assert result.success is True
    entries = {e["name"]: e for e in result.output["entries"]}
    assert entries["folder"]["type"] == "directory"
    assert entries["b.txt"]["type"] == "file"
    assert entries["b.txt"]["size_bytes"] == 1
    # directories sort before files
    assert result.output["entries"][0]["name"] == "folder"


async def test_list_missing_directory(tmp_path):
    result = await ListDirectoryTool().execute(path=str(tmp_path / "nope"))
    assert result.success is False


async def test_list_file_refused(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("x")
    result = await ListDirectoryTool().execute(path=str(target))
    assert result.success is False
    assert "read_file" in result.error


# ---------------------------------------------------------------- move_file

async def test_move_into_existing_directory(tmp_path):
    src = tmp_path / "doc.txt"
    src.write_text("content")
    dest_dir = tmp_path / "archive"
    dest_dir.mkdir()
    result = await MoveFileTool().execute(source=str(src), destination=str(dest_dir))
    assert result.success is True
    assert not src.exists()
    assert (dest_dir / "doc.txt").read_text() == "content"


async def test_move_to_explicit_path(tmp_path):
    src = tmp_path / "doc.txt"
    src.write_text("content")
    target = tmp_path / "renamed_doc.txt"
    result = await MoveFileTool().execute(source=str(src), destination=str(target))
    assert result.success is True
    assert target.exists() and not src.exists()


async def test_move_never_overwrites(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("new")
    existing = tmp_path / "b.txt"
    existing.write_text("old")
    result = await MoveFileTool().execute(source=str(src), destination=str(existing))
    assert result.success is False
    assert "overwrite" in result.error.lower()
    assert existing.read_text() == "old" and src.exists()


async def test_move_missing_source(tmp_path):
    result = await MoveFileTool().execute(
        source=str(tmp_path / "ghost.txt"), destination=str(tmp_path),
    )
    assert result.success is False


async def test_move_directory_refused(tmp_path):
    src = tmp_path / "folder"
    src.mkdir()
    result = await MoveFileTool().execute(source=str(src), destination=str(tmp_path / "elsewhere"))
    assert result.success is False
    assert "directory" in result.error


async def test_move_to_missing_parent(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("x")
    result = await MoveFileTool().execute(
        source=str(src), destination=str(tmp_path / "no_such_dir" / "a.txt"),
    )
    assert result.success is False
    assert "does not exist" in result.error


# ---------------------------------------------------------------- rename_file

async def test_rename_file(tmp_path):
    src = tmp_path / "old.txt"
    src.write_text("x")
    result = await RenameFileTool().execute(path=str(src), new_name="new.txt")
    assert result.success is True
    assert (tmp_path / "new.txt").exists() and not src.exists()


async def test_rename_rejects_path_separators(tmp_path):
    src = tmp_path / "old.txt"
    src.write_text("x")
    result = await RenameFileTool().execute(path=str(src), new_name="sub/new.txt")
    assert result.success is False
    assert "move_file" in result.error


async def test_rename_never_overwrites(tmp_path):
    src = tmp_path / "old.txt"
    src.write_text("x")
    (tmp_path / "taken.txt").write_text("y")
    result = await RenameFileTool().execute(path=str(src), new_name="taken.txt")
    assert result.success is False
    assert src.exists()


async def test_rename_missing_path(tmp_path):
    result = await RenameFileTool().execute(path=str(tmp_path / "ghost"), new_name="x")
    assert result.success is False


# ---------------------------------------------------------------- delete_file

async def test_delete_file(tmp_path, monkeypatch):
    monkeypatch.setattr(file_tools, "TRASH_DIR", tmp_path / "trash")
    target = tmp_path / "junk.tmp"
    target.write_text("junk")
    result = await DeleteFileTool().execute(path=str(target))
    assert result.success is True
    assert not target.exists()
    assert result.output["deleted"] == str(target)
    # The file is recoverable from the trash, contents intact
    backup = Path(result.output["backed_up_to"])
    assert backup.exists()
    assert backup.parent == tmp_path / "trash"
    assert backup.read_text() == "junk"


async def test_delete_file_trash_collision(tmp_path, monkeypatch):
    """Two deletes of same-named files in the same second must not clobber."""
    monkeypatch.setattr(file_tools, "TRASH_DIR", tmp_path / "trash")
    backups = []
    for content in ("first", "second"):
        target = tmp_path / "same_name.txt"
        target.write_text(content)
        result = await DeleteFileTool().execute(path=str(target))
        assert result.success is True
        backups.append(Path(result.output["backed_up_to"]))
    assert backups[0] != backups[1]
    assert backups[0].read_text() == "first"
    assert backups[1].read_text() == "second"


async def test_delete_directory_refused(tmp_path):
    folder = tmp_path / "folder"
    folder.mkdir()
    result = await DeleteFileTool().execute(path=str(folder))
    assert result.success is False
    assert folder.exists()
    assert "directory" in result.error


async def test_delete_missing_file(tmp_path):
    result = await DeleteFileTool().execute(path=str(tmp_path / "ghost.txt"))
    assert result.success is False


# ------------------------------------------- end-to-end through the registry

async def test_delete_via_registry_requires_approval(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(file_tools, "TRASH_DIR", tmp_path / "trash")
    target = tmp_path / "victim.txt"
    target.write_text("data")

    blocked = await execute_tool("delete_file", {"path": str(target)}, db_session)
    assert blocked.success is False
    assert blocked.requires_approval is True
    assert target.exists()  # nothing happened

    approved = await execute_tool(
        "delete_file", {"path": str(target)}, db_session, approved=True,
    )
    assert approved.success is True
    assert not target.exists()
