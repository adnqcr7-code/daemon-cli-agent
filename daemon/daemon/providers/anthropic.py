"""Anthropic Messages API adapter.

Native implementation of ``POST /v1/messages`` (content blocks, tool use,
extended thinking passthrough, SSE streaming) and ``GET /v1/models``.
"""

from __future__ import annotations

import json
from typing import AsyncIterator

from ..errors import ProviderError
from ..log import get_logger
from .base import ProviderAdapter
from .catalog import ProviderDescriptor
from .types import (
    ChatRequest,
    Message,
    ModelCapabilities,
    RemoteModel,
    StreamEvent,
    ToolCall,
    Usage,
)

_logger = get_logger("providers.anthropic")
_ANTHROPIC_VERSION = "2023-06-01"
_DEFAULT_MAX_TOKENS = 8192


class AnthropicAdapter(ProviderAdapter):
    # -- auth --------------------------------------------------------------

    def auth_headers(self) -> dict[str, str]:
        headers = {"anthropic-version": _ANTHROPIC_VERSION}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    # -- protocol mapping ----------------------------------------------------

    @staticmethod
    def _messages_to_wire(request: ChatRequest) -> list[dict]:
        """Merge consecutive same-role turns (the API requires alternation)."""
        wire: list[dict] = []
        for message in request.messages:
            blocks = AnthropicAdapter._blocks_for(message)
            if wire and wire[-1]["role"] == _wire_role(message.role) and \
                    message.role != "assistant":
                wire[-1]["content"].extend(blocks)
            else:
                wire.append({"role": _wire_role(message.role), "content": blocks})
        return wire

    @staticmethod
    def _blocks_for(message: Message) -> list[dict]:
        blocks: list[dict] = []
        if message.text:
            blocks.append({"type": "text", "text": message.text})
        for call in message.tool_calls:
            blocks.append({"type": "tool_use", "id": call.id, "name": call.name,
                           "input": call.arguments})
        for result in message.tool_results:
            blocks.append({
                "type": "tool_result",
                "tool_use_id": result.tool_call_id,
                "content": result.content[:180_000],
                **({"is_error": True} if result.is_error else {}),
            })
        if not blocks:
            blocks.append({"type": "text", "text": ""})
        return blocks

    def _build_body(self, request: ChatRequest) -> dict:
        body: dict = {
            "model": request.model,
            "max_tokens": request.max_tokens or _DEFAULT_MAX_TOKENS,
            "messages": self._messages_to_wire(request),
        }
        if request.system:
            body["system"] = request.system
        if request.tools:
            body["tools"] = [
                {"name": tool.name, "description": tool.description,
                 "input_schema": tool.parameters or {"type": "object",
                                                     "properties": {}}}
                for tool in request.tools
            ]
        if request.temperature is not None:
            body["temperature"] = request.temperature
        body.update(request.extra)
        return body

    # -- chat ----------------------------------------------------------------

    async def chat(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        body = self._build_body(request)
        url = f"{self.endpoint}/v1/messages"
        if request.stream:
            body["stream"] = True
            async for event in self._stream_chat(url, body):
                yield event
        else:
            payload = await self._request_json("POST", url, json=body)
            for event in self._events_from_response(payload):
                yield event

    async def _stream_chat(self, url: str, body: dict) -> AsyncIterator[StreamEvent]:
        input_tokens = 0
        output_tokens = 0
        open_tool_calls: dict[int, dict] = {}
        finish_reason: str | None = None
        async for data in self._stream_sse("POST", url, json=body):
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            if kind == "message_start":
                usage = (event.get("message") or {}).get("usage") or {}
                input_tokens = int(usage.get("input_tokens") or 0)
            elif kind == "content_block_start":
                block = event.get("content_block") or {}
                if block.get("type") == "tool_use":
                    open_tool_calls[int(event.get("index", 0))] = {
                        "id": block.get("id"), "name": block.get("name"),
                        "arguments": ""}
            elif kind == "content_block_delta":
                delta = event.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    yield StreamEvent(kind="text", text=delta["text"])
                elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
                    yield StreamEvent(kind="text", thinking=delta["thinking"])
                elif delta.get("type") == "input_json_delta":
                    index = int(event.get("index", 0))
                    slot = open_tool_calls.setdefault(index, {"id": None, "name": None,
                                                              "arguments": ""})
                    slot["arguments"] += delta.get("partial_json") or ""
            elif kind == "message_delta":
                usage = event.get("usage") or {}
                output_tokens = int(usage.get("output_tokens") or output_tokens)
                finish_reason = event.get("delta", {}).get("stop_reason") or finish_reason
            elif kind == "error":
                error = event.get("error") or {}
                raise ProviderError(f"{self.display_name}: {error.get('message', error)}",
                                    provider=self.id)
        usage = Usage(input_tokens=input_tokens, output_tokens=output_tokens)
        if usage.total_tokens:
            yield StreamEvent(kind="usage", usage=usage)
        if open_tool_calls:
            calls = []
            for index in sorted(open_tool_calls):
                slot = open_tool_calls[index]
                arguments: dict = {}
                if slot["arguments"]:
                    try:
                        arguments = json.loads(slot["arguments"])
                    except json.JSONDecodeError:
                        arguments = {"_raw": slot["arguments"]}
                calls.append(ToolCall(id=str(slot["id"] or f"call_{index}"),
                                      name=str(slot["name"] or "unknown"),
                                      arguments=arguments))
            yield StreamEvent(kind="tool_calls", tool_calls=calls)
        yield StreamEvent(kind="done", finish_reason=finish_reason or "end_turn")

    def _events_from_response(self, payload: object) -> list[StreamEvent]:
        if not isinstance(payload, dict):
            raise ProviderError(f"{self.display_name} returned an unexpected payload",
                                provider=self.id)
        if isinstance(payload.get("error"), dict):
            error = payload["error"]
            raise ProviderError(f"{self.display_name}: {error.get('message', error)}",
                                provider=self.id)
        usage = Usage(
            input_tokens=int((payload.get("usage") or {}).get("input_tokens") or 0),
            output_tokens=int((payload.get("usage") or {}).get("output_tokens") or 0),
        )
        events: list[StreamEvent] = []
        if usage.total_tokens:
            events.append(StreamEvent(kind="usage", usage=usage))
        tool_calls: list[ToolCall] = []
        for block in payload.get("content") or []:
            if block.get("type") == "text" and block.get("text"):
                events.append(StreamEvent(kind="text", text=block["text"]))
            elif block.get("type") == "thinking" and block.get("thinking"):
                events.append(StreamEvent(kind="text", thinking=block["thinking"]))
            elif block.get("type") == "tool_use":
                tool_calls.append(ToolCall(id=str(block.get("id") or "call"),
                                           name=str(block.get("name") or "unknown"),
                                           arguments=block.get("input") or {}))
        if tool_calls:
            events.append(StreamEvent(kind="tool_calls", tool_calls=tool_calls))
        events.append(StreamEvent(kind="done",
                                  finish_reason=payload.get("stop_reason") or "end_turn"))
        return events

    # -- model discovery -------------------------------------------------------

    async def list_models(self) -> list[RemoteModel]:
        payload = await self._request_json("GET", f"{self.endpoint}/v1/models")
        if not isinstance(payload, dict):
            return []
        models: list[RemoteModel] = []
        for entry in payload.get("data") or []:
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            models.append(RemoteModel(
                model_id=str(entry["id"]),
                provider=self.id,
                display_name=str(entry.get("display_name") or entry["id"]),
                capabilities=ModelCapabilities(
                    tool_calling=True, streaming=True),
                discovered=True,
            ))
        return models


def _wire_role(role: str) -> str:
    return "assistant" if role == "assistant" else "user"
