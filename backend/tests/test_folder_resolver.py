"""
Phase 6 hardening (2026-07-12) — same-named folder disambiguation.

Live bug: "find all PDF files in downloads ..." searched the HOME Downloads
(C:\\Users\\DELL\\Downloads) when the user meant D:\\Downloads, found no PDFs,
and failed. The guard probes the drives before scoping a read to the default
home copy: two or more copies pause AWAITING_CHOICE, exactly one non-home copy
is substituted in code, and an explicit drive in the user's words stands down.

The unit tests drive `detect` directly; the integration tests run a real
AgentPlanner with a scripted FakeProvider so the pause/substitute happens
end-to-end through the execute node.
"""
import json
from pathlib import Path
from typing import AsyncIterator, List

import pytest

from app.agents import folder_resolver
from app.agents.folder_resolver import (
    FolderResolution,
    build_question,
    detect,
    find_duplicate_folders,
)
from app.agents.planner import AgentPlanner
from app.agents.schemas import PlanStatus, PlanStep
from app.core.base_tool import PermissionLevel
from app.providers.base import EmbeddingResponse, LLMProvider, LLMResponse


# --------------------------------------------------------------------- helpers

def _read_step(tool: str, **params) -> PlanStep:
    return PlanStep(
        description="a read step",
        tool=tool,
        parameters=dict(params),
        permission_level=PermissionLevel.READ,
        requires_approval=False,
    )


def _write_step(tool: str, *, description: str = "a write step", **params) -> PlanStep:
    return PlanStep(
        description=description,
        tool=tool,
        parameters=dict(params),
        permission_level=PermissionLevel.WRITE,
        requires_approval=True,
    )


def _wire(monkeypatch, home: Path, drives: List[Path]) -> None:
    monkeypatch.setattr(folder_resolver, "HOME", home)
    monkeypatch.setattr(folder_resolver, "DRIVES", [str(d) for d in drives])


def _mkdir(*parts: Path) -> None:
    for p in parts:
        p.mkdir(parents=True, exist_ok=True)


# ======================================================================= detect

def test_none_for_non_directory_tool(tmp_path, monkeypatch):
    _wire(monkeypatch, tmp_path / "home", [tmp_path / "d"])
    step = _read_step("delete_file", path="downloads")
    assert detect(step, "delete downloads", []) is None


def test_ask_when_two_copies_exist(tmp_path, monkeypatch):
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home / "downloads", d / "downloads")
    _wire(monkeypatch, home, [d])
    step = _read_step("search_files", directory="downloads", file_type="pdf")

    res = detect(step, "find all pdf files in downloads", [])

    assert res is not None
    assert res.action == "ask"
    assert res.name == "downloads"
    assert len(res.paths) == 2
    assert str(home / "downloads") in res.paths
    assert str(d / "downloads") in res.paths


def test_substitute_when_only_non_home_copy(tmp_path, monkeypatch):
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home, d / "downloads")  # home exists but has NO downloads
    _wire(monkeypatch, home, [d])
    step = _read_step("search_files", directory="downloads")

    res = detect(step, "list everything in downloads", [])

    assert res is not None and res.action == "substitute"
    assert res.paths == [str(d / "downloads")]


def test_none_when_only_home_copy(tmp_path, monkeypatch):
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home / "downloads", d)  # only the home copy exists
    _wire(monkeypatch, home, [d])
    step = _read_step("search_files", directory="downloads")
    assert detect(step, "search downloads", []) is None


def test_none_when_drive_qualifier_in_goal(tmp_path, monkeypatch):
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home / "downloads", d / "downloads")
    _wire(monkeypatch, home, [d])
    step = _read_step("search_files", directory="downloads")
    # The user was explicit about the drive — the guard must not override it.
    assert detect(step, "find pdfs in the downloads on d drive", []) is None


