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
from collections import deque
from pathlib import Path

from ..errors import ToolError
from ..security import resolve_in_root, strip_secret_env
from .base import Tool, ToolContext, ToolOutput

_MAX_OUTPUT_CHARS = 100_000
_MAX_LIVE_OUTPUT_LINES = 100
_MAX_LIVE_LINE_CHARS = 500
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
    Retained output is bounded, but oversized output is drained to completion
    rather than killing a useful command. Live output is capped separately.
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
    tail_parts: deque[str] = deque()
    tail_size = 0
    size = 0
    total_size = 0
    total_lines = 0
    live_output_abbreviated = False

    async def _pump() -> None:
        nonlocal size, total_size, total_lines, tail_size
        nonlocal live_output_abbreviated
        assert process.stdout is not None
        async for raw in process.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            chunk = line + "\n"
            total_lines += 1
            total_size += len(chunk)
            if size < _MAX_OUTPUT_CHARS:
                retained = chunk[:_MAX_OUTPUT_CHARS - size]
                collected.append(retained)
                size += len(retained)
            tail_limit = max(1, _MAX_OUTPUT_CHARS // 5)
            tail_chunk = chunk[-tail_limit:]
            tail_parts.append(tail_chunk)
            tail_size += len(tail_chunk)
            while tail_size > tail_limit:
                excess = tail_size - tail_limit
                first = tail_parts.popleft()
                if len(first) > excess:
                    tail_parts.appendleft(first[excess:])
                    tail_size -= excess
                else:
                    tail_size -= len(first)
            if on_line is not None:
                if total_lines <= _MAX_LIVE_OUTPUT_LINES:
                    preview = line[:_MAX_LIVE_LINE_CHARS]
                    if len(line) > _MAX_LIVE_LINE_CHARS:
                        preview += " [... line abbreviated]"
                    await on_line(preview)
                elif not live_output_abbreviated:
                    await on_line(
                        f"[live output abbreviated after "
                        f"{_MAX_LIVE_OUTPUT_LINES} lines; command continues]")
                    live_output_abbreviated = True

    timed_out = False
    try:
        pump = asyncio.create_task(_pump())
        await asyncio.wait_for(asyncio.shield(_wait(process)), timeout_s)
    except TimeoutError:
        timed_out = True
        _kill(process)
    finally:
        if not pump.done():
            await asyncio.wait_for(asyncio.shield(pump), 5)
    output = "".join(collected)
    truncated = total_size > _MAX_OUTPUT_CHARS
    if truncated:
        head_limit = int(_MAX_OUTPUT_CHARS * 0.7)
        head = output[:head_limit]
        tail = "".join(tail_parts)
        omitted_chars = max(0, total_size - len(head) - len(tail))
        omitted_lines = max(0, total_lines - head.count("\n") - tail.count("\n"))
        output = (
            head
            + f"\n[... {omitted_chars} characters across approximately "
            f"{omitted_lines} lines omitted; command completed ...]\n"
            + tail
        )
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
