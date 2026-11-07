"""Failure-loop detection: notice a repeating failing call and intervene.

A coding agent loses most of its wasted tokens by retrying the same failing
command or edit with the same arguments. This detector records failed tool
results, recognizes the same call failing repeatedly inside a short window,
and produces one short recommendation for the next request.

It never terminates a run and never touches permissions: it only adds a nudge
to the system prompt telling the model to change approach. Run output is
untrusted, so the nudge names the tool and the count but never echoes the
error text into the system prompt.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field

from ..config import Config

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_NUMBERS = re.compile(r"\b(?:0x[0-9a-f]+|\d+)\b", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

# Where to point the model when a tool keeps failing.
_TOOL_ALTERNATIVES: dict[str, tuple[str, ...]] = {
    "edit_file": ("apply_patch", "write_file"),
    "apply_patch": ("edit_file", "write_file"),
    "write_file": ("edit_file", "apply_patch"),
    "read_file": ("list_dir", "project_map", "search_files"),
    "list_dir": ("glob", "search_files"),
    "glob": ("search_files", "list_dir"),
    "search_files": ("glob", "find_symbols"),
    "run_command": ("run_tests", "read_file"),
    "run_tests": ("run_command", "read_file"),
    "find_symbols": ("search_files", "references"),
    "references": ("find_symbols", "search_files"),
    "diagnostics": ("run_command", "read_file"),
    "git_commit": ("git_status", "git_diff"),
    "git_diff": ("git_status", "run_command"),
    "web_fetch": ("web_search", "read_file"),
    "web_search": ("web_fetch",),
    "design_get": ("design_search",),
    "design_search": ("design_get",),
}


@dataclass
class FailurePattern:
    """One repeated (tool, error, arguments) failure."""

    tool_name: str
    error_signature: str
    args_fingerprint: str
    count: int = 1
    first_seen: float = field(default_factory=time.monotonic)
    last_seen: float = field(default_factory=time.monotonic)
    last_seq: int = 0
    notified_at: int = 0  # count already surfaced to the model


@dataclass
class FailureReport:
    """Outcome of one analysis pass."""

    detected: bool = False
    patterns: list[FailurePattern] = field(default_factory=list)
    recommendation: str | None = None
    suggested_tools: list[str] = field(default_factory=list)


class FailureLoopDetector:
    """Records tool failures and reports repeated identical ones."""

    def __init__(self, config: Config | None = None) -> None:
        global_enabled = config.get("optimization.enabled", True) if config else True
        self.enabled = bool(global_enabled) and bool(
            config.get("optimization.failure_detection.enabled", True)
            if config else True)
        self.max_failures = int(
            config.get("optimization.failure_detection.max_failures", 3)
            if config else 3)
        self.window_turns = int(
            config.get("optimization.failure_detection.window_turns", 10)
            if config else 10)
        self._patterns: dict[tuple[str, str, str], FailurePattern] = {}
        self._seq = 0

    # ------------------------------------------------------------ recording

    def record(self, tool_name: str, args: dict, content: str,
               *, is_error: bool = True) -> None:
        """Record one tool outcome (failures feed the detector)."""
        if not self.enabled:
            return
        self._seq += 1
        fingerprint = self._fingerprint_args(args)

        if not is_error:
            # A success with the same call breaks any loop in progress.
            for key in [key for key in self._patterns
                        if key[0] == tool_name and key[2] == fingerprint]:
                self._patterns.pop(key, None)
            return

        signature = self._normalize_error(content)
        key = (tool_name, signature, fingerprint)
        pattern = self._patterns.get(key)
        if pattern is None:
            pattern = FailurePattern(
                tool_name=tool_name, error_signature=signature,
                args_fingerprint=fingerprint)
            self._patterns[key] = pattern
        else:
            pattern.count += 1
            pattern.last_seen = time.monotonic()
        pattern.last_seq = self._seq
        self._expire()

    # ------------------------------------------------------------ analysis

    def analyze(self) -> FailureReport:
        """Report the worst loop not yet surfaced to the model."""
        if not self.enabled:
            return FailureReport()
        candidates = [
            pattern for pattern in self._patterns.values()
            if pattern.count >= self.max_failures
            and pattern.count > pattern.notified_at
            and self._seq - pattern.last_seq <= self.window_turns
        ]
        if not candidates:
            return FailureReport()
        worst = max(candidates, key=lambda pattern: pattern.count)
        worst.notified_at = worst.count
        alternatives = self.suggest_alternatives(worst.tool_name)
        recommendation = (
            f"`{worst.tool_name}` failed {worst.count} times with the same "
            f"error in this session. Do not repeat that call unchanged: read "
            f"the actual error in the tool output, change the command or "
            f"arguments, and try {', '.join(f'`{name}`' for name in alternatives)} "
            f"instead. If the tool itself is unsuitable, say so and explain "
            f"what is blocking you.")
        return FailureReport(detected=True, patterns=[worst],
                             recommendation=recommendation,
                             suggested_tools=alternatives)

    def record_report(self, report: FailureReport) -> None:
        """Hook for callers that persist reports (kept for symmetry)."""
        return None

    def reset(self) -> None:
        self._patterns.clear()
        self._seq = 0

    # ------------------------------------------------------------- helpers

    def suggest_alternatives(self, tool_name: str) -> list[str]:
        alternatives = _TOOL_ALTERNATIVES.get(tool_name)
        if alternatives:
            return list(alternatives)
        return ["read_file", "search_files", "run_command"]

    def _expire(self) -> None:
        """Forget patterns older than the analysis window."""
        stale = [key for key, pattern in self._patterns.items()
                 if self._seq - pattern.last_seq > self.window_turns]
        for key in stale:
            self._patterns.pop(key, None)

    @staticmethod
    def _fingerprint_args(args: dict) -> str:
        try:
            payload = json.dumps(args, sort_keys=True, default=str)
        except (TypeError, ValueError):
            payload = str(args)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    @staticmethod
    def _normalize_error(content: str) -> str:
        """First meaningful line of an error, stripped of volatile detail."""
        text = _ANSI.sub("", content or "")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        line = lines[0] if lines else ""
        line = _CONTROL.sub("", _NUMBERS.sub("N", line)).lower()
        return line[:120]
