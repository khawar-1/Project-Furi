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
and never execute a program. It is a property of the code rather than of an
allowlist, which is what separates this tool from `run_command`.

⚠️ It became LOAD-BEARING later the same day, when the tool dropped to READ on
the report that opening a folder should not ask permission. The approval gate
no longer stands behind it: the invariant, `_blocked_reason`, and the planner's
`_MUST_EXIST_PARAMS` pre-flight check are now the whole of the safety story,
so each is pinned here directly.

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

def test_registered_as_a_read_tool():
    tool = registry.get("open_folder")
    assert tool is not None, "open_folder missing from the registry"
    # READ (2026-08-06): showing a folder asks for no approval. The argument
    # that settles it is one tool over — `list_directory` is READ and pulls
    # that same folder's whole contents into an LLM prompt and into the chat.
    assert tool.permission_level == PermissionLevel.READ
    # The contrast that keeps the line meaningful: launch_app RUNS a program.
    assert registry.get("launch_app").permission_level == PermissionLevel.WRITE


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


# --------------------------------------- no approval, and what replaces it

async def test_opening_a_folder_asks_for_no_approval(db_session, tmp_path, opened):
    """THE 2026-08-06 UX FIX, pinned. Reported as: "opening a file/folder isn't
    a destructive task so it shouldn't ask permission — it should only ask if
    there are two folders of the same name". Note the omitted `approved=`."""
    result = await execute_tool("open_folder", {"path": str(tmp_path)}, db_session)

    assert result.success is True
    assert result.requires_approval is False
    assert opened == [tmp_path]


async def test_without_the_gate_the_never_execute_invariant_carries_alone(
    db_session, tmp_path, opened
):
    """⚠️ Removing the approval gate makes the code guards MORE load-bearing,
    not less — they are now the whole of it. Both still hold with no approval
    anywhere in sight: an executable resolves to its PARENT (so the OS is
    handed a directory, never a program), and a protected path is refused."""
    exe = tmp_path / "setup.exe"
    exe.write_text("not really an installer")

    result = await execute_tool("open_folder", {"path": str(exe)}, db_session)
    assert result.success is True
    assert opened == [tmp_path], "the OS must never be handed the .exe itself"

    opened.clear()
    protected = [p for p in file_tools._PROTECTED if os.path.isdir(p)]
    if protected:
        refused = await execute_tool(
            "open_folder", {"path": str(protected[0])}, db_session
        )
        assert refused.success is False
        assert opened == []


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

    # ⚠️ THE GUARD THAT SURVIVED THE GATE. With open_folder at READ there is no
    # approval pause left to catch a guessed path, so the pre-flight
    # `_MUST_EXIST_PARAMS` check is now the ONLY thing standing between a
    # hallucinated folder and the OS — and it is independent of approval. The
    # guess never opens; the replan's REAL folder does.
    assert plan.status == PlanStatus.COMPLETED
    assert opened == [real], "only the real folder may ever reach the OS"
    assert str(guessed) not in [str(p) for p in opened]


async def test_opening_a_folder_completes_in_one_turn_with_no_card(
    db_session, tmp_path, opened
):
    """THE UX FIX AT PLAN LEVEL. This used to pause on an approval card for
    the crime of showing the user their own folder; now the whole request is
    one uninterrupted turn. The unit test above proves the TOOL asks for no
    approval — this proves the PLANNER does not manufacture a pause anyway,
    which is the distinction that has cost this codebase four rounds."""
    from tests.test_agent_planner import FakeProvider, plan_json, step

    from app.agents.planner import AgentPlanner
    from app.agents.schemas import PlanStatus

    folder = tmp_path / "reports"
    folder.mkdir()
    steps = [step("Open the reports folder", "open_folder", path=str(folder))]
    provider = FakeProvider([plan_json(steps), plan_json(steps)])

    plan = await AgentPlanner(db_session, provider).start("open the reports folder")

    assert plan.status == PlanStatus.COMPLETED
    assert opened == [folder]
    assert not [s for s in plan.steps if s.requires_approval], (
        "showing a folder must not require approval"
    )


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


# ============================================================================
# The 2026-08-06 live round: "open donwloads" / "open folder 'fomi'"
#
# Two messages, three defects. Root-caused from the routing audit and the
# stored plan payloads, not from the screenshot:
#   - "open donwloads"     -> opened C:\Users\DELL\Downloads without asking,
#                             while D:\Downloads also existed.
#   - "open folder 'fomi'" -> 93 SECONDS of planning: the PENDING placeholder
#                             was resolvable from the search that had just run,
#                             but no map knew the tool, so the step FAILED, a
#                             replan burned, and the user answered a question
#                             and an approval card for one folder.
# ============================================================================