# ------------------------------------- the stand-down is folder-SCOPED (07-30)
#
# Live incident 2026-07-30: "create folder 'pdff' on dashboard and move all the
# pdf files from downloads in it". The 'dashboard' typo made the plan ask which
# folder was meant; the user clicked the verified option C:\Users\DELL\Desktop;
# that answer landed in user_answers, and the OLD _user_was_explicit joined
# goal+answers into one string and matched the 'C:\' in it — so an answer about
# the DESKTOP stood the DOWNLOADS guard down. The plan searched the empty home
# Downloads, found 0 of the 85 PDFs sitting in D:\Downloads, and reported
# "nothing to do". Because every clarifying question with verified path options
# puts a drive-qualified string into user_answers, ANY path question in a plan
# disarmed this guard for the rest of that plan.

def test_answer_about_another_folder_does_not_disarm_the_guard(tmp_path, monkeypatch):
    """THE INCIDENT. A path answer naming a DIFFERENT well-known folder says
    nothing about where 'downloads' is — the question must still be asked."""
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home / "downloads", d / "downloads", home / "desktop")
    _wire(monkeypatch, home, [d])
    step = _read_step("search_files", directory="downloads", file_type="pdf")
    clicked_desktop = str(home / "desktop")

    res = detect(step, "create folder 'pdff' on dashboard and move all the pdf "
                       "files from downloads in it", [clicked_desktop])

    assert res is not None, "a Desktop answer must not disarm the Downloads guard"
    assert res.action == "ask"
    assert sorted(res.paths) == sorted([str(home / "downloads"), str(d / "downloads")])


def test_answer_path_to_an_unrelated_folder_does_not_disarm_the_guard(tmp_path, monkeypatch):
    """Same rule for a path that is not a well-known folder at all: a concrete
    location the user gave for something else is not a location for this one."""
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home / "downloads", d / "downloads", d / "projects")
    _wire(monkeypatch, home, [d])
    step = _read_step("search_files", directory="downloads")

    res = detect(step, "find pdfs in downloads", [str(d / "projects")])

    assert res is not None and res.action == "ask"


def test_vague_drive_answer_still_stands_down(tmp_path, monkeypatch):
    """TERMINATION — load-bearing. A spoken drive steer names no folder, so it
    is taken as the answer for the folder in question. Without this a vague
    reply re-triggers the same question until the budget is spent, and the plan
    then searches the home copy anyway (i.e. the incident, slower)."""
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home / "downloads", d / "downloads")
    _wire(monkeypatch, home, [d])
    step = _read_step("search_files", directory="downloads")

    assert detect(step, "find pdfs in downloads", ["the one on d drive"]) is None
    assert detect(step, "find pdfs in downloads",
                  [str(home / "desktop"), "use the d drive one"]) is None


def test_vague_drive_answer_naming_another_folder_does_not_stand_down(tmp_path, monkeypatch):
    """A spoken steer that names a DIFFERENT well-known folder is about that
    folder, not this one."""
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home / "downloads", d / "downloads")
    _wire(monkeypatch, home, [d])
    step = _read_step("search_files", directory="downloads")

    res = detect(step, "find pdfs in downloads", ["the desktop one on d drive"])

    assert res is not None and res.action == "ask"


def test_answer_path_to_this_folder_still_stands_down(tmp_path, monkeypatch):
    """The scoping must not break the case the stand-down exists for: a path
    naming THIS folder is the user being specific, even when it is not a
    directory we can verify (a verifiable one substitutes instead)."""
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home / "downloads", d / "downloads")
    _wire(monkeypatch, home, [d])
    step = _read_step("search_files", directory="downloads")
    ghost = str(d / "gone" / "downloads")  # basename is 'downloads', unverifiable

    assert detect(step, "find pdfs in downloads", [ghost]) is None


