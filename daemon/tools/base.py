"""Tool contracts: schema, execution, validation, cancellation, timeouts.

Every capability Daemon exposes to a model is a ``Tool`` with:

* a stable name and JSON-Schema parameter description,
* declarative permission metadata (read-only / mutating / network),
* a bounded execution with timeout and clean error reporting.

Tools are independently usable and testable - the registry never special
cases a tool. MCP remote tools and plugins register through the same
interface.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..errors import ToolError
from ..log import audit, get_logger
from ..security import redact

_logger = get_logger("tools")


@dataclass
class ToolOutput:
    """What a tool reports back. ``content`` is what the model sees."""

    content: str
    is_error: bool = False
    display: str | None = None  # optional human-facing rendering
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolEvent:
    """Progress notification streamed to the UI while a tool runs."""

    tool: str
    line: str


class ToolContext:
    """Everything a tool may need, injected per session."""

    def __init__(
        self,
        workspace_root,
        config=None,
        *,
        checkpoints=None,
        memory=None,
        on_event: Callable[[ToolEvent], Awaitable[None]] | None = None,
    ) -> None:
        from pathlib import Path

        self.workspace_root = Path(workspace_root).resolve()
        self.config = config
        self.checkpoints = checkpoints  # CheckpointManager or None
        self.memory = memory  # MemoryStore or None
        self._on_event = on_event

    async def emit(self, tool: str, line: str) -> None:
        if self._on_event is not None:
            await self._on_event(ToolEvent(tool=tool, line=redact(line)))


class Tool:
    """Base class for every Daemon tool."""

    name: str = "tool"
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=lambda: {
        "type": "object", "properties": {}, "required": []
    })
    read_only: bool = False
    mutating: bool = False
    network: bool = False
    timeout_s: int = 120
    category: str = "general"

    def describe(self) -> str:
        flags = []
        if self.read_only:
            flags.append("read-only")
        if self.mutating:
            flags.append("mutating")
        if self.network:
            flags.append("network")
        suffix = f" ({', '.join(flags)})" if flags else ""
        return f"{self.name}{suffix}: {self.description}".strip()

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        raise ToolError(f"{self.name} is not implemented")

    # -- validation ----------------------------------------------------------

    def validate(self, args: dict[str, Any]) -> None:
        if not isinstance(args, dict):
            raise ToolError(f"{self.name}: arguments must be an object")
        schema = self.parameters or {}
        required = schema.get("required") or []
        for key in required:
            if key not in args or args[key] in (None, ""):
                raise ToolError(f"{self.name}: missing required parameter '{key}'")
        properties = schema.get("properties") or {}
        for key, value in args.items():
            spec = properties.get(key)
            if spec is None:
                continue
            expected = spec.get("type")
            if expected and not _type_matches(value, expected):
                raise ToolError(
                    f"{self.name}: parameter '{key}' must be {expected}, "
                    f"got {type(value).__name__}")

    def spec(self) -> dict[str, Any]:
        """Provider-facing tool advertisement."""
        parameters = self.parameters or {
            "type": "object", "properties": {}, "required": []}
        return {"name": self.name, "description": self.description,
                "parameters": parameters}


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return True


class ToolRegistry:
    """Registration, lookup and bounded execution of tools."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool, *, replace: bool = False) -> Tool:
        if tool.name in self._tools and not replace:
            raise ToolError(f"Tool '{tool.name}' is already registered")
        self._tools[tool.name] = tool
        return tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(f"Unknown tool '{name}'")
        return tool

    def names(self) -> list[str]:
        return sorted(self._tools)

    def tools(self) -> list[Tool]:
        return [self._tools[name] for name in self.names()]

    def specs(self) -> list[dict[str, Any]]:
        return [tool.spec() for tool in self.tools()]

    async def execute(self, name: str, args: dict[str, Any],
                      ctx: ToolContext, *,
                      timeout_s: int | None = None) -> ToolOutput:
        """Validated, time-bounded execution with clean error wrapping."""
        tool = self.get(name)
        limit = timeout_s or tool.timeout_s or 120
        started = asyncio.get_running_loop().time()
        try:
            tool.validate(args)
            result = await asyncio.wait_for(tool.run(args, ctx), timeout=limit)
        except asyncio.TimeoutError:
            audit("tool-timeout", tool=name, seconds=limit)
            return ToolOutput(
                content=f"Tool '{name}' timed out after {limit}s. Narrow the "
                        f"task or increase the timeout.",
                is_error=True)
        except ToolError as exc:
            audit("tool-error", tool=name, detail=str(exc))
            return ToolOutput(content=f"{name}: {exc}", is_error=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - tools must never crash the agent
            _logger.exception("tool %s crashed", name)
            from ..log import describe_exception

            audit("tool-crash", tool=name, detail=describe_exception(exc))
            return ToolOutput(
                content=f"{name} failed unexpectedly: "
                        f"{describe_exception(exc)}",
                is_error=True)
        elapsed = asyncio.get_running_loop().time() - started
        audit("tool-executed", tool=name, seconds=f"{elapsed:.2f}",
              ok="false" if result.is_error else "true")
        return result


def is_coroutine_function(func: Callable) -> bool:
    return inspect.iscoroutinefunction(func)
