"""
Jarvis OS — Terminal Tools (Phase 3, Part 3)
Three single-action tools for shell access:

  create_file     WRITE        create a new text file with given content
  run_command     DESTRUCTIVE  run a shell command
  execute_script  DESTRUCTIVE  run a script file with an interpreter

Safety model:
- HARD BLOCKLIST, enforced in code before anything is spawned: system-
  destroying commands (format, shutdown, rm -rf on roots/home, mkfs, dd to
  devices, fork bombs, ...) are refused even when approved=True — approval
  never overrides the blocklist. The check is token-based per sub-command
  (split on &&, |, ;), so "npm run format" and "git log --format=%H" are
  NOT false positives, while "echo hi && shutdown /s" IS caught.
- PowerShell -EncodedCommand (and its -e/-enc prefixes) is refused outright:
  base64 hides the real command from the scanner. execute_script runs the
  same blocklist over the SCRIPT CONTENTS before spawning — a script file is
  not a way around the blocklist (best-effort: language indirection can
  evade it; the approval gate remains the primary control).
- 30-second timeout on every command/script; the process is killed on expiry.
- stdout/stderr are captured and capped; a non-zero exit code is a FAILED
  ToolResult (the planner's error recovery keys off success).
- Both run tools are DESTRUCTIVE — the registry's approval gate applies on
  top of everything here.
"""
import asyncio
import re
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from app.core.base_tool import BaseTool, PermissionLevel, ToolDefinition, ToolResult
from app.tools.file_tools import _blocked_reason, _fail, _ok, _resolve_path
from app.tools.registry import register_tool

# ------------------------------------------------------------------ limits
COMMAND_TIMEOUT_SECONDS = 30
OUTPUT_CAP_CHARS = 10_000
CREATE_MAX_BYTES = 5_242_880  # 5 MB cap for create_file content


# ============================================================== blocklist

# Commands refused when they appear in COMMAND POSITION of any sub-command
# (never as an argument — "npm run format" is fine, "format c:" is not).
_BLOCKED_COMMAND_WORDS = {
    "format", "shutdown", "reboot", "poweroff", "halt", "logoff",
    "diskpart", "fdisk", "mkswap", "vssadmin", "bcdedit",
    # PowerShell cmdlets
    "format-volume", "clear-disk", "stop-computer", "restart-computer",
}

# Shell/elevation wrappers skipped to find the real command word
_WRAPPER_TOKENS = {
    "sudo", "doas", "cmd", "cmd.exe", "powershell", "powershell.exe",
    "pwsh", "pwsh.exe", "bash", "sh", "zsh",
}

# Unix system prefixes a recursive delete may never target
_UNIX_PROTECTED_PREFIXES = (
    "/bin", "/sbin", "/usr", "/etc", "/boot", "/sys", "/proc", "/dev", "/lib",
)
# Windows system prefixes, normalized to backslashes
_WIN_PROTECTED_PREFIXES = ("c:\\windows", "c:\\program files", "c:\\programdata")

_SUBCOMMAND_SPLIT_RE = re.compile(r"[&|;\n]+")
_DRIVE_ROOT_RE = re.compile(r"[a-z]:")


def _is_protected_delete_target(raw: str) -> bool:
    """True when a recursive-delete target is a filesystem root, the user's
    home, or a system directory — the 'destroy everything' shapes."""
    t = raw.strip("'\"").lower()
    if t in {"/", "/*", "~", "~/", "~/*", "$home", "$home/", "%userprofile%", "%userprofile%\\"}:
        return True
    stripped = t.rstrip("*")
    if any(stripped == p or stripped.startswith(p + "/") for p in _UNIX_PROTECTED_PREFIXES):
        return True
    n = stripped.replace("/", "\\").rstrip("\\")
    if not n:  # was nothing but slashes/stars → filesystem root
        return True
    if _DRIVE_ROOT_RE.fullmatch(n):  # bare drive root: c:, c:\, c:/*
        return True
    if any(n == p or n.startswith(p + "\\") for p in _WIN_PROTECTED_PREFIXES):
        return True
    if n == str(Path.home()).lower().replace("/", "\\"):
        return True
    return False


