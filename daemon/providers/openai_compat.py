"""OpenAI-compatible protocol adapter.

Speaks the de-facto standard ``/chat/completions`` + ``/models`` API used by
OpenAI itself and dozens of compatible providers (DeepSeek, Groq, Together,
Fireworks, DeepInfra, Mistral, xAI, Moonshot, Z.AI, DashScope's compatible
mode, OpenRouter, LiteLLM, vLLM, Ollama, LM Studio, ...). Includes an Azure
OpenAI URL scheme (deployments + api-version).
"""

from __future__ import annotations

import json
from typing import AsyncIterator

from ..errors import ProviderError
from ..log import get_logger
from .base import ProviderAdapter, merge_usage
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

_logger = get_logger("providers.openai")

_AZURE_API_VERSION = "2024-10-21"
_REASONING_MODEL_HINTS = ("o1", "o3", "o4", "gpt-5")


def _looks_reasoning(model_id: str) -> bool:
    lowered = model_id.lower()
    return any(hint in lowered for hint in _REASONING_MODEL_HINTS)


class OpenAICompatAdapter(ProviderAdapter):
    """Chat Completions protocol with tool calls and SSE streaming."""

    # -- auth --------------------------------------------------------------

    def auth_headers(self) -> dict[str, str]:
        if not self.api_key:
            return {}
        if self.descriptor.id == "azure":
            return {"api-key": self.api_key}
        return {"Authorization": f"Bearer {self.api_key}"}

    # -- endpoints ----------------------------------------------------------

    def _chat_url(self, model: str) -> str:
        if self.descriptor.id == "azure":
            return (f"{self.endpoint}/deployments/{model}/chat/completions"
                    f"?api-version={_AZURE_API_VERSION}")
        return f"{self.endpoint}/chat/completions"

    def _models_url(self) -> str:
        if self.descriptor.id == "azure":
            return f"{self.endpoint}/models?api-version={_AZURE_API_VERSION}"
        return f"{self.endpoint}/models"

    # -- protocol mapping ----------------------------------------------------

    @staticmethod
    def _message_to_wire(message: Message) -> list[dict]:
        if message.role == "assistant":
            entry: dict = {"role": "assistant"}
            if message.text:
                entry["content"] = message.text
            if message.tool_calls:
                entry["tool_calls"] = [
                    {"id": call.id, "type": "function",
                     "function": {"name": call.name,
                                  "arguments": json.dumps(call.arguments)}}
                    for call in message.tool_calls
                ]
            if not message.text and not message.tool_calls:
                entry["content"] = ""
            return [entry]
        if message.role == "user":
            entries = []
            for result in message.tool_results:
                entries.append({
                    "role": "tool",
                    "tool_call_id": result.tool_call_id,
                    "content": result.content[:200_000],
                })
            if message.text:
                entries.append({"role": "user", "content": message.text})
            return entries
        return [{"role": message.role, "content": message.text or ""}]

    def _build_body(self, request: ChatRequest) -> dict:
        messages: list[dict] = []
        for message in self._conversation(request):
            messages.extend(self._message_to_wire(message))
        body: dict = {"model": request.model, "messages": messages}
        if request.tools:
            body["tools"] = [
                {"type": "function",
                 "function": {"name": tool.name, "description": tool.description,
                              "parameters": tool.parameters or {"type": "object",
                                                                "properties": {}}}}
                for tool in request.tools
            ]
            body["tool_choice"] = "auto"
        reasoning = _looks_reasoning(request.model)
        if request.temperature is not None and not reasoning:
            body["temperature"] = request.temperature
        if request.max_tokens is not None:
            if reasoning:
                body["max_completion_tokens"] = request.max_tokens
            else:
                body["max_tokens"] = request.max_tokens
        body.update(request.extra)
        return body

    # -- chat ----------------------------------------------------------------

    async def chat(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        body = self._build_body(request)
        url = self._chat_url(request.model)
        if request.stream:
            if self.descriptor.stream_usage:
                body["stream"] = True
                body["stream_options"] = {"include_usage": True}
            else:
                body["stream"] = True
            async for event in self._stream_chat(url, body):
                yield event
        else:
            payload = await self._request_json("POST", url, json=body)
            for event in self._events_from_response(payload):
                yield event

    async def _stream_chat(self, url: str, body: dict) -> AsyncIterator[StreamEvent]:
        usage = Usage()
        tool_calls: dict[int, dict] = {}
        finish_reason: str | None = None
        async for data in self._stream_sse("POST", url, json=body):
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                _logger.debug("unparseable SSE payload from %s", self.id)
                continue
            if isinstance(chunk.get("error"), dict):
                error = chunk["error"]
                raise ProviderError(
                    f"{self.display_name}: {error.get('message', error)}",
                    provider=self.id, status=error.get("code"))
            merge_usage(usage, chunk.get("usage"))
            choices = chunk.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            finish_reason = choice.get("finish_reason") or finish_reason
            delta = choice.get("delta") or {}
            if isinstance(delta.get("reasoning_content"), str):
                thinking = delta["reasoning_content"]
                if thinking:
                    yield StreamEvent(kind="text", thinking=thinking)
            content = delta.get("content")
            if isinstance(content, str) and content:
                yield StreamEvent(kind="text", text=content)
            for wire_call in delta.get("tool_calls") or []:
                index = wire_call.get("index", 0)
                slot = tool_calls.setdefault(index, {"id": None, "name": None,
                                                     "arguments": ""})
                if wire_call.get("id"):
                    slot["id"] = wire_call["id"]
                function = wire_call.get("function") or {}
                if function.get("name"):
                    slot["name"] = function["name"]
                if function.get("arguments"):
                    slot["arguments"] += function["arguments"]
        if usage.input_tokens or usage.output_tokens:
            yield StreamEvent(kind="usage", usage=usage)
        if tool_calls:
            yield StreamEvent(kind="tool_calls",
                              tool_calls=self._finalise_calls(tool_calls))
        yield StreamEvent(kind="done", finish_reason=finish_reason or "stop")

    @staticmethod
    def _finalise_calls(tool_calls: dict[int, dict]) -> list[ToolCall]:
        final: list[ToolCall] = []
        for index in sorted(tool_calls):
            slot = tool_calls[index]
            arguments: dict = {}
            raw = slot.get("arguments") or ""
            if raw:
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        arguments = parsed
                    else:
                        arguments = {"value": parsed}
                except json.JSONDecodeError:
                    arguments = {"_raw": raw}  # let the runtime report a tool error
            final.append(ToolCall(
                id=str(slot.get("id") or f"call_{index}"),
                name=str(slot.get("name") or "unknown"),
                arguments=arguments,
            ))
        return final

    def _events_from_response(self, payload: object) -> list[StreamEvent]:
        if not isinstance(payload, dict):
            raise ProviderError(f"{self.display_name} returned an unexpected payload",
                                provider=self.id)
        if isinstance(payload.get("error"), dict):
            error = payload["error"]
            raise ProviderError(f"{self.display_name}: {error.get('message', error)}",
                                provider=self.id)
        choices = payload.get("choices") or []
        if not choices:
            raise ProviderError(f"{self.display_name} returned no choices",
                                provider=self.id)
        choice = choices[0]
        message = choice.get("message") or {}
        events: list[StreamEvent] = []
        usage = Usage()
        merge_usage(usage, payload.get("usage"))
        if usage.total_tokens:
            events.append(StreamEvent(kind="usage", usage=usage))
        if isinstance(message.get("reasoning_content"), str) and message["reasoning_content"]:
            events.append(StreamEvent(kind="text", thinking=message["reasoning_content"]))
        if isinstance(message.get("content"), str) and message["content"]:
            events.append(StreamEvent(kind="text", text=message["content"]))
        wire_calls = message.get("tool_calls") or []
        if wire_calls:
            calls = []
            for wire_call in wire_calls:
                function = wire_call.get("function") or {}
                arguments: dict = {}
                raw = function.get("arguments") or ""
                if raw:
                    try:
                        arguments = json.loads(raw)
                    except json.JSONDecodeError:
                        arguments = {"_raw": raw}
                calls.append(ToolCall(id=str(wire_call.get("id") or "call"),
                                      name=str(function.get("name") or "unknown"),
                                      arguments=arguments))
            events.append(StreamEvent(kind="tool_calls", tool_calls=calls))
        events.append(StreamEvent(kind="done",
                                  finish_reason=choice.get("finish_reason") or "stop"))
        return events

    # -- model discovery -------------------------------------------------------

    async def list_models(self) -> list[RemoteModel]:
        url = self._models_url()
        payload = await self._request_json("GET", url)
        if not isinstance(payload, dict):
            return []
        models: list[RemoteModel] = []
        for entry in payload.get("data") or []:
            model_id = entry.get("id") if isinstance(entry, dict) else entry
            if not model_id:
                continue
            models.append(RemoteModel(
                model_id=str(model_id),
                provider=self.id,
                display_name=str(model_id),
                capabilities=ModelCapabilities(streaming=True),
                discovered=True,
            ))
        if self.descriptor.id == "azure":
            for model in models:
                model.capabilities.tool_calling = None
        return models
