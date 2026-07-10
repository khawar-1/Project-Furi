"""
Deterministic placeholder resolution + goal-scope fidelity (2026-07-10).

Live incident (screenshot): "hey list all file/folders in desktop and delete
all files in phase3test" —
  1. the draft narrowed "all files" to a '.txt'-only search because
     long-term memory held ".txt" facts from the previous day's testing;
  2. two PENDING-placeholder steps were treated as FAILURES, each burning an
     LLM replan round — the user watched two red steps that never touched a
     tool, plus the identical phase3test search run twice;
  3. the plan paused for approval on a step still reading "PENDING: .txt
     file paths", and after the user approved it the placeholder "failed"
     again and the replan call died on the provider's daily rate limit —
     with every path the plan needed already sitting in completed results.

The fixes under test, all structural:
  - placeholder_resolver: placeholders fill IN CODE from completed step
    results (per-file templates expand, folder names substitute, zero
    matches skip honestly) — checked BEFORE the approval pause;
  - _scope_violation: an extension filter the user never said, on an
    explicitly-universal "all files" goal, is rejected with retry feedback;
  - _drop_completed_duplicates: a revision re-issuing an already-completed
    signature is deduplicated in code.
"""
import json
from typing import AsyncIterator, List, Optional

import pytest

import app.tools  # noqa: F401 — registers the real file/terminal/memory tools
from app.agents import placeholder_resolver
from app.agents.placeholder_resolver import paths_from_step, resolve
from app.agents.planner import (
    AgentPlanner,
    _drop_completed_duplicates,
    _scope_violation,
)
from app.agents.schemas import AgentPlan, PlanStatus, PlanStep, StepStatus
from app.core.base_tool import PermissionLevel, ToolResult
from app.providers.base import (
    EmbeddingResponse,
    LLMMessage,
    LLMProvider,
    LLMResponse,
)

GOAL = "hey list all file/folders in desktop and delete all files in phase3test"


