"""
open_folder — showing the user a folder on screen (2026-08-06).

THE GAP THIS CLOSES. "open my downloads folder" had no tool at all. It landed
on `list_directory`, which prints the contents into the chat when the user
asked for a WINDOW — or, following plan rule 25's own advice to "use
run_command for anything else", on a DESTRUCTIVE shell-command approval card,
which then reported FAILED because `explorer.exe` exits non-zero on success.

THE PROPERTY EVERY TEST HERE ORBITS: what reaches the OS is ALWAYS A DIRECTORY.
A file path opens the folder CONTAINING it, so the tool can only ever put a
file-explorer window on screen — it can never open a document in its handler
and never execute a program. That is what keeps it at WRITE instead of
DESTRUCTIVE, and it is a property of the code rather than of an allowlist.

The launcher is stubbed everywhere (conftest `_hermetic_open_folder` refuses by
default): a suite that opened twenty real Explorer windows is a suite nobody
runs twice.
"""
import os
import sys
from pathlib import Path

import pytest

import app.tools  # noqa: F401 — importing the package registers the tools
from app.core.base_tool import PermissionLevel
from app.tools import file_tools
from app.tools.registry import execute_tool, registry


@pytest.fixture
def opened(monkeypatch):
    """Records every folder handed to the OS, and opens nothing."""
    calls: list[Path] = []
    monkeypatch.setattr(file_tools, "OPEN_LAUNCHER", lambda folder: calls.append(folder))
    return calls


# ---------------------------------------------------------------- registration

def test_registered_as_a_write_tool():
    tool = registry.get("open_folder")
    assert tool is not None, "open_folder missing from the registry"
    # WRITE, not DESTRUCTIVE: it shows the user files they already have access
    # to and changes nothing. Not READ either — it acts outside the chat.
    assert tool.permission_level == PermissionLevel.WRITE


def test_the_tool_takes_no_command_or_argument_parameter():
    """The `launch_app` argument, one tool over: a thin ShellExecute over a
    model-supplied string is `run_command` with the approval gate weakened.
    `path` is the ONLY input, so there is no command line to inject into."""
    schema = registry.get("open_folder").definition().parameters
    assert set(schema["properties"]) == {"path"}


# --------------------------------------------- THE INVARIANT: never execute

async def test_a_file_opens_its_containing_folder_never_the_file(db_session, tmp_path, opened):
    """⚠️ THE LOAD-BEARING TEST. `os.startfile("setup.exe")` RUNS it, and
    `os.startfile("payload.bat")` RUNS it. Resolving a file to its parent is
    the single line that makes "this tool cannot execute anything" true."""
    target = tmp_path / "report.pdf"
    target.write_text("not really a pdf")

    result = await execute_tool(
        "open_folder", {"path": str(target)}, db_session, approved=True,
    )

    assert result.success
    assert opened == [tmp_path], "the FILE itself must never reach the OS"
    assert result.output["opened"] == str(tmp_path)
    assert result.output["showed_containing_folder"] is True


@pytest.mark.parametrize("name", ["setup.exe", "payload.bat", "run.cmd", "x.ps1", "page.html"])
async def test_an_executable_file_is_still_only_ever_revealed(
    db_session, tmp_path, opened, name
):
    """No extension allowlist exists, and none is needed: every file resolves
    to its parent, so the dangerous types are handled by the same one line as
    the harmless ones. An allowlist would be the seventh instance of the
    'a second copy of a list is a hole' defect."""
    target = tmp_path / name
    target.write_text("x")

    result = await execute_tool(
        "open_folder", {"path": str(target)}, db_session, approved=True,
    )

    assert result.success
    assert opened == [tmp_path]
    assert str(target) not in [str(p) for p in opened]


async def test_a_directory_opens_itself(db_session, tmp_path, opened):
    folder = tmp_path / "phase3test"
    folder.mkdir()

    result = await execute_tool(
        "open_folder", {"path": str(folder)}, db_session, approved=True,
    )

    assert result.success
    assert opened == [folder]
    assert result.output["opened"] == str(folder)
    assert result.output["showed_containing_folder"] is False


def test_folder_to_show_is_the_whole_invariant(tmp_path):
    """Stated as a unit so the property is checkable without the tool around
    it — this is the function every safety claim in the module rests on."""
    folder = tmp_path / "d"
    folder.mkdir()
    a_file = tmp_path / "f.exe"
    a_file.write_text("x")

    assert file_tools._folder_to_show(folder) == folder
    assert file_tools._folder_to_show(a_file) == tmp_path


# ------------------------------------------------------------- path guards

async def test_a_path_that_does_not_exist_fails_and_says_how_to_find_it(
    db_session, tmp_path, opened
):
    """The `_missing_target` philosophy: a code-authored error that steers the
    replan to a search, rather than a dead end."""
    result = await execute_tool(
        "open_folder", {"path": str(tmp_path / "ghost")}, db_session, approved=True,
    )

    assert result.success is False
    assert "does not exist" in result.error
    assert "search_files" in result.error
    assert opened == []