def test_answer_path_is_enforced_not_trusted(tmp_path, monkeypatch):
    """A picked option / typed full path is ENFORCED in code: a step still
    heading for a different same-named copy gets the user's path substituted.
    The original design stood the guard down and trusted the revise LLM to
    fill the choice — live failure 2026-07-12: the user picked D:\\Downloads,
    the revision kept the home copy, and Furi searched the wrong folder."""
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home / "downloads", d / "downloads")
    _wire(monkeypatch, home, [d])
    step = _read_step("search_files", directory="downloads")
    answer = str(d / "downloads")

    res = detect(step, "find pdfs in downloads", [answer])

    assert res is not None and res.action == "substitute"
    assert res.paths == [answer]


def test_answer_path_matching_step_stands_down(tmp_path, monkeypatch):
    """A step already targeting the user's chosen copy is correct — no action
    (this is what terminates the answer→re-plan loop)."""
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home / "downloads", d / "downloads")
    _wire(monkeypatch, home, [d])
    chosen = str(d / "downloads")
    step = _read_step("search_files", directory=chosen)
    assert detect(step, "find pdfs in downloads", [chosen]) is None


def test_home_copy_chosen_explicitly_stands_down(tmp_path, monkeypatch):
    """Picking the HOME copy is as binding as picking a drive copy — the step
    already targets it, so nothing happens (and nothing re-asks)."""
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home / "downloads", d / "downloads")
    _wire(monkeypatch, home, [d])
    chosen = str(home / "downloads")
    step = _read_step("search_files", directory="downloads")  # resolves to home
    assert detect(step, "find pdfs in downloads", [chosen]) is None


def test_embedded_answer_path_substitutes(tmp_path, monkeypatch):
    """A typed sentence carrying the path ('its the D:\\... one') pins the
    choice too — same enforcement as a clicked option."""
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home / "downloads", d / "downloads")
    _wire(monkeypatch, home, [d])
    step = _read_step("search_files", directory="downloads")
    answer = f"i meant {d / 'downloads'}, that one"

    res = detect(step, "find pdfs in downloads", [answer])

    assert res is not None and res.action == "substitute"
    assert res.paths == [str(d / "downloads")]


def test_two_conflicting_answer_paths_never_pick(tmp_path, monkeypatch):
    """Two DIFFERENT same-named paths in the user's words: code never picks —
    the guard falls back to standing down (the drive qualifier rule)."""
    home, d, e = tmp_path / "home", tmp_path / "d", tmp_path / "e"
    _mkdir(home / "downloads", d / "downloads", e / "downloads")
    _wire(monkeypatch, home, [d, e])
    step = _read_step("search_files", directory="downloads")
    answers = [str(d / "downloads"), str(e / "downloads")]
    assert detect(step, "find pdfs in downloads", answers) is None


def test_nonexistent_answer_path_falls_through(tmp_path, monkeypatch):
    """An answer path that doesn't exist on disk pins nothing — the guard
    behaves exactly as before (drive qualifier present → stands down)."""
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home / "downloads", d / "downloads")
    _wire(monkeypatch, home, [d])
    step = _read_step("search_files", directory="downloads")
    ghost = str(d / "gone" / "downloads")
    assert detect(step, "find pdfs in downloads", [ghost]) is None


def test_a_name_outside_the_common_list_is_disambiguated_too(tmp_path, monkeypatch):
    """Until 2026-08-01 this asserted the OPPOSITE — a hardcoded six-name list
    was the gate, so "projects" duplicated across drives was silently wrong in
    exactly the way "downloads" was. The list never carried the meaning; the
    "anchored directly under home" test does."""
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home / "projects", d / "projects")
    _wire(monkeypatch, home, [d])
    step = _read_step("search_files", directory="projects")
    res = detect(step, "search projects", [])
    assert res is not None and res.action == "ask"
    assert res.paths == [str(home / "projects"), str(d / "projects")]


