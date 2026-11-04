import asyncio
import json
import sys

from daemon.config import Config
from daemon.tools.base import ToolContext
from daemon.tools.codeintel import FindSymbolsTool
from daemon.tools.lsp import (
    configured_server,
    diagnostics,
    document_symbols,
    find_references,
)


_FAKE_SERVER = r'''
import json
import sys

def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        key, value = line.decode().split(":", 1)
        headers[key.lower()] = value.strip()
    return json.loads(sys.stdin.buffer.read(int(headers["content-length"])))

def send(message):
    body = json.dumps(message).encode()
    sys.stdout.buffer.write(b"Content-Length: " + str(len(body)).encode()
                            + b"\r\n\r\n" + body)
    sys.stdout.buffer.flush()

document_uri = ""
while True:
    message = read_message()
    if message is None:
        break
    method = message.get("method")
    if "id" not in message:
        if method == "textDocument/didOpen":
            document_uri = message["params"]["textDocument"]["uri"]
        continue
    if method == "textDocument/documentSymbol":
        result = [{
            "name": "calculate", "kind": 12,
            "selectionRange": {"start": {"line": 2, "character": 4}},
            "children": [{"name": "nested", "kind": 6,
                          "selectionRange": {"start": {"line": 3, "character": 8}}}]
        }]
    elif method == "textDocument/references":
        result = [{
            "uri": document_uri,
            "range": {"start": {"line": 4, "character": 2}},
        }]
    elif method == "textDocument/diagnostic":
        result = {
            "kind": "full",
            "items": [{
                "range": {"start": {"line": 1, "character": 2}},
                "severity": 1,
                "message": "unexpected token",
            }],
        }
    else:
        result = {"capabilities": {}}
    send({"jsonrpc": "2.0", "id": message["id"], "result": result})
    if method == "exit":
        break
'''


def test_configured_language_server_provides_nested_symbols(tmp_path):
    source, context = _source_and_context(tmp_path)

    symbols, error = asyncio.run(document_symbols(context, source))
    output = asyncio.run(FindSymbolsTool().run({"path": "module.ts"}, context))

    assert error is None
    assert symbols == [
        {"name": "calculate", "kind": "function", "line": 3, "signature": ""},
        {"name": "calculate.nested", "kind": "method", "line": 4, "signature": ""},
    ]
    assert "calculate.nested" in output.content
    assert output.metadata["method"] == "language server"


def test_configured_language_server_provides_references_and_diagnostics(tmp_path):
    source, context = _source_and_context(tmp_path)

    references, error = asyncio.run(find_references(context, source, 1, 5))
    findings, diagnostics_errors = asyncio.run(diagnostics(context, [source]))

    assert error is None
    assert references is not None
    assert references[0]["range"]["start"] == {"line": 4, "character": 2}
    assert not diagnostics_errors
    assert findings == ["module.ts:2:3: error: unexpected token"]


def test_project_config_cannot_launch_a_language_server(tmp_path):
    config = Config({
        "code_intelligence": {
            "lsp": {"servers": {"typescript": ["untrusted-project-command"]}}
        }
    }, user_path=tmp_path / "user-config.toml",
       project_path=tmp_path / "project-config.toml")

    assert configured_server(ToolContext(tmp_path, config), "typescript") is None


def _source_and_context(tmp_path):
    source = tmp_path / "module.ts"
    source.write_text("export function calculate() {}", encoding="utf-8")
    user_config = tmp_path / "user-config.toml"
    command = [sys.executable, "-u", "-c", _FAKE_SERVER]
    serialized = ", ".join(json.dumps(part) for part in command)
    user_config.write_text(
        "[code_intelligence.lsp.servers]\n"
        f"typescript = [{serialized}]\n",
        encoding="utf-8")
    config = Config({}, user_path=user_config, project_path=None)
    return source, ToolContext(tmp_path, config)