def test_the_placeholder_resolver_knows_the_tool():
    """THE COVERAGE INVARIANT, and it is exact: if the pre-flight guard
    requires a parameter to point at something REAL on disk, then the designed
    flow for that parameter is "read first, then PENDING" - so a PENDING there
    MUST be resolvable in code. A parameter in _MUST_EXIST_PARAMS with no
    placeholder map dead-ends that flow into an LLM replan, which is exactly
    what happened to open_folder.

    It holds for every other entry (delete/rename/move/execute_script via
    _FILE_PARAMS, run_command via _DIR_PARAMS) and held for open_folder only
    after this round. Walking the map means the NEXT tool fails a test rather
    than a user - the discipline that found `read_file.path` in
    `test_every_path_param_is_covered_or_exempt`."""
    from app.agents import placeholder_resolver as pr
    from app.agents.planner import _MUST_EXIST_LIST_PARAMS, _MUST_EXIST_PARAMS

    single_maps = (
        pr._FILE_PARAMS, pr._DIR_PARAMS, pr._OPEN_TARGET_PARAMS,
        pr._EMAIL_TO_PARAMS, pr._EVENT_ID_PARAMS, pr._ENTITY_ID_PARAMS,
        pr._WINDOW_HANDLE_PARAMS, pr._URL_PARAMS,
    )
    uncovered = [
        f"{tool}.{param}"
        for tool, param in _MUST_EXIST_PARAMS.items()
        if not any(m.get(tool) == param for m in single_maps)
    ]
    uncovered += [
        f"{tool}.{param}"
        for tool, param in _MUST_EXIST_LIST_PARAMS.items()
        if pr._LIST_PARAMS.get(tool) != param
    ]
    assert not uncovered, (
        "a pre-flight-guarded parameter with no placeholder resolver: "
        f"{uncovered}. A 'PENDING:' there fails the step and burns an LLM "
        "replan round instead of being filled from the read that precedes it."
    )


def _search_step(matches):
    from app.agents.schemas import PlanStep, StepStatus
    from app.core.base_tool import ToolResult

    step = PlanStep(
        id="s1", description="find it", tool="search_files",
        parameters={"query": "fomi", "include_folders": True},
        permission_level="read", requires_approval=False,
    )
    step.status = StepStatus.COMPLETED
    step.result = ToolResult(
        success=True, output={"matches": matches, "count": len(matches)},
    )
    return step


def _open_template(text="PENDING: full path of the folder named 'fomi'"):
    from app.agents.schemas import PlanStep

    return PlanStep(
        id="s2", description="Open the 'fomi' folder found by the search.",
        tool="open_folder", parameters={"path": text},
        permission_level="read", requires_approval=False,
    )


def _resolved(matches, template=None):
    from app.agents.placeholder_resolver import resolve
    from app.agents.schemas import AgentPlan

    plan = AgentPlan(
        goal="open folder 'fomi'",
        steps=[_search_step(matches), template or _open_template()],
    )
    return resolve(plan, 1, max_new=8)


def test_the_fomi_placeholder_resolves_in_code():
    """THE INCIDENT, frozen. The search had already found the folder; the
    resolver simply had no branch for the tool. Filling it here is what
    removes a failed step, a replan round, a clarifying question and an
    approval card from a one-folder request."""
    steps = _resolved([
        {"path": r"C:\Users\DELL\Desktop\FOMI", "type": "folder"},
    ])

    assert steps is not None, "must NOT fall through to the LLM replan path"
    assert len(steps) == 1
    assert steps[0].tool == "open_folder"
    assert steps[0].parameters["path"] == r"C:\Users\DELL\Desktop\FOMI"
    assert "PENDING" not in steps[0].parameters["path"].upper()


def test_a_named_folder_wins_over_the_others_the_search_returned():
    steps = _resolved([
        {"path": r"D:\stuff\notes", "type": "folder"},
        {"path": r"C:\Users\DELL\Desktop\FOMI", "type": "folder"},
        {"path": r"D:\archive\backup", "type": "folder"},
    ])
    assert steps is not None
    assert steps[0].parameters["path"] == r"C:\Users\DELL\Desktop\FOMI"


