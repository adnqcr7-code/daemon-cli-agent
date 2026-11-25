"""Minimal append-only event kernel (v0.9.0 Phase 1).

One SQLite table, one closed type set, append-only. This is the typed
cross-cutting record the transcript journal and the audit log are not:
the journal is messages only, the audit log is human-oriented lines
with no sequence and no projections. The kernel records a run's full
lifecycle - run, messages, tool calls, permission decisions,
verification, checkpoints, plans - so it can be replayed and diffed
against the live session store (`daemon replay`).

Design constraints, all deliberate:

* Append-only: there is no UPDATE/DELETE code path for the events
  table anywhere in this module. ``append`` is the only writer.
* ``seq`` is per-run monotonic, assigned inside a ``BEGIN IMMEDIATE``
  transaction (SQLite's own writer lock) - no separate lock file, so
  a wedged lock holder can never hang a run the way a file lock
  could (cf. the grant-state lock timeout in permissions.py).
* Best effort: a failure to append (unwritable home, corrupt db)
  returns None and logs at debug. The run's behavior is unchanged
  when the event store is unavailable - the kernel must never break
  a run, and a run must never wait on it.
* Payloads are redacted the same way the audit log is (security
  redactor) and content is stored under a documented budget
  (``_CONTENT_MAX`` chars); over-budget content is stored as a
  prefix with ``truncated: true`` and ``content_chars`` so replay
  can still verify everything it holds.
* ``run_id`` is the session id. A run that predates the kernel has
  no events; replay reports that as "no event record", never as a
  divergence.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import daemon_home
from .log import get_logger
from .security import redact

_logger = get_logger("events")

# Closed type set (v0.9.0 plan, Phase 1). Nothing outside this set is
# ever stored; append() refuses unknown types.
EVENT_TYPES = frozenset({
    "run.started",
    "message.user",
    "message.assistant",
    "plan.created",
    "plan.edited",
    "plan.approved",
    "tool.called",
    "tool.result",
    "permission.decided",
    "verification.result",
    "checkpoint.created",
    "run.finished",
})

_DB_NAME = "events.db"
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS events ("
    "id INTEGER PRIMARY KEY, "
    "run_id TEXT NOT NULL, "
    "seq INTEGER NOT NULL, "
    "type TEXT NOT NULL, "
    "payload_json TEXT NOT NULL, "
    "created_at TEXT NOT NULL, "
    "UNIQUE(run_id, seq)"
    ")"
)
_INDEX = ("CREATE INDEX IF NOT EXISTS idx_events_run_seq "
          "ON events(run_id, seq)")

# Stored-content budget per payload field, in characters. Over-budget
# strings are stored as a prefix with "truncated": true and
# "content_chars" (full length) so replay verifies the prefix it has.
_CONTENT_MAX = 4000


@dataclass(frozen=True)
class Event:
    """One stored row."""

    id: int
    run_id: str
    seq: int
    type: str
    payload: dict[str, Any]
    created_at: str


def _redact_value(value: Any) -> Any:
    """Recursively redact strings in a payload (audit-log discipline)."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    return value


def truncate_content(text: str, *, limit: int = _CONTENT_MAX) -> tuple[str, bool, int]:
    """Cap stored text: (stored, truncated?, full length)."""
    if len(text) <= limit:
        return text, False, len(text)
    return text[:limit], True, len(text)


