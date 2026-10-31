"""Filesystem tools: read, write, edit, search, glob, tree, patch.

All path arguments are confined to the trusted workspace (see
``security.resolve_in_root``); escapes raise a tool error the model can act
on. Every mutating tool snapshots the affected file through the checkpoint
manager before changing anything, so /undo always works.
"""

from __future__ import annotations

import asyncio
import difflib
import fnmatch
import os
import re
import shutil
import stat
import time
from pathlib import Path

from ..errors import ToolError
from ..security import resolve_in_root
from .base import Tool, ToolContext, ToolOutput
from .patches import apply_hunks, parse_unified_diff, unified_diff_text

# Directories that are never listed, searched or indexed by default.
IGNORE_DIRS: frozenset[str] = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea",
    ".vscode", "dist", "build", "target", "out", "coverage", "htmlcov",
    ".next", ".nuxt", ".turbo", ".gradle", ".cache", ".daemon",
})

_MAX_READ_LINES = 1500
_MAX_READ_BYTES = 120_000
_MAX_SEARCH_RESULTS = 200
_MAX_GLOB_RESULTS = 500
_MAX_TREE_ENTRIES = 400
_MAX_FILE_BYTES = 4 * 1024 * 1024


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _read_text_bounded(path: Path, *, offset: int = 0, limit: int | None = None,
                       max_lines: int = _MAX_READ_LINES,
                       max_bytes: int = _MAX_READ_BYTES) -> tuple[str, int, bool]:
    """Read text with caps. Returns (content, total_lines, truncated)."""
    size = path.stat().st_size
    if size > _MAX_FILE_BYTES:
        raise ToolError(
            f"{path.name} is {size / 1e6:.1f} MB; too large to read whole. "
            f"Use offset/limit for slices or search_files for targeted matches.")
    data = path.read_bytes()
    if b"\x00" in data[:8192]:
        raise ToolError(f"{path.name} appears to be a binary file.")
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    total = len(lines)
    end = total if limit is None else min(offset + limit, total)
    start = max(0, min(offset, total))
    selected = lines[start:end]
    truncated_chars = len("\n".join(selected))
    while len(selected) > 10 and truncated_chars > max_bytes:
        selected = selected[:int(len(selected) * max_bytes / truncated_chars)]
        truncated_chars = len("\n".join(selected))
    truncated = start > 0 or end < total or truncated_chars > max_bytes
    content = "\n".join(selected)
    if content and (text.endswith("\n")) and end == total:
        content += "\n"
    return content, total, truncated


async def _checkpoint(ctx: ToolContext, *paths: Path) -> None:
    """Snapshot existing files; brand-new files need no checkpoint (their
    undo is simply deletion by the user or delete_path)."""
    if ctx.checkpoints is None:
        return
    existing = [path for path in paths if path.exists() and path.is_file()]
    if existing:
        await ctx.checkpoints.create(existing)


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "Read a text file from the workspace. Returns the content with a "
        "location header. Use offset/limit for large files; binary files "
        "are rejected.")
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "Workspace-relative file path"},
            "offset": {"type": "integer", "description": "0-based start line"},
            "limit": {"type": "integer", "description": "Max lines to return"},
        },
        "required": ["path"],
    }
    read_only = True
    category = "filesystem"
    timeout_s = 30

    async def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        path = resolve_in_root(ctx.workspace_root, args["path"])
        if not path.exists():
            raise ToolError(f"File not found: {args['path']}")
        if path.is_dir():
            raise ToolError(f"{args['path']} is a directory; use list_dir.")
        offset = int(args.get("offset") or 0)
        limit = args.get("limit")
        limit = int(limit) if limit is not None else None
        content, total, truncated = await asyncio.to_thread(
            _read_text_bounded, path, offset=offset, limit=limit)
        rel = _relative(path, ctx.workspace_root)
        shown_lines = len(content.splitlines())
        header = (f"[{rel} | lines {offset + 1}-{offset + shown_lines} "
                  f"of {total}]\n")
        if truncated:
            header += f"[truncated - use offset/limit to read more]\n"
        return ToolOutput(content=header + content,
                          metadata={"path": rel, "total_lines": total})