def _split_flags_targets(tokens: list[str], flag_prefixes: tuple[str, ...]) -> tuple[list[str], list[str]]:
    flags = [t for t in tokens if t.startswith(flag_prefixes)]
    targets = [t for t in tokens if not t.startswith(flag_prefixes)]
    return flags, targets


_POWERSHELL_TOKENS = {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}


def _is_encoded_command_flag(token: str) -> bool:
    """True for -EncodedCommand and every prefix PowerShell accepts for it
    (-e, -en, -enc, ...). '-executionpolicy' and friends are NOT prefixes of
    '-encodedcommand', so they pass."""
    return token == "-e" or (len(token) >= 3 and "-encodedcommand".startswith(token))


def blocked_command_reason(command: str) -> Optional[str]:
    """
    Non-None when the command is on the hard blocklist. Called before any
    process is spawned, and NEVER overridden by user approval.
    """
    if ":(){" in command.replace(" ", ""):
        return "BLOCKED: fork bomb pattern — this will never be run"

    for sub in _SUBCOMMAND_SPLIT_RE.split(command):
        tokens = [t.strip("'\"").lower() for t in sub.split() if t.strip("'\"")]

        # Base64-encoded PowerShell hides its real command from this scanner —
        # refuse it outright; commands must be written in plain text.
        if any(
            t.replace("/", "\\").rsplit("\\", 1)[-1] in _POWERSHELL_TOKENS
            for t in tokens
        ) and any(_is_encoded_command_flag(t) for t in tokens):
            return (
                "BLOCKED: PowerShell -EncodedCommand hides the real command — "
                "write the command in plain text"
            )
        # Skip elevation/shell wrappers and their flags to find the command word
        i = 0
        while i < len(tokens) and (
            tokens[i] in _WRAPPER_TOKENS
            or tokens[i].startswith("-")
            or tokens[i] in {"/c", "/k"}
        ):
            i += 1
        rest = tokens[i:]
        if not rest:
            continue
        # Bare command word: strip a path prefix and .exe suffix
        cmd_word = rest[0].replace("/", "\\").rsplit("\\", 1)[-1]
        if cmd_word.endswith(".exe"):
            cmd_word = cmd_word[:-4]

        if cmd_word in _BLOCKED_COMMAND_WORDS or cmd_word.startswith("mkfs"):
            return f"BLOCKED: '{cmd_word}' is a system-destroying command and will never be run"

        if cmd_word == "systemctl" and len(rest) > 1 and rest[1] in {
            "poweroff", "reboot", "halt", "suspend", "hibernate",
        }:
            return f"BLOCKED: 'systemctl {rest[1]}' will never be run"

        # Recursive deletes aimed at roots / home / system dirs
        if cmd_word in {"rm", "remove-item", "ri"}:
            flags, targets = _split_flags_targets(rest[1:], ("-",))
            flag_text = " ".join(flags)
            recursive = (
                any("r" in f.lstrip("-") for f in flags if not f.startswith("--"))
                or "--recursive" in flags
                or "-recurse" in flag_text
            )
            if recursive and any(_is_protected_delete_target(t) for t in targets):
                return f"BLOCKED: recursive delete of a system root or home directory ('{sub.strip()}') will never be run"

        if cmd_word in {"del", "erase", "rd", "rmdir"}:
            flags, targets = _split_flags_targets(rest[1:], ("/",))
            recursive = "/s" in flags or cmd_word in {"rd", "rmdir"}
            if recursive and any(_is_protected_delete_target(t) for t in targets):
                return f"BLOCKED: recursive delete of a system root or home directory ('{sub.strip()}') will never be run"

        # dd writing to a raw device
        if cmd_word == "dd" and any(
            t.startswith("of=/dev/") or t.startswith("of=\\\\.\\") for t in rest[1:]
        ):
            return "BLOCKED: 'dd' writing to a raw device will never be run"

        # Deleting machine-wide registry hives
        if cmd_word == "reg" and len(rest) > 2 and rest[1] == "delete" and rest[2].startswith("hklm"):
            return "BLOCKED: deleting HKLM registry keys will never be run"

    return None


# ============================================================ subprocess run

def _truncate(text: str) -> str:
    if len(text) > OUTPUT_CAP_CHARS:
        return text[:OUTPUT_CAP_CHARS] + f"… (+{len(text) - OUTPUT_CAP_CHARS} chars truncated)"
    return text