def test_a_bare_name_duplicated_nowhere_is_left_alone(tmp_path, monkeypatch):
    """The widening cannot invent an ambiguity: find_duplicate_folders only
    ever reports folders that actually exist."""
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home / "projects", d)
    _wire(monkeypatch, home, [d])
    step = _read_step("search_files", directory="projects")
    assert detect(step, "search projects", []) is None


def test_none_when_target_is_explicit_drive(tmp_path, monkeypatch):
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home / "downloads", d / "downloads")
    _wire(monkeypatch, home, [d])
    # An absolute path already on a specific drive is the model being correct.
    step = _read_step("search_files", directory=str(d / "downloads"))
    assert detect(step, "search downloads", []) is None


def test_none_when_folder_not_named_in_words(tmp_path, monkeypatch):
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home / "downloads", d / "downloads")
    _wire(monkeypatch, home, [d])
    step = _read_step("search_files", directory="downloads")
    # Grounding: only disambiguate a folder the USER actually named.
    assert detect(step, "list my files", []) is None


def test_none_when_multi_root_directories(tmp_path, monkeypatch):
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home / "downloads", d / "downloads")
    _wire(monkeypatch, home, [d])
    step = _read_step("search_files", directories=["downloads", str(d)])
    assert detect(step, "search downloads", []) is None


def test_pending_placeholder_ignored(tmp_path, monkeypatch):
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home / "downloads", d / "downloads")
    _wire(monkeypatch, home, [d])
    step = _read_step("search_files", directory="PENDING: the downloads folder")
    assert detect(step, "search downloads", []) is None


# ===================================== 2026-08-01: the folder a step writes INTO

def test_move_destination_is_disambiguated(tmp_path, monkeypatch):
    """THE INCIDENT. `move all pdf files from 'pdfff2' to downloads` moved 85
    PDFs into C:\\Users\\DELL\\Downloads when D:\\Downloads was meant. detect()
    WAS called on that move_files step and returned None on its first line,
    because the tool map held only search_files and list_directory."""
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home / "downloads", d / "downloads")
    _wire(monkeypatch, home, [d])
    step = _write_step(
        "move_files",
        destination=str(home / "downloads"),
        sources=[str(home / "Desktop" / "pdfff2" / "a.pdf")],
    )
    res = detect(step, "move all pdf files from 'pdfff2' to downloads", [])
    assert res is not None and res.action == "ask"
    assert res.key == "destination"
    assert res.paths == [str(home / "downloads"), str(d / "downloads")]


def test_a_move_destination_is_marked_mutating_from_the_registry(tmp_path, monkeypatch):
    """`mutating` decides which budget the ask is charged to, and it is read
    from the registry — never a hand-kept name list (registry.mutates, and the
    2026-07-30 incident that put it there)."""
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home / "downloads", d / "downloads")
    _wire(monkeypatch, home, [d])

    move = detect(
        _write_step("move_files", destination="downloads", sources=["x"]),
        "move them to downloads", [],
    )
    search = detect(
        _read_step("search_files", directory="downloads"), "search downloads", []
    )
    assert move is not None and move.mutating is True
    assert search is not None and search.mutating is False


def test_single_move_destination_is_disambiguated(tmp_path, monkeypatch):
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home / "downloads", d / "downloads")
    _wire(monkeypatch, home, [d])
    step = _write_step("move_file", source="a.pdf", destination="downloads")
    res = detect(step, "move a.pdf to downloads", [])
    assert res is not None and res.action == "ask" and res.key == "destination"


def test_parent_mode_keeps_the_basename(tmp_path, monkeypatch):
    """create_file/create_folder name something INSIDE the ambiguous folder, so
    the folder is the parameter's PARENT and the substitution has to carry the
    basename over."""
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home, d / "downloads")  # only the drive copy exists
    _wire(monkeypatch, home, [d])

    res = detect(
        _write_step("create_file", path="downloads/notes.txt", content="hi"),
        "save a note in downloads", [],
    )
    assert res is not None and res.action == "substitute"
    assert res.name == "downloads"
    assert res.value == str(d / "downloads" / "notes.txt")


