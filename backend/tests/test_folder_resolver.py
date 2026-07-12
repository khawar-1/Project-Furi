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


def test_none_when_answer_carries_a_path(tmp_path, monkeypatch):
    """A picked option / typed path is a drive qualifier → the re-plan loop
    terminates (the guard stands down once the user has chosen)."""
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home / "downloads", d / "downloads")
    _wire(monkeypatch, home, [d])
    step = _read_step("search_files", directory="downloads")
    answer = str(d / "downloads")
    assert detect(step, "find pdfs in downloads", [answer]) is None


def test_none_when_not_well_known(tmp_path, monkeypatch):
    home, d = tmp_path / "home", tmp_path / "d"
    _mkdir(home / "projects", d / "projects")
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
