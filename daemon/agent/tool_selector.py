"""Task-aware tool pruning: advertise only the tools a turn is likely to use.

Every tool definition is sent with every request, so unused tools are paid for
on each step. This selector keeps the small core loop (read, edit, search,
shell) plus whatever the current task and this session's usage make relevant,
and leaves the rest out of the tool list for that request.

Pruning is advertisement-only: the registry still holds every tool, so a call
to a pruned name still executes normally. Security-critical and core tools are
never hidden, and tools already used in the session are always kept because
the model knows about them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import Config
from ..tools.base import Tool
from .context import estimate_tokens

_WORD = re.compile(r"[a-z0-9_]+")

# Filler words that carry no signal about which tool a task needs.
_STOPWORDS = frozenset("""
a about after all also an and any are as at be been before build but by can
change code could create did do does doing done edit file files find fix for
from get got has have how i if in into is it its just keep like look make me
my need new not now of on once one only or other our out please run same
should so some that the their them then there these they this those to up us
use used using very want was we were what when where which while who why will
with work would you your
""".split())

# The always-available coding loop. Hiding any of these would cost more in
# failed or blind steps than the tokens it saves.
_CORE_TOOLS = (
    "read_file", "write_file", "edit_file", "apply_patch",
    "list_dir", "glob", "search_files", "file_info",
    "run_command", "run_tests", "git_status", "git_diff",
    "compact_context", "memory_note",
)


def _words(text: str | None) -> set[str]:
    if not text:
        return set()
    found = _WORD.findall(text.lower())
    return {word for word in found if len(word) > 2 and word not in _STOPWORDS}


def _tool_words(tool: Tool) -> set[str]:
    name_words = set(tool.name.split("_"))
    return name_words | _words(tool.description) | _words(tool.category)


def _tool_tokens(tool: Tool) -> int:
    """Token cost of advertising one tool (same accounting as
    ``context.conversation_tokens``)."""
    return (estimate_tokens(tool.description)
            + estimate_tokens(tool.name) + 60)


@dataclass
class ToolSelectionStats:
    """What pruning saved for one selection pass."""

    original_count: int = 0
    selected_count: int = 0
    original_tokens: int = 0
    selected_tokens: int = 0
    tokens_saved: int = 0


class ToolSelector:
    """Picks the tool subset to advertise for the current task."""

    def __init__(self, config: Config | None = None) -> None:
        global_enabled = config.get("optimization.enabled", True) if config else True
        tools_enabled = (config.get("optimization.tools.enabled", True)
                         if config else True)
        self.enabled = bool(global_enabled) and bool(tools_enabled)
        self.min_score = float(
            config.get("optimization.tools.min_score", 0.15) if config else 0.15)
        self.max_tools = int(
            config.get("optimization.tools.max_tools", 20) if config else 20)
        configured = (config.get("optimization.tools.always_include", None)
                      if config else None)
        self.core = {str(name) for name in configured} if configured \
            else set(_CORE_TOOLS)
        self._used: set[str] = set()
        self.last_stats = ToolSelectionStats()

    # ------------------------------------------------------------- usage

    def record_use(self, name: str) -> None:
        """Remember a tool the model actually called in this session."""
        self._used.add(name)

    def reset(self) -> None:
        self._used.clear()
        self.last_stats = ToolSelectionStats()

    # ----------------------------------------------------------- selection

    def select(self, task: str, tools: list[Tool]) -> list[Tool]:
        """Return the tools to advertise for ``task`` (registry order kept)."""
        original_tokens = sum(_tool_tokens(tool) for tool in tools)
        if not self.enabled or len(tools) <= self.max_tools:
            self.last_stats = ToolSelectionStats(
                original_count=len(tools), selected_count=len(tools),
                original_tokens=original_tokens,
                selected_tokens=original_tokens)
            return list(tools)

        terms = _words(task)
        chosen = {tool.name for tool in tools
                  if tool.name in self.core or tool.name in self._used}

        ranked = sorted(
            ((self._score(tool, terms), tool) for tool in tools
             if tool.name not in chosen),
            key=lambda pair: (-pair[0], pair[1].name))
        for score, tool in ranked:
            if len(chosen) >= self.max_tools:
                break
            if score >= self.min_score:
                chosen.add(tool.name)

        selected = [tool for tool in tools if tool.name in chosen]
        selected_tokens = sum(_tool_tokens(tool) for tool in selected)
        self.last_stats = ToolSelectionStats(
            original_count=len(tools), selected_count=len(selected),
            original_tokens=original_tokens, selected_tokens=selected_tokens,
            tokens_saved=max(0, original_tokens - selected_tokens))
        return selected

    def _score(self, tool: Tool, task_terms: set[str]) -> float:
        """0..1 keyword overlap between the task and the tool's vocabulary."""
        if not task_terms:
            return 0.0
        overlap = len(task_terms & _tool_words(tool))
        return overlap / len(task_terms)
