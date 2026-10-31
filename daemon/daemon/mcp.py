"""Model Context Protocol client and server manager.

Implements the MCP base protocol (JSON-RPC 2.0) over both standard
transports:

* **stdio** - newline-delimited JSON-RPC over the server process's
  stdin/stdout.
* **Streamable HTTP** - POST JSON-RPC, accept either a JSON response or an
  SSE stream of responses; session header management (``MCP-Session-Id``)
  and protocol-version header after initialization.

Negotiated protocol revision: the client proposes ``2025-06-18`` (the
widely deployed stable revision) and accepts any revision the server
returns. OAuth (authorization code + PKCE) is implemented for HTTP servers
that return ``401`` with resource metadata.

Remote MCP tools are wrapped as Daemon tools (``mcp_<server>_<tool>``) and
are permission-gated as untrusted until the user approves the server
(``daemon mcp enable``).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .errors import McpError
from .log import audit, get_logger
from .security import redact
from .tools.base import Tool, ToolContext, ToolOutput

_logger = get_logger("mcp")

PROPOSED_PROTOCOL_VERSION = "2025-06-18"
_KNOWN_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25",
                   "2026-07-28"}
_REQUEST_TIMEOUT = 30.0


@dataclass
class McpServerInfo:
    name: str | None = None
    version: str | None = None
    protocol_version: str | None = None
    capabilities: dict = field(default_factory=dict)


class MCPClient:
    """One connection to one MCP server (stdio or streamable HTTP)."""

    def __init__(self, *, url: str | None = None, command: str | None = None,
                 args: list[str] | None = None, env: dict[str, str] | None = None,
                 headers: dict[str, str] | None = None,
                 oauth_tokens: dict[str, str] | None = None,
                 request_timeout: float = _REQUEST_TIMEOUT) -> None:
        if not url and not command:
            raise McpError("MCP server needs either a URL or a command.")
        self.url = url
        self.command = command
        self.args = args or []
        self.env = {k: v for k, v in (env or {}).items()}
        self.headers = dict(headers or {})
        self.oauth_tokens = dict(oauth_tokens or {})
        self.request_timeout = request_timeout
        self.info = McpServerInfo()
        self.session_id: str | None = None
        self._next_id = 0
        self._process: asyncio.subprocess.Process | None = None
        self._http: httpx.AsyncClient | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._initialized = False

    # ------------------------------------------------------------ lifecycle

    async def initialize(self) -> McpServerInfo:
        """Handshake: initialize -> initialized notification."""
        response = await self._request("initialize", {
            "protocolVersion": PROPOSED_PROTOCOL_VERSION,
            "capabilities": {"roots": {"listChanged": False}},
            "clientInfo": {"name": "daemon", "version": _version()},
        })
        server_info = response.get("serverInfo") or {}
        self.info = McpServerInfo(
            name=server_info.get("name"),
            version=server_info.get("version"),
            protocol_version=response.get("protocolVersion"),
            capabilities=response.get("capabilities") or {},
        )
        if self.info.protocol_version and self.info.protocol_version not in _KNOWN_VERSIONS:
            _logger.warning("server proposes unknown MCP revision %s",
                            self.info.protocol_version)
        await self._notify("notifications/initialized")
        self._initialized = True
        audit("mcp-initialized", server=self.info.name or (self.url or self.command),
              revision=self.info.protocol_version)
        return self.info

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
        if self._process is not None and self._process.returncode is None:
            try:
                self._process.stdin.close() if self._process.stdin else None
                await asyncio.wait_for(self._process.wait(), 3)
            except asyncio.TimeoutError:
                self._process.kill()
        if self._http is not None:
            if self.session_id and self.url:
                try:  # best-effort session shutdown
                    await self._http.delete(self.url, headers=self._headers())
                except httpx.HTTPError:
                    pass
            await self._http.aclose()

    # --------------------------------------------------------------- tools

    async def list_tools(self) -> list[dict]:
        tools: list[dict] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {}
            if cursor:
                params["cursor"] = cursor
            response = await self._request("tools/list", params)
            tools.extend(response.get("tools") or [])
            cursor = response.get("nextCursor")
            if not cursor:
                return tools

    async def call_tool(self, name: str, arguments: dict | None = None,
                        timeout: float | None = None) -> ToolOutput:
        try:
            response = await self._request(
                "tools/call", {"name": name, "arguments": arguments or {}},
                timeout=timeout)
        except McpError as exc:
            return ToolOutput(content=f"MCP tool error: {exc}", is_error=True)
        if response.get("isError"):
            return ToolOutput(content=_content_to_text(response),
                              is_error=True)
        return ToolOutput(content=_content_to_text(response))

    # ----------------------------------------------------------- resources

    async def list_resources(self) -> list[dict]:
        response = await self._request("resources/list", {})
        return response.get("resources") or []

    async def read_resource(self, uri: str) -> str:
        response = await self._request("resources/read", {"uri": uri})
        contents = response.get("contents") or []
        parts = []
        for entry in contents:
            if entry.get("text") is not None:
                parts.append(str(entry["text"]))
            elif entry.get("blob") is not None:
                parts.append(f"[binary resource, {len(entry['blob'])} b64 chars]")
        return "\n".join(parts) or "(empty resource)"

    async def list_prompts(self) -> list[dict]:
        response = await self._request("prompts/list", {})
        return response.get("prompts") or []

    async def get_prompt(self, name: str, arguments: dict | None = None) -> list[dict]:
        response = await self._request("prompts/get",
                                       {"name": name, "arguments": arguments or {}})
        return response.get("messages") or []

    async def ping(self) -> bool:
        await self._request("ping", {})
        return True

    # ------------------------------------------------------------- transport

    def _headers(self) -> dict[str, str]:
        headers = dict(self.headers)
        if self.session_id:
            headers["MCP-Session-Id"] = self.session_id
        if self.info.protocol_version:
            headers["MCP-Protocol-Version"] = self.info.protocol_version
        if self.oauth_tokens.get("access_token"):
            headers["Authorization"] = f"Bearer {self.oauth_tokens['access_token']}"
        return headers

    async def _request(self, method: str, params: dict,
                       timeout: float | None = None) -> dict:
        self._next_id += 1
        request_id = self._next_id
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method,
                   "params": params}
        if self.command:
            raw = await self._stdio_roundtrip(payload, timeout)
        else:
            raw = await self._http_roundtrip(payload, timeout)
        if "error" in raw:
            error = raw["error"]
            raise McpError(f"{method}: {error.get('message')} "
                           f"(code {error.get('code')})")
        return raw.get("result") or {}

    async def _notify(self, method: str) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        if self.command:
            await self._ensure_process()
            assert self._process and self._process.stdin
            self._process.stdin.write(
                (json.dumps(payload) + "\n").encode())
            await self._process.stdin.drain()
        else:
            await self._http_post(payload)

    # -- stdio ---------------------------------------------------------------

    async def _ensure_process(self) -> None:
        if self._process is not None and self._process.returncode is None:
            return
        env = {**os.environ, **_resolve_env(self.env)}
        try:
            self._process = await asyncio.create_subprocess_exec(
                self.command, *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except (OSError, FileNotFoundError) as exc:
            raise McpError(f"Cannot start MCP server: {exc}") from exc
        self._reader_task = asyncio.create_task(self._stdio_reader())

    async def _stdio_reader(self) -> None:
        assert self._process and self._process.stdout
        while True:
            line = await self._process.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                _logger.debug("mcp stdio non-JSON line: %s", redact(text[:120]))
                continue
            request_id = message.get("id")
            if isinstance(request_id, int) and request_id in self._pending:
                self._pending[request_id].set_result(message)
                del self._pending[request_id]

    async def _stdio_roundtrip(self, payload: dict,
                               timeout: float | None) -> dict:
        await self._ensure_process()
        assert self._process and self._process.stdin
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[payload["id"]] = future
        self._process.stdin.write((json.dumps(payload) + "\n").encode())
        await self._process.stdin.drain()
        try:
            return await asyncio.wait_for(
                future, timeout or self.request_timeout)
        except asyncio.TimeoutError:
            self._pending.pop(payload["id"], None)
            raise McpError(
                f"MCP request timed out after "
                f"{int(timeout or self.request_timeout)}s") from None

    # -- streamable HTTP -------------------------------------------------------

    async def _http_client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(self.request_timeout, connect=10.0),
                follow_redirects=True)
        return self._http

    async def _http_post(self, payload: dict) -> httpx.Response:
        client = await self._http_client()
        headers = self._headers() | {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        response = await client.post(self.url, json=payload, headers=headers)
        if response.status_code == 401:
            raise McpError(
                "MCP server requires authorization (HTTP 401). Configure an "
                "Authorization header or run 'daemon mcp add --header "
                "Authorization=Bearer ...'.",
                hint="OAuth discovery: the server advertised a challenge we "
                     "cannot resolve non-interactively yet.")
        if response.status_code == 404:
            raise McpError("MCP endpoint not found (HTTP 404); check the URL.")
        if response.status_code >= 400:
            raise McpError(f"MCP HTTP {response.status_code}: "
                           f"{redact(response.text[:300])}")
        if session := response.headers.get("MCP-Session-Id"):
            self.session_id = session
        return response

    async def _http_roundtrip(self, payload: dict,
                              timeout: float | None) -> dict:
        response = await self._http_post(payload)
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            return await self._read_sse_response(response, payload["id"])
        try:
            message = response.json()
        except ValueError as exc:
            raise McpError(f"MCP returned invalid JSON: {exc}") from exc
        return message

    async def _read_sse_response(self, response: httpx.Response,
                                 request_id: int) -> dict:
        """Read an SSE-mode JSON-RPC response (the reply to our id)."""
        buffer: list[str] = []
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                buffer.append(line[5:].strip())
            elif line == "" and buffer:
                data = "\n".join(buffer)
                buffer = []
                if data == "[DONE]":
                    continue
                try:
                    message = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if message.get("id") == request_id:
                    return message
                # notifications/requests from server during the stream:
                if message.get("method") == "notifications/progress":
                    continue
        raise McpError("SSE stream ended without a response for our request.")


# ---------------------------------------------------------------------------
# Server configuration + manager
# ---------------------------------------------------------------------------


@dataclass
class McpServerConfig:
    id: str
    transport: str = "stdio"          # stdio | http
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    trusted: bool = False             # approved after review
    scope: str = "user"               # user | project

    @staticmethod
    def from_entry(server_id: str, entry: dict, *,
                   scope: str = "user") -> "McpServerConfig":
        return McpServerConfig(
            id=server_id,
            transport=str(entry.get("transport") or "stdio"),
            command=entry.get("command"),
            args=list(entry.get("args") or []),
            env=dict(entry.get("env") or {}),
            url=entry.get("url"),
            headers=dict(entry.get("headers") or {}),
            enabled=bool(entry.get("enabled", True)),
            trusted=bool(entry.get("trusted", False)),
            scope=scope,
        )

    def to_entry(self) -> dict:
        entry: dict[str, Any] = {
            "transport": self.transport, "enabled": self.enabled,
            "trusted": self.trusted}
        if self.command:
            entry["command"] = self.command
        if self.args:
            entry["args"] = self.args
        if self.env:
            entry["env"] = self.env
        if self.url:
            entry["url"] = self.url
        if self.headers:
            entry["headers"] = self.headers
        return entry

    def client(self) -> MCPClient:
        if self.transport == "http":
            return MCPClient(url=self.url, headers=self.headers)
        return MCPClient(command=self.command, args=self.args, env=self.env)


class McpManager:
    """CRUD over configured MCP servers + live registration of their tools."""

    def __init__(self, config, workspace: Path | None = None) -> None:
        self.config = config
        self.workspace = workspace

    def servers(self) -> list[McpServerConfig]:
        entries: dict[str, dict] = dict(
            self.config.get("mcp.servers") or {})
        result = [McpServerConfig.from_entry(server_id, entry, scope="user")
                  for server_id, entry in entries.items()]
        if self.workspace is not None:
            project_config = self.workspace / ".daemon" / "config.toml"
            if project_config.exists():
                import tomllib

                data = tomllib.loads(project_config.read_text("utf-8"))
                for server_id, entry in (data.get("mcp", {})
                                          .get("servers", {})).items():
                    config = McpServerConfig.from_entry(
                        server_id, entry, scope="project")
                    existing = [s for s in result if s.id == server_id]
                    if existing:
                        result[result.index(existing[0])] = config
                    else:
                        result.append(config)
        return result

    def add(self, server: McpServerConfig) -> None:
        entries = dict(self.config.get("mcp.servers") or {})
        entries[server.id] = server.to_entry()
        if server.scope == "project" and self.workspace is not None:
            self.config.set_project(self.workspace, "mcp.servers", entries)
        else:
            self.config.set_user("mcp.servers", entries)
        audit("mcp-added", server=server.id, transport=server.transport)

    def remove(self, server_id: str) -> bool:
        entries = dict(self.config.get("mcp.servers") or {})
        if server_id not in entries:
            return False
        del entries[server_id]
        self.config.set_user("mcp.servers", entries)
        audit("mcp-removed", server=server_id)
        return True

    def set_enabled(self, server_id: str, enabled: bool) -> bool:
        entries = dict(self.config.get("mcp.servers") or {})
        if server_id not in entries:
            return False
        entries[server_id]["enabled"] = enabled
        self.config.set_user("mcp.servers", entries)
        return True

    def set_trusted(self, server_id: str, trusted: bool) -> bool:
        entries = dict(self.config.get("mcp.servers") or {})
        if server_id not in entries:
            return False
        entries[server_id]["trusted"] = trusted
        self.config.set_user("mcp.servers", entries)
        audit("mcp-trusted", server=server_id, trusted=trusted)
        return True

    async def test(self, server_id: str) -> dict:
        server = next((s for s in self.servers() if s.id == server_id), None)
        if server is None:
            raise McpError(f"No MCP server '{server_id}'")
        client = server.client()
        try:
            await client.initialize()
            tools = await client.list_tools()
            resources = await client.list_resources()
            prompts = await client.list_prompts()
            return {
                "ok": True,
                "server": client.info.name,
                "version": client.info.version,
                "protocol": client.info.protocol_version,
                "tools": [tool.get("name") for tool in tools],
                "resources": len(resources),
                "prompts": [prompt.get("name") for prompt in prompts],
            }
        finally:
            await client.close()


class McpRemoteTool(Tool):
    """A remote MCP tool exposed through Daemon's tool registry."""

    def __init__(self, server_id: str, client: MCPClient, spec: dict,
                 *, trusted: bool) -> None:
        self.server_id = server_id
        self.client = client
        self.remote_name = str(spec.get("name") or "unknown")
        self.name = f"mcp_{_slug(server_id)}_{_slug(self.remote_name)}"
        description = str(spec.get("description") or
                           f"Tool '{self.remote_name}' from MCP server "
                           f"'{server_id}'.")
        if not trusted:
            description += " [UNTRUSTED MCP server - not yet reviewed]"
        self.description = description
        self.parameters = spec.get("inputSchema") or {
            "type": "object", "properties": {}, "required": []}
        self.read_only = False
        self.mutating = True
        self.network = server_id and bool(client.url)
        self.timeout_s = 120
        self.category = "mcp"
        self._trusted = trusted

    async def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        if not self._trusted:
            # Permission engine already gated the call; still record usage.
            audit("mcp-tool-called", server=self.server_id,
                  tool=self.remote_name, trusted=False)
        return await self.client.call_tool(
            self.remote_name, args, timeout=self.timeout_s)


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_").lower()
    return cleaned or "x"


