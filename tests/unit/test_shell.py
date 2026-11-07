"""Shell output stays bounded without prematurely stopping commands."""

import asyncio
import os
import shlex
import sys
from pathlib import Path

from daemon.tools import shell


def test_large_output_is_drained_and_live_stream_is_abbreviated(monkeypatch):
    monkeypatch.setattr(shell, "_MAX_OUTPUT_CHARS", 100)
    monkeypatch.setattr(shell, "_MAX_LIVE_OUTPUT_LINES", 3)
    lines: list[str] = []
    code = (
        "import sys; "
        "[print('row-' + str(i) + '-' + 'x' * 20) for i in range(20)]; "
        "print('TAIL-MARKER')"
    )
    if os.name == "nt":
        command = f"& '{sys.executable}' -c \"{code}\""
    else:
        command = f"{shlex.quote(sys.executable)} -c \"{code}\""

    async def on_line(line: str) -> None:
        lines.append(line)

    async def go():
        return await shell.run_shell(
            command, cwd=Path.cwd(), on_line=on_line)

    exit_code, output, timed_out = asyncio.run(go())

    assert exit_code == 0
    assert not timed_out
    assert "row-0-" in output
    assert "TAIL-MARKER" in output
    assert "characters across approximately" in output
    assert len(lines) == 4
    assert "live output abbreviated" in lines[-1]
