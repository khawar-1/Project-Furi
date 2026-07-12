"""
Phase 6 Part 6 — File Intelligence: frequent-folder aggregation + the planner
save-location suggestion surfacing.

The aggregation reads the ActivityLog audit trail directly (seeded here); the
surfacing test drives a real AgentPlanner with a scripted FakeProvider and
asserts the learned folder rides into the plan prompt under rule 18.
"""
import json
from datetime import datetime
from typing import AsyncIterator, List, Optional

import app.tools  # noqa: F401 — registers the real file/terminal tools
from app.agents.planner import AgentPlanner
from app.agents.schemas import PlanStatus
from app.core.file_intelligence import (
    FolderUsage,
    _TRASH_DIR,
    format_frequent_folders,
    frequent_folders,
)
from app.db.models import ActivityLog
from app.providers.base import EmbeddingResponse, LLMMessage, LLMProvider, LLMResponse

_BASE = datetime(2026, 7, 12, 9, 0, 0)


def _row(
    tool: str,
    *,
    params: Optional[dict] = None,
    result: Optional[dict] = None,
    success: bool = True,
    created_at: Optional[datetime] = None,
) -> ActivityLog:
    return ActivityLog(
        tool_name=tool,
        action=f"{tool}(...)",
        parameters=json.dumps(params or {}),
        result_summary=json.dumps(result) if result is not None else "ok",
        success=success,
        permission_level="write",
        created_at=created_at or _BASE,
    )


async def _seed(db, rows: list[ActivityLog]) -> None:
    db.add_all(rows)
    await db.commit()


def _create(path: str, created_at: Optional[datetime] = None) -> ActivityLog:
    return _row(
        "create_file",
        params={"path": path, "content": "x"},
        result={"created": path, "size_bytes": 1},
        created_at=created_at,
    )


def _move(dst_file: str, created_at: Optional[datetime] = None) -> ActivityLog:
    return _row(
        "move_file",
        params={"source": "/somewhere/a.txt", "destination": dst_file},
        result={"moved_from": "/somewhere/a.txt", "moved_to": dst_file},
        created_at=created_at,
    )


# ================================================================= aggregation

async def test_empty_when_no_history(db_session):
    assert await frequent_folders(db_session) == []


async def test_ranks_by_frequency(db_session):
    await _seed(db_session, [
        _create("C:/Users/x/Notes/a.txt"),
        _create("C:/Users/x/Notes/b.txt"),
        _create("C:/Users/x/Notes/c.txt"),
        _create("C:/Users/x/Reports/r.txt"),
    ])
    ranked = await frequent_folders(db_session)
    assert ranked[0].folder.endswith("Notes")
    assert ranked[0].count == 3
    assert ranked[1].folder.endswith("Reports")
    assert ranked[1].count == 1


async def test_recency_breaks_ties(db_session):
    await _seed(db_session, [
        _create("C:/Users/x/Older/a.txt", created_at=datetime(2026, 7, 1, 9, 0)),
        _create("C:/Users/x/Older/b.txt", created_at=datetime(2026, 7, 1, 10, 0)),
        _create("C:/Users/x/Newer/a.txt", created_at=datetime(2026, 7, 10, 9, 0)),
        _create("C:/Users/x/Newer/b.txt", created_at=datetime(2026, 7, 10, 10, 0)),
    ])
    ranked = await frequent_folders(db_session)
    assert ranked[0].count == 2 and ranked[1].count == 2
    assert ranked[0].folder.endswith("Newer")  # more recent tie-winner first


async def test_move_uses_result_destination_folder(db_session):
    # destination param is a FOLDER the file lands inside; the true folder is
    # the parent of the real moved_to path, not the parent of the folder.
    await _seed(db_session, [_move("C:/Users/x/Archive/report.pdf")])
    ranked = await frequent_folders(db_session)
    assert len(ranked) == 1
    assert ranked[0].folder.replace("\\", "/").endswith("Users/x/Archive")