async def test_a_protected_system_directory_is_refused(db_session, opened):
    """Reuses file_tools' own `_blocked_reason` rather than re-deriving path
    safety — living in file_tools is what makes that reuse free."""
    protected = file_tools._PROTECTED
    if not protected:
        pytest.skip("no protected roots on this platform")

    result = await execute_tool(
        "open_folder", {"path": str(protected[0])}, db_session, approved=True,
    )

    assert result.success is False
    assert "protected system directory" in result.error
    assert opened == []


async def test_a_filesystem_root_is_refused(db_session, opened):
    root = Path(sys.executable).resolve().anchor
    result = await execute_tool(
        "open_folder", {"path": root}, db_session, approved=True,
    )

    assert result.success is False
    assert "filesystem root" in result.error
    assert opened == []


async def test_the_folder_the_window_would_open_is_guarded_too(
    db_session, tmp_path, opened, monkeypatch
):
    """Defence in depth, and it is reachable rather than theoretical: a file
    sitting directly at a drive root passes the first guard (the FILE is
    neither a root nor inside a protected dir) while the folder we would
    actually open IS the root. Both are checked."""
    root = Path(sys.executable).resolve().anchor
    monkeypatch.setattr(file_tools, "_folder_to_show", lambda p: Path(root))

    result = await execute_tool(
        "open_folder", {"path": str(tmp_path)}, db_session, approved=True,
    )

    assert result.success is False
    assert "filesystem root" in result.error
    assert opened == []


async def test_an_empty_path_is_refused(db_session, opened):
    result = await execute_tool("open_folder", {"path": "  "}, db_session, approved=True)
    assert result.success is False
    assert opened == []


async def test_a_launcher_failure_names_the_folder_it_could_not_open(
    db_session, tmp_path, monkeypatch
):
    """`safe_execute` already guarantees no exception escapes any tool, so
    "it did not raise" is not what this pins — it would pass without the
    handler. What the local handler buys is a DIAGNOSABLE message: which
    folder, and what went wrong, instead of the wrapper's generic
    "raised an unexpected error"."""
    def _boom(folder):
        raise OSError("no window manager")

    monkeypatch.setattr(file_tools, "OPEN_LAUNCHER", _boom)
    result = await execute_tool(
        "open_folder", {"path": str(tmp_path)}, db_session, approved=True,
    )

    assert result.success is False
    assert "Could not open" in result.error
    assert str(tmp_path) in result.error
    assert "no window manager" in result.error
    assert "unexpected error" not in result.error


async def test_a_launcher_that_returns_is_success(db_session, tmp_path, opened):
    """⚠️ THE FALSE-FAILED FIX, pinned. `explorer.exe` conventionally exits
    non-zero having opened the window perfectly well, and `run_command` treats
    any non-zero exit as failure — which is how Jarvis used to open the folder
    and then report the step FAILED. This tool never consults an exit code:
    a launcher that starts is a success, and only one that cannot start fails."""
    result = await execute_tool(
        "open_folder", {"path": str(tmp_path)}, db_session, approved=True,
    )
    assert result.success is True
    assert opened == [tmp_path]


# ------------------------------------------- the approval gate, end to end

async def test_unapproved_open_is_structurally_blocked(db_session, tmp_path, opened):
    blocked = await execute_tool("open_folder", {"path": str(tmp_path)}, db_session)

    assert blocked.success is False
    assert blocked.requires_approval is True
    assert opened == [], "nothing may reach the OS without approval"


# ------------------------------------------------------ coverage of the maps
# Each of these is a map a new WRITE tool must appear in. They are asserted
# directly as well as by the registry-walking coverage tests, so a failure
# names open_folder rather than reporting an anonymous gap.

def test_the_pre_flight_guard_requires_the_path_to_exist():
    """So a hallucinated folder fails into the replan loop BEFORE the approval
    pause — the user is never asked to approve a guessed path."""
    from app.agents.planner import _MUST_EXIST_PARAMS

    assert _MUST_EXIST_PARAMS.get("open_folder") == "path"


def test_the_which_drive_guard_covers_it():
    """"open downloads" with C:\\Downloads and D:\\Downloads both present must
    ASK, exactly as a move destination does — the path is the LLM writing a
    bare name the user spoke, which is precisely what that guard is for."""
    from app.agents.folder_resolver import _EXEMPT_PATH_PARAMS, _FOLDER_PARAMS

    assert _FOLDER_PARAMS.get("open_folder") == ("path", "self")
    assert ("open_folder", "path") not in _EXEMPT_PATH_PARAMS


def test_it_has_a_spoken_form():
    from app.agents.spoken import SPOKEN_STEPS

    spoken = SPOKEN_STEPS["open_folder"]({"path": r"C:\Users\DELL\Downloads"})
    assert "Downloads" in spoken
    assert "folder" in spoken


