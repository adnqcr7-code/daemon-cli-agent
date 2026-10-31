"""A minimal real MCP server over stdio used by the integration tests."""

import json
import sys


def send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" not in message:  # notification (initialized, cancelled...)
            continue
        method = message.get("method", "")
        request_id = message["id"]
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": request_id, "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                "serverInfo": {"name": "echo-mcp", "version": "1.2.3"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": [
                {"name": "echo",
                 "description": "Echo text back",
                 "inputSchema": {"type": "object",
                                 "properties": {"text": {"type": "string"}},
                                 "required": ["text"]}},
                {"name": "add",
                 "description": "Add two integers",
                 "inputSchema": {"type": "object",
                                 "properties": {"a": {"type": "integer"},
                                                "b": {"type": "integer"}},
                                 "required": ["a", "b"]}},
            ]}})
        elif method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name == "echo":
                send({"jsonrpc": "2.0", "id": request_id, "result": {
                    "content": [{"type": "text",
                                 "text": "echo: " + str(arguments.get("text", ""))}]}})
            elif name == "add":
                total = int(arguments.get("a", 0)) + int(arguments.get("b", 0))
                send({"jsonrpc": "2.0", "id": request_id, "result": {
                    "content": [{"type": "text", "text": str(total)}]}})
            else:
                send({"jsonrpc": "2.0", "id": request_id, "error": {
                    "code": -32602, "message": f"unknown tool {name}"}})
        elif method == "resources/list":
            send({"jsonrpc": "2.0", "id": request_id, "result": {
                "resources": [{"uri": "echo://test", "name": "Test resource",
                               "mimeType": "text/plain"}]}})
        elif method == "resources/read":
            send({"jsonrpc": "2.0", "id": request_id, "result": {
                "contents": [{"uri": "echo://test", "text": "resource body"}]}})
        elif method == "prompts/list":
            send({"jsonrpc": "2.0", "id": request_id, "result": {
                "prompts": [{"name": "greet", "description": "Say hi"}]}})
        elif method == "prompts/get":
            send({"jsonrpc": "2.0", "id": request_id, "result": {
                "messages": [{"role": "user",
                              "content": {"type": "text", "text": "Hello!"}}]}})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": request_id, "result": {}})
        else:
            send({"jsonrpc": "2.0", "id": request_id, "error": {
                "code": -32601, "message": f"unknown method {method}"}})


if __name__ == "__main__":
    main()
