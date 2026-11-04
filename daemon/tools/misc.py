"""Miscellaneous agent tools (memory notes now; JSON/CSV/SQLite inspection
helpers are natural future residents of this module)."""

from __future__ import annotations

from ..errors import ToolError
from .base import Tool, ToolContext, ToolOutput


class CompactContextTool(Tool):
    name = "compact_context"
    description = (
        "Summarize older conversation history while preserving the user's "
        "goal, decisions, changed files, command results, and next steps. "
        "Use when the context usage indicator approaches 70%, before more "
        "important history is lost to the context limit.")
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    read_only = True
    category = "context"
    timeout_s = 180

    async def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        if ctx.compact_context is None:
            raise ToolError("Context compaction is unavailable in this session.")
        compacted, before, after = await ctx.compact_context()
        if not compacted:
            return ToolOutput(
                content="Context compaction failed; the existing history was kept.",
                is_error=True)
        if after >= before:
            return ToolOutput(
                content="No older conversation history could be compacted.")
        return ToolOutput(
            content=(f"Older context summarized. Estimated conversation size "
                     f"changed from {before:,} to {after:,} tokens "
                     f"(about {before - after:,} fewer)."))


class MemoryNoteTool(Tool):
    name = "memory_note"
    description = (
        "Persist a durable note, decision or research finding to project "
        "memory. Notes survive sessions and are searchable. Content is "
        "redacted for secrets before storage.")
    parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The note to store"},
            "kind": {"type": "string",
                     "enum": ["note", "fact", "decision", "research"],
                     "default": "note"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["content"],
    }
    read_only = False
    mutating = False
    category = "memory"

    async def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        if ctx.memory is None:
            raise ToolError("Memory storage is unavailable in this session.")
        entry = ctx.memory.add(
            args["content"],
            kind=str(args.get("kind") or "note"),
            scope="project",
            tags=args.get("tags") or [],
            source="agent",
        )
        return ToolOutput(content=f"Stored memory {entry.id} ({entry.kind}).",
                          metadata={"id": entry.id})


def register_misc_tools(registry) -> None:
    registry.register(CompactContextTool())
    registry.register(MemoryNoteTool())