def test_the_approval_card_names_the_folder():
    from app.agents.planner import _step_action_detail

    detail = _step_action_detail("open_folder", {"path": r"C:\Users\DELL\Downloads"})
    assert r"C:\Users\DELL\Downloads" in detail


def test_the_audit_trail_reads_it_back_in_english():
    from app.agents.rendering import _ACTION_LINE_KEYS

    assert _ACTION_LINE_KEYS["open_folder"] == ("opened folder", "path", None)


def test_both_agents_that_can_be_asked_to_open_a_folder_can_see_it():
    """"open my downloads folder" fires the strong_domain gate on 'downloads'
    and routes TASK (the file agent); "open the downloads folder" can equally
    land on DESKTOP. An agent without the tool falls back to run_command —
    the defect being removed."""
    from app.agents.agent_registry import AGENTS

    assert "open_folder" in AGENTS["file"].tools
    assert "open_folder" in AGENTS["desktop"].tools


def test_the_plan_rules_point_at_the_tool_and_no_longer_at_the_shell():
    """Rule 25 used to end with "use run_command for anything else", which is
    what produced the DESTRUCTIVE shell card for opening a folder."""
    from app.agents.planner import _PLAN_RULES

    assert "open_folder" in _PLAN_RULES
    assert "use run_command for anything else" not in _PLAN_RULES


# --------------------------------------------------- through the real planner
# The unit tests above prove the tool behaves. These prove the PLANNER reaches
# it correctly — the distinction that has cost this codebase four separate
# rounds (a feature that never fired under 1,578 green tests).

async def test_a_guessed_folder_never_reaches_the_approval_card(db_session, tmp_path, opened):
    from tests.test_agent_planner import FakeProvider, plan_json, step

    real = tmp_path / "phase3test"
    real.mkdir()
    guessed = tmp_path / "does-not-exist"

    bad = [step("Open the folder", "open_folder", path=str(guessed))]
    good = [step("Open the folder", "open_folder", path=str(real))]
    # draft, reflect, then the replan the pre-flight guard forces.
    provider = FakeProvider([plan_json(bad), plan_json(bad), plan_json(good)])

    from app.agents.planner import AgentPlanner
    from app.agents.schemas import PlanStatus

    plan = await AgentPlanner(db_session, provider).start("open the phase3test folder")

    assert opened == [], "nothing was opened while the path was still a guess"
    pending = [s for s in plan.steps if s.status.value == "pending"]
    assert plan.status == PlanStatus.AWAITING_APPROVAL
    # The card the user finally sees names the REAL folder, not the guess.
    assert pending, "expected a step waiting for approval"
    assert str(real) in (pending[-1].action_detail or "")
    assert str(guessed) not in (pending[-1].action_detail or "")


async def test_approving_the_card_opens_exactly_that_folder(db_session, tmp_path, opened):
    from tests.test_agent_planner import FakeProvider, plan_json, step

    from app.agents.planner import AgentPlanner
    from app.agents.schemas import PlanStatus

    folder = tmp_path / "reports"
    folder.mkdir()
    steps = [step("Open the reports folder", "open_folder", path=str(folder))]
    provider = FakeProvider([plan_json(steps), plan_json(steps)])
    planner = AgentPlanner(db_session, provider)

    plan = await planner.start("open the reports folder")
    assert plan.status == PlanStatus.AWAITING_APPROVAL
    assert opened == []  # the pause really is before the action

    plan = await planner.resume(plan, approved=True)

    assert plan.status == PlanStatus.COMPLETED
    assert opened == [folder]


# ------------------------------------------------------------ the real launcher
# The default launcher is replaced everywhere above. These check the one thing
# that matters about it and never actually call it.

def test_the_production_default_is_the_real_launcher():
    """Asserted against the SOURCE, not the live binding: the autouse
    `_hermetic_open_folder` fixture has already swapped the live one out, which
    is the point of it. What needs pinning is that the stub is a test artifact
    and production gets the real thing — i.e. nobody ever committed the seam
    pointing at a no-op, which would be a tool that silently opens nothing."""
    source = Path(file_tools.__file__).read_text(encoding="utf-8")
    assert "\nOPEN_LAUNCHER = _default_open_launcher\n" in source


@pytest.mark.skipif(os.name != "nt", reason="the Windows branch")
def test_the_windows_launcher_uses_no_shell_and_no_exit_code():
    """`os.startfile` on a DIRECTORY invokes the folder's open verb. There is
    no command line, so nothing to quote wrongly and nothing to inject into —
    and no exit code to misread."""
    import inspect

    source = inspect.getsource(file_tools._default_open_launcher)
    assert "os.startfile" in source
    assert "shell=True" not in source
    assert "check=True" not in source
    assert "returncode" not in source
