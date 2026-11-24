"""Optimization reporting: per-session metrics, persistence, comparison.

Collects the numbers the optimization engine already produces — context
and tool-selection token savings, tool calls, failure loops, verified
facts, usage and cost — into one :class:`OptimizationReport` per session.

Honesty rule (from OPTIMIZATION_ENGINE_DESIGN.md section 5): report only
what was actually recorded. An unknown cost stays ``None`` and renders as
"unknown"; no percentage or total is ever fabricated from missing data.
Reports persist as JSON under ``<workspace>/.daemon/reports/`` so a
baseline run (optimization off) can be compared with a candidate run
(optimization on) via :func:`compare_reports`.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .optimizer import ContextOptimizationStats


@dataclass
class OptimizationReport:
    """One session's optimization metrics."""

    session_id: str
    model: str = ""
    provider: str = ""
    optimization_enabled: bool = True
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    context_tokens_saved: int = 0
    tool_tokens_saved: int = 0
    total_tool_calls: int = 0
    failed_tool_calls: int = 0
    failure_loops_detected: int = 0
    verified_facts_used: int = 0
    reflex_firings: int = 0
    reflex_follows: int = 0
    reflex_overrides: int = 0
    reflex_quarantines: int = 0
    files_changed: int = 0
    verification_passed: bool | None = None
    estimated_cost_usd: float | None = None
    duration_s: float = 0.0
    created: float = field(default_factory=time.time)

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    @property
    def total_tokens_saved(self) -> int:
        return self.context_tokens_saved + self.tool_tokens_saved

    def to_json(self) -> dict:
        return {
            "session_id": self.session_id,
            "model": self.model,
            "provider": self.provider,
            "optimization_enabled": self.optimization_enabled,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "context_tokens_saved": self.context_tokens_saved,
            "tool_tokens_saved": self.tool_tokens_saved,
            "total_tool_calls": self.total_tool_calls,
            "failed_tool_calls": self.failed_tool_calls,
            "failure_loops_detected": self.failure_loops_detected,
            "verified_facts_used": self.verified_facts_used,
            "reflex_firings": self.reflex_firings,
            "reflex_follows": self.reflex_follows,
            "reflex_overrides": self.reflex_overrides,
            "reflex_quarantines": self.reflex_quarantines,
            "files_changed": self.files_changed,
            "verification_passed": self.verification_passed,
            "estimated_cost_usd": self.estimated_cost_usd,
            "duration_s": self.duration_s,
            "created": self.created,
        }

    @staticmethod
    def from_json(data: dict) -> OptimizationReport:
        fields = OptimizationReport.__dataclass_fields__
        kwargs = {key: data[key] for key in fields if key in data}
        return OptimizationReport(**kwargs)

    def summary(self) -> str:
        """One compact human-readable block; unknown values stay unknown."""
        enabled = "enabled" if self.optimization_enabled else "disabled"
        if self.verification_passed is None:
            verification = "unknown"
        else:
            verification = "passed" if self.verification_passed else "FAILED"
        cost = (
            f"${self.estimated_cost_usd:.4f}"
            if self.estimated_cost_usd is not None
            else "unknown"
        )
        header = f"Session {self.session_id}"
        if self.model or self.provider:
            header += " · " + "/".join(
                part for part in (self.provider, self.model) if part)
        lines = [
            header,
            f"Optimization: {enabled}",
            f"Tokens: {self.total_input_tokens} in / {self.total_output_tokens} out "
            f"({self.total_tokens} total) · saved {self.total_tokens_saved} "
            f"(context {self.context_tokens_saved}, tools {self.tool_tokens_saved})",
            f"Tool calls: {self.total_tool_calls} "
            f"({self.failed_tool_calls} failed) · "
            f"failure loops: {self.failure_loops_detected}",
            f"Verified facts used: {self.verified_facts_used} · "
            f"files changed: {self.files_changed}",
            f"Verification: {verification}",
            f"Cost: {cost}",
            f"Duration: {self.duration_s:.2f}s",
        ]
        if (self.reflex_firings or self.reflex_follows
                or self.reflex_overrides or self.reflex_quarantines):
            lines.insert(
                -2,
                f"Reflex: {self.reflex_firings} firing(s) · "
                f"{self.reflex_follows} followed · "
                f"{self.reflex_overrides} override(s) · "
                f"{self.reflex_quarantines} quarantine(s)")
        return "\n".join(lines)


