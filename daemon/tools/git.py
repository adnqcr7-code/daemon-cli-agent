"""Git tools: status, diff, log, branches, commits, inspection.

Destructive operations (reset --hard, force push, branch -D, rebase) are
deliberately NOT exposed as agent tools - the user performs those manually.
Rollback safety comes from Daemon's own file-level checkpoints, which work
with or without a repository.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..errors import ToolError
from ..log import get_logger
from .base import Tool, ToolContext, ToolOutput
from .patches import unified_diff_text

_logger = get_logger("tools.git")
_MAX_DIFF_CHARS = 120_000


async def git(*args: str, cwd: Path, timeout_s: float = 60) -> tuple[int, str]:
    """Run a git subcommand; returns (exit_code, output)."""
    process = await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        raw, _ = await asyncio.wait_for(process.communicate(), timeout_s)
    except asyncio.TimeoutError:
        process.kill()
        raise ToolError(f"git {' '.join(args)} timed out")
    return process.returncode or 0, raw.decode("utf-8", errors="replace")


async def _require_repo(ctx: ToolContext) -> None:
    code, out = await git("rev-parse", "--is-inside-work-tree", cwd=ctx.workspace_root)
    if code != 0 or out.strip() != "true":
        raise ToolError(
            f"Not a git repository: {ctx.workspace_root}. "
            f"Run 'git init' or open a repository.")


def _bound(text: str, limit: int = _MAX_DIFF_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[truncated at {limit} chars]"


class GitStatusTool(Tool):
    name = "git_status"
    description = "Show branch and working-tree status (porcelain + branch)."
    parameters = {"type": "object", "properties": {}}
    read_only = True
    category = "git"
    timeout_s = 30

    async def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        await _require_repo(ctx)
        code, out = await git("status", "--porcelain=v1", "-b",
                              cwd=ctx.workspace_root)
        if code != 0:
            raise ToolError(f"git status failed: {out.strip()}")
        if not out.strip():
            return ToolOutput(content="Working tree clean.")
        return ToolOutput(content=out.strip())


class GitDiffTool(Tool):
    name = "git_diff"
    description = (
        "Show changes. Without arguments: unstaged working-tree diff. "
        "With ref: diff between ref and the working tree. Add --stat for "
        "a compact summary.")
    parameters = {
        "type": "object",
        "properties": {
            "ref": {"type": "string", "description": "Commit/ref to compare against"},
            "cached": {"type": "boolean", "default": False,
                       "description": "Show staged changes instead"},
            "paths": {"type": "array", "items": {"type": "string"}},
            "stat": {"type": "boolean", "default": False},
        },
    }
    read_only = True
    category = "git"
    timeout_s = 60

    async def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        await _require_repo(ctx)
        argv = ["diff", "--no-color"]
        if args.get("stat"):
            argv.append("--stat")
        if args.get("cached"):
            argv.append("--cached")
        if args.get("ref"):
            argv.append(str(args["ref"]))
        if args.get("paths"):
            argv.append("--")
            argv.extend(str(path) for path in args["paths"])
        code, out = await git(*argv, cwd=ctx.workspace_root)
        if code != 0:
            raise ToolError(f"git diff failed: {out.strip()}")
        return ToolOutput(content=_bound(out.strip()) or "No differences.")


class GitLogTool(Tool):
    name = "git_log"
    description = "Show recent commit history (oneline)."
    parameters = {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "default": 20},
            "path": {"type": "string", "description": "Limit to a path"},
        },
    }
    read_only = True
    category = "git"
    timeout_s = 30

    async def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        await _require_repo(ctx)
        argv = ["log", "--oneline", f"-n{min(int(args.get('limit') or 20), 200)}"]
        if args.get("path"):
            argv += ["--", str(args["path"])]
        code, out = await git(*argv, cwd=ctx.workspace_root)
        if code != 0:
            raise ToolError(f"git log failed: {out.strip()}")
        return ToolOutput(content=out.strip() or "No commits yet.")


class GitBranchesTool(Tool):
    name = "git_branches"
    description = "List local and remote branches."
    parameters = {"type": "object", "properties": {}}
    read_only = True
    category = "git"
    timeout_s = 30

    async def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        await _require_repo(ctx)
        code, out = await git("branch", "-a", "--format=%(refname:short) %(objectname:short)",
                              cwd=ctx.workspace_root)
        if code != 0:
            raise ToolError(f"git branch failed: {out.strip()}")
        return ToolOutput(content=out.strip() or "No branches.")


class GitCreateBranchTool(Tool):
    name = "git_create_branch"
    description = "Create a new branch (does not switch to it)."
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "from_ref": {"type": "string", "description": "Base ref (default: HEAD)"},
        },
        "required": ["name"],
    }
    category = "git"
    mutating = True
    timeout_s = 30

    async def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        await _require_repo(ctx)
        argv = ["branch", str(args["name"])]
        if args.get("from_ref"):
            argv.append(str(args["from_ref"]))
        code, out = await git(*argv, cwd=ctx.workspace_root)
        if code != 0:
            raise ToolError(f"git branch failed: {out.strip()}")
        return ToolOutput(content=f"Created branch {args['name']}.")


class GitCommitTool(Tool):
    name = "git_commit"
    description = (
        "Stage the given paths (or all changes with add_all) and create a "
        "commit. Requires approval by default.")
    parameters = {
        "type": "object",
        "properties": {
            "message": {"type": "string"},
            "paths": {"type": "array", "items": {"type": "string"},
                      "description": "Paths to stage (default: all tracked changes)"},
            "add_all": {"type": "boolean", "default": False,
                        "description": "Stage all changes including new files"},
        },
        "required": ["message"],
    }
    mutating = True
    category = "git"
    timeout_s = 60

    async def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        await _require_repo(ctx)
        message = args["message"].strip()
        if not message:
            raise ToolError("Commit message must not be empty.")
        if args.get("add_all"):
            code, out = await git("add", "-A", cwd=ctx.workspace_root)
        elif args.get("paths"):
            code, out = await git("add", "--",
                                  *[str(p) for p in args["paths"]],
                                  cwd=ctx.workspace_root)
        else:
            code, out = await git("add", "-u", cwd=ctx.workspace_root)
        if code != 0:
            raise ToolError(f"git add failed: {out.strip()}")
        code, staged = await git("diff", "--cached", "--stat",
                                 cwd=ctx.workspace_root)
        code, out = await git("commit", "-m", message, cwd=ctx.workspace_root)
        if code != 0:
            if "nothing to commit" in out:
                return ToolOutput(content="Nothing to commit.", is_error=True)
            raise ToolError(f"git commit failed: {out.strip()}")
        code, ref = await git("rev-parse", "--short", "HEAD", cwd=ctx.workspace_root)
        return ToolOutput(
            content=f"Committed {ref.strip()}: {message}\n{staged.strip()}",
            metadata={"commit": ref.strip()})


class GitShowTool(Tool):
    name = "git_show"
    description = "Show a commit (message + diff) by ref."
    parameters = {
        "type": "object",
        "properties": {"ref": {"type": "string", "default": "HEAD"}},
    }
    read_only = True
    category = "git"
    timeout_s = 60

    async def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        await _require_repo(ctx)
        code, out = await git("show", "--no-color", str(args.get("ref") or "HEAD"),
                              cwd=ctx.workspace_root)
        if code != 0:
            raise ToolError(f"git show failed: {out.strip()}")
        return ToolOutput(content=_bound(out.strip()))


class GitCheckpointContextTool(Tool):
    """Exposes checkpoint diffs/restore to the model via git-style names."""

    name = "git_rollback"
    description = (
        "Restore workspace files from a Daemon checkpoint taken before an "
        "earlier edit (reversible via /undo in the UI). Requires approval.")
    parameters = {
        "type": "object",
        "properties": {"checkpoint": {"type": "string",
                                      "description": "Checkpoint id (see /diff)"}},
    }
    mutating = True
    category = "git"
    timeout_s = 60

    async def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        if ctx.checkpoints is None:
            raise ToolError("Checkpoints are unavailable in this session.")
        checkpoint_id = args.get("checkpoint") or "latest"
        restored = await ctx.checkpoints.restore(checkpoint_id)
        return ToolOutput(content=f"Restored {restored} file(s) from {checkpoint_id}.")


def register_git_tools(registry) -> None:
    for tool in (GitStatusTool(), GitDiffTool(), GitLogTool(), GitBranchesTool(),
                 GitCreateBranchTool(), GitCommitTool(), GitShowTool(),
                 GitCheckpointContextTool()):
        registry.register(tool)


_ = unified_diff_text  # re-exported for diff rendering helpers
