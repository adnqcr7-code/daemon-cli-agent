"""Stored plans: the plan-mode gate (v0.9.0 §2.3).

A plan is created (``daemon run --plan``, ``daemon plan``, REPL /plan),
persisted as JSON under ``.daemon/plans/<id>.json`` with a content
hash, and STOPS there. Execution is a separate invocation that loads
the stored plan, re-hashes it, and refuses on any mutation; approval
goes through the permission broker (``PermissionEngine.approve_plan``)
and is recorded as a ``plan.approved`` kernel event.

Hash discipline: the hash covers the PLAN CONTENT only (goal, summary,
tasks) — never the metadata (id, created_at, created_by, status).
Metadata is bookkeeping; the content is what the user approved, and
the hash is what proves it is unchanged since approval. Task
statuses live in the in-memory TaskTracker during execution, never
written back, so the stored file stays hash-stable across a run.

The file's self-stored hash is tamper-EVIDENCE only: it is an
unsalted sha256 of public content, so anyone who can write the plan
file can recompute it (review round 1, R1-3). The binding anchor is
the kernel: the plan's creation is recorded in a ``plan.created``
event under DAEMON_HOME — outside the workspace — and
``verify_approved`` re-reads the file at execute time and refuses
unless its freshly computed hash equals BOTH the kernel-recorded
approval hash AND the hash in the latest recorded version
(plan.created, or a later plan.edited). The approval event alone
cannot anchor anything: every execute path emits it from the file
it just loaded, so a forged plan approved at the panel would
always match its own approval (review round 2, R1-3 — the
headline attack still worked after round 1).

Edit-before-approve is still supported, through the RECORDED edit
path only (``edit_plan_record``: ``daemon plan --edit`` / REPL
/edit-plan): the user opens the stored plan in their editor, and
the new content hash is recorded as a ``plan.edited`` event BEFORE
any approval, so approval binds to the recorded version and
execution compares against it. A file rewritten outside that path
— forged hash or not, hand-edited in any other editor — leaves
the kernel record at the old hash and refuses at execute time,
with a message naming the recorded edit command. An edit AFTER
approval is refused the same way: the recorded version moved,
the approval did not, and the plan must be approved again.

``--yes`` never substitutes for plan approval: the broker gate
refuses outright without an interactive approver (see
``PermissionEngine.approve_plan``).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .agent.planner import Plan, Task, TaskTracker, parse_plan
from .agent.prompts import planner_prompt
from .errors import DaemonError
from .events import emit_plan_created, emit_plan_edited, find_plan_approved, find_plan_recorded
from .project import project_dir
from .providers.types import ChatRequest, Message

PLAN_DIR_NAME = ".daemon/plans"
PLAN_STATUSES = ("pending", "approved", "executed", "failed")
_TASK_FIELDS = ("id", "title", "description", "files", "depends_on",
                "verification", "risk", "status", "assigned", "result")
_RECORD_FIELDS = ("id", "goal", "summary", "tasks", "created_at",
                  "created_by", "status", "hash")


class PlanError(DaemonError):
    """A stored plan is missing, malformed, or was mutated.

    Exit code 2: the plan will not run (as opposed to 1, which the
    execute path uses when the user declines approval).
    """

    exit_code = 2


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _task_to_dict(task: Task) -> dict[str, Any]:
    return {"id": task.id, "title": task.title,
            "description": task.description, "files": list(task.files),
            "depends_on": list(task.depends_on),
            "verification": task.verification, "risk": task.risk,
            "status": task.status, "assigned": task.assigned,
            "result": task.result}


def _task_from_dict(raw: Any, *, index: int) -> Task:
    """One task dict -> Task, strictly: the wrong shape is an error,
    not a silent default (a hand-edited plan that breaks the schema
    must fail validation, not execute half-understood)."""
    if not isinstance(raw, dict):
        raise PlanError(f"task {index}: not an object")
    unknown = sorted(set(raw) - set(_TASK_FIELDS))
    if unknown:
        raise PlanError(f"task {index}: unknown field(s): "
                        f"{', '.join(unknown)}")
    task_id = raw.get("id")
    title = raw.get("title")
    if not isinstance(task_id, str) or not task_id:
        raise PlanError(f"task {index}: 'id' must be a non-empty string")
    if not isinstance(title, str) or not title:
        raise PlanError(f"task {index}: 'title' must be a non-empty string")
    files = raw.get("files", [])
    depends = raw.get("depends_on", [])
    if not isinstance(files, list) or not all(
            isinstance(item, str) for item in files):
        raise PlanError(f"task {index}: 'files' must be a list of strings")
    if not isinstance(depends, list) or not all(
            isinstance(item, str) for item in depends):
        raise PlanError(
            f"task {index}: 'depends_on' must be a list of strings")
    risk = raw.get("risk", "low")
    if risk not in ("low", "medium", "high"):
        raise PlanError(
            f"task {index}: 'risk' must be low, medium or high, not {risk!r}")
    status = raw.get("status", "pending")
    if status not in ("pending", "in_progress", "done", "failed",
                      "blocked"):
        raise PlanError(f"task {index}: unknown status {status!r}")
    for key in ("description", "verification", "assigned", "result"):
        if key in raw and not isinstance(raw[key], str):
            raise PlanError(f"task {index}: '{key}' must be a string")
    return Task(id=task_id, title=title,
                description=raw.get("description", ""),
                files=list(files), depends_on=list(depends),
                verification=raw.get("verification", ""), risk=risk,
                status=status, assigned=raw.get("assigned", ""),
                result=raw.get("result", ""))


def compute_plan_hash(*, goal: str, summary: str,
                      tasks: list[dict[str, Any]]) -> str:
    """sha256 over the canonical JSON of the plan content."""
    content = {"goal": goal, "summary": summary, "tasks": tasks}
    canonical = json.dumps(content, sort_keys=True,
                           separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_plan_payload(data: Any, *, expect_id: str | None = None) -> None:
    """Schema validation for a stored-plan payload (also the
    edit-before-approve re-validation: a hand-edited plan that
    breaks the schema is rejected before it can execute)."""
    if not isinstance(data, dict):
        raise PlanError("plan file is not a JSON object")
    unknown = sorted(set(data) - set(_RECORD_FIELDS))
    if unknown:
        raise PlanError(f"unknown field(s): {', '.join(unknown)}")
    for key in _RECORD_FIELDS:
        if key not in data:
            raise PlanError(f"missing field: '{key}'")
    if expect_id is not None and data["id"] != expect_id:
        raise PlanError(
            f"plan id mismatch: file says {data['id']!r}, "
            f"expected {expect_id!r}")
    if not isinstance(data["id"], str) or not data["id"]:
        raise PlanError("'id' must be a non-empty string")
    if not isinstance(data["goal"], str) or not data["goal"]:
        raise PlanError("'goal' must be a non-empty string")
    if not isinstance(data["summary"], str):
        raise PlanError("'summary' must be a string")
    if not isinstance(data["tasks"], list) or not data["tasks"]:
        raise PlanError("'tasks' must be a non-empty list")
    if not isinstance(data["created_at"], str):
        raise PlanError("'created_at' must be a string")
    if not isinstance(data["created_by"], str):
        raise PlanError("'created_by' must be a string")
    if data["status"] not in PLAN_STATUSES:
        raise PlanError(f"unknown status {data['status']!r}")
    if not isinstance(data["hash"], str) or not data["hash"]:
        raise PlanError("'hash' must be a non-empty string")
    for index, raw in enumerate(data["tasks"], start=1):
        _task_from_dict(raw, index=index)


@dataclass
class PlanRecord:
    """One stored plan: content + metadata + its content hash."""

    id: str
    goal: str
    summary: str
    tasks: list[Task] = field(default_factory=list)
    created_at: str = ""
    created_by: str = ""
    status: str = "pending"
    hash: str = ""

    @property
    def plan(self) -> Plan:
        return Plan(goal=self.goal, summary=self.summary,
                    tasks=list(self.tasks))

    @property
    def path_name(self) -> str:
        return f"{self.id}.json"

    def content_dict(self) -> dict[str, Any]:
        return {"goal": self.goal, "summary": self.summary,
                "tasks": [_task_to_dict(task) for task in self.tasks]}

    def compute_hash(self) -> str:
        content = self.content_dict()
        return compute_plan_hash(goal=content["goal"],
                                 summary=content["summary"],
                                 tasks=content["tasks"])

    def to_dict(self) -> dict[str, Any]:
        data = self.content_dict()
        data.update({"id": self.id, "created_at": self.created_at,
                     "created_by": self.created_by, "status": self.status,
                     "hash": self.hash or self.compute_hash()})
        return data

    @classmethod
    def from_dict(cls, data: Any) -> PlanRecord:
        validate_plan_payload(data)
        return cls(
            id=data["id"], goal=data["goal"], summary=data["summary"],
            tasks=[_task_from_dict(raw, index=index)
                   for index, raw in enumerate(data["tasks"], start=1)],
            created_at=data["created_at"], created_by=data["created_by"],
            status=data["status"], hash=data["hash"])


class PlanStore:
    """The ``.daemon/plans/`` directory: one JSON file per plan."""

    def __init__(self, workspace: Path | str) -> None:
        self.dir = project_dir(workspace) / "plans"
        self.dir.mkdir(parents=True, exist_ok=True)

    def create(self, plan: Plan, *, created_by: str) -> PlanRecord:
        record = PlanRecord(
            id=secrets.token_hex(4), goal=plan.goal,
            summary=plan.summary, tasks=list(plan.tasks),
            created_at=_utcnow(), created_by=created_by, status="pending")
        record.hash = record.compute_hash()
        self.save(record)
        return record

    def path_for(self, record: PlanRecord) -> Path:
        return self.dir / record.path_name

    def save(self, record: PlanRecord) -> Path:
        record.hash = record.compute_hash()
        path = self.dir / record.path_name
        path.write_text(json.dumps(record.to_dict(), indent=2) + "\n",
                        encoding="utf-8")
        return path

    def load(self, plan_id: str, *, check_hash: bool = True) -> PlanRecord:
        """Load + validate + re-hash. Any failure is a PlanError:
        a stored plan that cannot prove it is unchanged never
        executes.

        ``check_hash=False`` is for the recorded edit path only
        (``edit_plan_record``): the file may already carry a stale
        self-stored hash from a hand-edit, and the edit command's
        job is to re-record the content as it stands, not to
        refuse it. Every execute path loads with the default True.
        """
        path = self.dir / f"{plan_id}.json"
        if not path.exists():
            raise PlanError(
                f"no stored plan '{plan_id}' under {self.dir} "
                f"(create one with `daemon run --plan <goal>` or /plan)")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PlanError(
                f"plan '{plan_id}' is unreadable: {exc}") from exc
        validate_plan_payload(data, expect_id=plan_id)
        record = PlanRecord.from_dict(data)
        if check_hash and record.compute_hash() != record.hash:
            raise PlanError(
                f"plan '{plan_id}' was modified after creation "
                f"(content hash mismatch) — it will not execute. "
                f"Re-record the edit with `daemon plan --edit "
                f"{plan_id}` (or /edit-plan in the REPL), or create "
                f"a new plan.")
        return record


async def draft_plan(ctx: Any, goal: str, *, created_by: str) -> PlanRecord:
    """Ask the model for a plan, persist it, emit plan.created — and
    STOP. Nothing here executes anything."""
    context = ctx.index.summary() if ctx.index else ""
    request = ChatRequest(
        model=ctx.model.model_id,
        messages=[Message.user(planner_prompt(goal, context))],
        stream=True, max_tokens=2000)
    text = ""
    async for event in ctx.adapter.chat(request):
        if event.kind == "text" and event.text:
            text += event.text
    plan = parse_plan(goal, text)
    record = PlanStore(ctx.workspace).create(plan, created_by=created_by)
    emit_plan_created(ctx.session.meta.id, plan, plan_id=record.id,
                      plan_hash=record.hash)
    return record


def _editor_command(editor: str | list[str] | None) -> list[str]:
    """The editor argv: explicit argument first, then $VISUAL,
    then $EDITOR, then vi. A string is split shell-style, so
    "code --wait" works as well as "vi"."""
    if editor is not None:
        return list(editor) if isinstance(editor, list) \
            else shlex.split(editor)
    for var in ("VISUAL", "EDITOR"):
        value = os.environ.get(var)
        if value:
            return shlex.split(value)
    return ["vi"]


def edit_plan_record(store: PlanStore, plan_id: str, *, run_id: str | None,
                     editor: str | list[str] | None = None
                     ) -> tuple[PlanRecord, bool]:
    """The RECORDED edit path (review round 2, R1-3).

    Opens the stored plan in the user's editor and records the
    result as a ``plan.edited`` kernel event BEFORE any approval,
    so the recorded version — the anchor ``verify_approved``
    binds execution to — moves to the edited content. This is the
    only legitimate way to change a plan after creation: a file
    rewritten any other way keeps the old kernel record and
    refuses at execute time.

    Returns (record, changed): ``changed`` is False when the
    edited content hashes to the version the kernel already
    holds (the user saved without changing anything, or the file
    only carried a stale self-stored hash, which ``save`` here
    normalizes). Raises PlanError when the plan is missing or
    schema-broken, the editor cannot start or exits non-zero, or
    the edit could not be recorded in the kernel — in that last
    case the file is restored to its pre-edit bytes, so a
    half-recorded edit never leaves the plan in a state that
    refuses to execute with no way back.

    No tty check lives here: the CLI layer requires an interactive
    terminal (a non-interactive caller cannot be the user at an
    editor), and the REPL is interactive by construction.
    """
    path = store.dir / f"{plan_id}.json"
    if not path.exists():
        raise PlanError(
            f"no stored plan '{plan_id}' under {store.dir} "
            f"(create one with `daemon run --plan <goal>` or /plan)")
    if not run_id:
        raise PlanError(
            "cannot record a plan edit without a session — the "
            "edit event needs a run id, and there is none")
    original = path.read_bytes()
    # Schema only, no self-hash check: the file may already carry
    # a stale hash from a hand-edit, and re-recording it is the
    # point of this command.
    store.load(plan_id, check_hash=False)
    try:
        command = _editor_command(editor)
    except ValueError as exc:
        raise PlanError(f"cannot parse editor setting: {exc}") from exc
    try:
        result = subprocess.run([*command, str(path)])
    except OSError as exc:
        raise PlanError(
            f"cannot start editor {command[0]!r}: {exc} — set EDITOR "
            f"or VISUAL to an editor that exists") from exc
    if result.returncode != 0:
        raise PlanError(
            f"editor exited with status {result.returncode} — the "
            f"plan is unchanged")
    after = store.load(plan_id, check_hash=False)
    after_hash = after.compute_hash()
    recorded = find_plan_recorded(plan_id)
    recorded_hash = recorded.get("plan_hash") if recorded else None
    if after_hash == recorded_hash:
        store.save(after)  # normalizes a stale self-stored hash
        return after, False
    seq = emit_plan_edited(run_id, after.plan, plan_id=after.id,
                           plan_hash=after_hash)
    if seq is None:
        try:
            path.write_bytes(original)
        except OSError:
            pass  # best effort: the error below names the recovery
        raise PlanError(
            "plan edit could not be recorded in the event kernel — "
            "refusing to claim it is recorded. The plan file was "
            "restored to its pre-edit content; re-run "
            f"`daemon plan --edit {plan_id}` once the kernel is "
            f"reachable.")
    store.save(after)
    return after, True


def verify_approved(record: PlanRecord) -> None:
    """Check a loaded plan against the kernel's records.

    Two anchors, both required (review round 2, R1-3):

    1. The RECORDED version — the latest plan.created / plan.edited
       event for the plan id. The file's freshly computed hash must
       equal it. A plan rewritten outside the recorded edit flow —
       forged hash or not — fails here even when the approval panel
       was shown the forged content and the user approved it: the
       approval was recorded from the same forged file, so the
       approval check alone can never bite on a pre-approval
       forgery. Only a plan.edited event (the user opening the
       plan in their editor through ``daemon plan --edit`` /
       /edit-plan) moves this anchor.
    2. The APPROVAL — the latest plan.approved event. The file's
       hash must equal it too: a recorded edit after approval (the
       version moved, the approval did not) refuses until the
       edited plan is approved again.

    The file's own stored hash is never trusted here — only the
    kernel copies, which live under DAEMON_HOME, outside the
    workspace. The execute path calls this AFTER approval, on a
    record freshly re-loaded from disk.
    """
    recorded = find_plan_recorded(record.id)
    if recorded is None:
        raise PlanError(
            f"plan '{record.id}' has no creation record in the "
            f"event kernel — it will not execute. Re-record it "
            f"with `daemon plan --edit {record.id}` (or /edit-plan "
            f"in the REPL), or create a new plan.")
    recorded_hash = recorded.get("plan_hash")
    if not isinstance(recorded_hash, str) or not recorded_hash:
        raise PlanError(
            f"plan '{record.id}' creation record carries no content "
            f"hash — it will not execute.")
    current = record.compute_hash()
    if current != recorded_hash:
        raise PlanError(
            f"plan '{record.id}' differs from the version recorded "
            f"in the event kernel (recorded hash "
            f"{recorded_hash[:12]}…, file now {current[:12]}…) — "
            f"it will not execute. The plan file was rewritten "
            f"outside the recorded edit flow; re-record the edit "
            f"with `daemon plan --edit {record.id}` (or /edit-plan "
            f"in the REPL) and approve it again.")
    approved = find_plan_approved(record.id)
    if approved is None:
        raise PlanError(
            f"plan '{record.id}' has no approval recorded in the "
            f"event kernel — it will not execute. Approve it through "
            f"the broker gate first.")
    approved_hash = approved.get("plan_hash")
    if not isinstance(approved_hash, str) or not approved_hash:
        raise PlanError(
            f"plan '{record.id}' approval record carries no content "
            f"hash — it will not execute.")
    if current != approved_hash:
        raise PlanError(
            f"plan '{record.id}' changed after approval "
            f"(approved hash {approved_hash[:12]}…, file now "
            f"{current[:12]}…) — it will not execute. Re-approve "
            f"the edited plan to run it.")


async def run_plan_tasks(runtime: Any, tracker: TaskTracker, *,
                        renderer: Any, note: str = "") -> bool:
    """Execute the tracker's tasks in order through the runtime.

    Returns True when every task finished; False when the loop
    stopped early (a task failed, its verification failed, the run
    was interrupted, or the runtime raised). Per-task verification:
    the runtime's enforced post-turn verification runs after every
    task that changes files, and a FAILED verification stops the
    loop exactly like a failed task — the plan does not continue
    past a task that did not prove itself.
    """
    assert tracker.plan is not None
    while True:
        task = tracker.plan.next_task()
        if task is None:
            return True
        tracker.start(task)
        renderer.rule(f"task {task.id}: {task.title}")
        brief = (f"Execute task {task.id}: {task.title}\n"
                 f"{task.description}\n"
                 + (f"Files: {', '.join(task.files)}\n" if task.files
                    else "")
                 + (f"Verification: {task.verification}\n"
                    if task.verification else "")
                 + (f"Note: {note}" if note else ""))
        try:
            result = await runtime.run(brief)
        except Exception as exc:  # noqa: BLE001 - a task error stops the loop
            tracker.finish(task, success=False, result=str(exc))
            renderer.line("error", f"task {task.id} failed: {exc}")
            return False
        verified = runtime.verification_passed
        if result.interrupted:
            tracker.finish(task, success=False, result="interrupted")
            renderer.line("warn", f"task {task.id} interrupted — stopping")
            return False
        if verified is False:
            tracker.finish(
                task, success=False,
                result=result.verification or "verification failed")
            renderer.line(
                "error",
                f"task {task.id} verification FAILED — stopping")
            return False
        tracker.finish(task, success=True, result=result.text or "")
        renderer.task_list(tracker)