class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "Create or overwrite a file in the workspace with the given text "
        "content. The previous version is checkpointed automatically.")
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative path"},
            "content": {"type": "string", "description": "Full file content"},
            "create_dirs": {"type": "boolean",
                            "description": "Create parent directories (default true)"},
        },
        "required": ["path", "content"],
    }
    mutating = True
    category = "filesystem"

    async def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        path = resolve_in_root(ctx.workspace_root, args["path"])
        if path.exists() and path.is_dir():
            raise ToolError(f"{args['path']} is a directory.")
        await _checkpoint(ctx, path)
        if args.get("create_dirs", True):
            path.parent.mkdir(parents=True, exist_ok=True)
        elif not path.parent.exists():
            raise ToolError(f"Parent directory missing: {path.parent}")

        def _write() -> None:
            tmp = path.with_name(path.name + ".daemon-tmp")
            tmp.write_text(args["content"], encoding="utf-8")
            os.replace(tmp, path)

        await asyncio.to_thread(_write)
        rel = _relative(path, ctx.workspace_root)
        return ToolOutput(
            content=f"Wrote {len(args['content'])} chars to {rel}.",
            metadata={"path": rel, "bytes": len(args["content"])})


class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "Replace an exact text snippet in a file. old_text must match the "
        "file byte-for-byte and be unique unless replace_all is true. "
        "The file is checkpointed before the edit.")
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_text": {"type": "string", "description": "Exact text to replace"},
            "new_text": {"type": "string", "description": "Replacement text"},
            "replace_all": {"type": "boolean", "default": False},
        },
        "required": ["path", "old_text", "new_text"],
    }
    mutating = True
    category = "filesystem"

    async def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        path = resolve_in_root(ctx.workspace_root, args["path"])
        if not path.is_file():
            raise ToolError(f"File not found: {args['path']}")
        old_text = args["old_text"]
        new_text = args["new_text"]
        original = await asyncio.to_thread(path.read_text, "utf-8")
        count = original.count(old_text)
        if count == 0:
            hint = self._nearest_hint(original, old_text)
            raise ToolError(
                f"old_text not found in {args['path']}.{hint}")
        if count > 1 and not args.get("replace_all"):
            raise ToolError(
                f"old_text occurs {count} times in {args['path']}; "
                f"include more surrounding context to make it unique, or set "
                f"replace_all=true.")
        await _checkpoint(ctx, path)
        updated = original.replace(old_text, new_text) if args.get("replace_all") \
            else original.replace(old_text, new_text, 1)
        await asyncio.to_thread(path.write_text, updated, "utf-8")
        rel = _relative(path, ctx.workspace_root)
        return ToolOutput(
            content=f"Edited {rel}: replaced {count if args.get('replace_all') else 1} "
                    f"occurrence(s).",
            display=self._diff_display(original, updated, rel),
            metadata={"path": rel, "replacements": count})

    @staticmethod
    def _nearest_hint(original: str, old_text: str) -> str:
        first_line = old_text.strip().splitlines()[0] if old_text.strip() else ""
        if first_line:
            close = difflib.get_close_matches(
                first_line, original.splitlines(), n=1, cutoff=0.75)
            if close:
                return (f" A similar line exists: {close[0]!r} - "
                        f"check whitespace/indentation and copy it exactly.")
        return " Verify whitespace and indentation; the match is exact."

    @staticmethod
    def _diff_display(old: str, new: str, rel: str) -> str:
        return unified_diff_text(old, new, rel)


class CreateDirTool(Tool):
    name = "create_dir"
    description = "Create a directory (with parents) inside the workspace."
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }
    mutating = True
    category = "filesystem"

    async def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        path = resolve_in_root(ctx.workspace_root, args["path"])
        await asyncio.to_thread(path.mkdir, {"parents": True, "exist_ok": True})
        return ToolOutput(content=f"Created directory {args['path']}.")


