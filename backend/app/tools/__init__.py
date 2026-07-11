"""
Jarvis OS — Tool plugin package (Phase 3)
Importing this package registers every built-in tool into the global
registry. New tools: create a module here, decorate each tool class with
@register_tool, and import the module below — no other code changes.
"""
from app.tools.registry import ToolRegistry, execute_tool, register_tool, registry

# Built-in tool modules are imported here as they are added, so that
# `import app.tools` registers everything:
from app.tools import email_tools  # noqa: F401, E402
from app.tools import file_tools  # noqa: F401, E402
from app.tools import memory_tools  # noqa: F401, E402
from app.tools import terminal_tools  # noqa: F401, E402

__all__ = ["ToolRegistry", "execute_tool", "register_tool", "registry"]
