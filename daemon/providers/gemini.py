"""Google Gemini API adapter.

Native implementation of ``:generateContent`` / ``:streamGenerateContent``
(function calling, SSE streaming, systemInstruction) and ``GET /v1beta/models``.
Auth uses the ``x-goog-api-key`` header so keys never appear in URLs.
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

_logger = get_logger("providers.gemini")


class GeminiAdapter(ProviderAdapter):
    # -- auth --------------------------------------------------------------

    def auth_headers(self) -> dict[str, str]:
        if self.api_key:
            return {"x-goog-api-key": self.api_key}
        return {}

    # -- protocol mapping ----------------------------------------------------

    @staticmethod
    def _contents(request: ChatRequest) -> list[dict]:
        contents: list[dict] = []
        for message in request.messages:
            role = "model" if message.role == "assistant" else "user"
            parts = GeminiAdapter._parts_for(message)
            if contents and contents[-1]["role"] == role:
                contents[-1]["parts"].extend(parts)
            else:
                contents.append({"role": role, "parts": parts})
        return contents

    @staticmethod
    def _parts_for(message: Message) -> list[dict]:
        parts: list[dict] = []
        if message.text:
            parts.append({"text": message.text})
        for call in message.tool_calls:
            parts.append({"functionCall": {"name": call.name, "args": call.arguments}})
        for result in message.tool_results:
            response: dict = {"result": result.content[:180_000]}
            if result.is_error:
                response = {"error": result.content[:180_000]}
            parts.append({"functionResponse": {"name": result.tool_name,
                                               "response": response}})
        if not parts:
            parts.append({"text": ""})
        return parts

    def _build_body(self, request: ChatRequest) -> dict:
        body: dict = {"contents": self._contents(request)}
        if request.system:
            body["systemInstruction"] = {"parts": [{"text": request.system}]}
        if request.tools:
            body["tools"] = [{
                "functionDeclarations": [
                    {"name": tool.name, "description": tool.description,
                     "parameters": tool.parameters or {"type": "object",
                                                        "properties": {}}}
                    for tool in request.tools
                ]
            }]
        generation: dict = {}
        if request.temperature is not None:
            generation["temperature"] = request.temperature
        if request.max_tokens is not None:
            generation["maxOutputTokens"] = request.max_tokens
        if generation:
            body["generationConfig"] = generation
        body.update(request.extra)
        return body

    # -- chat ----------------------------------------------------------------

    async def chat(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        body = self._build_body(request)
        model = request.model.split("/").pop()
        if request.stream:
            url = (f"{self.endpoint}/v1beta/models/{model}:streamGenerateContent"
                   f"?alt=sse")
            async for event in self._stream_chat(url, body):
                yield event
        else:
            url = f"{self.endpoint}/v1beta/models/{model}:generateContent"
            payload = await self._request_json("POST", url, json=body)
            for event in self._events_from_payload(payload):
                yield event

    async def _stream_chat(self, url: str, body: dict) -> AsyncIterator[StreamEvent]:
        usage = Usage()
        tool_calls: list[ToolCall] = []
        finish_reason: str | None = None
        call_index = 0
        async for data in self._stream_sse("POST", url, json=body):
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(chunk.get("error"), dict):
                error = chunk["error"]
                raise ProviderError(
                    f"{self.display_name}: {error.get('message', error)}",
                    provider=self.id, status=error.get("code"))
            meta = chunk.get("usageMetadata") or {}
            if meta:
                usage.input_tokens = int(meta.get("promptTokenCount") or usage.input_tokens)
                usage.output_tokens = int(
                    meta.get("candidatesTokenCount") or usage.output_tokens)
            candidates = chunk.get("candidates") or []
            if not candidates:
                continue
            candidate = candidates[0]
            finish_reason = candidate.get("finishReason") or finish_reason
            for part in (candidate.get("content") or {}).get("parts") or []:
                if part.get("thought") and part.get("text"):
                    yield StreamEvent(kind="text", thinking=part["text"])
                elif part.get("text"):
                    yield StreamEvent(kind="text", text=part["text"])
                elif part.get("functionCall"):
                    call = part["functionCall"]
                    tool_calls.append(ToolCall(
                        id=f"call_{call_index}",
                        name=str(call.get("name") or "unknown"),
                        arguments=call.get("args") or {},
                    ))
                    call_index += 1
        if usage.total_tokens:
            yield StreamEvent(kind="usage", usage=usage)
        if tool_calls:
            yield StreamEvent(kind="tool_calls", tool_calls=tool_calls)
        yield StreamEvent(kind="done", finish_reason=finish_reason or "STOP")

    def _events_from_payload(self, payload: object) -> list[StreamEvent]:
        if not isinstance(payload, dict):
            raise ProviderError(f"{self.display_name} returned an unexpected payload",
                                provider=self.id)
        if isinstance(payload.get("error"), dict):
            error = payload["error"]
            raise ProviderError(f"{self.display_name}: {error.get('message', error)}",
                                provider=self.id, status=error.get("code"))
        events: list[StreamEvent] = []
        meta = payload.get("usageMetadata") or {}
        usage = Usage(input_tokens=int(meta.get("promptTokenCount") or 0),
                      output_tokens=int(meta.get("candidatesTokenCount") or 0))
        if usage.total_tokens:
            events.append(StreamEvent(kind="usage", usage=usage))
        tool_calls: list[ToolCall] = []
        candidates = payload.get("candidates") or []
        if candidates:
            for part in (candidates[0].get("content") or {}).get("parts") or []:
                if part.get("thought") and part.get("text"):
                    events.append(StreamEvent(kind="text", thinking=part["text"]))
                elif part.get("text"):
                    events.append(StreamEvent(kind="text", text=part["text"]))
                elif part.get("functionCall"):
                    call = part["functionCall"]
                    tool_calls.append(ToolCall(
                        id=f"call_{len(tool_calls)}",
                        name=str(call.get("name") or "unknown"),
                        arguments=call.get("args") or {}))
            events.append(StreamEvent(
                kind="done",
                finish_reason=candidates[0].get("finishReason") or "STOP"))
        if tool_calls:
            events.append(StreamEvent(kind="tool_calls", tool_calls=tool_calls))
        return events

    # -- model discovery -------------------------------------------------------

    async def list_models(self) -> list[RemoteModel]:
        payload = await self._request_json(
            "GET", f"{self.endpoint}/v1beta/models?pageSize=1000")
        if not isinstance(payload, dict):
            return []
        models: list[RemoteModel] = []
        for entry in payload.get("models") or []:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            methods = entry.get("supportedGenerationMethods") or []
            if methods and "generateContent" not in methods:
                continue  # embeddings / deprecated models are not chat models
            model_id = str(entry["name"]).removeprefix("models/")
            models.append(RemoteModel(
                model_id=model_id,
                provider=self.id,
                display_name=str(entry.get("displayName") or model_id),
                capabilities=ModelCapabilities(
                    context_window=entry.get("inputTokenLimit"),
                    tool_calling=None,
                    streaming=True,
                ),
                discovered=True,
            ))
        return models