class EventStore:
    """Append-only SQLite event store under the daemon home.

    One store per process use; connections are opened per operation
    and closed immediately, so no connection is ever held across a
    run and a crashed writer cannot wedge the store.
    """

    def __init__(self, home: Path | str | None = None) -> None:
        self.home = Path(home) if home is not None else daemon_home()
        self.path = self.home / _DB_NAME

    # ------------------------------------------------------------ writing

    def append(self, run_id: str, type: str,
               payload: dict[str, Any]) -> int | None:
        """Append one event; returns its seq, or None on any failure.

        Unknown types are refused (not stored). A failure - bad
        payload, unwritable home, corrupt database - is logged at
        debug and returns None; callers must not treat it as fatal.
        """
        if not run_id or not isinstance(run_id, str):
            return None
        if type not in EVENT_TYPES:
            _logger.warning("refusing unknown event type %r", type)
            return None
        try:
            clean = _redact_value(payload)
            body = json.dumps(clean, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            _logger.debug("event payload not serializable (%s)", exc)
            return None
        created_at = datetime.now(UTC).isoformat()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path)
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(_SCHEMA)
                conn.execute(_INDEX)
                row = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM events "
                    "WHERE run_id = ?", (run_id,)).fetchone()
                seq = int(row[0]) + 1
                conn.execute(
                    "INSERT INTO events (run_id, seq, type, payload_json, "
                    "created_at) VALUES (?, ?, ?, ?, ?)",
                    (run_id, seq, type, body, created_at))
                conn.commit()
                return seq
            except sqlite3.Error:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
            finally:
                conn.close()
        except (OSError, sqlite3.Error) as exc:
            _logger.debug("event append failed (%s)", exc)
            return None

    # ------------------------------------------------------------ reading

    def read(self, run_id: str) -> list[Event]:
        """All events for one run, in seq order (empty on any failure)."""
        if not run_id or not self.path.exists():
            return []
        try:
            conn = sqlite3.connect(self.path)
            try:
                rows = conn.execute(
                    "SELECT id, run_id, seq, type, payload_json, created_at "
                    "FROM events WHERE run_id = ? ORDER BY seq",
                    (run_id,)).fetchall()
            finally:
                conn.close()
        except (OSError, sqlite3.Error) as exc:
            _logger.debug("event read failed (%s)", exc)
            return []
        events: list[Event] = []
        for row in rows:
            try:
                payload = json.loads(row[4])
            except ValueError:
                _logger.warning("dropping event %s with unparseable payload",
                                row[0])
                continue
            if not isinstance(payload, dict):
                continue
            events.append(Event(id=row[0], run_id=row[1], seq=row[2],
                                type=row[3], payload=payload,
                                created_at=row[5]))
        return events

    def has_run(self, run_id: str) -> bool:
        """True when at least one event exists for the run."""
        if not run_id or not self.path.exists():
            return False
        try:
            conn = sqlite3.connect(self.path)
            try:
                row = conn.execute(
                    "SELECT 1 FROM events WHERE run_id = ? LIMIT 1",
                    (run_id,)).fetchone()
            finally:
                conn.close()
        except (OSError, sqlite3.Error):
            return False
        return row is not None

    def read_by_type(self, type: str) -> list[Event]:
        """All events of one type, across runs, in creation order.

        Used by `daemon undo` (§2.5): the checkpoint.created events
        are the index of undoable checkpoints. Empty on any failure,
        like read().
        """
        if not self.path.exists():
            return []
        try:
            conn = sqlite3.connect(self.path)
            try:
                rows = conn.execute(
                    "SELECT id, run_id, seq, type, payload_json, created_at "
                    "FROM events WHERE type = ? ORDER BY created_at, id",
                    (type,)).fetchall()
            finally:
                conn.close()
        except (OSError, sqlite3.Error) as exc:
            _logger.debug("event read failed (%s)", exc)
            return []
        events: list[Event] = []
        for row in rows:
            try:
                payload = json.loads(row[4])
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            events.append(Event(id=row[0], run_id=row[1], seq=row[2],
                                type=row[3], payload=payload,
                                created_at=row[5]))
        return events


def emit(run_id: str | None, type: str, payload: dict[str, Any]) -> int | None:
    """Best-effort module-level append; never raises.

    The call sites (runtime, permissions, checkpoints, planner) use
    this so a dead event store can never change a run's behavior.
    """
    if not run_id:
        return None
    try:
        return EventStore().append(run_id, type, payload)
    except Exception as exc:  # noqa: BLE001 - the kernel never breaks a run
        _logger.debug("event emit failed (%s)", exc)
        return None


def _plan_task_payload(task: Any) -> dict[str, Any]:
    """One task as a kernel payload row: title, description and
    files included, so the approval record shows exactly what the
    user approved (review round 1: the panel and the event used to
    carry titles only in the plan file, never in the record)."""
    if isinstance(task, dict):
        return {"id": task.get("id", ""), "title": task.get("title", ""),
                "description": task.get("description", ""),
                "files": list(task.get("files") or []),
                "verification": task.get("verification", ""),
                "risk": task.get("risk", "")}
    return {"id": getattr(task, "id", ""),
            "title": getattr(task, "title", ""),
            "description": getattr(task, "description", ""),
            "files": list(getattr(task, "files", []) or []),
            "verification": getattr(task, "verification", ""),
            "risk": getattr(task, "risk", "")}