def _decode(raw: bytes) -> str:
    return _truncate(raw.decode("utf-8", errors="replace"))


async def _finish(proc: asyncio.subprocess.Process) -> tuple[Optional[int], str, str, bool]:
    """Wait for the process with the timeout; kill it on expiry.
    Returns (exit_code, stdout, stderr, timed_out)."""
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), COMMAND_TIMEOUT_SECONDS)
        return proc.returncode, _decode(out_b), _decode(err_b), False
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass
        return None, "", "", True


async def _run_shell(command: str, cwd: Path) -> tuple[Optional[int], str, str, bool]:
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
    )
    return await _finish(proc)


async def _run_exec(args: list[str], cwd: Path) -> tuple[Optional[int], str, str, bool]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
    )
    return await _finish(proc)


# ============================================================== WRITE tools

@register_tool
class CreateFileTool(BaseTool):
    """Create a new text file with the given content. Never overwrites."""

    @property
    def name(self) -> str:
        return "create_file"

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.WRITE

    async def execute(self, **kwargs: Any) -> ToolResult:
        content = str(kwargs.get("content") or "")
        try:
            path = _resolve_path(kwargs.get("path"))
        except ValueError as e:
            return _fail(self, str(e))
        if reason := _blocked_reason(path):
            return _fail(self, reason)
        encoded = content.encode("utf-8")
        if len(encoded) > CREATE_MAX_BYTES:
            return _fail(self, f"Content is {len(encoded)} bytes — larger than the {CREATE_MAX_BYTES} byte limit")
        return await asyncio.to_thread(self._create, path, encoded)

    def _create(self, path: Path, encoded: bytes) -> ToolResult:
        if path.exists():
            return _fail(self, f"Refusing to overwrite: '{path}' already exists")
        if not path.parent.exists():
            return _fail(self, f"Parent folder does not exist: '{path.parent}'")
        path.write_bytes(encoded)
        return _ok(self, {"created": str(path), "size_bytes": len(encoded)})

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="Create a new text file with the given content. Never overwrites an existing file.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path of the file to create"},
                    "content": {"type": "string", "description": "Text content to write"},
                },
                "required": ["path"],
            },
            permission_level=self.permission_level,
        )


# ======================================================== DESTRUCTIVE tools

@register_tool
class RunCommandTool(BaseTool):
    """Run a shell command with a hard blocklist and 30s timeout."""

    @property
    def name(self) -> str:
        return "run_command"

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.DESTRUCTIVE

    async def execute(self, **kwargs: Any) -> ToolResult:
        command = str(kwargs.get("command") or "").strip()
        if not command:
            return _fail(self, "Command is empty")

        # The blocklist is checked BEFORE anything is spawned and is never
        # overridden by approval.
        if reason := blocked_command_reason(command):
            logger.warning(f"run_command refused by blocklist: {command!r}")
            return _fail(self, reason)

        try:
            cwd = _resolve_path(kwargs.get("working_directory") or Path.home())
        except ValueError as e:
            return _fail(self, str(e))
        if reason := _blocked_reason(cwd):
            return _fail(self, reason)
        if not cwd.is_dir():
            return _fail(self, f"Working directory does not exist: '{cwd}'")

        code, stdout, stderr, timed_out = await _run_shell(command, cwd)
        return self._result(command, str(cwd), code, stdout, stderr, timed_out)

    def _result(
        self, command: str, cwd: str,
        code: Optional[int], stdout: str, stderr: str, timed_out: bool,
    ) -> ToolResult:
        output = {
            "command": command,
            "working_directory": cwd,
            "exit_code": code,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": timed_out,
        }
        if timed_out:
            return ToolResult(
                success=False, output=output,
                error=f"Command timed out after {COMMAND_TIMEOUT_SECONDS}s and was killed",
                permission_level=self.permission_level,
            )
        if code != 0:
            snippet = stderr.strip()[:200]
            return ToolResult(
                success=False, output=output,
                error=f"Command exited with code {code}" + (f": {snippet}" if snippet else ""),
                permission_level=self.permission_level,
            )
        return _ok(self, output)

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Run a shell command (30s timeout, output captured). "
                "System-destroying commands are refused by a hard blocklist."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run"},
                    "working_directory": {"type": "string", "description": "Directory to run in (default: home)"},
                },
                "required": ["command"],
            },
            permission_level=self.permission_level,
        )


