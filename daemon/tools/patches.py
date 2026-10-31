"""Unified diff parsing and application with fuzzy context matching.

Used by the ``apply_patch`` tool, the /diff REPL command and the checkpoint
restore path. Pure stdlib (``difflib``) - no external patch dependency, and
deterministic enough to unit test heavily.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from ..errors import ToolError

_HUNK_HEADER = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[tuple[str, str]] = field(default_factory=list)  # (" ",+,-) text


def unified_diff_text(old: str, new: str, path: str = "a/file",
                      new_path: str | None = None) -> str:
    """Produce a unified diff string between two file contents."""
    diff = difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=path, tofile=new_path or path, n=3)
    return "".join(diff)


def parse_unified_diff(diff_text: str) -> tuple[str | None, list[Hunk]]:
    """Parse hunks from a unified diff; returns (new_path_hint, hunks)."""
    new_path: str | None = None
    hunks: list[Hunk] = []
    current: Hunk | None = None
    for raw_line in diff_text.splitlines():
        if raw_line.startswith("+++ "):
            candidate = raw_line[4:].strip()
            candidate = re.sub(r"^\w/", "", candidate)  # strip b/ prefix
            new_path = candidate or new_path
            continue
        if raw_line.startswith(("--- ", "diff ", "index ", "\\ ",
                                "new file mode", "deleted file mode",
                                "old mode", "new mode", "similarity index",
                                "rename from", "rename to")):
            continue
        match = _HUNK_HEADER.match(raw_line)
        if match:
            current = Hunk(
                old_start=int(match.group(1)),
                old_count=int(match.group(2) or 1),
                new_start=int(match.group(3)),
                new_count=int(match.group(4) or 1),
            )
            hunks.append(current)
            continue
        if current is not None:
            if raw_line.startswith("+"):
                current.lines.append(("+", raw_line[1:]))
            elif raw_line.startswith("-"):
                current.lines.append(("-", raw_line[1:]))
            elif raw_line.startswith(" ") or raw_line == "":
                current.lines.append((" ", raw_line[1:] if raw_line else ""))
            elif raw_line.startswith("\\"):  # "\ No newline at end of file"
                continue
            else:
                # Ambiguous context line: treat as context.
                current.lines.append((" ", raw_line))
    return new_path, hunks


def raw_labelled(line: str) -> bool:
    return line.startswith(("--- ", "diff ", "index ", "\\ "))


def apply_hunks(text: str, hunks: list[Hunk], *, fuzzy: bool = True) -> str:
    """Apply parsed hunks to ``text``. Raises ToolError when a hunk fails.

    Hunk line numbers refer to the ORIGINAL file; ``delta`` tracks the
    cumulative shift introduced by previously applied hunks. Splitting on
    newline characters (rather than splitlines) keeps the trailing-newline
    state exact.
    """
    lines = text.split("\n")
    delta = 0
    for index, hunk in enumerate(hunks, start=1):
        old_block = [line for tag, line in hunk.lines if tag in (" ", "-")]
        target = hunk.old_start - 1 + delta
        position = _find_position(lines, old_block, target, fuzzy)
        if position is None:
            raise ToolError(
                f"Hunk #{index} does not match the file content "
                f"(expected near line {hunk.old_start}). The file may have "
                f"changed since the diff was produced.")
        lines = (lines[:position] + _new_lines(hunk) +
                 lines[position + len(old_block):])
        delta += len(_new_lines(hunk)) - len(old_block)
    return "\n".join(lines)


def _new_lines(hunk: Hunk) -> list[str]:
    return [line for tag, line in hunk.lines if tag in (" ", "+")]


def _find_position(lines: list[str], old_block: list[str], target: int,
                   fuzzy: bool) -> int | None:
    if _matches_at(lines, target, old_block):
        return target
    if not old_block:
        return max(0, min(target, len(lines)))
    if fuzzy:
        for delta in range(1, len(lines) + 1):
            for candidate in (target - delta, target + delta):
                if 0 <= candidate <= len(lines) - len(old_block) and \
                        _matches_at(lines, candidate, old_block):
                    return candidate
    return None


def _matches_at(lines: list[str], position: int, block: list[str]) -> bool:
    if position < 0 or position + len(block) > len(lines):
        return False
    return lines[position:position + len(block)] == block


def count_changes(hunks: list[Hunk]) -> tuple[int, int]:
    added = sum(1 for hunk in hunks for tag, _ in hunk.lines if tag == "+")
    removed = sum(1 for hunk in hunks for tag, _ in hunk.lines if tag == "-")
    return added, removed
