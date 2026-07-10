"""
Part 1 — Tool registry + activity logging.
Covers: registration, the structural approval gate (WRITE/DESTRUCTIVE never
run unapproved), exception safety via safe_execute, and the ActivityLog
audit row written for every attempt — executed and blocked alike.
"""
import json
from typing import Any

import pytest
from sqlalchemy import select

from app.core.base_tool import BaseTool, PermissionLevel, ToolDefinition, ToolResult
from app.db.models import ActivityLog
from app.tools.registry import ToolRegistry, execute_tool, register_tool, registry


class EchoTool(BaseTool):
    """READ tool: returns its input."""

    @property
    def name(self) -> str:
        return "echo_test"

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.READ

    async def execute(self, **kwargs: Any) -> ToolResult:
        return ToolResult(success=True, output=f"echo: {kwargs.get('text', '')}")

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="Echoes text back",
            parameters={"text": {"type": "string"}},
            permission_level=self.permission_level,
        )


class MarkerWriteTool(BaseTool):
    """WRITE tool: flips a flag so tests can prove execute() ran (or didn't)."""

    def __init__(self) -> None:
        self.executed = False

    @property
    def name(self) -> str:
        return "marker_write_test"

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.WRITE

    async def execute(self, **kwargs: Any) -> ToolResult:
        self.executed = True
        return ToolResult(success=True, output="wrote something")

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="Marks execution",
            parameters={},
            permission_level=self.permission_level,
        )


class MarkerDestructiveTool(MarkerWriteTool):
    """DESTRUCTIVE variant of the marker tool."""

    @property
    def name(self) -> str:
        return "marker_destructive_test"

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.DESTRUCTIVE


class BoomTool(BaseTool):
    """READ tool whose execute() raises — must be caught by safe_execute."""

    @property
    def name(self) -> str:
        return "boom_test"

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.READ

    async def execute(self, **kwargs: Any) -> ToolResult:
        raise RuntimeError("kaboom")

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="Always raises",
            parameters={},
            permission_level=self.permission_level,
        )


@pytest.fixture
def reg() -> ToolRegistry:
    """Fresh, isolated registry per test — the global one stays untouched."""
    return ToolRegistry()


async def _activity_rows(db_session) -> list[ActivityLog]:
    result = await db_session.execute(
        select(ActivityLog).order_by(ActivityLog.created_at.asc())
    )
    return list(result.scalars().all())


# ------------------------------------------------------------------ registry

async def test_register_and_execute_read_tool(db_session, reg):
    reg.register(EchoTool())
    result = await execute_tool(
        "echo_test", {"text": "hello"}, db_session,
        session_id="sess-1", tool_registry=reg,
    )
    assert result.success is True
    assert result.output == "echo: hello"


async def test_duplicate_registration_raises(reg):
    reg.register(EchoTool())
    with pytest.raises(ValueError, match="already registered"):
        reg.register(EchoTool())


async def test_unknown_tool_returns_failed_result(db_session, reg):
    result = await execute_tool("nope", {}, db_session, tool_registry=reg)
    assert result.success is False
    assert "Unknown tool" in (result.error or "")


async def test_definitions_lists_registered_tools(reg):
    reg.register(EchoTool())
    reg.register(MarkerWriteTool())
    defs = reg.definitions()
    assert [d.name for d in defs] == ["echo_test", "marker_write_test"]
    assert defs[1].permission_level == PermissionLevel.WRITE


async def test_register_tool_decorator_uses_global_registry():
    @register_tool
    class DecoratedEcho(EchoTool):
        @property
        def name(self) -> str:
            return "decorated_echo_test"

    try:
        assert registry.get("decorated_echo_test") is not None
    finally:
        registry.unregister("decorated_echo_test")


# ------------------------------------------------------------- approval gate

async def test_write_tool_blocked_without_approval(db_session, reg):
    tool = MarkerWriteTool()
    reg.register(tool)
    result = await execute_tool("marker_write_test", {}, db_session, tool_registry=reg)

    assert result.success is False
    assert result.requires_approval is True
    assert tool.executed is False  # execute() was never reached
    assert "approval" in (result.approval_prompt or "").lower()


async def test_write_tool_runs_with_approval(db_session, reg):
    tool = MarkerWriteTool()
    reg.register(tool)
    result = await execute_tool(
        "marker_write_test", {}, db_session, approved=True, tool_registry=reg,
    )
    assert result.success is True
    assert tool.executed is True


async def test_destructive_tool_blocked_without_approval(db_session, reg):
    tool = MarkerDestructiveTool()
    reg.register(tool)
    result = await execute_tool("marker_destructive_test", {}, db_session, tool_registry=reg)

    assert result.success is False
    assert result.requires_approval is True
    assert result.permission_level == PermissionLevel.DESTRUCTIVE
    assert tool.executed is False


async def test_read_tool_needs_no_approval(db_session, reg):
    reg.register(EchoTool())
    result = await execute_tool("echo_test", {"text": "x"}, db_session, tool_registry=reg)
    assert result.success is True
    assert result.requires_approval is False


# ------------------------------------------------------------ error safety

async def test_raising_tool_returns_failed_result(db_session, reg):
    reg.register(BoomTool())
    result = await execute_tool("boom_test", {}, db_session, tool_registry=reg)
    assert result.success is False
    assert "kaboom" in (result.error or "")


# ------------------------------------------------------------- activity log

async def test_successful_execution_writes_activity_log(db_session, reg):
    reg.register(EchoTool())
    await execute_tool(
        "echo_test", {"text": "hello"}, db_session,
        session_id="sess-log", tool_registry=reg,
    )

    rows = await _activity_rows(db_session)
    assert len(rows) == 1
    row = rows[0]
    assert row.tool_name == "echo_test"
    assert row.session_id == "sess-log"
    assert row.success is True
    assert row.permission_level == "read"
    assert row.duration_ms is not None
    assert "echo_test" in row.action and "hello" in row.action
    assert json.loads(row.parameters) == {"text": "hello"}
    assert "echo: hello" in row.result_summary


async def test_blocked_execution_is_also_audited(db_session, reg):
    reg.register(MarkerDestructiveTool())
    await execute_tool(
        "marker_destructive_test", {"target": "stuff"}, db_session,
        session_id="sess-blocked", tool_registry=reg,
    )

    rows = await _activity_rows(db_session)
    assert len(rows) == 1
    row = rows[0]
    assert row.success is False
    assert row.permission_level == "destructive"
    assert row.duration_ms is None  # never ran
    assert "not approved" in row.result_summary


async def test_failed_execution_logs_error_summary(db_session, reg):
    reg.register(BoomTool())
    await execute_tool("boom_test", {}, db_session, tool_registry=reg)

    rows = await _activity_rows(db_session)
    assert len(rows) == 1
    assert rows[0].success is False
    assert "kaboom" in rows[0].result_summary


async def test_long_param_values_are_capped_in_audit_row(db_session, reg):
    reg.register(EchoTool())
    await execute_tool(
        "echo_test", {"text": "x" * 5000}, db_session, tool_registry=reg,
    )

    rows = await _activity_rows(db_session)
    params = json.loads(rows[0].parameters)
    assert len(params["text"]) < 400
    assert "chars)" in params["text"]  # truncation marker
    assert len(rows[0].action) <= 256
