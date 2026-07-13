"""
Part 3 — Terminal tools.
The heart of this file is the blocklist matrix: every system-destroying
shape must be refused, and everyday commands (npm run format, git log
--format=...) must NOT be false positives. Also: the 30s timeout kills
hanging processes, non-zero exits are failures, and approval NEVER
overrides the blocklist.
"""
import sys

import pytest

from app.core.base_tool import PermissionLevel
from app.tools import terminal_tools
from app.tools.registry import execute_tool, registry
from app.tools.terminal_tools import (
    CreateFileTool,
    ExecuteScriptTool,
    RunCommandTool,
    blocked_command_reason,
)

IS_WIN = sys.platform == "win32"


# ---------------------------------------------------------------- registration

def test_all_three_tools_registered():
    for name in ("create_file", "run_command", "execute_script"):
        assert registry.get(name) is not None, f"'{name}' missing from registry"


def test_permission_levels_match_spec():
    assert registry.get("create_file").permission_level == PermissionLevel.WRITE
    assert registry.get("run_command").permission_level == PermissionLevel.DESTRUCTIVE
    assert registry.get("execute_script").permission_level == PermissionLevel.DESTRUCTIVE


# ------------------------------------------------------------------ blocklist

@pytest.mark.parametrize("command", [
    "rm -rf /",
    "rm -rf /*",
    "rm -fr /",
    "sudo rm -rf /",
    "rm -rf ~",
    "rm -rf $HOME",
    "rm --recursive --force /",
    "rm -rf /etc",
    "rm -rf C:\\",
    "format c:",
    "FORMAT C:",
    "shutdown /s /t 0",
    "shutdown -h now",
    "reboot",
    "poweroff",
    "mkfs.ext4 /dev/sda1",
    "diskpart",
    "fdisk /dev/sda",
    "del /s /q C:\\",
    "rd /s /q C:\\",
    "rmdir /s C:\\Windows",
    "dd if=/dev/zero of=/dev/sda",
    ":(){ :|:& };:",
    "reg delete HKLM\\Software /f",
    "echo hi && shutdown /s",
    "ls | poweroff",
    "Remove-Item -Recurse -Force C:\\",
    "powershell -Command Remove-Item -Recurse C:\\",
    "systemctl poweroff",
    "vssadmin delete shadows /all",
    "C:\\Windows\\System32\\shutdown.exe /s",
    # Encoded PowerShell hides the real command from the token scanner
    "powershell -EncodedCommand cwBoAHUAdABkAG8AdwBuAA==",
    "powershell -enc cwBoAHUAdABkAG8AdwBuAA==",
    "powershell -e cwBoAHUAdABkAG8AdwBuAA==",
    "pwsh -EncodedCommand cwBoAHUAdABkAG8AdwBuAA==",
    "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe -enc x",
    "echo hi && powershell -enc cwBoAHUAdABkAG8AdwBuAA==",
])
def test_blocklist_catches_destructive_commands(command):
    reason = blocked_command_reason(command)
    assert reason is not None, f"NOT blocked but should be: {command!r}"
    assert "BLOCKED" in reason


@pytest.mark.parametrize("command", [
    "echo hello",
    "git log --format=%H",
    "npm run format",
    "python format.py",
    "rm -rf ./build",
    "rm -rf node_modules",
    "del temp.txt",
    "rd /s .\\dist",  # relative — allowed (approval gate still applies)
    "dir",
    "ls -la",
    "echo shutdown is at 5pm",
    "pip install requests",
    "reg query HKLM\\Software",
    # PowerShell flags that are NOT -EncodedCommand must stay allowed
    "powershell -ExecutionPolicy Bypass -File build.ps1",
    "powershell -NoProfile -Command Get-Date",
    "pwsh -File deploy.ps1",
    # -e / -enc only matter when PowerShell itself is invoked
    "grep -e pattern file.txt",
])
def test_blocklist_allows_normal_commands(command):
    assert blocked_command_reason(command) is None, f"False positive: {command!r}"


async def test_blocked_command_never_spawns():
    result = await RunCommandTool().execute(command="shutdown /s /t 0")
    assert result.success is False
    assert "BLOCKED" in result.error


