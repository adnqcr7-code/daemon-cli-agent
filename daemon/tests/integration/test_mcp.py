"""MCP client over real stdio and streamable-HTTP transports."""

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from daemon.errors import McpError
from daemon.mcp import (
    MCPClient,
    McpManager,
    McpRemoteTool,
    McpServerConfig,
    discover_oauth_endpoints,
    make_pkce,
    parse_resource_metadata,
)
from daemon.tools import ToolContext

ECHO_SERVER = str(Path(__file__).parent.parent / "mcp_echo_server.py")


def _stdio_client() -> MCPClient:
    return MCPClient(command=sys.executable, args=[ECHO_SERVER])


def test_initialize_handshake():
    async def go():
        client = _stdio_client()
        try:
            info = await client.initialize()
            assert info.name == "echo-mcp"
            assert info.version == "1.2.3"
            assert info.protocol_version == "2025-06-18"
            assert "tools" in info.capabilities
            await client.close()
        finally:
            await client.close()

    asyncio.run(go())


def test_tools_list_and_call():
    async def go():
        client = _stdio_client()
        await client.initialize()
        tools = await client.list_tools()
        assert {tool["name"] for tool in tools} == {"echo", "add"}
        echo = await client.call_tool("echo", {"text": "hello"})
        assert echo.content == "echo: hello"
        assert not echo.is_error
        addition = await client.call_tool("add", {"a": 2, "b": 3})
        assert addition.content == "5"
        await client.close()

    asyncio.run(go())


def test_resources_and_prompts():
    async def go():
        client = _stdio_client()
        await client.initialize()
        resources = await client.list_resources()
        assert resources and resources[0]["uri"] == "echo://test"
        body = await client.read_resource("echo://test")
        assert body == "resource body"
        prompts = await client.list_prompts()
        assert prompts[0]["name"] == "greet"
        messages = await client.get_prompt("greet")
        assert messages[0]["content"]["text"] == "Hello!"
        await client.close()

    asyncio.run(go())


def test_unknown_tool_returns_error_result():
    async def go():
        client = _stdio_client()
        await client.initialize()
        result = await client.call_tool("nonexistent", {})
        assert result.is_error
        await client.close()

    asyncio.run(go())


def test_remote_tool_wrapper_registers_and_executes(workspace):
    async def go():
        client = _stdio_client()
        await client.initialize()
        specs = await client.list_tools()
        tool = McpRemoteTool("testsrv", client, specs[0], trusted=True)
        assert tool.name == "mcp_testsrv_echo"
        assert "[UNTRUSTED" not in tool.description
        untrusted = McpRemoteTool("testsrv", client, specs[0], trusted=False)
        assert "[UNTRUSTED" in untrusted.description
        ctx = ToolContext(workspace)
        output = await tool.run({"text": "wrapped"}, ctx)
        assert output.content == "echo: wrapped"
        await client.close()

    asyncio.run(go())


def test_manager_add_enable_disable(config):
    manager = McpManager(config)
    manager.add(McpServerConfig(
        id="echo", command=sys.executable, args=[ECHO_SERVER]))
    servers = manager.servers()
    assert servers[0].id == "echo"
    assert servers[0].trusted is False  # untrusted until reviewed
    manager.set_trusted("echo", True)
    assert manager.servers()[0].trusted is True
    manager.set_enabled("echo", False)
    assert manager.servers()[0].enabled is False
    manager.remove("echo")
    assert manager.servers() == []


def test_manager_test_connects_and_lists(config):
    async def go():
        manager = McpManager(config)
        manager.add(McpServerConfig(
            id="echo", command=sys.executable, args=[ECHO_SERVER],
            trusted=True))
        report = await manager.test("echo")
        assert report["ok"] is True
        assert report["server"] == "echo-mcp"
        assert set(report["tools"]) == {"echo", "add"}
        assert report["resources"] == 1
        manager.remove("echo")

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Streamable HTTP transport
# ---------------------------------------------------------------------------