class DeletePathTool(Tool):
    name = "delete_path"
    description = (
        "Delete a file or directory. Files are checkpointed first. Requires "
        "approval by default.")
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "recursive": {"type": "boolean", "default": False,
                          "description": "Required for non-empty directories"},
        },
        "required": ["path"],
    }
    mutating = True
    category = "filesystem"

    async def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        path = resolve_in_root(ctx.workspace_root, args["path"])
        if not path.exists():
            raise ToolError(f"Nothing to delete at {args['path']}.")
        if path == ctx.workspace_root:
            raise ToolError("Refusing to delete the workspace root itself.")
        if path.is_dir():
            children = list(path.iterdir())
            if children and not args.get("recursive"):
                raise ToolError(
                    f"{args['path']} is a non-empty directory; pass "
                    f"recursive=true to delete it and {len(children)} child "
                    f"entries.")
            if children:  # checkpoint every file being removed
                files = [child for child in _walk_files(path)]
                await _checkpoint(ctx, *files)
            await asyncio.to_thread(shutil.rmtree, path)
        else:
            await _checkpoint(ctx, path)
            await asyncio.to_thread(path.unlink)
        return ToolOutput(content=f"Deleted {args['path']}.")


class MovePathTool(Tool):
    name = "move_path"
    description = "Move or rename a file/directory. Checkpointed before moving."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Source path"},
            "target": {"type": "string", "description": "Destination path"},
        },
        "required": ["path", "target"],
    }
    mutating = True
    category = "filesystem"

    async def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        source = resolve_in_root(ctx.workspace_root, args["path"])
        target = resolve_in_root(ctx.workspace_root, args["target"])
        if not source.exists():
            raise ToolError(f"Source not found: {args['path']}")
        files = [source] if source.is_file() else list(_walk_files(source))[:200]
        await _checkpoint(ctx, *files)
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.move, str(source), str(target))
        return ToolOutput(content=f"Moved {args['path']} -> {args['target']}.")


class ListDirTool(Tool):
    name = "list_dir"
    description = (
        "List a directory as an indented tree (depth-limited, ignores "
        "dependency/build directories).")
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory (default: workspace root)"},
            "depth": {"type": "integer", "default": 2},
        },
    }
    read_only = True
    category = "filesystem"

    async def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        rel = args.get("path") or "."
        path = resolve_in_root(ctx.workspace_root, rel, must_exist=True)
        if not path.is_dir():
            raise ToolError(f"{rel} is not a directory.")
        depth = max(1, min(int(args.get("depth") or 2), 6))
        lines: list[str] = [f"{_relative(path, ctx.workspace_root) or '.'}/"]
        shown = 0

        def walk(directory: Path, level: int, prefix: str) -> None:
            nonlocal shown
            if level > depth or shown >= _MAX_TREE_ENTRIES:
                return
            try:
                entries = sorted(
                    directory.iterdir(),
                    key=lambda item: (item.is_file(), item.name.lower()))
            except PermissionError:
                lines.append(prefix + "[permission denied]")
                return
            for entry in entries:
                if entry.name in IGNORE_DIRS:
                    continue
                if shown >= _MAX_TREE_ENTRIES:
                    lines.append(prefix + "...")
                    return
                shown += 1
                marker = "/" if entry.is_dir() else ""
                lines.append(f"{prefix}{entry.name}{marker}")
                if entry.is_dir():
                    walk(entry, level + 1, prefix + "  ")

        await asyncio.to_thread(walk, path, 1, "  ")
        note = "" if shown < _MAX_TREE_ENTRIES else \
            f"\n[list truncated at {_MAX_TREE_ENTRIES} entries]"
        return ToolOutput(content="\n".join(lines) + note)


