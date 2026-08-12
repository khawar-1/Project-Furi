"""
Furi OS — Tool Registry (Phase 3)
Global registry for the plugin tool architecture.

Tools self-register (register_tool decorator or registry.register); the rest
of the system only ever calls execute_tool(), which:
  1. Looks the tool up by name
  2. Structurally enforces the approval gate — WRITE/DESTRUCTIVE tools are
     NEVER executed without approved=True, regardless of what the caller
     (the planner LLM included) asked for. This is the code-level guarantee
     behind "DESTRUCTIVE always requires approval", not a prompt rule.
  3. Runs safe_execute() so raw exceptions never propagate
  4. Writes an ActivityLog row for every attempt — executed AND blocked —
     so the Timeline UI shows everything Furi did or tried to do

An audit-log failure never breaks the tool call itself: the result is still
returned and the failure goes to loguru.
"""
import json
import time
from datetime import timedelta
from typing import Any, Callable, Optional

from loguru import logger
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_tool import BaseTool, PermissionLevel, ToolDefinition, ToolResult
from app.db.models import ActivityLog, utc_now

# Column limits / display caps for the ActivityLog row
_ACTION_MAX_LEN = 256
_PARAM_VALUE_MAX_LEN = 300
_RESULT_SUMMARY_MAX_LEN = 1000

# ⚠️ RETENTION IS COUPLED TO A LEARNING FEATURE — do not shorten this casually.
# `activity_log` was unbounded until 2026-08-03: nothing ever aged a row out,
# so a long-lived install grew it forever. But it is not just a UI feed —
# `app/core/file_intelligence.frequent_folders` ranks the user's save/move
# habits by counting successful file-tool rows over ALL time, with no date
# filter. Purging aggressively would silently shrink a signal the planner uses
# (rule 18), which is exactly the kind of action-at-a-distance this codebase
# keeps having to unpick.
#
# A year bounds the table — the actual defect — while leaving the folder-habit
# ranking materially intact. Deliberately far longer than the 30 days routing
# decisions and plan traces get: those record one turn's reasoning, this records
# what Furi DID, which is the audit trail the whole approval story rests on.
ACTIVITY_RETENTION_DAYS = 365


