"""Unified diffs: preview at decision time, display after the edit
(v0.9.0 §2.5).

One computation, two renderings. The preview a permission ASK shows
and the display a tool reports afterwards are the SAME unified diff
of the same (original, updated) pair — the preview just computes the
updated content in memory without writing anything. Both renderings
are truncated by the same budget (default 200 lines / ~8k tokens,
configurable via ``ui.diff_max_lines`` / ``ui.diff_max_tokens``) with
a "… N more lines" marker, so the prompt shows exactly what the edit
will report.

Token estimate: ~4 chars per token (documented approximation, used
only for the display budget — never for billing or context windows).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .log import get_logger

_logger = get_logger("diffs")

DEFAULT_DIFF_MAX_LINES = 200
DEFAULT_DIFF_MAX_TOKENS = 8000
_CHARS_PER_TOKEN = 4
_MORE_LINES = "… {n} more line{s} (diff truncated to the display budget)"


def diff_budget_from_config(config: Any | None) -> tuple[int, int]:
    """(max_lines, max_tokens) from config, with the defaults."""
    if config is None:
        return DEFAULT_DIFF_MAX_LINES, DEFAULT_DIFF_MAX_TOKENS

    def _int(key: str, default: int) -> int:
        try:
            value = int(config.get(key, default))
        except (TypeError, ValueError):
            return default
        return max(1, value)

    return (_int("ui.diff_max_lines", DEFAULT_DIFF_MAX_LINES),
            _int("ui.diff_max_tokens", DEFAULT_DIFF_MAX_TOKENS))


def unified_diff(old: str, new: str, path: str) -> str:
    """The single unified-diff computation (tools/patches.py)."""
    # Lazy import: tools.patches is imported by the tool registry,
    # and this module is imported by the runtime and the renderer —
    # importing it at module level would risk a cycle.
    from .tools.patches import unified_diff_text

    return unified_diff_text(old, new, path)


def truncate_diff(diff: str, *, max_lines: int = DEFAULT_DIFF_MAX_LINES,
                  max_tokens: int = DEFAULT_DIFF_MAX_TOKENS) -> str:
    """Truncate a diff to the display budget, with a marker naming
    how many lines were cut. Within budget, the diff is unchanged."""
    if not diff:
        return diff
    lines = diff.splitlines()
    max_chars = max_tokens * _CHARS_PER_TOKEN
    if len(lines) <= max_lines and len(diff) <= max_chars:
        return diff
    kept = lines[:max_lines]
    text = "\n".join(kept)
    if len(text) > max_chars:
        # Cut at a line boundary inside the char budget.
        cut = text[:max_chars].rsplit("\n", 1)[0]
        kept = cut.splitlines()
        text = cut
    remaining = len(lines) - len(kept)
    marker = _MORE_LINES.format(
        n=remaining, s="s" if remaining != 1 else "")
    return text + ("\n" if text else "") + marker


def preview_edit_diff(workspace: Path | str, tool_name: str,
                      args: dict[str, Any]) -> str | None:
    """The unified diff a file edit WOULD produce, computed without
    mutating anything (read-only: the file is read, the new content
    is built in memory, nothing is written).

    Covers the file-edit tools: edit_file, write_file, apply_patch.
    Returns None when the tool is not a file edit, the file cannot be
    read, or the edit cannot be computed (e.g. old_text not found —
    the tool itself will fail after approval, and the panel shows no
    diff rather than a wrong one).
    """
    from .security import resolve_in_root

    if tool_name not in ("edit_file", "write_file", "apply_patch"):
        return None
    path_arg = args.get("path")
    if not isinstance(path_arg, str) or not path_arg:
        return None
    try:
        path = resolve_in_root(Path(workspace).resolve(), path_arg)
    except Exception:  # noqa: BLE001 - unresolvable path: no preview
        return None
    try:
        original = path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError:
        return None
    if path.is_dir():
        return None
    try:
        if tool_name == "edit_file":
            old_text = args.get("old_text")
            new_text = args.get("new_text")
            if not isinstance(old_text, str) or not isinstance(new_text, str):
                return None
            if old_text not in original:
                return None
            updated = (original.replace(old_text, new_text)
                       if args.get("replace_all")
                       else original.replace(old_text, new_text, 1))
        elif tool_name == "write_file":
            content = args.get("content")
            if not isinstance(content, str):
                return None
            updated = content
        else:  # apply_patch
            from .tools.patches import apply_hunks, parse_unified_diff

            diff_text = args.get("diff")
            if not isinstance(diff_text, str):
                return None
            _, hunks = parse_unified_diff(diff_text)
            if not hunks:
                return None
            updated = apply_hunks(original, hunks)
    except Exception as exc:  # noqa: BLE001 - a preview that cannot be
        # computed is None, never a crash in the approval path.
        _logger.debug("diff preview failed for %s: %s", tool_name, exc)
        return None
    if updated == original:
        return None
    return unified_diff(original, updated, path_arg)