class RecordingProvider(LLMProvider):
    """Scripted responses; records the FULL message list of every call so
    tests can assert on structural retry feedback."""

    def __init__(self, responses: List[str]) -> None:
        self._responses = list(responses)
        self.calls = 0
        self.message_log: List[List[str]] = []

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model_name(self) -> str:
        return "fake-model"

    async def chat(
        self,
        messages: List[LLMMessage],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        self.calls += 1
        self.message_log.append([m.content for m in messages])
        if not self._responses:
            raise AssertionError(f"RecordingProvider exhausted after {self.calls - 1} responses")
        return LLMResponse(
            content=self._responses.pop(0), model="fake-model", provider="fake",
        )

    async def stream_chat(
        self,
        messages: List[LLMMessage],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[str]:
        yield ""

    async def embed(self, text: str) -> EmbeddingResponse:
        return EmbeddingResponse(embedding=[0.0] * 384, model="fake", provider="fake")


def step(description: str, tool: str, **parameters) -> dict:
    return {"description": description, "tool": tool, "parameters": parameters}


def plan_json(steps: list, reason: Optional[str] = None) -> str:
    return json.dumps(
        {"steps": steps, "unachievable_reason": reason, "question": None}
    )


def make_step(
    tool: str,
    parameters: dict,
    status: StepStatus = StepStatus.PENDING,
    output: Optional[dict] = None,
    level: PermissionLevel = PermissionLevel.READ,
) -> PlanStep:
    s = PlanStep(
        description=f"{tool} step",
        tool=tool,
        parameters=parameters,
        permission_level=level,
        requires_approval=level != PermissionLevel.READ,
        status=status,
    )
    if output is not None:
        s.result = ToolResult(success=True, output=output, error=None, permission_level=level)
    return s


def search_result(*matches: tuple[str, str]) -> dict:
    return {
        "matches": [{"path": p, "type": t} for p, t in matches],
        "count": len(matches),
        "truncated": False,
    }


# ------------------------------------------------------------ paths_from_step

def test_paths_from_step_reads_search_list_and_create_outputs():
    search = make_step(
        "search_files", {}, StepStatus.COMPLETED,
        search_result((r"C:\d\a.txt", "file"), (r"C:\d\sub", "folder")),
    )
    listing = make_step(
        "list_directory", {}, StepStatus.COMPLETED,
        {"path": r"C:\d", "entries": [
            {"name": "b.txt", "type": "file"},
            {"name": "kid", "type": "directory"},
        ]},
    )
    created = make_step(
        "create_file", {}, StepStatus.COMPLETED, {"created": r"C:\d\new.txt"},
        level=PermissionLevel.WRITE,
    )
    assert paths_from_step(search) == ([r"C:\d\a.txt"], [r"C:\d\sub"])
    files, folders = paths_from_step(listing)
    assert files == [str(__import__("pathlib").PurePath(r"C:\d") / "b.txt")]
    assert folders == [str(__import__("pathlib").PurePath(r"C:\d") / "kid")]
    assert paths_from_step(created) == ([r"C:\d\new.txt"], [])


def test_paths_from_step_ignores_pending_and_failed_steps():
    pending = make_step("search_files", {}, StepStatus.PENDING)
    assert paths_from_step(pending) == ([], [])


# ------------------------------------------------------------- file expansion

def plan_with(goal: str, *steps: PlanStep) -> AgentPlan:
    return AgentPlan(goal=goal, steps=list(steps))


def test_template_expands_into_one_concrete_step_per_found_file():
    search = make_step(
        "search_files", {"directory": r"C:\d"}, StepStatus.COMPLETED,
        search_result((r"C:\d\a.txt", "file"), (r"C:\d\b.md", "file")),
    )
    template = make_step(
        "delete_file", {"path": "PENDING: the found file paths"},
        level=PermissionLevel.DESTRUCTIVE,
    )
    plan = plan_with("delete all files in d", search, template)

    out = resolve(plan, 1, max_new=28)

    assert [s.parameters["path"] for s in out] == [r"C:\d\a.txt", r"C:\d\b.md"]
    assert all(s.tool == "delete_file" for s in out)
    assert all(s.requires_approval for s in out)
    # Fresh signatures: an approval of the placeholder form covers nothing
    assert out[0].signature() != template.signature()
    assert "a.txt" in out[0].description


def test_extension_tokens_in_the_placeholder_filter_the_pool():
    search = make_step(
        "search_files", {}, StepStatus.COMPLETED,
        search_result((r"C:\d\a.txt", "file"), (r"C:\d\b.md", "file")),
    )
    template = make_step("read_file", {"path": "PENDING: the .md files found"})
    plan = plan_with("read the md files in d", search, template)

    out = resolve(plan, 1, max_new=28)
    assert [s.parameters["path"] for s in out] == [r"C:\d\b.md"]


def test_universal_goal_ignores_an_extension_the_user_never_said():
    """THE incident's second half: the delete template said '.txt file
    paths' although the goal asked for ALL files — the invented filter must
    not narrow the expansion."""
    search = make_step(
        "search_files", {}, StepStatus.COMPLETED,
        search_result((r"C:\d\a.txt", "file"), (r"C:\d\b.md", "file")),
    )
    template = make_step(
        "delete_file", {"path": "PENDING: .txt file paths"},
        level=PermissionLevel.DESTRUCTIVE,
    )
    plan = plan_with(GOAL, search, template)

    out = resolve(plan, 1, max_new=28)
    assert [s.parameters["path"] for s in out] == [r"C:\d\a.txt", r"C:\d\b.md"]


def test_zero_matches_expand_to_zero_steps():
    empty_search = make_step(
        "search_files", {}, StepStatus.COMPLETED, search_result(),
    )
    template = make_step(
        "delete_file", {"path": "PENDING: found paths"},
        level=PermissionLevel.DESTRUCTIVE,
    )
    plan = plan_with("delete all tmp files", empty_search, template)
    assert resolve(plan, 1, max_new=28) == []


def test_unresolvable_cases_fall_back_to_the_llm():
    template = make_step(
        "delete_file", {"path": "PENDING: found paths"},
        level=PermissionLevel.DESTRUCTIVE,
    )
    # No completed step at all → None (not zero-expansion: nothing searched)
    plan = plan_with("delete stuff", template)
    assert resolve(plan, 0, max_new=28) is None

    # Two placeholder parameters → too ambiguous
    two = make_step(
        "move_file",
        {"source": "PENDING: source", "destination": "PENDING: destination"},
        level=PermissionLevel.WRITE,
    )
    search = make_step(
        "search_files", {}, StepStatus.COMPLETED,
        search_result((r"C:\d\a.txt", "file")),
    )
    plan = plan_with("move things", search, two)
    assert resolve(plan, 1, max_new=28) is None

    # More files than the plan-size cap allows → None
    big = make_step(
        "search_files", {}, StepStatus.COMPLETED,
        search_result(*[(rf"C:\d\f{i}.txt", "file") for i in range(5)]),
    )
    template2 = make_step(
        "delete_file", {"path": "PENDING: found"}, level=PermissionLevel.DESTRUCTIVE,
    )
    plan = plan_with("delete all files in d", big, template2)
    assert resolve(plan, 1, max_new=3) is None


# --------------------------------------------------------- folder substitution

def test_folder_placeholder_resolves_when_the_name_pins_one_candidate():
    listing = make_step(
        "list_directory", {}, StepStatus.COMPLETED,
        {"path": r"C:\Users\x\Desktop", "entries": [
            {"name": "phase3test", "type": "directory"},
            {"name": "ocr", "type": "directory"},
            {"name": "a.txt", "type": "file"},
        ]},
    )
    template = make_step(
        "search_files", {"directory": "PENDING: path of the phase3test folder"},
    )
    plan = plan_with(GOAL, listing, template)

    out = resolve(plan, 1, max_new=28)
    assert len(out) == 1
    assert out[0].parameters["directory"].endswith("phase3test")
    assert out[0].description == template.description  # substitution, not expansion


def test_folder_placeholder_stays_unresolved_between_several_candidates():
    listing = make_step(
        "list_directory", {}, StepStatus.COMPLETED,
        {"path": r"C:\x", "entries": [
            {"name": "one", "type": "directory"},
            {"name": "two", "type": "directory"},
        ]},
    )
    template = make_step("list_directory", {"path": "PENDING: the folder"})
    plan = plan_with("list the folder", listing, template)
    assert resolve(plan, 1, max_new=28) is None  # code never picks


# ------------------------------------------------------------ scope violation

def scope_steps(**parameters) -> list[PlanStep]:
    return [make_step("search_files", parameters)]


def test_scope_guard_rejects_an_unmentioned_extension_on_an_all_files_goal():
    feedback = _scope_violation(
        scope_steps(query=".txt", directory=r"C:\d"), GOAL, "",
    )
    assert feedback is not None
    assert ".txt" in feedback
    assert "ALL files" in feedback


def test_scope_guard_checks_file_type_and_placeholder_extensions_too():
    assert _scope_violation(
        scope_steps(file_type="txt", directory=r"C:\d"), GOAL, "",
    ) is not None
    delete = [make_step(
        "delete_file", {"path": "PENDING: .txt file paths"},
        level=PermissionLevel.DESTRUCTIVE,
    )]
    assert _scope_violation(delete, GOAL, "") is not None


def test_scope_guard_allows_extensions_the_user_actually_said():
    goal = "delete all the .txt files in phase3test"
    assert _scope_violation(scope_steps(file_type="txt"), goal, "") is None


def test_scope_guard_inactive_without_a_universal_goal():
    # "the temp files" is the model's judgment call, not a narrowing of ALL
    assert _scope_violation(
        scope_steps(file_type="tmp"), "delete the temp files in x", "",
    ) is None


def test_scope_guard_accepts_extensions_grounded_in_conversation():
    feedback = _scope_violation(
        scope_steps(file_type="txt"), GOAL, "user: I mean the txt ones",
    )
    assert feedback is None


# ------------------------------------------------------- completed duplicates

def test_leading_completed_duplicate_is_dropped():
    done = make_step(
        "search_files", {"query": "phase3test"}, StepStatus.COMPLETED,
        search_result((r"C:\d\phase3test", "folder")),
    )
    dup = make_step("search_files", {"query": "phase3test"})
    fresh = make_step("list_directory", {"path": r"C:\d\phase3test"})

    kept, reject = _drop_completed_duplicates([dup, fresh], {done.signature()})
    assert reject is None
    assert [s.tool for s in kept] == ["list_directory"]


def test_all_duplicate_revision_is_rejected_not_completed():
    done = make_step("search_files", {"query": "x"}, StepStatus.COMPLETED, search_result())
    dup = make_step("search_files", {"query": "x"})
    kept, reject = _drop_completed_duplicates([dup], {done.signature()})
    assert kept == [dup]  # unchanged — the reject drives a retry instead
    assert "ALREADY run successfully" in reject


def test_duplicate_after_a_state_changing_step_is_legitimate():
    done = make_step("search_files", {"query": "x"}, StepStatus.COMPLETED, search_result())
    create = make_step(
        "create_file", {"path": r"C:\d\x.txt", "content": ""},
        level=PermissionLevel.WRITE,
    )
    dup = make_step("search_files", {"query": "x"})
    kept, reject = _drop_completed_duplicates([create, dup], {done.signature()})
    assert reject is None
    assert len(kept) == 2  # the world may have changed — the repeat stands


# ------------------------------------------------- the live incident, end to end

async def test_the_screenshot_incident_end_to_end(db_session, tmp_path):
    """Full replay of the 2026-07-10 failure with all three guards on:
    the memory-narrowed '.txt' draft is rejected in code; the faithful retry
    plans placeholders; BOTH placeholders resolve deterministically (folder
    substitution, then per-file expansion); the approval pause shows exact
    real paths and not one LLM call is spent past draft+retry+reflect."""
    desktop = tmp_path / "Desktop"
    folder = desktop / "phase3test"
    folder.mkdir(parents=True)
    (folder / "created.txt").write_text("")
    (folder / "notes.md").write_text("x")

    narrowed = [
        step("List all files and folders in the Desktop", "list_directory",
             path=str(desktop)),
        step("Search for the phase3test folder", "search_files",
             query="phase3test", directory=str(desktop), include_folders=True),
        step("Search for all files with '.txt' extension", "search_files",
             query=".txt", directory="PENDING: phase3test path"),
        step("Delete each .txt file found", "delete_file",
             path="PENDING: .txt file paths"),
    ]
    faithful = [
        step("List all files and folders in the Desktop", "list_directory",
             path=str(desktop)),
        step("Search for the phase3test folder", "search_files",
             query="phase3test", directory=str(desktop), include_folders=True),
        step("Search for all files in the phase3test folder", "search_files",
             directory="PENDING: path of the phase3test folder"),
        step("Delete each file found in the phase3test folder", "delete_file",
             path="PENDING: file paths from the search"),
    ]
    provider = RecordingProvider([
        plan_json(narrowed),  # draft — narrows to .txt → rejected in code
        plan_json(faithful),  # retry after the scope feedback
        plan_json(faithful),  # reflect
    ])
    planner = AgentPlanner(
        db_session, provider, session_id="s-incident",
        memory="[2026-07-09] Planning to delete all files with '.txt' extension",
    )

    plan = await planner.start(GOAL)

    # Scope guard: the retry message carried the structural feedback
    assert provider.calls == 3
    retry_feedback = provider.message_log[1][-1]
    assert "ALL files" in retry_feedback and ".txt" in retry_feedback
    # Both placeholders resolved in code: the pause shows CONCRETE deletes
    assert plan.status == PlanStatus.AWAITING_APPROVAL
    pending = plan.pending_steps()
    assert sorted(s.parameters["path"] for s in pending) == [
        str(folder / "created.txt"), str(folder / "notes.md"),
    ]
    assert not any("PENDING" in json.dumps(s.parameters) for s in pending)
    # The searches ran exactly once each — no duplicated work
    searches = [s for s in plan.steps if s.tool == "search_files"]
    assert all(s.status == StepStatus.COMPLETED for s in searches)
    assert len(searches) == 2

    plan = await planner.resume(plan, approved=True)
    assert plan.status == PlanStatus.COMPLETED
    assert provider.calls == 3  # the whole rest of the plan needed no LLM
    assert not (folder / "created.txt").exists()
    assert not (folder / "notes.md").exists()


async def test_replan_duplicating_a_completed_search_is_deduped(db_session, tmp_path):
    """The visible half of the incident's waste: after a real failure, the
    revision re-issued the exact search that had already succeeded. The
    duplicate is dropped in code and only the new work runs."""
    folder = tmp_path / "phase3test"
    folder.mkdir()
    ghost = folder / "ghost.txt"
    ghost.mkdir()  # read_file on a directory fails deterministically

    search = step("Find the folder", "search_files", query="phase3test",
                  directory=str(tmp_path), include_folders=True)
    draft = [search, step("Read ghost.txt", "read_file", path=str(ghost))]
    revision = [search, step("List it instead", "list_directory", path=str(ghost))]

    provider = RecordingProvider([
        plan_json(draft), plan_json(draft),  # plan, reflect
        plan_json(revision),                  # replan repeats the done search
    ])
    plan = await AgentPlanner(db_session, provider).start(
        "read ghost.txt in phase3test"
    )

    assert plan.status == PlanStatus.COMPLETED
    ran_searches = [
        s for s in plan.steps
        if s.tool == "search_files" and s.status == StepStatus.COMPLETED
    ]
    assert len(ran_searches) == 1  # the duplicate never ran again


async def test_approval_pause_never_shows_a_placeholder(db_session, tmp_path):
    """Structural: a write/destructive step that still carries PENDING can
    no longer be what the user is asked to approve — it either expanded to
    concrete steps first or failed into the replan loop."""
    victim = tmp_path / "a.tmp"
    victim.write_text("x")
    draft = [
        step("Find the tmp files", "search_files",
             directory=str(tmp_path), file_type="tmp"),
        step("Delete each", "delete_file", path="PENDING: found paths"),
    ]
    provider = RecordingProvider([plan_json(draft), plan_json(draft)])

    plan = await AgentPlanner(db_session, provider).start("delete my tmp files")

    assert plan.status == PlanStatus.AWAITING_APPROVAL
    for s in plan.pending_steps():
        assert "PENDING" not in json.dumps(s.parameters)
        assert "PENDING" not in (s.action_detail or "")
