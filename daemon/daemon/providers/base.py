"""ProviderAdapter base class: shared HTTP transport, retries, SSE parsing.

Subclasses implement ``chat()`` (protocol mapping + streaming) and
``list_models()`` (model discovery). Everything transport-related - timeout
policy, retry with backoff, rate-limit handling, error normalization -
lives here so every protocol adapter benefits equally.
"""

from __future__ import annotations

import asyncio
import json
import random
from abc import ABC, abstractmethod
from types import TracebackType
from typing import AsyncIterator, TypeVar

import httpx

from ..config import Config
from ..errors import ProviderError
from ..log import get_logger
from .catalog import ProviderDescriptor
from .types import ChatRequest, Message, RemoteModel, StreamEvent, Usage

T = TypeVar("T")
_logger = get_logger("providers")

RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class ProviderAdapter(ABC):
    """One authenticated connection to one provider."""

    def __init__(
        self,
        descriptor: ProviderDescriptor,
        *,
        api_key: str | None = None,
        endpoint: str | None = None,
        config: Config | None = None,
        account_id: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.descriptor = descriptor
        self.api_key = api_key
        self.endpoint = (endpoint or descriptor.base_url).rstrip("/")
        self.config = config
        self.account_id = account_id
        self._extra_headers = extra_headers or {}
        self._client: httpx.AsyncClient | None = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def id(self) -> str:
        return self.descriptor.id

    @property
    def display_name(self) -> str:
        return self.descriptor.name

    def _timeouts(self) -> httpx.Timeout:
        if self.config is not None:
            connect, overall = self.config.request_timeouts()
        else:
            connect, overall = 10.0, 180.0
        return httpx.Timeout(connect=connect, read=overall, write=60.0, pool=connect)

    def _client_http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self._timeouts(),
                headers={"User-Agent": self._user_agent(), **self.auth_headers(),
                         **self._extra_headers},
                follow_redirects=True,
            )
        return self._client

    def _user_agent(self) -> str:
        from .. import USER_AGENT

        return USER_AGENT

    def auth_headers(self) -> dict[str, str]:
        """Protocol-specific auth headers (subclass override)."""
        return {}

    async def close(self) -> None:
        client = self._client
        self._client = None
        if client is None or client.is_closed:
            return
        try:
            await client.aclose()
        except RuntimeError as exc:
            # httpx transports created by a previous asyncio.run() can still
            # be attached to its already-closed Windows event loop. There is
            # no work left for this loop to perform in that case.
            if str(exc) != "Event loop is closed":
                self._client = client
                raise
            _logger.debug("adapter transport already belonged to a closed loop")

    async def __aenter__(self) -> "ProviderAdapter":
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None,
                        exc: BaseException | None,
                        tb: TracebackType | None) -> None:
        await self.close()

    # -- public API --------------------------------------------------------

    @abstractmethod
    async def chat(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        """Stream a chat completion. Yields StreamEvent objects."""

    @abstractmethod
    async def list_models(self) -> list[RemoteModel]:
        """Discover models accessible with the current credentials."""

    async def verify(self) -> list[RemoteModel]:
        """Credential check = a successful model listing."""
        return await self.list_models()

    def supports_tools(self) -> bool:
        return True

    # -- transport helpers --------------------------------------------------

    async def _request_json(self, method: str, url: str, **kwargs: object) -> object:
        """Non-streaming request with retries; returns parsed JSON."""
        client = self._client_http()
        attempts = (self.config.retries() if self.config else 3) + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = await client.request(method, url, **kwargs)  # type: ignore[arg-type]
            except httpx.TimeoutException as exc:
                last_error = exc
                await self._backoff(attempt, f"timeout: {exc}")
                continue
            except httpx.TransportError as exc:
                last_error = exc
                await self._backoff(attempt, f"transport error: {exc}")
                continue
            if response.status_code in RETRYABLE_STATUS and attempt < attempts - 1:
                await self._backoff(attempt, f"HTTP {response.status_code}",
                                    retry_after=response.headers.get("Retry-After"))
                continue
            if response.status_code >= 400:
                raise self._error(response)
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type and response.text.strip():
                return {"_raw_text": response.text}
            try:
                return response.json()
            except json.JSONDecodeError as exc:  # pragma: no cover - provider bug
                raise ProviderError(
                    f"{self.display_name} returned invalid JSON: {exc}",
                    provider=self.id, retryable=False) from exc
        raise ProviderError(
            f"{self.display_name} request failed after {attempts} attempts "
            f"({last_error})", provider=self.id, retryable=True)

    async def _stream_sse(self, method: str, url: str, **kwargs: object) -> AsyncIterator[str]:
        """POST returning an iterator of SSE ``data:`` payloads with retry."""
        client = self._client_http()
        attempts = (self.config.retries() if self.config else 3) + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            emitted = False
            try:
                async with client.stream(
                    method, url, headers={"Accept": "text/event-stream"},
                    **kwargs,  # type: ignore[arg-type]
                ) as response:
                    if response.status_code in RETRYABLE_STATUS and attempt < attempts - 1:
                        await response.aread()
                        await self._backoff(attempt, f"HTTP {response.status_code}",
                                            retry_after=response.headers.get("Retry-After"))
                        continue
                    if response.status_code >= 400:
                        await response.aread()
                        raise self._error(response)
                    async for event in self._iter_sse_events(response):
                        emitted = True
                        yield event
                    return
            except ProviderError:
                raise
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if emitted:  # partial output already delivered: do not duplicate
                    raise ProviderError(
                        f"{self.display_name} stream broke mid-response: {exc}",
                        provider=self.id, retryable=False) from exc
                await self._backoff(attempt, str(exc))
                continue
        raise ProviderError(
            f"{self.display_name} streaming failed after {attempts} attempts "
            f"({last_error})", provider=self.id, retryable=True)

    @staticmethod
    async def _iter_sse_events(response: httpx.Response) -> AsyncIterator[str]:
        """Minimal SSE parser: yields concatenated ``data:`` payloads."""
        data_lines: list[str] = []
        async for raw_line in response.aiter_lines():
            line = raw_line.rstrip("\r")
            if line.startswith(":"):
                continue  # comment / keep-alive
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))
            elif line == "":
                if data_lines:
                    yield "\n".join(data_lines)
                    data_lines = []
            # Non-data fields (event:, id:, retry:) are ignored.
        if data_lines:
            yield "\n".join(data_lines)

    async def _backoff(self, attempt: int, reason: str,
                       retry_after: str | None = None) -> None:
        if retry_after:
            try:
                delay = min(float(retry_after), 30.0)
            except ValueError:
                delay = None
        else:
            delay = None
        if delay is None:
            delay = min(0.8 * (2 ** attempt) + random.uniform(0, 0.4), 20.0)
        _logger.debug("retrying %s in %.1fs (%s)", self.id, delay, reason)
        await asyncio.sleep(delay)

    def _error(self, response: httpx.Response) -> ProviderError:
        body = ""
        try:
            body = response.text[:500]
        except Exception:  # pragma: no cover
            pass
        from ..security import redact

        hint = None
        if response.status_code in (401, 403):
            hint = "Check the API key for this provider (daemon auth test)."
        elif response.status_code == 429:
            hint = "Rate limited or quota exceeded; retry shortly."
        return ProviderError(
            f"{self.display_name} HTTP {response.status_code}: {redact(body)}",
            provider=self.id, status=response.status_code,
            retryable=response.status_code in RETRYABLE_STATUS, hint=hint)

    # -- conversation helpers ------------------------------------------------

    @staticmethod
    def _conversation(request: ChatRequest) -> list[Message]:
        """Messages including an optional system message as first turn."""
        messages: list[Message] = []
        if request.system:
            messages.append(Message.system(request.system))
        messages.extend(request.messages)
        return messages


def merge_usage(current: Usage, incoming: dict[str, object] | None) -> None:
    """Merge a provider usage payload (any field names) into ``current``."""
    if not incoming:
        return
    lookup = {
        "input_tokens": ("input_tokens", "prompt_tokens", "input_token_count",
                         "promptTokenCount", "input_tokens_details"),
        "output_tokens": ("output_tokens", "completion_tokens", "output_token_count",
                          "candidatesTokenCount", "output_tokens_details"),
    }
    for target, names in lookup.items():
        for name in names:
            value = incoming.get(name)
            if isinstance(value, int):
                setattr(current, target, getattr(current, target) + value)
                break
    for key, value in incoming.items():
        if isinstance(value, int) and key not in (
            "input_tokens", "output_tokens", "prompt_tokens", "completion_tokens",
            "input_token_count", "output_token_count", "promptTokenCount",
            "candidatesTokenCount",
        ):
            current.extra[key] = current.extra.get(key, 0) + value
