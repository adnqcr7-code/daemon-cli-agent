"""Tool ecosystem: registry plus built-in filesystem, shell, git,
code-intelligence and research tools."""

from .base import Tool, ToolContext, ToolOutput, ToolEvent, ToolRegistry
from .fs import IGNORE_DIRS
from .patches import apply_hunks, parse_unified_diff, unified_diff_text


def register_builtin_tools(registry: ToolRegistry) -> ToolRegistry:
    """Register every first-party tool on a fresh registry."""
    from .fs import register_filesystem_tools
    from .shell import register_shell_tools
    from .git import register_git_tools
    from .codeintel import register_codeintel_tools
    from .web import register_web_tools
    from .misc import register_misc_tools

    register_filesystem_tools(registry)
    register_shell_tools(registry)
    register_git_tools(registry)
    register_codeintel_tools(registry)
    register_web_tools(registry)
    register_misc_tools(registry)
    return registry


def default_registry() -> ToolRegistry:
    return register_builtin_tools(ToolRegistry())


def read_only_registry() -> ToolRegistry:
    """Toolset for subagents that must not modify anything."""
    registry = ToolRegistry()
    from .fs import (ReadFileTool, ListDirTool, GlobTool, SearchFilesTool,
                     FileInfoTool, MakeDiffTool, ProjectMapTool)

    for tool in (ReadFileTool(), ListDirTool(), GlobTool(), SearchFilesTool(),
                 FileInfoTool(), MakeDiffTool(), ProjectMapTool()):
        registry.register(tool)
    from .git import GitStatusTool, GitDiffTool, GitLogTool, GitBranchesTool, GitShowTool

    for tool in (GitStatusTool(), GitDiffTool(), GitLogTool(), GitBranchesTool(),
                 GitShowTool()):
        registry.register(tool)
    from .codeintel import FindSymbolsTool, ReferencesTool, DiagnosticsTool

    for tool in (FindSymbolsTool(), ReferencesTool(), DiagnosticsTool()):
        registry.register(tool)
    return registry


def research_registry() -> ToolRegistry:
    """The deliberately small Research-agent toolset."""
    registry = ToolRegistry()
    from .fs import (ReadFileTool, ListDirTool, GlobTool, SearchFilesTool,
                     FileInfoTool, ProjectMapTool)
    from .web import WebSearchTool, WebFetchTool
    from .misc import MemoryNoteTool

    for tool in (ReadFileTool(), ListDirTool(), GlobTool(), SearchFilesTool(),
                 FileInfoTool(), ProjectMapTool(), WebSearchTool(),
                 WebFetchTool(), MemoryNoteTool()):
        registry.register(tool)
    return registry


__all__ = [
    "Tool", "ToolContext", "ToolOutput", "ToolEvent", "ToolRegistry",
    "register_builtin_tools", "default_registry", "read_only_registry",
    "research_registry", "IGNORE_DIRS", "apply_hunks", "parse_unified_diff",
    "unified_diff_text",
]