class OptimizationReporter:
    """Accumulates optimization metrics for one session."""

    def __init__(self, session_id: str, model: str = "", provider: str = "",
                 optimization_enabled: bool = True) -> None:
        self.session_id = session_id
        self.model = model
        self.provider = provider
        self.optimization_enabled = optimization_enabled
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._context_tokens_saved = 0
        self._tool_tokens_saved = 0
        self._total_tool_calls = 0
        self._failed_tool_calls = 0
        self._failure_loops_detected = 0
        self._verified_facts_used = 0
        self._reflex_firings = 0
        self._reflex_follows = 0
        self._reflex_overrides = 0
        self._reflex_quarantines = 0
        self._files_changed = 0
        self._verification_passed: bool | None = None
        self._estimated_cost_usd: float | None = None

    # ------------------------------------------------------------ recording

    def record_context(self, stats: ContextOptimizationStats | int) -> None:
        """Record context savings from stats or a plain tokens-saved int."""
        if isinstance(stats, int):
            saved = stats
        else:
            saved = int(getattr(stats, "tokens_saved", 0) or 0)
        self._context_tokens_saved += max(0, saved)

    def record_tool_selection(self, original_count: int, selected_count: int,
                              tokens_saved: int) -> None:
        """Record one tool-selection pass and the tokens it saved."""
        # original/selected counts describe the pass; only the saving is a
        # cumulative metric on the report.
        _ = (original_count, selected_count)
        self._tool_tokens_saved += max(0, int(tokens_saved))

    def record_tool_call(self, failed: bool = False) -> None:
        self._total_tool_calls += 1
        if failed:
            self._failed_tool_calls += 1

    def record_failure_loop(self) -> None:
        self._failure_loops_detected += 1

    def record_verified_facts(self, count: int) -> None:
        self._verified_facts_used += max(0, int(count))

    def record_reflex(self, firings: int, follows: int, overrides: int,
                      quarantines: int) -> None:
        """Record reflex event counters (design v4 section 3.6: events,
        never per-fact token estimates)."""
        self._reflex_firings += max(0, int(firings))
        self._reflex_follows += max(0, int(follows))
        self._reflex_overrides += max(0, int(overrides))
        self._reflex_quarantines += max(0, int(quarantines))

    def set_usage(self, input_tokens: int, output_tokens: int,
                  cost_usd: float | None = None) -> None:
        """Set the session's provider-reported usage totals."""
        self._total_input_tokens = int(input_tokens)
        self._total_output_tokens = int(output_tokens)
        if cost_usd is not None:
            self._estimated_cost_usd = float(cost_usd)

    def set_verification(self, passed: bool | None) -> None:
        self._verification_passed = passed

    def add_files_changed(self, count: int) -> None:
        self._files_changed += max(0, int(count))

    # ------------------------------------------------------------- finalize

    def finalize(self, duration_s: float = 0) -> OptimizationReport:
        return OptimizationReport(
            session_id=self.session_id,
            model=self.model,
            provider=self.provider,
            optimization_enabled=self.optimization_enabled,
            total_input_tokens=self._total_input_tokens,
            total_output_tokens=self._total_output_tokens,
            context_tokens_saved=self._context_tokens_saved,
            tool_tokens_saved=self._tool_tokens_saved,
            total_tool_calls=self._total_tool_calls,
            failed_tool_calls=self._failed_tool_calls,
            failure_loops_detected=self._failure_loops_detected,
            verified_facts_used=self._verified_facts_used,
            reflex_firings=self._reflex_firings,
            reflex_follows=self._reflex_follows,
            reflex_overrides=self._reflex_overrides,
            reflex_quarantines=self._reflex_quarantines,
            files_changed=self._files_changed,
            verification_passed=self._verification_passed,
            estimated_cost_usd=self._estimated_cost_usd,
            duration_s=float(duration_s),
            created=time.time(),
        )


# ---------------------------------------------------------------- persistence