class GlobTool(Tool):
    name = "glob"
    description = "Find files by glob pattern (e.g. '**/*.py', 'src/**/*.ts')."
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string", "description": "Base directory (default: root)"},
        },
        "required": ["pattern"],
    }
    read_only = True
    category = "filesystem"

    async def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        base = resolve_in_root(ctx.workspace_root, args.get("path") or ".")
        pattern = args["pattern"]
        if ".." in pattern.split("/"):
            raise ToolError("Glob patterns must not traverse upward.")

        def _glob() -> list[Path]:
            return sorted(p for p in base.glob(pattern) if _path_kept(p))

        matches = await asyncio.to_thread(_glob)
        root = ctx.workspace_root
        inside = [m for m in matches if _under(m, root)]
        truncated = len(inside) > _MAX_GLOB_RESULTS
        listing = "\n".join(
            f"{'d' if m.is_dir() else '-'} {_relative(m, root)}"
            for m in inside[:_MAX_GLOB_RESULTS])
        if not listing:
            listing = "(no matches)"
        if truncated:
            listing += f"\n[truncated at {_MAX_GLOB_RESULTS} matches]"
        return ToolOutput(content=listing)

    @staticmethod
    def _kept(path: Path) -> bool:  # retained for compatibility
        return _path_kept(path)


def _path_kept(path: Path) -> bool:
    """Skip files whose path passes through an ignored directory."""
    return not any(part in IGNORE_DIRS for part in path.parts)


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except ValueError:
        return False


def _walk_files(root: Path) -> list[Path]:
    collected: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
        for filename in filenames:
            collected.append(Path(dirpath) / filename)
        if len(collected) > 5000:
            break
    return collected


class SearchFilesTool(Tool):
    name = "search_files"
    description = (
        "Search file contents for a pattern (literal text or regular "
        "expression) and return file:line:match entries. Skips dependency "
        "and build directories.")
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "is_regex": {"type": "boolean", "default": False},
            "include": {"type": "string",
                        "description": "Filename glob filter, e.g. '*.py'"},
            "path": {"type": "string", "description": "Search root (default: root)"},
            "max_results": {"type": "integer", "default": 100},
        },
        "required": ["pattern"],
    }
    read_only = True
    category = "filesystem"
    timeout_s = 60

    async def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        base = resolve_in_root(ctx.workspace_root, args.get("path") or ".")
        pattern = args["pattern"]
        is_regex = bool(args.get("is_regex"))
        include = args.get("include")
        max_results = min(int(args.get("max_results") or 100), _MAX_SEARCH_RESULTS)
        try:
            regex = re.compile(pattern if is_regex else re.escape(pattern))
        except re.error as exc:
            raise ToolError(f"Invalid regex: {exc}") from exc

        def _search() -> list[tuple[str, int, str]]:
            hits: list[tuple[str, int, str]] = []
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
                for filename in sorted(filenames):
                    if include and not fnmatch.fnmatch(filename, include):
                        continue
                    full = Path(dirpath) / filename
                    try:
                        if full.stat().st_size > _MAX_FILE_BYTES:
                            continue
                        data = full.read_bytes()
                        if b"\x00" in data[:4096]:
                            continue
                        text = data.decode("utf-8", errors="replace")
                    except OSError:
                        continue
                    for line_no, line in enumerate(text.splitlines(), start=1):
                        if regex.search(line):
                            hits.append((
                                _relative(full, ctx.workspace_root), line_no,
                                line.strip()[:240]))
                            if len(hits) >= max_results:
                                return hits
            return hits

        hits = await asyncio.to_thread(_search)
        if not hits:
            return ToolOutput(content=f"No matches for pattern.")
        body = "\n".join(f"{file}:{line}: {text}" for file, line, text in hits)
        capped = "" if len(hits) < max_results else \
            f"\n[stopped at {max_results} matches]"
        files = sorted({hit[0] for hit in hits})
        return ToolOutput(
            content=f"{len(hits)} match(es) in {len(files)} file(s):\n{body}{capped}",
            metadata={"files": files[:50]})


