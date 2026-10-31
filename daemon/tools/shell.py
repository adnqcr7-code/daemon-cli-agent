"""Shell execution tool: cross-platform, time-bounded, cancellable.

Commands run in the workspace directory with credential-carrying environment
variables stripped by default. Output streams live to the UI and is capped
so a runaway command cannot destroy the context window. On POSIX the shell
is bash when available (else sh); on Windows PowerShell is used.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

from ..errors import ToolError
from ..security import resolve_in_root, strip_secret_env
from .base import Tool, ToolContext, ToolOutput

_MAX_OUTPUT_CHARS = 100_000
_WINDOWS_SHELLS = (("pwsh", "-NoProfile", "-Command"),
                   ("powershell", "-NoProfile", "-Command"))


def shell_argv() -> list[str]:
    """Platform shell used to interpret command strings."""
    if sys.platform.startswith("win"):
        for executable, *rest in _WINDOWS_SHELLS:
            if Path(executable).exists() or _which(executable):
                return [executable, *rest]
        return ["cmd", "/C"]
    return ["/bin/bash", "-c"] if Path("/bin/bash").exists() else ["/bin/sh", "-c"]


def _which(executable: str) -> str | None:
    from shutil import which

    return which(executable)


async def run_shell(
    command: str,
    *,
    cwd: Path,
    timeout_s: float = 120,
    env_extra: dict[str, str] | None = None,
    on_line=None,
    combined: bool = True,
) -> tuple[int, str, bool]:
    """Run a command; returns (exit_code, output, timed_out).

    Shared by the shell tool, the test runner, formatters and doctor.
    """
    argv = shell_argv() + [command]
    env = strip_secret_env(None)
    if env_extra:
        env.update(env_extra)
    process = await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd), env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT if combined
        else asyncio.subprocess.DEVNULL,
        stdin=asyncio.subprocess.DEVNULL,
        start_new_session=(not sys.platform.startswith("win")),
    )
    collected: list[str] = []
    size = 0
    truncated = False

    async def _pump() -> None:
        nonlocal size, truncated
        assert process.stdout is not None
        async for raw in process.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if size + len(line) > _MAX_OUTPUT_CHARS:
                remainder = _MAX_OUTPUT_CHARS - size
                if remainder > 0:
                    collected.append(line[:remainder])
                    size += remainder
                truncated = True
                break
            collected.append(line)
            size += len(line) + 1
            if on_line is not None:
                await on_line(line)
        if truncated:
            _kill(process)

    timed_out = False
    try:
        pump = asyncio.create_task(_pump())
        await asyncio.wait_for(asyncio.shield(_wait(process)), timeout_s)
    except asyncio.TimeoutError:
        timed_out = True
        _kill(process)
    finally:
        if not pump.done():
            await asyncio.wait_for(asyncio.shield(pump), 5)
    output = "\n".join(collected)
    if truncated:
        output += "\n[output truncated]"
    if timed_out:
        output += f"\n[command timed out after {int(timeout_s)}s and was killed]"
    exit_code = process.returncode if process.returncode is not None else -1
    return exit_code, output, timed_out


async def _wait(process: asyncio.subprocess.Process) -> None:
    await process.wait()


def _kill(process: asyncio.subprocess.Process) -> None:
    try:
        if sys.platform.startswith("win"):
            process.kill()
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except ProcessLookupError:
            pass


class RunCommandTool(Tool):
    name = "run_command"
    description = (
        "Run a shell command inside the workspace and return its output and "
        "exit code. Credential environment variables are stripped. Suitable "
        " for builds, tests, linters and inspections; avoid interactive "
        "commands.")
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The command line to run"},
            "cwd": {"type": "string", "description": "Working dir (default: workspace root)"},
            "timeout_s": {"type": "integer", "description": "Timeout in seconds"},
        },
        "required": ["command"],
    }
    mutating = True
    category = "terminal"
    timeout_s = 300

    async def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        command = args["command"].strip()
        if not command:
            raise ToolError("Empty command.")
        cwd = resolve_in_root(ctx.workspace_root, args.get("cwd") or ".")
        if not cwd.exists():
            raise ToolError(f"Working directory does not exist: {args.get('cwd')}")
        timeout = min(int(args.get("timeout_s") or 300), 900)

        async def on_line(line: str) -> None:
            await ctx.emit(self.name, line)

        exit_code, output, timed_out = await run_shell(
            command, cwd=cwd, timeout_s=timeout, on_line=on_line)
        verdict = "timed out" if timed_out else f"exit code {exit_code}"
        content = f"$ {command}\n{output}\n[{verdict}]"
        if timed_out or exit_code != 0:
            return ToolOutput(content=content, is_error=timed_out or exit_code != 0,
                              metadata={"exit_code": exit_code})
        return ToolOutput(content=content, metadata={"exit_code": exit_code})


def register_shell_tools(registry) -> None:
    registry.register(RunCommandTool())