async def test_ignores_failed_and_read_rows(db_session):
    await _seed(db_session, [
        _row("move_file", params={"destination": "C:/nope"},
             result=None, success=False),
        _row("search_files", params={"query": "x"}, result={"count": 0}),
        _row("list_directory", params={"path": "C:/Users/x/Notes"},
             result={"count": 3}),
        _create("C:/Users/x/Good/a.txt"),
    ])
    ranked = await frequent_folders(db_session)
    assert len(ranked) == 1
    assert ranked[0].folder.endswith("Good")


async def test_excludes_trash(db_session):
    trash_file = str(_TRASH_DIR / "20260712_note.txt")
    await _seed(db_session, [_move(trash_file), _create("C:/Users/x/Keep/a.txt")])
    ranked = await frequent_folders(db_session)
    assert len(ranked) == 1
    assert ranked[0].folder.endswith("Keep")
    assert all(str(_TRASH_DIR) not in u.folder for u in ranked)


async def test_normalizes_equivalent_paths(db_session):
    await _seed(db_session, [
        _create("C:/Users/x/Docs/a.txt"),
        _create("C:/Users/x/./Docs/b.txt"),  # same folder, non-normal form
    ])
    ranked = await frequent_folders(db_session)
    assert len(ranked) == 1
    assert ranked[0].count == 2


async def test_limit_is_respected(db_session):
    await _seed(db_session, [
        _create(f"C:/Users/x/F{i}/a.txt") for i in range(8)
    ])
    ranked = await frequent_folders(db_session, limit=3)
    assert len(ranked) == 3


async def test_existing_only_filters_absent_folders(db_session, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    await _seed(db_session, [
        _create(str(real / "note.txt")),
        _create("C:/definitely/not/here/ghost.txt"),
    ])
    everything = await frequent_folders(db_session, existing_only=False)
    only_live = await frequent_folders(db_session, existing_only=True)
    assert len(everything) == 2
    assert len(only_live) == 1
    assert only_live[0].folder == str(real)


# ==================================================================== format

def test_format_lists_folders_and_counts():
    text = format_frequent_folders([
        FolderUsage(folder="C:/Users/x/Notes", count=3, last_used=_BASE),
        FolderUsage(folder="C:/Users/x/Reports", count=1, last_used=_BASE),
    ])
    assert "C:/Users/x/Notes" in text
    assert "used 3 times" in text
    assert "used 1 time" in text  # singular


def test_format_empty_is_blank():
    assert format_frequent_folders([]) == ""


# ============================================= planner surfacing (rule 18)

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


async def test_frequent_folder_surfaces_in_planner_prompt(db_session, tmp_path):
    """The learned folder rides into the plan prompt as DATA (rule 18) — the
    planner can suggest it, but a create/move suggestion is still a WRITE step
    the user approves (the gate is untouched by this signal)."""
    saves = tmp_path / "Saves"
    saves.mkdir()
    await _seed(db_session, [
        _create(str(saves / "a.txt")),
        _create(str(saves / "b.txt")),
        _create(str(saves / "c.txt")),
    ])

    steps = [{"description": "List the folder", "tool": "list_directory",
              "parameters": {"path": str(tmp_path)}}]
    provider = _FakeProvider([_plan_json(steps), _plan_json(steps)])

    planner = AgentPlanner(db_session, provider, session_id="s-fi")
    plan = await planner.start("show me what is in the folder")

    assert plan.status == PlanStatus.COMPLETED
    assert "FREQUENTLY USED FOLDERS (background DATA only" in provider.prompts[0]
    assert str(saves) in provider.prompts[0]


async def test_no_folder_block_when_no_history(db_session, tmp_path):
    steps = [{"description": "List the folder", "tool": "list_directory",
              "parameters": {"path": str(tmp_path)}}]
    provider = _FakeProvider([_plan_json(steps), _plan_json(steps)])
    planner = AgentPlanner(db_session, provider, session_id="s-fi2")
    await planner.start("list the folder")
    assert "FREQUENTLY USED FOLDERS (background DATA only" not in provider.prompts[0]