class _HttpMcpMock:
    def __init__(self, *, mode="json") -> None:
        self.mode = mode
        self.session_ids: list[str] = []
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append({
                    "body": body,
                    "session": self.headers.get("MCP-Session-Id"),
                    "version": self.headers.get("MCP-Protocol-Version"),
                    "accept": self.headers.get("Accept"),
                })
                result = outer._result_for(body.get("method", ""),
                                           body.get("params") or {})
                if result is None:  # notification
                    self.send_response(202)
                    self.end_headers()
                    self.wfile.write(b"")
                    return
                payload = {"jsonrpc": "2.0", "id": body.get("id"),
                           "result": result}
                session_id = f"sess-{len(outer.session_ids)}"
                outer.session_ids.append(session_id)
                if outer.mode == "sse":
                    chunk = json.dumps(payload)
                    data = f"data: {chunk}\n\n".encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("MCP-Session-Id", session_id)
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    data = json.dumps(payload).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("MCP-Session-Id", session_id)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def _result_for(self, method: str, params: dict):
        if method == "initialize":
            return {"protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "http-mcp", "version": "0.1"}}
        if method == "tools/list":
            return {"tools": [{"name": "http_tool", "description": "over http",
                               "inputSchema": {"type": "object",
                                               "properties": {}}}]}
        if method == "tools/call":
            return {"content": [{"type": "text",
                                 "text": f"called {params.get('name')}"}]}
        if method == "ping":
            return {}
        return None

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def test_streamable_http_json_mode():
    mock = _HttpMcpMock(mode="json")

    async def go():
        client = MCPClient(url=mock.url)
        info = await client.initialize()
        assert info.name == "http-mcp"
        assert client.session_id is not None
        tools = await client.list_tools()
        assert tools[0]["name"] == "http_tool"
        result = await client.call_tool("http_tool", {})
        assert result.content == "called http_tool"
        # session + protocol headers flow on subsequent requests
        followups = [r for r in mock.requests if r["session"]]
        assert followups
        assert any(r["version"] == "2025-06-18" for r in followups)
        await client.close()

    asyncio.run(go())
    mock.stop()


def test_streamable_http_sse_mode():
    mock = _HttpMcpMock(mode="sse")

    async def go():
        client = MCPClient(url=mock.url)
        await client.initialize()
        tools = await client.list_tools()
        assert tools[0]["name"] == "http_tool"
        result = await client.call_tool("http_tool", {})
        assert result.content == "called http_tool"
        await client.close()

    asyncio.run(go())
    mock.stop()


def test_http_error_maps_to_mcp_error():
    class FailMock(_HttpMcpMock):
        def _result_for(self, method, params):
            return None

    mock = FailMock()

    async def go():
        client = MCPClient(url=mock.url)
        try:
            await client.initialize()
            raised = False
        except McpError:
            raised = True
        finally:
            await client.close()
        return raised

    assert asyncio.run(go())
    mock.stop()


# ---------------------------------------------------------------------------
# OAuth helpers
# ---------------------------------------------------------------------------


def test_pkce_pair_is_valid():
    import base64
    import hashlib

    verifier, challenge = make_pkce()
    assert len(verifier) == 64
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected


def test_parse_resource_metadata_header():
    header = ('Bearer realm="mcp", resource_metadata='
              '"https://auth.example.com/.well-known/oauth-authorization-server"')
    assert parse_resource_metadata(header) == \
        "https://auth.example.com/.well-known/oauth-authorization-server"
    assert parse_resource_metadata("Bearer") is None


def test_discover_oauth_endpoints_parses_metadata():
    import http.server

    class OAuthMock(_HttpMcpMock):
        def __init__(self):
            outer = self

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    return

                def do_GET(self):
                    data = json.dumps({
                        "authorization_endpoint": "https://auth/authorize",
                        "token_endpoint": "https://auth/token",
                    }).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

            self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            self.port = self.server.server_address[1]
            self.thread = threading.Thread(target=self.server.serve_forever,
                                           daemon=True)
            self.thread.start()

    mock = OAuthMock()

    async def go():
        return await discover_oauth_endpoints(
            f"http://127.0.0.1:{mock.port}/.well-known/oauth-authorization-server")

    metadata = asyncio.run(go())
    assert metadata["authorization_endpoint"] == "https://auth/authorize"
    mock.stop()