def emit_plan_created(run_id: str | None, plan: Any, *,
                      plan_id: str | None = None,
                      plan_hash: str | None = None) -> int | None:
    """Emit plan.created for a planner.Plan.

    Duck-typed (no import of the planner module - the planner is
    also used by the REPL and the CLI, and both call this helper, so
    the payload shape has exactly one implementation). The test
    drives this same helper through parse_plan. ``plan_id`` and
    ``plan_hash`` (from the stored record) anchor the event to the
    plan file; the execute-time check binds to this record as well
    as to the plan.approved event (review round 2, R1-3): the
    creation hash is the anchor a forger cannot move, because
    plan.created is written once, at creation, under DAEMON_HOME.
    An edit recorded later (plan.edited) supersedes it — that is
    the only way the anchor moves.
    """
    payload: dict[str, Any] = {
        "goal": plan.goal, "summary": plan.summary,
        "tasks": [_plan_task_payload(task) for task in plan.tasks]}
    if plan_id is not None:
        payload["plan_id"] = plan_id
    if plan_hash is not None:
        payload["plan_hash"] = plan_hash
    return emit(run_id, "plan.created", payload)


def emit_plan_edited(run_id: str | None, plan: Any, *,
                     plan_id: str | None = None,
                     plan_hash: str | None = None) -> int | None:
    """Emit plan.edited for a planner.Plan.

    The recorded edit path (``daemon plan --edit`` / REPL
    /edit-plan): the user deliberately opens the stored plan in
    their editor, and this event records the new content hash in
    the kernel BEFORE any approval. It is the only way the
    recorded version changes after creation — a file rewritten
    outside this path (a forger recomputing the self-stored hash,
    or a hand-edit in any other editor) leaves the kernel record
    at the old hash, and ``verify_approved`` refuses to execute
    it. Same payload shape as plan.created.
    """
    payload: dict[str, Any] = {
        "goal": plan.goal, "summary": plan.summary,
        "tasks": [_plan_task_payload(task) for task in plan.tasks]}
    if plan_id is not None:
        payload["plan_id"] = plan_id
    if plan_hash is not None:
        payload["plan_hash"] = plan_hash
    return emit(run_id, "plan.edited", payload)


def emit_plan_approved(run_id: str | None, plan: Any, *,
                       via: str, plan_id: str | None = None,
                       plan_hash: str | None = None) -> int | None:
    """Emit plan.approved for a planner.Plan.

    ``via`` names the acceptance point ("--execute" today; the Phase
    2 broker gate will add its own). The event records that the user
    accepted THIS plan - the approval gate in Phase 2 reuses it.
    ``plan_id`` and ``plan_hash`` are the binding: the execute path
    re-reads the plan file and refuses unless its freshly computed
    hash equals the hash recorded here, in the kernel store under
    DAEMON_HOME - outside the workspace, where a forger who can
    rewrite the plan file (and its self-stored hash) cannot reach
    it. The full task list rides along so the record shows what was
    approved, not just a hash.
    """
    payload: dict[str, Any] = {
        "goal": plan.goal, "task_count": len(plan.tasks), "via": via,
        "tasks": [_plan_task_payload(task) for task in plan.tasks]}
    if plan_id is not None:
        payload["plan_id"] = plan_id
    if plan_hash is not None:
        payload["plan_hash"] = plan_hash
    return emit(run_id, "plan.approved", payload)


def find_plan_approved(plan_id: str) -> dict[str, Any] | None:
    """The latest plan.approved payload for a plan id, across runs.

    Approval and execution are separate invocations (separate run
    ids), so the lookup is by plan id over all runs, not within one
    run's stream. None when no approval was ever recorded for the
    id - an unapproved (or unrecorded) plan must never execute.
    """
    latest: dict[str, Any] | None = None
    for event in EventStore().read_by_type("plan.approved"):
        if event.payload.get("plan_id") == plan_id:
            latest = dict(event.payload)
    return latest


def find_plan_recorded(plan_id: str) -> dict[str, Any] | None:
    """The latest recorded version of a plan: its plan.created
    payload, or a later plan.edited payload when the user re-recorded
    an edit through the recorded edit path.

    This is the creation anchor ``verify_approved`` binds execution
    to (review round 2, R1-3): the file's freshly computed hash must
    equal the hash in this record, so a plan rewritten outside the
    recorded edit flow — forged hash or not — cannot execute, even
    when the approval panel was shown the forged content and the
    user approved it. The approval event alone cannot anchor
    anything: every execute path emits it from the file it just
    loaded. None when the plan was never recorded (created before
    the kernel, or by hand) — such a plan must be re-recorded with
    ``daemon plan --edit`` or re-created before it can run.
    """
    latest: dict[str, Any] | None = None
    latest_id = -1
    for type in ("plan.created", "plan.edited"):
        for event in EventStore().read_by_type(type):
            if event.payload.get("plan_id") == plan_id \
                    and event.id > latest_id:
                latest = dict(event.payload)
                latest_id = event.id
    return latest


