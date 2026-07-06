"""
Jarvis OS — Base Tool Interface
Abstract base class that every tool in the plugin architecture must implement.
This enforces the open/closed principle: new tools extend this, never modify it.
"""
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel


class PermissionLevel(str, Enum):
    """Safety classification for tool actions."""
    READ = "read"        # No confirmation needed
    WRITE = "write"      # Confirmation required
    DESTRUCTIVE = "destructive"  # Confirmation + warning required


class ToolResult(BaseModel):
    """Standard return type for all tool executions."""
    success: bool
    output: Any
    error: Optional[str] = None
    requires_approval: bool = False
    approval_prompt: Optional[str] = None
    permission_level: PermissionLevel = PermissionLevel.READ


class ToolDefinition(BaseModel):
    """Metadata describing a tool — used by the planner agent."""
    name: str
    description: str
    parameters: Dict[str, Any]  # JSON Schema of parameters
    permission_level: PermissionLevel


class BaseTool(ABC):
    """
    Abstract base for all Jarvis OS tools.
    
    Every tool must:
    1. Declare its name and permission level
    2. Implement execute() with a ToolResult return
    3. Provide a definition() for the planner agent
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique tool identifier (snake_case)."""
        ...

    @property
    @abstractmethod
    def permission_level(self) -> PermissionLevel:
        """Minimum permission required to run this tool."""
        ...

    @abstractmethod
    async def execute(self, **kwargs: Any) -> ToolResult:
        """
        Execute the tool with the given parameters.
        Always returns a ToolResult — never raises raw exceptions.
        """
        ...

    @abstractmethod
    def definition(self) -> ToolDefinition:
        """Return the tool's schema for the planner agent."""
        ...

    async def safe_execute(self, **kwargs: Any) -> ToolResult:
        """
        Wrapper around execute() that catches all exceptions and
        returns a failed ToolResult instead of propagating.
        """
        try:
            return await self.execute(**kwargs)
        except Exception as exc:
            return ToolResult(
                success=False,
                output=None,
                error=f"Tool '{self.name}' raised an unexpected error: {exc}",
                permission_level=self.permission_level,
            )