def test_create_folder_inside_an_ambiguous_folder_asks(tmp_path, monkeypatch):
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home / "downloads", d / "downloads")
    _wire(monkeypatch, home, [d])
    res = detect(
        _write_step("create_folder", path="downloads/archive"),
        "make an archive folder in downloads", [],
    )
    assert res is not None and res.action == "ask"
    assert res.paths == [str(home / "downloads"), str(d / "downloads")]


def test_creating_the_ambiguous_folder_itself_does_not_ask(tmp_path, monkeypatch):
    """`create_folder path="downloads"` puts the new folder in home; its PARENT
    is home, which is not a duplicated name. Creating is not entering."""
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home, d / "downloads")
    _wire(monkeypatch, home, [d])
    step = _write_step("create_folder", path="downloads")
    assert detect(step, "create a downloads folder", []) is None


def test_run_command_working_directory_is_disambiguated(tmp_path, monkeypatch):
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home / "downloads", d / "downloads")
    _wire(monkeypatch, home, [d])
    step = _write_step("run_command", command="dir", working_directory="downloads")
    res = detect(step, "run dir in downloads", [])
    assert res is not None and res.action == "ask"
    assert res.key == "working_directory"


def test_exempt_path_params_stay_silent(tmp_path, monkeypatch):
    """The sources of a move and the path of a delete are concrete paths a
    prior READ step produced — never a home-anchored bare name."""
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home / "downloads", d / "downloads")
    _wire(monkeypatch, home, [d])
    goal = "delete everything in downloads"
    assert detect(_write_step("delete_file", path="downloads"), goal, []) is None
    assert detect(
        _write_step("move_files", sources=["downloads"], destination=str(d)), goal, []
    ) is None


def test_every_path_param_is_covered_or_exempt():
    """THE COVERAGE INVARIANT. A new file tool with a path parameter must be a
    deliberate decision, not an oversight — 2026-08-01's hole was a tool map
    that had simply never been revisited. This fails loudly instead.

    If this test fails: add the (tool, param) to _FOLDER_PARAMS if the step
    OPERATES INSIDE that folder, or to _EXEMPT_PATH_PARAMS (with a reason) if
    the parameter carries a concrete path an earlier step produced."""
    import app.tools  # noqa: F401 — self-registration
    from app.tools.registry import registry

    covered = {(t, p) for t, (p, _) in folder_resolver._FOLDER_PARAMS.items()}
    exempt = set(folder_resolver._EXEMPT_PATH_PARAMS)
    path_like = ("path", "paths", "source", "sources", "destination", "directory",
                 "directories", "working_directory", "script_path")

    missing = set()
    for tool in registry.definitions():
        for param in (tool.parameters.get("properties") or {}):
            if param not in path_like:
                continue
            if (tool.name, param) in covered or (tool.name, param) in exempt:
                continue
            missing.add((tool.name, param))

    # search_files.directories is handled inside detect() (an explicit
    # multi-root list is the model being specific, not defaulting to home).
    missing.discard(("search_files", "directories"))
    assert not missing, (
        f"path parameters in neither _FOLDER_PARAMS nor _EXEMPT_PATH_PARAMS: "
        f"{sorted(missing)}"
    )


# ======================================================= find_duplicate_folders

def test_find_duplicate_folders_dedups_and_orders(tmp_path, monkeypatch):
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home / "downloads", d / "downloads")
    # Same drive listed twice → deduped by normalized path.
    _wire(monkeypatch, home, [d, d])
    found = find_duplicate_folders("downloads")
    assert found[0] == str(home / "downloads")  # home first
    assert str(d / "downloads") in found
    assert len(found) == 2


def test_find_duplicate_folders_empty_when_none(tmp_path, monkeypatch):
    _wire(monkeypatch, tmp_path / "home", [tmp_path / "d"])
    assert find_duplicate_folders("downloads") == []