class FileInfoTool(Tool):
    name = "file_info"
    description = ("Show metadata for a path: size, type, modification time, "
                   "line count for text files.")
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }
    read_only = True
    category = "filesystem"

    async def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        path = resolve_in_root(ctx.workspace_root, args["path"])
        if not path.exists():
            raise ToolError(f"Not found: {args['path']}")
        stat_result = path.stat()
        kind = "directory" if path.is_dir() else (
            "symlink" if path.is_symlink() else "file")
        lines = [
            f"path: {_relative(path, ctx.workspace_root)}",
            f"type: {kind}",
            f"size: {stat_result.st_size} bytes",
            f"modified: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stat_result.st_mtime))}",
            f"permissions: {stat.filemode(stat_result.st_mode)}",
        ]
        if path.is_file():
            try:
                data = path.read_bytes()
                if b"\x00" not in data[:4096]:
                    lines.append(f"lines: {len(data.decode('utf-8', 'replace').splitlines())}")
                else:
                    lines.append("content: binary")
            except OSError:
                pass
        return ToolOutput(content="\n".join(lines))


class ApplyPatchTool(Tool):
    name = "apply_patch"
    description = (
        "Apply a unified diff to a file with fuzzy context matching. "
        "Checkpoints the file first. Use edit_file for simple replacements.")
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "diff": {"type": "string", "description": "Unified diff text"},
        },
        "required": ["path", "diff"],
    }
    mutating = True
    category = "filesystem"

    async def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        path = resolve_in_root(ctx.workspace_root, args["path"])
        if not path.is_file():
            raise ToolError(f"File not found: {args['path']}")
        original = await asyncio.to_thread(path.read_text, "utf-8")
        _, hunks = parse_unified_diff(args["diff"])
        if not hunks:
            raise ToolError("The diff contains no hunks (@@ sections).")
        updated = apply_hunks(original, hunks)
        await _checkpoint(ctx, path)
        await asyncio.to_thread(path.write_text, updated, "utf-8")
        return ToolOutput(
            content=f"Applied patch to {args['path']}.",
            display=unified_diff_text(original, updated, args['path']),
            metadata={"path": args["path"]})


class MakeDiffTool(Tool):
    name = "make_diff"
    description = ("Compare a file's current content with new content and "
                   "return the unified diff (no changes are written).")
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "new_content": {"type": "string"},
        },
        "required": ["path", "new_content"],
    }
    read_only = True
    category = "filesystem"

    async def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        path = resolve_in_root(ctx.workspace_root, args["path"])
        if not path.is_file():
            raise ToolError(f"File not found: {args['path']}")
        original = await asyncio.to_thread(path.read_text, "utf-8")
        diff = unified_diff_text(original, args["new_content"], args["path"])
        if not diff:
            return ToolOutput(content="No differences.")
        return ToolOutput(content=diff)


class ProjectMapTool(Tool):
    name = "project_map"
    description = (
        "Show or refresh the project index: structure summary, languages, "
        "dependencies, entrypoints, build and test commands.")
    parameters = {
        "type": "object",
        "properties": {
            "refresh": {"type": "boolean", "default": False,
                        "description": "Re-scan the workspace first"},
            "detail": {"type": "string", "enum": ["summary", "files"],
                       "default": "summary"},
        },
    }
    read_only = True
    category = "filesystem"

    async def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        from ..project import ProjectIndex

        index = ProjectIndex(ctx.workspace_root, config=ctx.config)
        if args.get("refresh"):
            await asyncio.to_thread(index.build, True)
        else:
            await asyncio.to_thread(index.build, False)
        if args.get("detail") == "files":
            return ToolOutput(content=index.files_listing())
        return ToolOutput(content=index.summary())


def register_filesystem_tools(registry) -> None:
    for tool in (
        ReadFileTool(), WriteFileTool(), EditFileTool(), CreateDirTool(),
        DeletePathTool(), MovePathTool(), ListDirTool(), GlobTool(),
        SearchFilesTool(), FileInfoTool(), ApplyPatchTool(), MakeDiffTool(),
        ProjectMapTool(),
    ):
        registry.register(tool)
