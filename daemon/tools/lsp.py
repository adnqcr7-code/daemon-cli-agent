"""Small optional LSP client for explicitly configured language servers."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

_LANGUAGES = {
    ".py": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".go": "go", ".rs": "rust", ".java": "java", ".cs": "csharp",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp",
    ".rb": "ruby", ".php": "php", ".kt": "kotlin", ".swift": "swift",
}

_SYMBOL_KINDS = {
    1: "file", 2: "module", 3: "namespace", 4: "package", 5: "class",
    6: "method", 7: "property", 8: "field", 9: "constructor", 10: "enum",
    11: "interface", 12: "function", 13: "variable", 14: "constant",
    15: "string", 16: "number", 17: "boolean", 18: "array", 19: "object",
    20: "key", 21: "null", 22: "enum member", 23: "struct", 24: "event",
    25: "operator", 26: "type parameter",
}


class LanguageServerError(RuntimeError):
    pass


def language_for_path(path: Path) -> str | None:
    return _LANGUAGES.get(path.suffix.lower())


def configured_server(ctx, language: str) -> list[str] | None:
    if ctx.config is None:
        return None
    user_path = ctx.config.user_path
    if user_path is None:
        return None
    from ..config import _read_toml

    user_config = _read_toml(user_path) if user_path.exists() else {}
    section = user_config.get("code_intelligence", {})
    lsp = section.get("lsp", {}) if isinstance(section, dict) else {}
    servers = lsp.get("servers", {}) if isinstance(lsp, dict) else {}
    command = servers.get(language) if isinstance(servers, dict) else None
    if isinstance(command, list) and command and all(
            isinstance(part, str) and part for part in command):
        return command
    return None


class LanguageServer:
    def __init__(self, command: list[str], workspace: Path) -> None:
        self.command = command
        self.workspace = workspace
        self.process: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._notifications: list[dict[str, Any]] = []

    async def __aenter__(self) -> LanguageServer:
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.command,
                cwd=self.workspace,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            result = await self.request("initialize", {
                "processId": None,
                "rootUri": self.workspace.as_uri(),
                "rootPath": str(self.workspace),
                "capabilities": {
                    "textDocument": {
                        "documentSymbol": {
                            "hierarchicalDocumentSymbolSupport": True},
                        "publishDiagnostics": {"relatedInformation": True},
                    },
                },
                "clientInfo": {"name": "Daemon", "version": "0.2.1"},
            })
            if not isinstance(result, dict):
                raise LanguageServerError("language server returned invalid initialize data")
            await self.notify("initialized", {})
            return self
        except (TimeoutError, OSError, LanguageServerError) as exc:
            await self._stop()
            raise LanguageServerError(
                f"could not start {self.command[0]}: {exc}") from exc

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self._stop()

    async def _stop(self) -> None:
        process = self.process
        if process is None:
            return
        if process.returncode is None and process.stdin is not None:
            try:
                await self.request("shutdown", {}, timeout=2.0)
                await self.notify("exit", {})
                process.stdin.close()
            except (TimeoutError, OSError, LanguageServerError):
                pass
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2.0)
                except TimeoutError:
                    process.kill()
                    await process.wait()
        self.process = None

    async def open_document(self, path: Path) -> str:
        uri = path.as_uri()
        text = path.read_text(encoding="utf-8", errors="replace")
        await self.notify("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": language_for_path(path) or "plaintext",
                "version": 1,
                "text": text,
            },
        })
        return uri

    async def request(self, method: str, params: dict[str, Any],
                      *, timeout: float = 20.0) -> Any:
        request_id = self._next_id
        self._next_id += 1
        await self._send({
            "jsonrpc": "2.0", "id": request_id,
            "method": method, "params": params,
        })
        async with asyncio.timeout(timeout):
            while True:
                message = await self._read()
                if message.get("id") == request_id:
                    if "error" in message:
                        error = message["error"]
                        raise LanguageServerError(
                            str(error.get("message", "request failed")))
                    return message.get("result")
                if "method" in message:
                    await self._handle_server_message(message)
                    if message.get("method") == "textDocument/publishDiagnostics":
                        self._notifications.append(message)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _handle_server_message(self, message: dict[str, Any]) -> None:
        if "id" not in message:
            return
        method = message.get("method")
        if method == "workspace/configuration":
            result = [None] * len(message.get("params", {}).get("items", []))
        else:
            result = None
        await self._send({"jsonrpc": "2.0", "id": message["id"], "result": result})

    async def _send(self, message: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise LanguageServerError("language server is not running")
        body = json.dumps(message, separators=(",", ":")).encode("utf-8")
        self.process.stdin.write(
            f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
        await self.process.stdin.drain()

    async def _read(self) -> dict[str, Any]:
        if self.process is None or self.process.stdout is None:
            raise LanguageServerError("language server is not running")
        headers: dict[str, str] = {}
        while True:
            line = await self.process.stdout.readline()
            if not line:
                raise LanguageServerError("language server closed its output")
            if line in (b"\r\n", b"\n"):
                break
            key, separator, value = line.decode("ascii", errors="replace").partition(":")
            if separator:
                headers[key.strip().lower()] = value.strip()
        try:
            length = int(headers["content-length"])
        except (KeyError, ValueError) as exc:
            raise LanguageServerError("invalid language-server message header") from exc
        if length < 0 or length > 32_000_000:
            raise LanguageServerError("language-server message exceeded size limit")
        body = await self.process.stdout.readexactly(length)
        try:
            message = json.loads(body)
        except json.JSONDecodeError as exc:
            raise LanguageServerError("language server returned invalid JSON") from exc
        if not isinstance(message, dict):
            raise LanguageServerError("language server returned a non-object message")
        return message

    async def document_symbols(self, uri: str) -> list[dict[str, Any]]:
        result = await self.request("textDocument/documentSymbol", {
            "textDocument": {"uri": uri},
        })
        if not isinstance(result, list):
            return []
        flattened: list[dict[str, Any]] = []

        def visit(symbol: dict[str, Any], parent: str = "") -> None:
            name = str(symbol.get("name") or "")
            full_name = f"{parent}.{name}" if parent and name else name
            selection = symbol.get("selectionRange") or symbol.get("range") or {}
            start = selection.get("start", {}) if isinstance(selection, dict) else {}
            flattened.append({
                "name": full_name,
                "kind": _SYMBOL_KINDS.get(symbol.get("kind"), "symbol"),
                "line": int(start.get("line", 0)) + 1,
                "signature": "",
            })
            for child in symbol.get("children") or []:
                if isinstance(child, dict):
                    visit(child, full_name)

        for item in result:
            if not isinstance(item, dict):
                continue
            if "location" in item:  # SymbolInformation response
                location = item.get("location") or {}
                start = (location.get("range") or {}).get("start", {})
                flattened.append({
                    "name": str(item.get("name") or ""),
                    "kind": _SYMBOL_KINDS.get(item.get("kind"), "symbol"),
                    "line": int(start.get("line", 0)) + 1,
                    "signature": "",
                })
            else:
                visit(item)
        return flattened

    async def references(self, uri: str, line: int, column: int) -> list[dict[str, Any]]:
        result = await self.request("textDocument/references", {
            "textDocument": {"uri": uri},
            "position": {"line": max(0, line - 1), "character": max(0, column - 1)},
            "context": {"includeDeclaration": True},
        })
        return result if isinstance(result, list) else []

    async def diagnostics(self, uri: str) -> list[dict[str, Any]]:
        try:
            result = await self.request("textDocument/diagnostic", {
                "textDocument": {"uri": uri},
            }, timeout=3.0)
        except (TimeoutError, LanguageServerError):
            result = None
        if isinstance(result, dict):
            items = result.get("items")
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
        for message in reversed(self._notifications):
            params = message.get("params") or {}
            if params.get("uri") == uri and isinstance(params.get("diagnostics"), list):
                return params["diagnostics"]
        return []


async def document_symbols(ctx, path: Path) -> tuple[list[dict[str, Any]] | None, str | None]:
    language = language_for_path(path)
    command = configured_server(ctx, language) if language else None
    if command is None:
        return None, None
    try:
        async with LanguageServer(command, ctx.workspace_root) as server:
            uri = await server.open_document(path)
            return await server.document_symbols(uri), None
    except (TimeoutError, OSError, LanguageServerError) as exc:
        return None, str(exc)


async def find_references(ctx, path: Path, line: int,
                          column: int) -> tuple[list[dict[str, Any]] | None, str | None]:
    language = language_for_path(path)
    command = configured_server(ctx, language) if language else None
    if command is None:
        return None, None
    try:
        async with LanguageServer(command, ctx.workspace_root) as server:
            uri = await server.open_document(path)
            return await server.references(uri, line, column), None
    except (TimeoutError, OSError, LanguageServerError) as exc:
        return None, str(exc)


async def diagnostics(ctx, paths: list[Path]) -> tuple[list[str], list[str]]:
    grouped: dict[str, list[Path]] = {}
    errors: list[str] = []
    for path in paths:
        language = language_for_path(path)
        if language and configured_server(ctx, language):
            grouped.setdefault(language, []).append(path)
    findings: list[str] = []
    for language, files in grouped.items():
        command = configured_server(ctx, language)
        assert command is not None
        try:
            async with LanguageServer(command, ctx.workspace_root) as server:
                for path in files:
                    uri = await server.open_document(path)
                    for diagnostic in await server.diagnostics(uri):
                        findings.append(_format_diagnostic(path, diagnostic,
                                                            ctx.workspace_root))
        except (TimeoutError, OSError, LanguageServerError) as exc:
            errors.append(f"{language} LSP ({command[0]}): {exc}")
    return findings, errors


def _format_diagnostic(path: Path, item: dict[str, Any], root: Path) -> str:
    location = item.get("range", {}).get("start", {})
    line = int(location.get("line", 0)) + 1
    column = int(location.get("character", 0)) + 1
    severity = {1: "error", 2: "warning", 3: "info", 4: "hint"}.get(
        item.get("severity"), "diagnostic")
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = path
    return f"{relative}:{line}:{column}: {severity}: {item.get('message', '')}"


def uri_path(uri: str) -> Path:
    parsed = urlsplit(uri)
    if parsed.scheme != "file":
        return Path(uri)
    path = url2pathname(unquote(parsed.path))
    if parsed.netloc:
        path = f"//{parsed.netloc}{path}"
    return Path(path)