# ------------------------------------------------------------ projections
# Pure functions over stored rows. The message projection mirrors the
# transcript.jsonl shape (Message.to_json dicts) so `daemon replay`
# can diff the two directly: user/assistant events map 1:1, and
# consecutive tool.result events group into the single "user"-role
# message with_tool_results() produces.


def message_payload(message: dict[str, Any], *,
                    limit: int = 100_000) -> dict[str, Any]:
    """Wrap a transcript message dict for a message.* event.

    Text over ``limit`` is stored as a prefix with a truncation
    marker (same convention as tool content) so replay can still
    verify the prefix it holds. The limit is generous - normal
    turns never hit it - and exists so one pathological turn cannot
    bloat the store.
    """
    data = dict(message)
    text = data.get("text")
    if isinstance(text, str) and len(text) > limit:
        data["text"] = text[:limit]
        data["truncated"] = True
        data["content_chars"] = len(text)
    return {"message": data}


def project_messages(events: list[Event]) -> list[dict[str, Any]]:
    """Message history from events, in seq order."""
    messages: list[dict[str, Any]] = []
    pending_results: list[dict[str, Any]] = []

    def flush_results() -> None:
        nonlocal pending_results
        if pending_results:
            messages.append({"role": "user", "text": None, "tool_calls": [],
                             "tool_results": pending_results})
            pending_results = []

    for event in events:
        if event.type == "message.user":
            flush_results()
            messages.append(dict(event.payload.get("message") or {}))
        elif event.type == "message.assistant":
            flush_results()
            messages.append(dict(event.payload.get("message") or {}))
        elif event.type == "tool.result":
            pending_results.append({
                "tool_call_id": event.payload.get("tool_call_id"),
                "tool_name": event.payload.get("tool_name"),
                "content": event.payload.get("content", ""),
                "is_error": bool(event.payload.get("is_error")),
                **({"truncated": True,
                    "content_chars": event.payload.get("content_chars")}
                   if event.payload.get("truncated") else {}),
            })
    flush_results()
    return messages


def project_plan(events: list[Event]) -> dict[str, Any]:
    """Plan state: the latest recorded plan and whether it was approved.

    A plan.edited event replaces the recorded version exactly like
    a fresh plan.created, and resets approval: the old approval
    covered the old content, not the edited one (review round 2,
    R1-3 — the projection must not claim an edited plan is still
    approved)."""
    created: dict[str, Any] | None = None
    approved = False
    approved_via: str | None = None
    for event in events:
        if event.type in ("plan.created", "plan.edited"):
            created = dict(event.payload)
            approved = False
            approved_via = None
        elif event.type == "plan.approved":
            approved = True
            approved_via = event.payload.get("via")
    return {"created": created, "approved": approved,
            "approved_via": approved_via}


def project_checkpoints(events: list[Event]) -> list[dict[str, Any]]:
    """Checkpoint records, in seq order."""
    return [dict(event.payload) for event in events
            if event.type == "checkpoint.created"]


# ------------------------------------------------------------ replay
# Deterministic comparison of the kernel's message projection against
# the live transcript. The kernel only records runs that happened
# after Phase 1, so the projection is compared against the TAIL of
# the transcript (a suffix), not the whole file: a resumed session's
# older turns predate the kernel and are not its business. A compacted
# transcript keeps its newest turns, so the suffix rule survives
# compaction too. Returns (output_lines, exit_code): 0 identical,
# 1 divergent, 2 no event record / no session / unreadable store.


def _content_matches(projected: str, transcript: str,
                     truncated: bool, content_chars: Any) -> bool:
    """Content equality under the storage budget.

    Untruncated content must match exactly. Truncated content must
    match on the stored prefix and agree on the full length.
    """
    if not truncated:
        return projected == transcript
    if not isinstance(content_chars, int):
        return False
    return (len(transcript) == content_chars
            and transcript.startswith(projected))