def test_several_equally_plausible_folders_are_left_to_the_planner():
    """Code never picks - the module's own rule. Two folders both named
    'fomi' and nothing in the placeholder text to separate them."""
    assert _resolved([
        {"path": r"C:\Users\DELL\Desktop\fomi", "type": "folder"},
        {"path": r"D:\projects\fomi", "type": "folder"},
    ]) is None


def test_a_file_resolves_so_show_me_where_this_lives_also_works():
    """Plan rule 9: "give open_folder a FILE path when the user wants to see
    where a file lives". Folder-only substitution would have dead-ended that
    flow exactly as the incident did - which is why open_folder is not simply
    another entry in _DIR_PARAMS."""
    steps = _resolved(
        [{"path": r"C:\Users\DELL\Documents\resume.pdf", "type": "file"}],
        template=_open_template("PENDING: the folder containing resume.pdf"),
    )
    assert steps is not None
    assert steps[0].parameters["path"] == r"C:\Users\DELL\Documents\resume.pdf"


def test_a_file_is_never_guessed_while_a_folder_was_also_found():
    """The ordering that keeps the rule free of a folder-vs-file judgement:
    once any folder is on the table, an unpinned choice is the planner's."""
    assert _resolved([
        {"path": r"D:\a\fomi", "type": "folder"},
        {"path": r"D:\b\fomi", "type": "folder"},
        {"path": r"C:\notes\fomi.txt", "type": "file"},
    ]) is None


def test_a_search_that_found_nothing_resolves_nothing():
    assert _resolved([]) is None


# ------------------------------------------ the which-drive guard, and typos

def _open_step(path):
    from app.agents.schemas import PlanStep

    return PlanStep(
        id="s", description="open it", tool="open_folder",
        parameters={"path": path}, permission_level="read",
        requires_approval=False,
    )


@pytest.fixture
def two_downloads(tmp_path, monkeypatch):
    """A home with Downloads, and a second drive that also has one."""
    from app.agents import folder_resolver

    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True)
    other = tmp_path / "D"
    (other / "Downloads").mkdir(parents=True)
    monkeypatch.setattr(folder_resolver, "HOME", home)
    monkeypatch.setattr(folder_resolver, "DRIVES", [str(other)])
    return home


def test_a_typo_still_asks_which_downloads(two_downloads):
    """THE INCIDENT, frozen. "open donwloads" opened the home copy while the
    other drive's Downloads also existed. The guard was correct and every
    other precondition held - `_named_in_words` alone stood it down, because a
    transposition does not match the exact word boundary. A typo is still the
    user naming the folder."""
    from app.agents.folder_resolver import detect

    res = detect(_open_step(str(two_downloads / "Downloads")), "open donwloads", [])

    assert res is not None, "a typo must not silently pick a drive"
    assert res.action == "ask"
    assert len(res.paths) == 2


@pytest.mark.parametrize("goal", [
    "open donwloads", "open downlods", "open dowloads", "open downlaods",
    "open my downloadss folder", "show me my download folder",
    "open downloads",
])
def test_the_ways_people_type_it(two_downloads, goal):
    from app.agents.folder_resolver import detect

    res = detect(_open_step(str(two_downloads / "Downloads")), goal, [])
    assert res is not None and res.action == "ask", goal


@pytest.mark.parametrize("goal", [
    "open documents",          # a different folder entirely
    "open the desktop folder",
    "open downstairs",         # measured 63.2 - a real word, not a typo
    "open notepad",
    "open the projects folder",
])
def test_words_that_are_not_that_folder_do_not_trigger_it(two_downloads, goal):
    """The corpus is still GROUNDING: only a name the user really said may
    open a question about it. A permissive floor must not become no floor."""
    from app.agents.folder_resolver import detect

    assert detect(_open_step(str(two_downloads / "Downloads")), goal, []) is None, goal


def test_the_floor_self_scales_with_name_length():
    """MEASURED, AND THE REASON THE FLOOR NEEDS NO SEPARATE LENGTH GATE.
    A one-character difference scores 66.7 at 3 letters and 85.7 at 7, so
    short names ("src", "docs", "fomi", "test") get NO typo tolerance - where
    one letter usually means a DIFFERENT word - and long names get it, where
    it almost always means a slip. Moving _TYPO_FLOOR breaks this and must
    fail loudly rather than quietly admitting `dogs` for `docs`."""
    from rapidfuzz import fuzz

    from app.agents.folder_resolver import _TYPO_FLOOR

    name = "abcdefghijkl"
    for n, tolerated in [(3, False), (4, False), (5, False), (6, False),
                         (7, True), (9, True), (11, True)]:
        stem = name[:n]
        one_off = "z" + stem[1:]
        assert (fuzz.ratio(one_off, stem) >= _TYPO_FLOOR) is tolerated, (
            f"a one-character difference in a {n}-letter name"
        )
    # The pairs that must never collapse into each other, measured at 75.0.
    for a, b in [("dogs", "docs"), ("text", "test"), ("form", "fomi")]:
        assert fuzz.ratio(a, b) < _TYPO_FLOOR, f"{a}/{b}"