# ================================================================ build_question

def test_build_question_lists_real_options():
    res = FolderResolution("ask", "directory", "downloads",
                           ["C:/Users/x/downloads", "D:/downloads"])
    q = build_question(res)
    assert "downloads" in q.text
    assert q.options == ["C:/Users/x/downloads", "D:/downloads"]


# ============================================================ planner integration

class _FakeProvider(LLMProvider):
    def __init__(self, responses: List[str]) -> None:
        self._responses = list(responses)
        self.prompts: List[str] = []

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model_name(self) -> str:
        return "fake-model"

    async def chat(self, messages, temperature: float = 0.7, max_tokens=None) -> LLMResponse:
        self.prompts.append(messages[0].content)
        if not self._responses:
            raise AssertionError("FakeProvider exhausted")
        return LLMResponse(content=self._responses.pop(0), model="fake-model", provider="fake")

    async def stream_chat(self, messages, temperature: float = 0.7, max_tokens=None) -> AsyncIterator[str]:
        yield ""

    async def embed(self, text: str) -> EmbeddingResponse:
        return EmbeddingResponse(embedding=[0.0] * 384, model="fake", provider="fake")


def _plan_json(steps: list) -> str:
    return json.dumps({"steps": steps, "unachievable_reason": None, "question": None})


async def test_planner_pauses_on_ambiguous_folder(db_session, tmp_path, monkeypatch):
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home / "downloads", d / "downloads")
    _wire(monkeypatch, home, [d])

    steps = [{"description": "Search PDFs in downloads", "tool": "search_files",
              "parameters": {"directory": "downloads", "file_type": "pdf"}}]
    provider = _FakeProvider([_plan_json(steps), _plan_json(steps)])

    planner = AgentPlanner(db_session, provider, session_id="s-fold")
    plan = await planner.start("find all pdf files in downloads and count them")

    assert plan.status == PlanStatus.AWAITING_CHOICE
    assert plan.question is not None
    assert set(plan.question.options) == {str(home / "downloads"), str(d / "downloads")}
    # The search never ran — we did not silently scan the wrong folder.
    assert plan.steps[0].status.value == "pending"


async def test_planner_substitutes_single_non_home_copy(db_session, tmp_path, monkeypatch):
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home, d / "downloads")  # only the drive copy exists
    (d / "downloads" / "report.pdf").write_text("x")
    _wire(monkeypatch, home, [d])

    steps = [{"description": "Search PDFs in downloads", "tool": "search_files",
              "parameters": {"directory": "downloads", "file_type": "pdf"}}]
    provider = _FakeProvider([_plan_json(steps), _plan_json(steps)])

    planner = AgentPlanner(db_session, provider, session_id="s-fold2")
    plan = await planner.start("find all pdf files in downloads")

    assert plan.status == PlanStatus.COMPLETED
    assert plan.steps[0].parameters["directory"] == str(d / "downloads")
    assert plan.steps[0].result.output["count"] == 1


