"""Coding intelligence: symbols, references, diagnostics, formatting, tests.

Python support is real AST-based analysis (stdlib ``ast``). Other languages
use a documented heuristic scanner (declaration regexes) which finds the
common shapes but is not a parser. Diagnostics and formatting delegate to
installed linters/formatters when present and say so honestly when absent.

A full Language Server Protocol client is the intended evolution of this
module (see docs/roadmap notes in the README); the AST/regex core is
deliberately dependency-free and works everywhere.
"""

from __future__ import annotations

import ast
import asyncio
import re
import shutil
import sys
from pathlib import Path

from ..errors import ToolError
from ..security import resolve_in_root
from .base import Tool, ToolContext, ToolOutput
from .fs import IGNORE_DIRS

_PY_SUFFIXES = {".py"}
_SCAN_SUFFIXES = {
    ".js": "js", ".jsx": "js", ".mjs": "js", ".ts": "ts", ".tsx": "ts",
    ".go": "go", ".rs": "rust", ".java": "java", ".cs": "csharp",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp",
    ".rb": "ruby", ".php": "php", ".kt": "kotlin", ".swift": "swift",
}

_DECL_PATTERNS: dict[str, list[tuple[str, re.Pattern[str]]]] = {
    "js": [
        ("function", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)")),
        ("class", re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+(\w+)")),
        ("const", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>")),
    ],
    "ts": [
        ("function", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)")),
        ("class", re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+(\w+)")),
        ("interface", re.compile(r"^\s*(?:export\s+)?interface\s+(\w+)")),
        ("type", re.compile(r"^\s*(?:export\s+)?type\s+(\w+)")),
        ("const", re.compile(r"^\s*(?:export\s+)?(?:const|let)\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>")),
    ],
    "go": [
        ("func", re.compile(r"^func\s+(?:\([^)]+\)\s*)?(\w+)")),
        ("type", re.compile(r"^type\s+(\w+)")),
    ],
    "rust": [
        ("fn", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)")),
        ("struct", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?struct\s+(\w+)")),
        ("enum", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?enum\s+(\w+)")),
        ("trait", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?trait\s+(\w+)")),
        ("impl", re.compile(r"^\s*impl(?:<[^>]*>)?\s+(?:\w+\s+for\s+)?(\w+)")),
    ],
    "java": [
        ("class", re.compile(r"^\s*(?:public|private|protected)?\s*(?:abstract\s+)?(?:final\s+)?class\s+(\w+)")),
        ("interface", re.compile(r"^\s*(?:public\s+)?interface\s+(\w+)")),
        ("method", re.compile(r"^\s+(?:public|private|protected)?\s*(?:static\s+)?(?:final\s+)?[\w<>\[\],\s]+\s+(\w+)\s*\(")),
    ],
    "csharp": [
        ("class", re.compile(r"^\s*(?:public|internal|private)?\s*(?:abstract\s+)?(?:sealed\s+)?(?:partial\s+)?class\s+(\w+)")),
        ("interface", re.compile(r"^\s*(?:public\s+)?interface\s+(\w+)")),
    ],
    "c": [
        ("function", re.compile(r"^[A-Za-z_][\w\s\*]*\**\s*(\w+)\s*\([^;]*\)\s*\{?\s*$")),
        ("struct", re.compile(r"^\s*(?:typedef\s+)?struct\s+(\w+)")),
    ],
    "cpp": [
        ("class", re.compile(r"^\s*(?:class|struct)\s+(\w+)")),
        ("function", re.compile(r"^[A-Za-z_][\w\s\*&:<>,]*\(\w+\)\s*\(")),
    ],
    "ruby": [
        ("def", re.compile(r"^\s*def\s+(\w+)")),
        ("class", re.compile(r"^\s*(?:class|module)\s+(\w+)")),
    ],
    "php": [
        ("function", re.compile(r"^\s*(?:public|private|protected)?\s*(?:static\s+)?function\s+(\w+)")),
        ("class", re.compile(r"^\s*(?:abstract\s+)?class\s+(\w+)")),
    ],
    "kotlin": [
        ("fun", re.compile(r"^\s*(?:private|internal|public)?\s*(?:suspend\s+)?fun\s+(\w+)")),
        ("class", re.compile(r"^\s*(?:data\s+)?(?:sealed\s+)?class\s+(\w+)")),
    ],
    "swift": [
        ("func", re.compile(r"^\s*(?:public|private|internal)?\s*func\s+(\w+)")),
        ("class", re.compile(r"^\s*(?:final\s+)?class\s+(\w+)")),
        ("struct", re.compile(r"^\s*struct\s+(\w+)")),
    ],
}


def _python_symbols(text: str) -> list[dict]:
    symbols: list[dict] = []
    tree = ast.parse(text)
    stack: list[str] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "async function" if isinstance(node, ast.AsyncFunctionDef) else "function"
            symbols.append({"name": ".".join(stack + [node.name]),
                            "kind": kind, "line": node.lineno,
                            "signature": _py_signature(node)})
        elif isinstance(node, ast.ClassDef):
            symbols.append({"name": ".".join(stack + [node.name]), "kind": "class",
                            "line": node.lineno, "signature": ""})
            stack.append(node.name)
            for child in node.body:
                visit(child)
            stack.pop()
            return
        for child in ast.iter_child_nodes(node):
            visit(child)

    for statement in tree.body:
        visit(statement)
    return symbols


def _py_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    try:
        args = [arg.arg for arg in node.args.posonlyargs + node.args.args]
        if node.args.vararg:
            args.append(f"*{node.args.vararg.arg}")
        args.extend(arg.arg for arg in node.args.kwonlyargs)
        if node.args.kwarg:
            args.append(f"**{node.args.kwarg.arg}")
        return f"({', '.join(args)})"
    except Exception:  # pragma: no cover - defensive
        return "()"


def _scanned_symbols(path: Path, language: str) -> list[dict]:
    symbols: list[dict] = []
    patterns = _DECL_PATTERNS.get(language, [])
    try:
        text = path.read_text("utf-8", errors="replace")
    except OSError:
        return symbols
    for line_no, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in patterns:
            match = pattern.match(line)
            if match:
                symbols.append({"name": match.group(1), "kind": kind,
                                "line": line_no, "signature": ""})
                break
    return symbols


def extract_symbols(path: Path) -> tuple[list[dict], str]:
    """Returns (symbols, method) where method is 'ast' or 'heuristic'."""
    if path.suffix in _PY_SUFFIXES:
        text = path.read_text("utf-8", errors="replace")
        return _python_symbols(text), "ast"
    language = _SCAN_SUFFIXES.get(path.suffix)
    if language is None:
        return [], "unsupported"
    return _scanned_symbols(path, language), "heuristic"


class FindSymbolsTool(Tool):
    name = "find_symbols"
    description = (
        "List symbols (functions, classes, types) in a file or directory. "
        "Python uses exact AST analysis; other languages use a heuristic "
        "declaration scanner.")
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File or directory (default: root)"},
            "symbol": {"type": "string", "description": "Filter by name substring"},
            "kind": {"type": "string", "description": "Filter by kind (function, class...)"},
        },
    }
    read_only = True
    category = "code-intelligence"
    timeout_s = 60

    async def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        root = resolve_in_root(ctx.workspace_root, args.get("path") or ".")
        files: list[Path]
        if root.is_file():
            files = [root]
        else:
            files = [p for p in _source_files(root)][:400]
        results: list[str] = []
        method = "ast"
        for path in files:
            rel = _rel(path, ctx.workspace_root)
            try:
                symbols, used = extract_symbols(path)
            except SyntaxError as exc:
                results.append(f"{rel}: SYNTAX ERROR line {exc.lineno}: {exc.msg}")
                continue
            except OSError:
                continue
            if used == "heuristic":
                method = "ast + heuristic"
            for symbol in symbols:
                if args.get("symbol") and args["symbol"].lower() not in \
                        symbol["name"].lower():
                    continue
                if args.get("kind") and args["kind"].lower() not in \
                        symbol["kind"].lower():
                    continue
                results.append(
                    f"{rel}:{symbol['line']} {symbol['kind']} "
                    f"{symbol['name']}{symbol['signature']}")
        if not results:
            return ToolOutput(content="No symbols found.")
        truncated = "" if len(results) <= 400 else f"\n[truncated at 400 symbols]"
        return ToolOutput(
            content="\n".join(results[:400]) + truncated +
                    f"\n[analysis: {method}]",
            metadata={"method": method})


def _source_files(root: Path, limit: int = 400) -> list[Path]:
    collected: list[Path] = []
    for dirpath, dirnames, filenames in os_walk_pruned(root):
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            if path.suffix in _PY_SUFFIXES or path.suffix in _SCAN_SUFFIXES:
                collected.append(path)
                if len(collected) >= limit:
                    return collected
    return collected


def os_walk_pruned(root: Path):
    import os

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
        yield dirpath, dirnames, filenames


class ReferencesTool(Tool):
    name = "references"
    description = (
        "Count references to a symbol across the project. Python counts "
        "are AST-accurate; other languages use word-boundary matching.")
    parameters = {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "path": {"type": "string", "description": "Limit search to a subtree"},
        },
        "required": ["symbol"],
    }
    read_only = True
    category = "code-intelligence"
    timeout_s = 90

    async def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        symbol = args["symbol"]
        root = resolve_in_root(ctx.workspace_root, args.get("path") or ".")
        word = re.compile(rf"\b{re.escape(symbol)}\b")

        def _scan() -> list[tuple[str, int]]:
            hits: list[tuple[str, int]] = []
            for path in _source_files(root if root.is_dir() else root.parent,
                                      limit=600):
                try:
                    text = path.read_text("utf-8", errors="replace")
                except OSError:
                    continue
                if path.suffix in _PY_SUFFIXES:
                    try:
                        count = sum(
                            1 for node in ast.walk(ast.parse(text))
                            if isinstance(node, ast.Name) and node.id == symbol
                            or isinstance(node, ast.Attribute) and node.attr == symbol)
                    except SyntaxError:
                        count = len(word.findall(text))
                else:
                    count = len(word.findall(text))
                if count:
                    hits.append((_rel(path, ctx.workspace_root), count))
            return hits

        hits = await asyncio.to_thread(_scan)
        if not hits:
            return ToolOutput(content=f"No references to '{symbol}' found.")
        body = "\n".join(f"{file}: {count}" for file, count in sorted(hits))
        total = sum(count for _, count in hits)
        return ToolOutput(
            content=f"'{symbol}': {total} reference(s) in {len(hits)} file(s)\n{body}")


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


class DiagnosticsTool(Tool):
    name = "diagnostics"
    description = (
        "Run available diagnostics: Python syntax checking always; linters "
        "and type checkers (ruff, mypy, tsc...) when installed and "
        "configured.")
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File or directory (default: root)"},
        },
    }
    read_only = True
    category = "code-intelligence"
    timeout_s = 180

    async def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        root = resolve_in_root(ctx.workspace_root, args.get("path") or ".")
        sections: list[str] = []

        syntax = await asyncio.to_thread(_python_syntax_report, root)
        if syntax:
            sections.append("Python syntax:\n" + syntax)

        if shutil.which("ruff"):
            code, out, _ = await _run_tool(f"ruff check --quiet {root}", ctx)
            if out.strip():
                sections.append(f"ruff:\n{out.strip()}")
        if shutil.which("mypy") and (root.suffix == ".py" or (root / "pyproject.toml").exists()):
            code, out, _ = await _run_tool(f"mypy --no-error-summary {root}", ctx)
            if out.strip():
                sections.append(f"mypy:\n{out.strip()[:8000]}")
        if shutil.which("tsc") and _has_tsconfig(root):
            code, out, _ = await _run_tool("tsc --noEmit", ctx)
            if out.strip():
                sections.append(f"tsc:\n{out.strip()[:8000]}")

        if not sections:
            return ToolOutput(
                content="No issues found (syntax check clean; no additional "
                        "linters installed).")
        return ToolOutput(content="\n\n".join(sections))


async def _run_tool(command: str, ctx: ToolContext):
    from .shell import run_shell

    return await run_shell(command, cwd=ctx.workspace_root, timeout_s=120)


def _python_syntax_report(root: Path) -> str:
    problems: list[str] = []
    files = [root] if root.is_file() else _source_files(root, limit=300)
    for path in files:
        if path.suffix != ".py":
            continue
        try:
            ast.parse(path.read_text("utf-8", errors="replace"))
        except SyntaxError as exc:
            problems.append(f"{path}:{exc.lineno}: {exc.msg}")
        except OSError:
            continue
    return "\n".join(problems)


def _has_tsconfig(root: Path) -> bool:
    candidates = [root] if root.is_file() else [root, root.parent]
    return any((candidate / "tsconfig.json").exists() for candidate in candidates)


class FormatCodeTool(Tool):
    name = "format_code"
    description = (
        "Format a file with the project's configured formatter (ruff/black "
        "for Python, prettier for JS/TS, gofmt, rustfmt). Checkpoints first. "
        "Fails honestly when no formatter is installed.")
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }
    mutating = True
    category = "code-intelligence"
    timeout_s = 120

    async def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        path = resolve_in_root(ctx.workspace_root, args["path"])
        if not path.is_file():
            raise ToolError(f"File not found: {args['path']}")
        suffix = path.suffix
        command: str | None = None
        if suffix == ".py":
            if shutil.which("ruff"):
                command = f"ruff format {path}"
            elif shutil.which("black"):
                command = f"black -q {path}"
        elif suffix in (".js", ".ts", ".jsx", ".tsx", ".json", ".css", ".html"):
            if shutil.which("prettier"):
                command = f"prettier --write {path}"
        elif suffix == ".go" and shutil.which("gofmt"):
            command = f"gofmt -w {path}"
        elif suffix == ".rs" and shutil.which("rustfmt"):
            command = f"rustfmt {path}"
        if command is None:
            available = [tool for tool, present in {
                "ruff": shutil.which("ruff"), "black": shutil.which("black"),
                "prettier": shutil.which("prettier"),
                "gofmt": shutil.which("gofmt"),
                "rustfmt": shutil.which("rustfmt")}.items() if present]
            raise ToolError(
                f"No formatter installed for '{suffix}' files. "
                f"Available on this machine: {', '.join(available) or 'none'}.")

        original = path.read_text("utf-8", errors="replace")
        if ctx.checkpoints is not None:
            await ctx.checkpoints.create([path])
        code, out, _ = await _run_tool(command, ctx)
        if code != 0:
            raise ToolError(f"Formatter failed: {out.strip()}")
        updated = path.read_text("utf-8", errors="replace")
        if updated == original:
            return ToolOutput(content=f"{args['path']} was already formatted.")
        from .patches import unified_diff_text

        return ToolOutput(
            content=f"Formatted {args['path']}.",
            display=unified_diff_text(original, updated, args["path"]))