def _message_matches(projected: dict[str, Any],
                     transcript: dict[str, Any]) -> str | None:
    """None when the two message dicts agree, else a reason string."""
    if projected.get("role") != transcript.get("role"):
        return (f"role mismatch (event {projected.get('role')} vs "
                f"transcript {transcript.get('role')})")
    p_text, t_text = projected.get("text"), transcript.get("text")
    if isinstance(p_text, str) and isinstance(t_text, str) \
            and projected.get("truncated"):
        if not _content_matches(p_text, t_text, True,
                                projected.get("content_chars")):
            return "text mismatch"
    elif p_text != t_text:
        return "text mismatch"
    p_calls = projected.get("tool_calls") or []
    t_calls = transcript.get("tool_calls") or []
    if p_calls != t_calls:
        return "tool_calls mismatch"
    p_results = projected.get("tool_results") or []
    t_results = transcript.get("tool_results") or []
    if len(p_results) != len(t_results):
        return (f"tool_results count mismatch "
                f"({len(p_results)} vs {len(t_results)})")
    for index, (p_res, t_res) in enumerate(zip(p_results, t_results)):
        for key in ("tool_call_id", "tool_name", "is_error"):
            if p_res.get(key) != t_res.get(key):
                return (f"tool result {index}: {key} mismatch "
                        f"({p_res.get(key)!r} vs {t_res.get(key)!r})")
        if not _content_matches(str(p_res.get("content", "")),
                                str(t_res.get("content", "")),
                                bool(p_res.get("truncated")),
                                p_res.get("content_chars")):
            return f"tool result {index}: content mismatch"
    return None


def replay_report(run_id: str) -> tuple[list[str], int]:
    """Diff one run's event record against its live session store.

    Output is deterministic: counts, ids and diff lines only, no
    timestamps, everything in seq order - two invocations produce
    byte-identical output.
    """
    from .config import daemon_home

    store = EventStore()
    events = store.read(run_id)
    header = [f"run {run_id}"]
    if not events:
        return header + [f"no event record for run {run_id}"], 2
    session_dir = daemon_home() / "sessions" / run_id
    if not (session_dir / "meta.json").exists():
        return header + [f"no session '{run_id}'"], 2
    try:
        meta = json.loads((session_dir / "meta.json").read_text("utf-8"))
    except (OSError, ValueError):
        return header + [f"session '{run_id}' meta is unreadable"], 2

    counts: dict[str, int] = {}
    for event in events:
        counts[event.type] = counts.get(event.type, 0) + 1
    type_summary = ", ".join(
        f"{name}={counts[name]}" for name in sorted(counts))
    lines = header + [f"events: {len(events)} ({type_summary})"]

    projected = project_messages(events)
    transcript: list[dict[str, Any]] = []
    transcript_path = session_dir / "transcript.jsonl"
    torn_tail = False
    if transcript_path.exists():
        raw_lines = transcript_path.read_text("utf-8").splitlines()
        for index, raw in enumerate(raw_lines):
            if not raw.strip():
                continue
            try:
                parsed = json.loads(raw)
            except ValueError:
                if index == len(raw_lines) - 1:
                    torn_tail = True  # a kill mid-append: the journal
                    # itself drops this line on load; replay says so
                    continue
                lines.append(f"DIVERGENT: transcript line {index + 1} "
                             f"is not valid JSON")
                return lines, 1
            if isinstance(parsed, dict):
                transcript.append(parsed)
    lines.append(f"messages: {len(projected)} projected, "
                 f"{len(transcript)} in transcript")

    checkpoints = project_checkpoints(events)
    workspace = Path(meta.get("workspace") or "")
    missing: list[str] = []
    for record in checkpoints:
        checkpoint_id = record.get("checkpoint_id", "")
        if workspace and not (
                workspace / ".daemon" / "checkpoints" / checkpoint_id
        ).is_dir():
            missing.append(str(checkpoint_id))
    lines.append(f"checkpoints: {len(checkpoints)} recorded, "
                 f"{len(checkpoints) - len(missing)} present on disk")

    divergences: list[str] = []
    if torn_tail:
        divergences.append("transcript has a torn final line "
                           "(kill mid-append)")
    if len(projected) > len(transcript):
        divergences.append(
            f"kernel holds {len(projected)} messages but the transcript "
            f"has only {len(transcript)}")
    else:
        tail = transcript[len(transcript) - len(projected):] \
            if projected else []
        for index, (proj, live) in enumerate(zip(projected, tail)):
            reason = _message_matches(proj, live)
            if reason is not None:
                divergences.append(f"message {index}: {reason}")
                break  # first divergence only: deterministic and short
    for checkpoint_id in missing:
        divergences.append(f"checkpoint {checkpoint_id} recorded but "
                           f"missing on disk")

    finished = any(event.type == "run.finished" for event in events)
    if not finished:
        lines.append("note: run unfinished (no run.finished event)")
    if divergences:
        lines.append("DIVERGENT:")
        lines.extend(f"- {item}" for item in divergences)
        return lines, 1
    lines.append("identical")
    return lines, 0
