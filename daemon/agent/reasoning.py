"""Reasoning-trace storage (v0.9.0 Phase 3).

Some providers surface a model's reasoning trace alongside the
answer (DeepSeek's ``reasoning_content``, Anthropic thinking
blocks, Gemini thought parts). The runtime displays those traces
dim and stores them here — in a file that is separate from the
conversation, per run:

``~/.daemon/sessions/<run_id>/reasoning.jsonl``

The separation is the point: a reasoning trace is the model's
private working, not history. Nothing in the runtime, the context
builder, the event kernel, or replay ever reads this file back
into a request — a trace can never be replayed as normal history,
and a later turn can never mistake a past trace for something the
user or the assistant said. The file exists for inspection only
(``read``), e.g. when debugging why a model chose a tool.

Traces are redacted with the same redactor as the audit log, and
writes are best-effort: a reasoning store failure must never break
a run.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..config import daemon_home
from ..log import get_logger
from ..security import redact

_logger = get_logger("reasoning")


class ReasoningStore:
    """Append-only per-run reasoning traces, stored apart from history."""

    def __init__(self, home: Path | None = None) -> None:
        self.home = Path(home) if home is not None else daemon_home()

    def path_for(self, run_id: str) -> Path:
        return self.home / "sessions" / run_id / "reasoning.jsonl"

    def append(self, run_id: str, text: str, *, provider: str,
               model: str) -> None:
        """Store one reasoning chunk. Never raises."""
        if not text:
            return
        record = {
            "ts": time.time(),
            "provider": provider,
            "model": model,
            "reasoning": redact(text),
        }
        path = self.path_for(run_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            _logger.debug("reasoning store write failed: %s", exc)

    def read(self, run_id: str) -> list[dict[str, Any]]:
        """All stored traces for a run, in order. Inspection only —
        callers must never feed these back into a request."""
        path = self.path_for(run_id)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        records: list[dict[str, Any]] = []
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
        return records