class RunTestsTool(Tool):
    name = "run_tests"
    description = (
        "Detect and run the project's test suite (pytest, go test, cargo "
        "test, npm test, make test) and report a parsed summary.")
    parameters = {
        "type": "object",
        "properties": {
            "args": {"type": "string", "description": "Extra runner arguments"},
            "path": {"type": "string", "description": "Test path (pytest/go)"},
        },
    }
    mutating = True
    category = "code-intelligence"
    timeout_s = 600

    async def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        root = ctx.workspace_root
        extra = args.get("args") or ""
        target = args.get("path") or ""
        candidates: list[tuple[str, str]] = []
        if (root / "pyproject.toml").exists() or (root / "pytest.ini").exists() \
                or (root / "tests").is_dir() or list(root.glob("test_*.py")):
            candidates.append(("pytest", f"python -m pytest -q {target} {extra}".strip()))
        if (root / "package.json").exists():
            candidates.append(("npm test", f"npm test -- {extra}".strip()))
        if (root / "go.mod").exists():
            candidates.append(("go test", f"go test ./... {extra}".strip()))
        if (root / "Cargo.toml").exists():
            candidates.append(("cargo test", f"cargo test {extra}".strip()))
        if (root / "Makefile").exists():
            candidates.append(("make test", "make test"))
        if not candidates:
            candidates.append(("pytest", f"python -m pytest -q {target} {extra}".strip()))

        from .shell import run_shell

        for label, command in candidates:
            exit_code, output, timed_out = await run_shell(
                command, cwd=root, timeout_s=self.timeout_s)
            summary = _parse_test_summary(label, output, exit_code, timed_out)
            content = f"$ {command}\n{output[-8000:]}\n\n{summary}"
            return ToolOutput(content=content, is_error=exit_code != 0,
                              display=summary,
                              metadata={"exit_code": exit_code,
                                        "runner": label})
        raise ToolError("No test runner could be determined.")  # unreachable


def _parse_test_summary(runner: str, output: str, exit_code: int,
                        timed_out: bool) -> str:
    if timed_out:
        return f"[{runner}] TIMED OUT"
    tail = output.strip().splitlines()[-1] if output.strip() else ""
    summary = tail[:200] if tail else f"[{runner}] exit code {exit_code}"
    status = "PASSED" if exit_code == 0 else "FAILED"
    return f"[{runner}] {status} - {summary}"


def register_codeintel_tools(registry) -> None:
    for tool in (FindSymbolsTool(), ReferencesTool(), DiagnosticsTool(),
                 FormatCodeTool(), RunTestsTool()):
        registry.register(tool)


_ = sys