# Interpreters execute_script may use, by executable stem
_ALLOWED_INTERPRETERS = {
    "python", "python3", "py", "node", "powershell", "pwsh",
    "bash", "sh", "zsh", "cmd", "ruby", "perl",
}

_SUFFIX_INTERPRETERS = {
    ".py": "python", ".js": "node", ".sh": "bash", ".ps1": "powershell",
    ".bat": "cmd", ".cmd": "cmd", ".rb": "ruby", ".pl": "perl",
}


@register_tool
class ExecuteScriptTool(BaseTool):
    """Run a script file with a known interpreter (30s timeout)."""

    @property
    def name(self) -> str:
        return "execute_script"

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.DESTRUCTIVE

    @staticmethod
    def _scan_script(script: Path) -> Optional[str]:
        """Read the script and run the command blocklist over its contents.
        Non-None = refuse. Unreadable or binary scripts are refused too."""
        try:
            raw = script.read_bytes()[: CREATE_MAX_BYTES]
        except OSError as e:
            return f"Could not read the script before running it: {e}"
        if b"\x00" in raw[:8192]:
            return f"'{script}' looks like a binary file — refusing to execute it as a script"
        text = raw.decode("utf-8", errors="replace")
        if reason := blocked_command_reason(text):
            return f"Script content refused: {reason}"
        return None

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            script = _resolve_path(kwargs.get("script_path"))
        except ValueError as e:
            return _fail(self, str(e))
        if reason := _blocked_reason(script):
            return _fail(self, reason)
        if not script.exists():
            return _fail(self, f"Script not found: '{script}'")
        if script.is_dir():
            return _fail(self, f"'{script}' is a directory, not a script")

        # The run_command blocklist also applies to script CONTENTS — a script
        # is not a way around it. Best-effort net (it can't see through every
        # language construct); approval remains the primary gate.
        if reason := await asyncio.to_thread(self._scan_script, script):
            logger.warning(f"execute_script refused by content scan: {script}")
            return _fail(self, reason)

        interpreter = str(kwargs.get("interpreter") or "").strip()
        if not interpreter:
            interpreter = _SUFFIX_INTERPRETERS.get(script.suffix.lower(), "")
            if not interpreter:
                return _fail(
                    self,
                    f"Cannot infer an interpreter for '{script.suffix}' files — pass one explicitly",
                )
        stem = Path(interpreter).stem.lower()
        if stem not in _ALLOWED_INTERPRETERS:
            return _fail(
                self,
                f"Interpreter '{interpreter}' is not allowed. "
                f"Allowed: {', '.join(sorted(_ALLOWED_INTERPRETERS))}",
            )

        if stem in {"powershell", "pwsh"}:
            args = [interpreter, "-ExecutionPolicy", "Bypass", "-File", str(script)]
        elif stem == "cmd":
            args = [interpreter, "/c", str(script)]
        else:
            args = [interpreter, str(script)]

        try:
            code, stdout, stderr, timed_out = await _run_exec(args, script.parent)
        except FileNotFoundError:
            return _fail(self, f"Interpreter '{interpreter}' was not found on this system")

        output = {
            "script": str(script),
            "interpreter": interpreter,
            "exit_code": code,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": timed_out,
        }
        if timed_out:
            return ToolResult(
                success=False, output=output,
                error=f"Script timed out after {COMMAND_TIMEOUT_SECONDS}s and was killed",
                permission_level=self.permission_level,
            )
        if code != 0:
            snippet = stderr.strip()[:200]
            return ToolResult(
                success=False, output=output,
                error=f"Script exited with code {code}" + (f": {snippet}" if snippet else ""),
                permission_level=self.permission_level,
            )
        return _ok(self, output)

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Execute a script file with a known interpreter (python, node, "
                "bash, powershell, ...). 30s timeout; output captured."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "script_path": {"type": "string", "description": "Path of the script to run"},
                    "interpreter": {"type": "string", "description": "Interpreter to use (inferred from the extension if omitted)"},
                },
                "required": ["script_path"],
            },
            permission_level=self.permission_level,
        )