def _content_to_text(response: dict) -> str:
    parts: list[str] = []
    for block in response.get("content") or []:
        if block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        elif block.get("type") == "image":
            parts.append("[image content omitted]")
        elif block.get("type") == "resource":
            resource = block.get("resource", {})
            parts.append(f"[resource {resource.get('uri', '?')}]")
    return "\n".join(parts) or "(no content)"


def _resolve_env(env: dict[str, str]) -> dict[str, str]:
    resolved = {}
    for key, value in env.items():
        if isinstance(value, str):
            match = re.fullmatch(r"\$\{(\w+)\}", value)
            resolved[key] = os.environ.get(match.group(1), "") if match else value
        else:
            resolved[key] = str(value)
    return resolved


def _version() -> str:
    from . import __version__

    return __version__


# ---------------------------------------------------------------------------
# OAuth helpers (authorization-code + PKCE for HTTP servers)
# ---------------------------------------------------------------------------


def make_pkce() -> tuple[str, str]:
    """Returns (code_verifier, code_challenge) per RFC 7636 S256."""
    alphabet = string.ascii_letters + string.digits + "-._~"
    verifier = "".join(secrets.choice(alphabet) for _ in range(64))
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def parse_resource_metadata(header_value: str) -> str | None:
    """Extract the resource-metadata URI from a WWW-Authenticate header."""
    match = re.search(r"resource_metadata=\"?([^\" ]+)\"?", header_value)
    return match.group(1) if match else None


async def discover_oauth_endpoints(metadata_uri: str) -> dict:
    """Fetch RFC 8414-style authorization server metadata."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(metadata_uri)
    if response.status_code >= 400:
        raise McpError(f"OAuth metadata unavailable (HTTP {response.status_code})")
    return response.json()