def test_a_typo_never_invents_a_folder(two_downloads):
    """The options are `find_duplicate_folders` output - real directories
    only. A fuzzy NAME match cannot conjure a path that is not on disk."""
    from app.agents.folder_resolver import detect

    res = detect(_open_step(str(two_downloads / "Downloads")), "open donwloads", [])
    assert res is not None
    assert all(os.path.isdir(p) for p in res.paths)


async def test_the_fomi_incident_end_to_end_costs_one_llm_call(
    db_session, tmp_path, opened
):
    """THE 93-SECOND DEFECT, frozen at plan level and measured in LLM calls.

    Live 2026-08-06, "open folder 'fomi'": the planner drafted exactly the flow
    the plan rules ask for - search first, then open the PENDING path - and the
    resolver had no branch for the tool, so the step FAILED on "unresolved
    'PENDING:' placeholders". The audit puts what followed at 93 seconds of
    planning: a burned replan round, a clarifying question the user answered,
    and an approval card, all for one folder.

    ONE scripted response is the whole assertion. The provider is handed a
    single draft and would raise "exhausted" on a second call, so a replan
    round cannot hide inside a passing test. Reflection is skipped because
    every step is read-level (open_folder is READ since this round), and the
    placeholder is filled from the search's own output with no model involved.
    """
    from tests.test_agent_planner import FakeProvider, plan_json, step

    from app.agents.planner import AgentPlanner
    from app.agents.schemas import PlanStatus, StepStatus

    target = tmp_path / "Desktop" / "FOMI"
    target.mkdir(parents=True)

    draft = plan_json([
        step("Search your computer for a folder named 'fomi'.", "search_files",
             query="fomi", directory=str(tmp_path), include_folders=True),
        step("Open the 'fomi' folder found by the search.", "open_folder",
             path="PENDING: full path of the folder named 'fomi' found by the search"),
    ])
    provider = FakeProvider([draft])

    plan = await AgentPlanner(db_session, provider).start("open folder 'fomi'")

    assert plan.status == PlanStatus.COMPLETED
    assert opened == [target], "the folder the search actually found, opened once"
    assert provider.calls == 1, (
        "one draft and nothing else: a replan round here is the defect"
    )
    assert not [s for s in plan.steps if s.status == StepStatus.FAILED], (
        "the placeholder step must not fail - it is resolvable from the search"
    )
    # A replan and a clarifying question each cost an LLM call, so
    # `provider.calls == 1` above already rules both out; this states the
    # user-visible half of the incident directly.
    assert plan.questions_asked == 0, "no question for a folder the search found"
    assert not [s for s in plan.steps if s.requires_approval]


def test_the_mode_rule_names_opening_a_folder_as_a_quick_read():
    """MEASURED, and unlike most prompt edits in this codebase it moves the
    needle. Live, "open donwloads" ran INLINE while "open folder 'fomi'" was
    DELEGATE'd to the background file agent - the same request, two different
    experiences, and the user asked why.

    A/B against the real model, 6 phrasings x 3 runs (scripts/
    _measure_open_mode.py): INLINE recall 5/12 -> 12/12, with both DELEGATE
    controls ("organize my downloads into folders by type", "delete every tmp
    file on my desktop") unchanged at 3/3. The clause about searching first is
    what carries "open folder 'fomi'", which needs a search step and therefore
    reads as multi-step without it.

    Recorded because this is NOT the falsified prompt-hardening shape: the
    2026-08-06 catalog line measured 12/12 in BOTH arms and was honestly
    written up as belt rather than cause. Whether a line is load-bearing is a
    measurement every time."""
    from app.api.task_router import _CLASSIFY_PROMPT

    assert "open a folder on screen" in _CLASSIFY_PROMPT
    assert "Opening a folder is INLINE even when Jarvis has to search" in _CLASSIFY_PROMPT
