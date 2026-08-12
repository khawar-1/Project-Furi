"""
recall_actions (app/tools/activity_tools.py) — Furi's own action history.

The tool exists because "the folder YOU created today" is a question about
Furi's audit trail, not the filesystem (live bug 2026-07-13: the planner
answered with every file any program created that day). Uses the memory_tools
test pattern: a file-backed DB the tool's own sessions point at
(SESSION_FACTORY) — no test touches the real jarvis.db.
"""
import json
from datetime import datetime, timedelta

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.tools  # noqa: F401 — registers every tool
from app.agents.rendering import _RESULT_FORMATTERS, _fmt_recall_actions
from app.core.base_tool import PermissionLevel
from app.db.database import Base
from app.db.models import ActivityLog, utc_now
from app.tools import activity_tools
from app.tools.registry import registry


# ================================================================= fixtures

@pytest_asyncio.fixture
async def act_db(tmp_path_factory, monkeypatch):
    db_dir = tmp_path_factory.mktemp("activity-db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_dir / 'act.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(activity_tools, "SESSION_FACTORY", factory)
    async with factory() as session:
        yield session
    await engine.dispose()


def _row(tool, level, *, params=None, success=True, created_at=None,
         result="ok", action=None):
    return ActivityLog(
        session_id="s1",
        tool_name=tool,
        action=action or f"{tool}(…)",
        parameters=json.dumps(params or {}),
        result_summary=result,
        success=success,
        permission_level=level,
        created_at=created_at or utc_now(),
    )


async def _seed(db) -> None:
    now = utc_now()
    db.add_all([
        _row("create_folder", "write",
             params={"path": r"C:\Users\me\Desktop\jarvis_test"},
             created_at=now - timedelta(minutes=30)),
        _row("create_file", "write",
             params={"path": r"C:\Users\me\Desktop\jarvis_test\notes.txt"},
             created_at=now - timedelta(minutes=20)),
        _row("delete_file", "destructive",
             params={"path": r"C:\Users\me\Desktop\jarvis_test\notes.txt"},
             created_at=now - timedelta(minutes=10)),
        # READ row — excluded by default ("what did you do" means actions).
        _row("search_files", "read",
             params={"directory": r"C:\Users\me\Desktop"},
             created_at=now - timedelta(minutes=5)),
        # Failed write — excluded by the success_only default.
        _row("move_file", "write", success=False,
             params={"source": r"C:\a.txt", "destination": r"C:\b"},
             result="source not found",
             created_at=now - timedelta(minutes=15)),
        # An action from two days ago — the date-bound tests split on it.
        _row("create_file", "write",
             params={"path": r"C:\Users\me\Desktop\old.txt"},
             created_at=now - timedelta(days=2)),
    ])
    await db.commit()


async def _run(**kwargs):
    return await registry.get("recall_actions").execute(**kwargs)


# ================================================================== filters

async def test_defaults_actions_only_success_only_newest_first(act_db):
    await _seed(act_db)
    result = await _run()
    assert result.success is True
    tools = [a["tool"] for a in result.output["actions"]]
    # No search_files (read), no failed move_file; newest first.
    assert tools == ["delete_file", "create_file", "create_folder", "create_file"]
    assert result.output["count"] == 4


async def test_after_bound_keeps_only_recent_actions(act_db):
    await _seed(act_db)
    today = datetime.now().date().isoformat()
    result = await _run(after=today)
    tools = [a["tool"] for a in result.output["actions"]]
    assert tools == ["delete_file", "create_file", "create_folder"]


async def test_before_bound_keeps_only_older_actions(act_db):
    await _seed(act_db)
    two_days_ago = (datetime.now() - timedelta(days=2)).date().isoformat()
    result = await _run(before=two_days_ago)
    assert [a["tool"] for a in result.output["actions"]] == ["create_file"]
    assert "old.txt" in result.output["actions"][0]["parameters"]["path"]


async def test_tool_name_filter(act_db):
    await _seed(act_db)
    result = await _run(tool_name="create_file")
    assert [a["tool"] for a in result.output["actions"]] == ["create_file", "create_file"]


async def test_include_reads_surfaces_lookups(act_db):
    await _seed(act_db)
    result = await _run(include_reads=True)
    assert "search_files" in [a["tool"] for a in result.output["actions"]]


async def test_success_only_false_shows_failed_attempts(act_db):
    await _seed(act_db)
    result = await _run(success_only=False)
    failed = [a for a in result.output["actions"] if a["success"] is False]
    assert [a["tool"] for a in failed] == ["move_file"]
    assert failed[0]["result"] == "source not found"


async def test_limit_caps_and_keeps_newest(act_db):
    await _seed(act_db)
    result = await _run(limit=2)
    assert [a["tool"] for a in result.output["actions"]] == ["delete_file", "create_file"]


async def test_non_iso_date_is_refused(act_db):
    result = await _run(after="03/04/2026")
    assert result.success is False
    assert "ISO" in result.error


async def test_empty_log_returns_zero_count(act_db):
    result = await _run()
    assert result.success is True
    assert result.output == {"actions": [], "count": 0}


# ======================================================== registration/level

def test_registered_as_read_level():
    tool = registry.get("recall_actions")
    assert tool is not None
    assert tool.permission_level == PermissionLevel.READ


# ================================================================ formatter

def test_fmt_recall_actions_registered():
    assert _RESULT_FORMATTERS["recall_actions"] is _fmt_recall_actions


def test_fmt_renders_readable_action_lines():
    out = {
        "count": 3,
        "actions": [
            {"time": "2026-07-13T13:40:00+00:00", "tool": "create_folder",
             "action": "create_folder(path=...)", "success": True,
             "parameters": {"path": r"C:\Users\me\Desktop\jarvis_test"},
             "result": "ok"},
            {"time": "2026-07-13T13:45:00+00:00", "tool": "move_file",
             "action": "move_file(...)", "success": True,
             "parameters": {"source": r"C:\a.txt", "destination": r"C:\dest"},
             # The RESULT's real final path beats the requested folder param.
             "result": json.dumps({"moved_to": r"C:\dest\a.txt"})},
            {"time": "2026-07-13T13:50:00+00:00", "tool": "send_email",
             "action": "send_email(...)", "success": True,
             "parameters": {"to": "x@y.com", "subject": "Hello"},
             "result": "ok"},
        ],
    }
    text = _fmt_recall_actions(out)
    assert "3 recorded action(s)" in text
    assert r"created folder `C:\Users\me\Desktop\jarvis_test`" in text
    assert r"moved `C:\a.txt` → `C:\dest\a.txt`" in text
    assert "sent an email to `x@y.com` — Hello" in text


def test_fmt_marks_failures_and_handles_unknown_tools():
    out = {
        "count": 1,
        "actions": [
            {"time": "", "tool": "custom_tool", "action": "custom_tool(x=1)",
             "success": False, "parameters": {}, "result": "boom"},
        ],
    }
    text = _fmt_recall_actions(out)
    assert "custom_tool(x=1)" in text  # falls back to the audit action string
    assert "(FAILED)" in text


def test_fmt_empty():
    assert "No recorded actions" in _fmt_recall_actions({"actions": [], "count": 0})