async def test_approval_does_not_override_blocklist(db_session):
    """approved=True passes the registry gate — the blocklist must still refuse."""
    result = await execute_tool(
        "run_command", {"command": "rm -rf /"}, db_session, approved=True,
    )
    assert result.success is False
    assert "BLOCKED" in result.error


# ---------------------------------------------------------------- run_command

async def test_run_command_captures_stdout(tmp_path):
    result = await RunCommandTool().execute(
        command="echo hello", working_directory=str(tmp_path),
    )
    assert result.success is True
    assert "hello" in result.output["stdout"]
    assert result.output["exit_code"] == 0
    assert result.output["timed_out"] is False


async def test_run_command_nonzero_exit_is_failure(tmp_path):
    result = await RunCommandTool().execute(
        command="echo oops 1>&2 && exit 3", working_directory=str(tmp_path),
    )
    assert result.success is False
    assert result.output["exit_code"] == 3
    assert "oops" in result.output["stderr"]
    assert "code 3" in result.error


async def test_run_command_respects_working_directory(tmp_path):
    command = "cd" if IS_WIN else "pwd"
    result = await RunCommandTool().execute(
        command=command, working_directory=str(tmp_path),
    )
    assert result.success is True
    assert str(tmp_path).lower() in result.output["stdout"].lower()


async def test_run_command_empty_is_refused():
    result = await RunCommandTool().execute(command="   ")
    assert result.success is False


async def test_run_command_missing_working_directory(tmp_path):
    result = await RunCommandTool().execute(
        command="echo hi", working_directory=str(tmp_path / "nope"),
    )
    assert result.success is False
    assert "does not exist" in result.error


async def test_run_command_timeout_kills_process(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal_tools, "COMMAND_TIMEOUT_SECONDS", 1)
    hang = "ping -n 30 127.0.0.1" if IS_WIN else "sleep 30"
    result = await RunCommandTool().execute(
        command=hang, working_directory=str(tmp_path),
    )
    assert result.success is False
    assert result.output["timed_out"] is True
    assert "timed out" in result.error


# ---------------------------------------------------------------- create_file

async def test_create_file_writes_content(tmp_path):
    target = tmp_path / "notes.txt"
    result = await CreateFileTool().execute(path=str(target), content="hello notes")
    assert result.success is True
    assert target.read_text(encoding="utf-8") == "hello notes"
    assert result.output["size_bytes"] == 11


async def test_create_file_never_overwrites(tmp_path):
    target = tmp_path / "exists.txt"
    target.write_text("original")
    result = await CreateFileTool().execute(path=str(target), content="new")
    assert result.success is False
    assert target.read_text() == "original"
    assert "overwrite" in result.error.lower()


async def test_create_file_missing_parent(tmp_path):
    result = await CreateFileTool().execute(
        path=str(tmp_path / "no_dir" / "f.txt"), content="x",
    )
    assert result.success is False
    assert "Parent folder" in result.error


async def test_create_file_parent_is_a_file_names_the_problem(tmp_path):
    """Live bug 2026-07-12: a 0-byte create_file posed as the jarvis_test
    folder; files created 'inside' it failed with an OS-level error that sent
    the replan searching. The tool now names the real problem — and the fix."""
    imposter = tmp_path / "jarvis_test"
    imposter.write_text("")
    result = await CreateFileTool().execute(
        path=str(imposter / "notes.txt"), content="test note",
    )
    assert result.success is False
    assert "FILE, not a folder" in result.error
    assert "create_folder" in result.error


async def test_create_file_empty_content_allowed(tmp_path):
    target = tmp_path / "empty.txt"
    result = await CreateFileTool().execute(path=str(target))
    assert result.success is True
    assert target.read_text() == ""


async def test_create_file_content_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal_tools, "CREATE_MAX_BYTES", 10)
    result = await CreateFileTool().execute(
        path=str(tmp_path / "big.txt"), content="x" * 50,
    )
    assert result.success is False
    assert "limit" in result.error