async def test_the_move_incident_pauses_before_moving_anything(
    db_session, tmp_path, monkeypatch
):
    """2026-08-01, end to end: `move all pdf files from 'pdfff2' to downloads`
    moved 85 PDFs into the home Downloads without ever asking. The plan must now
    stop at the which-Downloads question with BOTH real paths on offer, and the
    files must still be sitting in pdfff2 when it does."""
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home / "downloads", d / "downloads")
    src = home / "Desktop" / "pdfff2"
    _mkdir(src)
    for name in ("a.pdf", "b.pdf"):
        (src / name).write_text("x")
    _wire(monkeypatch, home, [d])

    steps = [
        {"description": "Find the PDFs in pdfff2", "tool": "search_files",
         "parameters": {"directory": str(src), "file_type": "pdf"}},
        {"description": "Move them into Downloads", "tool": "move_files",
         "parameters": {"destination": str(home / "downloads"),
                        "sources": ["PENDING: the pdf files found above"]}},
    ]
    provider = _FakeProvider([_plan_json(steps), _plan_json(steps)])

    planner = AgentPlanner(db_session, provider, session_id="s-move-incident")
    plan = await planner.start("move all pdf files from 'pdfff2' to downloads")

    assert plan.status == PlanStatus.AWAITING_CHOICE
    assert plan.question is not None
    assert set(plan.question.options) == {
        str(home / "downloads"), str(d / "downloads")
    }
    # Charged to the folder budget, not the LLM clarification cap.
    assert plan.folder_handoffs == 1
    assert plan.questions_asked == 0
    # Nothing moved, and nothing landed in either Downloads.
    assert sorted(p.name for p in src.iterdir()) == ["a.pdf", "b.pdf"]
    assert list((home / "downloads").iterdir()) == []
    assert list((d / "downloads").iterdir()) == []


async def test_the_answered_move_shows_the_chosen_drive_on_the_card(
    db_session, tmp_path, monkeypatch
):
    """A substituted destination must reach action_detail AND the code-authored
    description. Both quote the path verbatim (_describe_batch produced the
    incident's literal "Move 85 file(s) (181.9 MB) into C:\\Users\\DELL\\
    Downloads"), and the rewrite used to touch only step.parameters — invisible
    while the guard saw reads only, a lie on the approval card the moment it
    sees a move."""
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home / "downloads", d / "downloads")
    src = home / "Desktop" / "pdfff2"
    _mkdir(src)
    for name in ("a.pdf", "b.pdf"):
        (src / name).write_text("x")
    _wire(monkeypatch, home, [d])

    steps = [
        {"description": "Find the PDFs in pdfff2", "tool": "search_files",
         "parameters": {"directory": str(src), "file_type": "pdf"}},
        {"description": "Move them into Downloads", "tool": "move_files",
         "parameters": {"destination": str(home / "downloads"),
                        "sources": ["PENDING: the pdf files found above"]}},
    ]
    # draft + reflect (pause), then a DISOBEDIENT revision keeping the home copy.
    provider = _FakeProvider([_plan_json(steps), _plan_json(steps), _plan_json(steps)])

    planner = AgentPlanner(db_session, provider, session_id="s-move-answer")
    plan = await planner.start("move all pdf files from 'pdfff2' to downloads")
    assert plan.status == PlanStatus.AWAITING_CHOICE

    answered = await planner.answer(plan, str(d / "downloads"))

    # It pauses for APPROVAL — the user still has to say yes to the real path.
    move = next(s for s in answered.steps if s.tool == "move_files")
    assert move.parameters["destination"] == str(d / "downloads")
    assert str(d / "downloads") in (move.action_detail or "")
    assert str(d / "downloads") in move.description
    # …and the drive nobody chose appears in neither text.
    assert str(home / "downloads") not in (move.action_detail or "")
    assert str(home / "downloads") not in move.description
    # Still nothing moved: the approval card has not been answered.
    assert sorted(p.name for p in src.iterdir()) == ["a.pdf", "b.pdf"]


