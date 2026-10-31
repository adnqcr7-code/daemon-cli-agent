"""Miscellaneous agent tools (memory notes now; JSON/CSV/SQLite inspection
helpers are natural future residents of this module)."""

from __future__ import annotations

from ..errors import ToolError
from .base import Tool, ToolContext, ToolOutput


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
    registry.register(MemoryNoteTool())