def _reports_dir(workspace: Path | str) -> Path:
    return Path(workspace) / ".daemon" / "reports"


def save_report(report: OptimizationReport, workspace: Path | str) -> Path:
    """Write ``report`` as JSON under ``<workspace>/.daemon/reports/``."""
    directory = _reports_dir(workspace)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{report.session_id}.json"
    path.write_text(json.dumps(report.to_json(), indent=2), encoding="utf-8")
    return path


def load_report(workspace: Path | str,
                session_id: str) -> OptimizationReport | None:
    """Load one saved report, or None when missing or unreadable."""
    path = _reports_dir(workspace) / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        return OptimizationReport.from_json(
            json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


def list_reports(workspace: Path | str,
                 limit: int = 20) -> list[OptimizationReport]:
    """Saved reports, newest first (by ``created``)."""
    directory = _reports_dir(workspace)
    if not directory.exists():
        return []
    reports: list[OptimizationReport] = []
    for path in directory.glob("*.json"):
        try:
            reports.append(OptimizationReport.from_json(
                json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError, TypeError):
            continue
    reports.sort(key=lambda report: -report.created)
    return reports[:limit]


# ---------------------------------------------------------------- comparison


def _delta(value: int | float, *, fmt: str = "d") -> str:
    return f"({value:+{fmt}})"


def compare_reports(baseline: OptimizationReport,
                    candidate: OptimizationReport) -> str:
    """Readable baseline-vs-candidate comparison with signed deltas."""
    def enabled_text(report: OptimizationReport) -> str:
        return "enabled" if report.optimization_enabled else "disabled"

    lines = [
        "Optimization comparison",
        f"Baseline: {baseline.session_id} "
        f"(optimization {enabled_text(baseline)})",
        f"Candidate: {candidate.session_id} "
        f"(optimization {enabled_text(candidate)})",
        f"Total tokens: {baseline.total_tokens} -> {candidate.total_tokens} "
        f"{_delta(candidate.total_tokens - baseline.total_tokens)}",
        f"Input tokens: {baseline.total_input_tokens} -> "
        f"{candidate.total_input_tokens} "
        f"{_delta(candidate.total_input_tokens - baseline.total_input_tokens)}",
        f"Output tokens: {baseline.total_output_tokens} -> "
        f"{candidate.total_output_tokens} "
        f"{_delta(candidate.total_output_tokens - baseline.total_output_tokens)}",
        f"Tokens saved: {baseline.total_tokens_saved} -> "
        f"{candidate.total_tokens_saved} "
        f"{_delta(candidate.total_tokens_saved - baseline.total_tokens_saved)} "
        f"(context {baseline.context_tokens_saved} -> "
        f"{candidate.context_tokens_saved}, tools "
        f"{baseline.tool_tokens_saved} -> {candidate.tool_tokens_saved})",
        f"Tool calls: {baseline.total_tool_calls} -> "
        f"{candidate.total_tool_calls} "
        f"{_delta(candidate.total_tool_calls - baseline.total_tool_calls)} "
        f"(failed {baseline.failed_tool_calls} -> "
        f"{candidate.failed_tool_calls})",
        f"Failure loops: {baseline.failure_loops_detected} -> "
        f"{candidate.failure_loops_detected} "
        f"{_delta(candidate.failure_loops_detected - baseline.failure_loops_detected)}",
        f"Files changed: {baseline.files_changed} -> {candidate.files_changed} "
        f"{_delta(candidate.files_changed - baseline.files_changed)}",
        f"Duration: {baseline.duration_s:.2f}s -> {candidate.duration_s:.2f}s "
        f"{_delta(candidate.duration_s - baseline.duration_s, fmt='.2f')}s",
    ]
    if baseline.estimated_cost_usd is not None and candidate.estimated_cost_usd is not None:
        delta = candidate.estimated_cost_usd - baseline.estimated_cost_usd
        lines.append(
            f"Cost: ${baseline.estimated_cost_usd:.4f} -> "
            f"${candidate.estimated_cost_usd:.4f} ({delta:+.4f})")
    else:
        lines.append("Cost: unknown (not recorded for one or both runs)")
    return "\n".join(lines)