async def test_a_write_asks_even_when_the_question_budget_is_spent(
    db_session, tmp_path, monkeypatch
):
    """MAX_QUESTIONS is the LLM's clarification cap. A mutating step must not
    lose its turn to it and then run on a guessed drive — that is the incident's
    outcome reached by a different road. Driven by setting MAX_QUESTIONS to 0:
    a read would fall through here; the write must still stop."""
    from app.agents import planner as planner_mod

    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home / "downloads", d / "downloads")
    src = home / "Desktop" / "pdfff2"
    _mkdir(src)
    (src / "a.pdf").write_text("x")
    _wire(monkeypatch, home, [d])
    monkeypatch.setattr(planner_mod, "MAX_QUESTIONS", 0)

    steps = [
        {"description": "Find the PDFs", "tool": "search_files",
         "parameters": {"directory": str(src), "file_type": "pdf"}},
        {"description": "Move them into Downloads", "tool": "move_files",
         "parameters": {"destination": str(home / "downloads"),
                        "sources": ["PENDING: the pdf files found above"]}},
    ]
    provider = _FakeProvider([_plan_json(steps), _plan_json(steps)])

    planner = AgentPlanner(db_session, provider, session_id="s-move-budget")
    plan = await planner.start("move all pdf files from 'pdfff2' to downloads")

    assert plan.status == PlanStatus.AWAITING_CHOICE
    assert plan.folder_handoffs == 1
    assert plan.questions_asked == 0        # charged to neither the LLM's cap…
    assert list((home / "downloads").iterdir()) == []   # …nor moved anything


async def test_a_spent_folder_budget_names_the_alternatives_on_the_card(
    db_session, tmp_path, monkeypatch
):
    """The last resort. A plan touching more ambiguous folders than the budget
    allows runs on the guessed copy — but the card must never imply the choice
    was unambiguous when code knows it was not."""
    from app.agents import planner as planner_mod

    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home / "downloads", d / "downloads")
    src = home / "Desktop" / "pdfff2"
    _mkdir(src)
    (src / "a.pdf").write_text("x")
    _wire(monkeypatch, home, [d])
    monkeypatch.setattr(planner_mod, "_MAX_FOLDER_HANDOFFS", 0)

    steps = [
        {"description": "Find the PDFs", "tool": "search_files",
         "parameters": {"directory": str(src), "file_type": "pdf"}},
        {"description": "Move them into Downloads", "tool": "move_files",
         "parameters": {"destination": str(home / "downloads"),
                        "sources": ["PENDING: the pdf files found above"]}},
    ]
    provider = _FakeProvider([_plan_json(steps), _plan_json(steps)])

    planner = AgentPlanner(db_session, provider, session_id="s-move-spent")
    plan = await planner.start("move all pdf files from 'pdfff2' to downloads")

    move = next(s for s in plan.steps if s.tool == "move_files")
    detail = move.action_detail or ""
    assert "2 folders named 'downloads' exist" in detail
    assert str(d / "downloads") in detail
    # It still had to be approved — the note is disclosure, not permission.
    assert move.status.value == "pending"


async def test_answered_choice_is_enforced_against_disobedient_revision(
    db_session, tmp_path, monkeypatch
):
    """The 2026-07-12 verification failure, end to end: the plan pauses on
    the which-downloads question, the user picks the drive copy, and the
    revise LLM DISOBEYS the answer (keeps the bare home-resolving name).
    The execute node must enforce the picked path in code — the search runs
    against the folder the user chose, never the home copy the model kept."""
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home / "downloads", d / "downloads")
    (d / "downloads" / "report.pdf").write_text("x")  # only the D copy has PDFs

    _wire(monkeypatch, home, [d])

    steps = [{"description": "Search PDFs in downloads", "tool": "search_files",
              "parameters": {"directory": "downloads", "file_type": "pdf"}}]
    # draft + reflect (pause), then the DISOBEDIENT post-answer revision:
    # identical bare-name step, exactly what groq/llama produced live.
    provider = _FakeProvider([_plan_json(steps), _plan_json(steps), _plan_json(steps)])

    planner = AgentPlanner(db_session, provider, session_id="s-fold3")
    plan = await planner.start("find all pdf files in downloads and count them")
    assert plan.status == PlanStatus.AWAITING_CHOICE

    final = await planner.answer(plan, str(d / "downloads"))

    assert final.status == PlanStatus.COMPLETED
    search = final.steps[-1]
    assert search.parameters["directory"] == str(d / "downloads")
    assert search.result.output["count"] == 1
    assert "report.pdf" in search.result.output["matches"][0]["path"]