class ToolRegistry:
    """Name → tool instance map. One global instance; tests may make their own."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> BaseTool:
        """Register a tool instance. Duplicate names are a programming error."""
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered")
        self._tools[tool.name] = tool
        logger.info(f"Tool registered: {tool.name} [{tool.permission_level.value}]")
        return tool

    def unregister(self, name: str) -> None:
        """Remove a tool (used by tests)."""
        self._tools.pop(name, None)

    def get(self, name: str) -> Optional[BaseTool]:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools.keys())

    def definitions(self) -> list[ToolDefinition]:
        """Schemas of every registered tool, for the planner and /api/agent/tools."""
        return [self._tools[name].definition() for name in self.names()]


# The global registry. Tool modules register into this at import time.
registry = ToolRegistry()


def register_tool(cls: type[BaseTool]) -> type[BaseTool]:
    """
    Class decorator: instantiate the tool and add it to the global registry.

        @register_tool
        class ReadFileTool(BaseTool): ...
    """
    registry.register(cls())
    return cls


def mutates(tool: str) -> bool:
    """Does calling this tool CHANGE anything? Read from the REGISTRY, never a
    hand-kept name list — the registry already owns permission levels, and
    every copy of that fact drifts.

    Callers whose rules key on the answer: placeholder_resolver (a truncated
    source search is refused for a mutation, and its pool defaults to the
    searched folder's OWN files) and folder_resolver (an ambiguous folder gets
    the structural hand-off budget instead of the LLM clarification cap).

    ⚠️ This lives here, and not next to either caller, because of 2026-07-30:
    it WAS a literal set — `{"move_file", "delete_file", "rename_file"}` — that
    silently omitted the PLURAL tools, so both of the first caller's rules
    switched off for exactly the shape the same round's rule 4 had just started
    telling the planner to draft, and 85 PDFs moved with no partition. A second
    copy in a second module is the same bug waiting for its own incident.

    Unknown tool → treat as a mutation: the strict rules are the safe default.
    """
    spec = registry.get(tool)
    return spec is None or spec.permission_level != PermissionLevel.READ


def _sanitize_params(params: dict[str, Any]) -> dict[str, Any]:
    """Cap long values (e.g. file content) so the audit row stays readable."""
    out: dict[str, Any] = {}
    for key, value in params.items():
        text = value if isinstance(value, (int, float, bool, type(None))) else str(value)
        if isinstance(text, str) and len(text) > _PARAM_VALUE_MAX_LEN:
            text = text[:_PARAM_VALUE_MAX_LEN] + f"… (+{len(text) - _PARAM_VALUE_MAX_LEN} chars)"
        out[key] = text
    return out


def _render_action(name: str, params: dict[str, Any]) -> str:
    """Compact human-readable call rendering for the Timeline UI."""
    parts = ", ".join(f"{k}={v!r}" for k, v in _sanitize_params(params).items())
    action = f"{name}({parts})"
    if len(action) > _ACTION_MAX_LEN:
        action = action[: _ACTION_MAX_LEN - 1] + "…"
    return action


def _summarize_result(result: ToolResult) -> str:
    """One-line summary of what happened, for the audit row."""
    if not result.success:
        return (result.error or "failed")[:_RESULT_SUMMARY_MAX_LEN]
    if result.output is None:
        return "ok"
    text = result.output if isinstance(result.output, str) else json.dumps(result.output, default=str)
    if len(text) > _RESULT_SUMMARY_MAX_LEN:
        text = text[:_RESULT_SUMMARY_MAX_LEN] + "…"
    return text


async def _log_activity(
    db: AsyncSession,
    *,
    tool_name: str,
    action: str,
    params: dict[str, Any],
    result: ToolResult,
    session_id: Optional[str],
    duration_ms: Optional[int],
) -> None:
    """Write the audit row. Failures are logged, never raised — the tool
    result must reach the caller even if the audit write fails."""
    try:
        db.add(ActivityLog(
            session_id=session_id,
            tool_name=tool_name,
            action=action,
            parameters=json.dumps(_sanitize_params(params), default=str),
            result_summary=_summarize_result(result),
            success=result.success,
            permission_level=result.permission_level.value,
            duration_ms=duration_ms,
        ))
        await db.commit()
    except Exception as e:
        logger.warning(f"ActivityLog write failed for '{tool_name}' (non-critical): {e}")


async def execute_tool(
    name: str,
    params: dict[str, Any],
    db: AsyncSession,
    session_id: Optional[str] = None,
    approved: bool = False,
    tool_registry: Optional[ToolRegistry] = None,
) -> ToolResult:
    """
    The single entry point for running any tool.

    approved=True means the USER explicitly approved this action (the
    planner's approval gate, Part 4). Without it, WRITE and DESTRUCTIVE
    tools are refused here — structurally, before execute() is ever
    reached — and the blocked attempt is still written to the audit log.
    """
    reg = tool_registry if tool_registry is not None else registry
    tool = reg.get(name)
    if tool is None:
        logger.warning(f"execute_tool: unknown tool '{name}'")
        return ToolResult(
            success=False,
            output=None,
            error=f"Unknown tool: '{name}'",
        )

    action = _render_action(name, params)

    if tool.permission_level != PermissionLevel.READ and not approved:
        level = tool.permission_level.value
        logger.info(f"execute_tool: '{name}' blocked — {level} requires approval")
        result = ToolResult(
            success=False,
            output=None,
            error=f"Tool '{name}' is a {level} action and was not approved",
            requires_approval=True,
            approval_prompt=f"'{name}' is a {level} action — user approval is required before it can run.",
            permission_level=tool.permission_level,
        )
        await _log_activity(
            db,
            tool_name=name,
            action=action,
            params=params,
            result=result,
            session_id=session_id,
            duration_ms=None,
        )
        return result

    started = time.perf_counter()
    result = await tool.safe_execute(**params)
    duration_ms = int((time.perf_counter() - started) * 1000)

    # The tool reports its own permission level on the result for the audit row
    if result.permission_level != tool.permission_level:
        result.permission_level = tool.permission_level

    await _log_activity(
        db,
        tool_name=name,
        action=action,
        params=params,
        result=result,
        session_id=session_id,
        duration_ms=duration_ms,
    )
    logger.info(
        f"Tool executed: {action} → {'ok' if result.success else 'FAILED'} ({duration_ms}ms)"
    )
    return result


async def purge_old_activity(db: AsyncSession, days: int = ACTIVITY_RETENTION_DAYS) -> int:
    """Age out audit rows past the retention window. Called by the housekeeping
    sweep. Lives here because the module that decides what goes into the audit
    trail is the one that should decide how long it stays — see the ⚠️ note on
    ACTIVITY_RETENTION_DAYS for the file_intelligence coupling."""
    cutoff = utc_now() - timedelta(days=days)
    result = await db.execute(delete(ActivityLog).where(ActivityLog.created_at < cutoff))
    await db.commit()
    return int(result.rowcount or 0)