@pytest.mark.skipif(not IS_WIN, reason="Windows system paths")
async def test_create_file_blocked_in_system_dir():
    result = await CreateFileTool().execute(path=r"C:\Windows\evil.txt", content="x")
    assert result.success is False
    assert "protected system directory" in result.error


async def test_create_file_via_registry_requires_approval(db_session, tmp_path):
    target = tmp_path / "gated.txt"
    blocked = await execute_tool(
        "create_file", {"path": str(target), "content": "x"}, db_session,
    )
    assert blocked.success is False
    assert blocked.requires_approval is True
    assert not target.exists()

    approved = await execute_tool(
        "create_file", {"path": str(target), "content": "x"}, db_session, approved=True,
    )
    assert approved.success is True
    assert target.exists()


# ------------------------------------------------------------- execute_script

async def test_execute_python_script(tmp_path):
    script = tmp_path / "hello.py"
    script.write_text("print('hello from script')")
    result = await ExecuteScriptTool().execute(
        script_path=str(script), interpreter=sys.executable,
    )
    assert result.success is True
    assert "hello from script" in result.output["stdout"]
    assert result.output["exit_code"] == 0


async def test_execute_script_failing_exit_code(tmp_path):
    script = tmp_path / "boom.py"
    script.write_text("import sys; sys.stderr.write('bad'); sys.exit(2)")
    result = await ExecuteScriptTool().execute(
        script_path=str(script), interpreter=sys.executable,
    )
    assert result.success is False
    assert result.output["exit_code"] == 2
    assert "bad" in result.output["stderr"]


async def test_execute_script_runs_in_script_directory(tmp_path):
    script = tmp_path / "where.py"
    script.write_text("import os; print(os.getcwd())")
    result = await ExecuteScriptTool().execute(
        script_path=str(script), interpreter=sys.executable,
    )
    assert result.success is True
    assert str(tmp_path).lower() in result.output["stdout"].lower()


async def test_execute_script_missing_file(tmp_path):
    result = await ExecuteScriptTool().execute(script_path=str(tmp_path / "ghost.py"))
    assert result.success is False
    assert "not found" in result.error.lower()


async def test_script_content_blocklisted(tmp_path):
    """A script file is not a way around the command blocklist."""
    script = tmp_path / "evil.bat"
    script.write_text("echo starting\nshutdown /s /t 0\n")
    result = await ExecuteScriptTool().execute(script_path=str(script))
    assert result.success is False
    assert "BLOCKED" in result.error
    assert "Script content refused" in result.error


async def test_script_content_scan_approved_does_not_override(db_session, tmp_path):
    """approved=True passes the registry gate — the content scan still refuses."""
    script = tmp_path / "evil.ps1"
    script.write_text("Stop-Computer -Force")
    result = await execute_tool(
        "execute_script", {"script_path": str(script)}, db_session, approved=True,
    )
    assert result.success is False
    assert "BLOCKED" in result.error


async def test_script_binary_refused(tmp_path):
    script = tmp_path / "blob.py"
    script.write_bytes(b"\x00\x01\x02binary")
    result = await ExecuteScriptTool().execute(
        script_path=str(script), interpreter=sys.executable,
    )
    assert result.success is False
    assert "binary" in result.error.lower()


async def test_execute_script_disallowed_interpreter(tmp_path):
    script = tmp_path / "s.py"
    script.write_text("print('x')")
    result = await ExecuteScriptTool().execute(
        script_path=str(script), interpreter="format",
    )
    assert result.success is False
    assert "not allowed" in result.error


async def test_execute_script_unknown_extension_needs_interpreter(tmp_path):
    script = tmp_path / "mystery.xyz"
    script.write_text("???")
    result = await ExecuteScriptTool().execute(script_path=str(script))
    assert result.success is False
    assert "interpreter" in result.error.lower()


async def test_execute_script_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal_tools, "COMMAND_TIMEOUT_SECONDS", 1)
    script = tmp_path / "hang.py"
    script.write_text("import time; time.sleep(30)")
    result = await ExecuteScriptTool().execute(
        script_path=str(script), interpreter=sys.executable,
    )
    assert result.success is False
    assert result.output["timed_out"] is True
    assert "timed out" in result.error
