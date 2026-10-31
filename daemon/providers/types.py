"""Normalized, protocol-agnostic chat primitives.

Every adapter (OpenAI-compatible, Anthropic, Gemini) translates between its
wire format and the types in this module. The agent runtime only ever sees
these types, which is what keeps the engine provider-independent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@dataclass
class ToolSpec:
    """A tool advertisement sent to the model alongside the prompt."""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)  # JSON Schema

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "parameters": self.parameters}


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


@dataclass
class ToolCall:
    """The model asked to invoke a tool."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    """The outcome of a tool invocation, fed back to the model."""

    tool_call_id: str
    tool_name: str
    content: str
    is_error: bool = False


@dataclass
class Message:
    """A conversation turn.

    Roles: ``system``, ``user``, ``assistant``. Tool results travel on a
    ``user`` message (the Anthropic convention) which every adapter maps to
    its native representation.
    """

    role: str
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)

    @staticmethod
    def system(text: str) -> "Message":
        return Message(role="system", text=text)

    @staticmethod
    def user(text: str) -> "Message":
        return Message(role="user", text=text)

    @staticmethod
    def assistant(text: str | None = None,
                  tool_calls: list[ToolCall] | None = None) -> "Message":
        return Message(role="assistant", text=text, tool_calls=tool_calls or [])

    @staticmethod
    def with_tool_results(results: list[ToolResult]) -> "Message":
        return Message(role="user", tool_results=results)

    def to_json(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "text": self.text,
            "tool_calls": [
                {"id": call.id, "name": call.name, "arguments": call.arguments}
                for call in self.tool_calls
            ],
            "tool_results": [
                {"tool_call_id": result.tool_call_id, "tool_name": result.tool_name,
                 "content": result.content, "is_error": result.is_error}
                for result in self.tool_results
            ],
        }

    @staticmethod
    def from_json(data: dict[str, Any]) -> "Message":
        return Message(
            role=data["role"],
            text=data.get("text"),
            tool_calls=[
                ToolCall(id=call["id"], name=call["name"],
                         arguments=call.get("arguments") or {})
                for call in data.get("tool_calls") or []
            ],
            tool_results=[
                ToolResult(tool_call_id=result["tool_call_id"],
                           tool_name=result.get("tool_name", ""),
                           content=result.get("content", ""),
                           is_error=bool(result.get("is_error")))
                for result in data.get("tool_results") or []
            ],
        )


# ---------------------------------------------------------------------------
# Requests / streaming
# ---------------------------------------------------------------------------


@dataclass
class ChatRequest:
    model: str
    messages: list[Message]
    tools: list[ToolSpec] = field(default_factory=list)
    system: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    stream: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    extra: dict[str, int] = field(default_factory=dict)

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        for key, value in other.extra.items():
            self.extra[key] = self.extra.get(key, 0) + value

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_json(self) -> dict[str, Any]:
        return {"input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens, **self.extra}


@dataclass
class StreamEvent:
    """One event from a streaming chat completion.

    kind: ``text`` (delta in ``text``), ``tool_calls`` (complete calls),
    ``usage``, ``done`` (finish_reason set), ``error``.
    """

    kind: str
    text: str | None = None
    thinking: str | None = None  # provider-reported reasoning trace (dim display)
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage | None = None
    finish_reason: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass
class ModelCapabilities:
    """What we know about a model. ``None`` means *unknown*, never guessed."""

    context_window: int | None = None
    tool_calling: bool | None = None
    vision: bool | None = None
    reasoning: bool | None = None
    structured_output: bool | None = None
    streaming: bool | None = None
    input_price_per_mtok: float | None = None
    output_price_per_mtok: float | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "context_window": self.context_window,
            "tool_calling": self.tool_calling,
            "vision": self.vision,
            "reasoning": self.reasoning,
            "structured_output": self.structured_output,
            "streaming": self.streaming,
            "input_price_per_mtok": self.input_price_per_mtok,
            "output_price_per_mtok": self.output_price_per_mtok,
        }

    @staticmethod
    def from_json(data: dict[str, Any]) -> "ModelCapabilities":
        return ModelCapabilities(**{
            key: data.get(key) for key in (
                "context_window", "tool_calling", "vision", "reasoning",
                "structured_output", "streaming",
                "input_price_per_mtok", "output_price_per_mtok",
            )
        })


@dataclass
class RemoteModel:
    model_id: str
    provider: str
    display_name: str | None = None
    capabilities: ModelCapabilities = field(default_factory=ModelCapabilities)
    discovered: bool = False  # True when returned by the provider's API

    @property
    def name(self) -> str:
        return self.display_name or self.model_id

    def to_json(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "provider": self.provider,
            "display_name": self.display_name,
            "capabilities": self.capabilities.to_json(),
            "discovered": self.discovered,
        }

    @staticmethod
    def from_json(data: dict[str, Any]) -> "RemoteModel":
        return RemoteModel(
            model_id=data["model_id"],
            provider=data["provider"],
            display_name=data.get("display_name"),
            capabilities=ModelCapabilities.from_json(data.get("capabilities") or {}),
            discovered=bool(data.get("discovered")),
        )
