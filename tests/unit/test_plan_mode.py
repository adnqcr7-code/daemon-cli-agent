"""Tests for v0.9.0 §2.3 plan mode: persist + stop, broker approval,
re-hash refusal, --yes never substituting, per-task verification."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from click.testing import CliRunner

from daemon.agent.planner import Plan, Task
from daemon.cli import main
from daemon.plans import PlanError, PlanRecord, PlanStore, compute_plan_hash
from daemon.ui.render import Renderer

# Scripted editor for the recorded edit path: retargets task 1 to
# b.py. The stored hash is left stale on purpose — the edit path
# re-hashes on save, which is exactly what a hand-edit in a real
# editor looks like.
_EDITOR_RETARGET_B = (
    "import json,sys;p=sys.argv[1];d=json.load(open(p));"
    "d['tasks'][0]['title']='Execute: add a docstring to b.py';"
    "d['tasks'][0]['description']='Edited: operate on b.py.';"
    "d['tasks'][0]['files']=['b.py'];json.dump(d,open(p,'w'))"
)


def _retarget_editor_argv() -> list[str]:
    return [sys.executable, "-c", _EDITOR_RETARGET_B]


def _sample_plan() -> Plan:
    return Plan(goal="add a docstring to a.py", summary="scripted",
                tasks=[Task(id="1", title="Execute: add a docstring",
                            description="docstring a.py", files=["a.py"],
                            verification="check the docstring",
                            risk="low")])


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.py").write_text("def add(a, b):\n    return a + b\n",
                                    encoding="utf-8")
    return workspace


# ------------------------------------------------------------ store + hash


def test_store_round_trip_and_hash(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    store = PlanStore(workspace)
    record = store.create(_sample_plan(), created_by="test")
    assert record.hash == compute_plan_hash(
        goal=record.goal, summary=record.summary,
        tasks=[{"id": "1", "title": "Execute: add a docstring",
                "description": "docstring a.py", "files": ["a.py"],
                "depends_on": [], "verification": "check the docstring",
                "risk": "low", "status": "pending", "assigned": "",
                "result": ""}])
    loaded = store.load(record.id)
    assert loaded.goal == record.goal
    assert loaded.tasks[0].title == record.tasks[0].title
    assert loaded.hash == record.hash


def test_mutated_plan_refuses_to_load(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    store = PlanStore(workspace)
    record = store.create(_sample_plan(), created_by="test")
    path = store.path_for(record)
    data = json.loads(path.read_text("utf-8"))
    data["tasks"][0]["title"] = "Execute: delete everything"
    path.write_text(json.dumps(data), encoding="utf-8")
    try:
        store.load(record.id)
    except PlanError as exc:
        assert "modified after creation" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("mutated plan loaded")


def test_schema_violation_refuses_to_load(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    store = PlanStore(workspace)
    record = store.create(_sample_plan(), created_by="test")
    path = store.path_for(record)
    data = json.loads(path.read_text("utf-8"))
    data["tasks"][0]["risk"] = "certain doom"
    path.write_text(json.dumps(data), encoding="utf-8")
    try:
        store.load(record.id)
    except PlanError as exc:
        assert "risk" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("schema-breaking plan loaded")


def test_unknown_plan_refuses(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    try:
        PlanStore(workspace).load("deadbeef")
    except PlanError as exc:
        assert "no stored plan" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("missing plan loaded")


def test_record_rejects_unknown_fields() -> None:
    record = PlanRecord(id="abcd1234", goal="g", summary="s")
    data = record.to_dict()
    data["surprise"] = True
    try:
        PlanRecord.from_dict(data)
    except PlanError as exc:
        assert "unknown field" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("unknown field accepted")


# ------------------------------------------------------- CLI: create + stop


def test_run_plan_creates_and_stops(tmp_path) -> None:
    """`daemon run --plan <goal>` persists the plan and STOPS: a.py
    is untouched, the plan file exists with a hash."""
    workspace = _workspace(tmp_path)
    result = CliRunner().invoke(main, [
        "run", "add a docstring to a.py", "--plan", "--provider", "demo",
        "-w", str(workspace)])
    assert result.exit_code == 0, result.output
    assert "created (not executed)" in result.output
    plans = list((workspace / ".daemon" / "plans").glob("*.json"))
    assert len(plans) == 1
    record = PlanStore(workspace).load(plans[0].stem)
    assert record.goal == "add a docstring to a.py"
    assert '"""Add."""' not in (workspace / "a.py").read_text("utf-8")
    assert "daemon run --execute-plan " in result.output


def test_run_plan_execute_approves_and_runs(tmp_path, monkeypatch) -> None:
    """plan -> approve (stubbed interactive approver) -> execute:
    the docstring lands and verification passes."""
    workspace = _workspace(tmp_path)
    runner = CliRunner()
    created = runner.invoke(main, [
        "run", "add a docstring to a.py", "--plan", "--provider", "demo",
        "-w", str(workspace)])
    assert created.exit_code == 0, created.output
    plan_id = re.search(r"plan ([0-9a-f]+) created", created.output).group(1)
    monkeypatch.setattr(Renderer, "approve",
                        lambda self, decision: (True, False))
    executed = runner.invoke(main, [
        "run", "--execute-plan", plan_id, "--provider", "demo",
        "-w", str(workspace)])
    assert executed.exit_code == 0, executed.output
    assert '"""Add."""' in (workspace / "a.py").read_text("utf-8")


def test_run_plan_execute_refuses_without_approver(tmp_path) -> None:
    """No tty in CliRunner -> the renderer's approve refuses, and
    --yes does NOT substitute: nothing runs."""
    workspace = _workspace(tmp_path)
    runner = CliRunner()
    created = runner.invoke(main, [
        "run", "add a docstring to a.py", "--plan", "--provider", "demo",
        "-w", str(workspace)])
    plan_id = re.search(r"plan ([0-9a-f]+) created", created.output).group(1)
    refused = runner.invoke(main, [
        "run", "--execute-plan", plan_id, "--provider", "demo", "--yes",
        "-w", str(workspace)])
    assert refused.exit_code == 1
    assert "not approved" in refused.output
    assert '"""Add."""' not in (workspace / "a.py").read_text("utf-8")


def test_run_plan_execute_refuses_mutated_plan(tmp_path,
                                              monkeypatch) -> None:
    workspace = _workspace(tmp_path)
    runner = CliRunner()
    created = runner.invoke(main, [
        "run", "add a docstring to a.py", "--plan", "--provider", "demo",
        "-w", str(workspace)])
    plan_id = re.search(r"plan ([0-9a-f]+) created", created.output).group(1)
    store = PlanStore(workspace)
    path = store.path_for(store.load(plan_id))
    data = json.loads(path.read_text("utf-8"))
    data["tasks"][0]["title"] = "Execute: rm -rf /"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(Renderer, "approve",
                        lambda self, decision: (True, False))
    result = runner.invoke(main, [
        "run", "--execute-plan", plan_id, "--provider", "demo",
        "-w", str(workspace)])
    assert result.exit_code == 2
    assert "modified after creation" in result.output
    assert '"""Add."""' not in (workspace / "a.py").read_text("utf-8")


def test_plan_command_persists(tmp_path) -> None:
    """`daemon plan` (no --execute) also persists and stops."""
    workspace = _workspace(tmp_path)
    result = CliRunner().invoke(main, [
        "plan", "add a docstring to a.py", "--provider", "demo",
        "-w", str(workspace)])
    assert result.exit_code == 0, result.output
    plans = list((workspace / ".daemon" / "plans").glob("*.json"))
    assert len(plans) == 1
    assert '"""Add."""' not in (workspace / "a.py").read_text("utf-8")


# ------------------------------------------- review round 1, R1-3 (plan gate)


def _forge_plan(path: Path, *, title: str, description: str,
                files: list[str]) -> None:
    """Rewrite a stored plan the way a forger would: change the
    tasks AND recompute the self-stored hash, so the file's own
    check passes."""
    data = json.loads(path.read_text("utf-8"))
    data["tasks"][0]["title"] = title
    data["tasks"][0]["description"] = description
    data["tasks"][0]["files"] = files
    data["hash"] = compute_plan_hash(goal=data["goal"],
                                     summary=data["summary"],
                                     tasks=data["tasks"])
    path.write_text(json.dumps(data), encoding="utf-8")


def test_forged_plan_with_recomputed_hash_refuses(tmp_path) -> None:
    """R1-3a (round 2): a plan forged with a recomputed self-stored
    hash loads fine, but the kernel-anchored check refuses it — the
    creation record holds the ORIGINAL hash, outside the workspace,
    and the approval recorded from the forged file cannot move it."""
    from daemon.events import emit_plan_approved, emit_plan_created
    from daemon.permissions import PermissionEngine
    from daemon.plans import verify_approved

    workspace = _workspace(tmp_path)
    store = PlanStore(workspace)
    record = store.create(_sample_plan(), created_by="test")
    assert emit_plan_created("test-run-1", record.plan,
                             plan_id=record.id,
                             plan_hash=record.hash) is not None
    engine = PermissionEngine(
        workspace, approver=lambda decision: (True, False))
    assert engine.approve_plan(
        plan_id=record.id, goal=record.goal,
        task_count=len(record.tasks),
        files=[file for task in record.tasks for file in task.files],
        plan_hash=record.hash, tasks=list(record.tasks))
    seq = emit_plan_approved("test-run-1", record.plan, via="test",
                             plan_id=record.id, plan_hash=record.hash)
    assert seq is not None
    _forge_plan(store.path_for(record),
                title="Execute: add a docstring to b.py",
                description="Tampered: operate on b.py.",
                files=["b.py"])
    forged = store.load(record.id)  # loads: the self-hash is recomputed
    assert forged.tasks[0].files == ["b.py"]
    try:
        verify_approved(forged)
    except PlanError as exc:
        assert "differs from the version recorded" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("forged plan passed the kernel check")


def test_verify_refuses_plan_with_no_creation_record(tmp_path) -> None:
    """R1-3 (round 2): a plan with an approval but no creation
    record cannot execute — the anchor is missing, so there is
    nothing to compare the file against. Fail closed, with the
    recovery named."""
    from daemon.events import emit_plan_approved
    from daemon.plans import verify_approved

    workspace = _workspace(tmp_path)
    store = PlanStore(workspace)
    record = store.create(_sample_plan(), created_by="test")
    assert emit_plan_approved("test-run-1", record.plan, via="test",
                              plan_id=record.id,
                              plan_hash=record.hash) is not None
    try:
        verify_approved(store.load(record.id))
    except PlanError as exc:
        assert "no creation record" in str(exc)
        assert "daemon plan --edit" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("unrecorded plan passed the kernel check")


def test_execute_plan_refuses_tampered_plan(tmp_path,
                                           monkeypatch) -> None:
    """R1-3a (round 2): when a plan is tampered BEFORE approval, the
    approval panel still shows the tampered titles/descriptions —
    the user sees what the file says — but execution REFUSES: the
    forged content does not match the creation record, and the
    approval recorded from the forged file cannot move that anchor.
    Neither file is touched. (Round 1 pinned the opposite as
    intended; that behaviour was the vulnerability.)"""
    workspace = _workspace(tmp_path)
    (workspace / "b.py").write_text(
        "def sub(a, b):\n    return a - b\n", encoding="utf-8")
    runner = CliRunner()
    created = runner.invoke(main, [
        "run", "add a docstring to a.py", "--plan", "--provider", "demo",
        "-w", str(workspace)])
    assert created.exit_code == 0, created.output
    plan_id = re.search(r"plan ([0-9a-f]+) created", created.output).group(1)
    store = PlanStore(workspace)
    _forge_plan(store.path_for(store.load(plan_id)),
                title="Execute: add a docstring to b.py",
                description="Tampered: operate on b.py instead of a.py.",
                files=["b.py"])
    seen: dict[str, object] = {}

    def _approve(self, decision):
        seen["decision"] = decision
        return True, False

    monkeypatch.setattr(Renderer, "approve", _approve)
    executed = runner.invoke(main, [
        "run", "--execute-plan", plan_id, "--provider", "demo",
        "-w", str(workspace)])
    assert executed.exit_code == 2, executed.output
    assert "differs from the version recorded" in executed.output
    decision = seen["decision"]
    assert isinstance(decision, object)
    details = "\n".join(decision.details)
    assert "add a docstring to b.py" in details
    assert "Tampered: operate on b.py instead of a.py." in details
    assert '"""Sub."""' not in (workspace / "b.py").read_text("utf-8")
    assert '"""Add."""' not in (workspace / "a.py").read_text("utf-8")


def test_execute_refuses_plan_tampered_during_approval(
        tmp_path, monkeypatch) -> None:
    """R1-3a (round 2): a plan rewritten between approval and
    execution — the forger acting while the user is at the prompt —
    refuses with exit 2, because the kernel's creation record
    still holds the pre-tamper hash. The approval was emitted from
    the forged file, so only the creation anchor can catch it."""
    workspace = _workspace(tmp_path)
    (workspace / "b.py").write_text(
        "def sub(a, b):\n    return a - b\n", encoding="utf-8")
    runner = CliRunner()
    created = runner.invoke(main, [
        "run", "add a docstring to a.py", "--plan", "--provider", "demo",
        "-w", str(workspace)])
    assert created.exit_code == 0, created.output
    plan_id = re.search(r"plan ([0-9a-f]+) created", created.output).group(1)
    store = PlanStore(workspace)

    def _approve_and_tamper(self, decision):
        _forge_plan(store.path_for(store.load(plan_id)),
                    title="Execute: add a docstring to b.py",
                    description="Tampered mid-approval.",
                    files=["b.py"])
        return True, False

    monkeypatch.setattr(Renderer, "approve", _approve_and_tamper)
    result = runner.invoke(main, [
        "run", "--execute-plan", plan_id, "--provider", "demo",
        "-w", str(workspace)])
    assert result.exit_code == 2, result.output
    assert "differs from the version recorded" in result.output
    assert '"""Add."""' not in (workspace / "a.py").read_text("utf-8")
    assert '"""Add."""' not in (workspace / "b.py").read_text("utf-8")


def test_repl_build_executes_edited_plan(tmp_path, monkeypatch) -> None:
    """R1-3b (round 2): an edit to the stored plan between /plan and
    /build is honored — but only through the RECORDED edit path:
    /edit-plan opens the plan in $EDITOR (scripted here to retarget
    it to b.py) and records a plan.edited event, so /build runs the
    edited file, not the stale in-memory tracker, and the kernel
    anchor moves with it.

    The assertion is on the brief handed to the runtime (the
    instruction the model acts on) and on the tracker state, not on
    which file the scripted demo provider touches: the demo keys
    off the session's FIRST user message ("/plan ... a.py"), so it
    edits a.py whatever the brief says. A real model acts on the
    brief, and the brief is what the stale tracker used to get
    wrong.
    """
    from daemon.cli import _make_renderer
    from daemon.config import Config
    from daemon.context import build_context
    from daemon.events import EventStore
    from daemon.ui.repl import Repl

    workspace = _workspace(tmp_path)
    (workspace / "b.py").write_text(
        "def sub(a, b):\n    return a - b\n", encoding="utf-8")
    config = Config.load(workspace)
    renderer = _make_renderer(config, plain=True)
    ctx = build_context(workspace=workspace, config=config,
                        renderer=renderer, interactive=True,
                        profile_id="demo")
    repl = Repl(ctx, renderer)
    try:
        repl._loop.run_until_complete(
            repl._do_plan("add a docstring to a.py"))
        plan_id = repl._active_plan_id
        assert plan_id is not None
        monkeypatch.setenv(
            "EDITOR",
            f"{sys.executable} -c \"{_EDITOR_RETARGET_B}\"")
        assert repl._cmd_edit_plan("") is False
        events = EventStore().read_by_type("plan.edited")
        assert any(event.payload.get("plan_id") == plan_id
                   for event in events)
        ctx.permissions.approver = lambda decision: (True, False)
        briefs: list[str] = []
        original_run = repl.runtime.run

        async def _spy_run(user_text: str):
            briefs.append(user_text)
            return await original_run(user_text)

        repl.runtime.run = _spy_run
        repl._loop.run_until_complete(repl._execute_plan(""))
        assert len(briefs) == 1
        assert "Execute: add a docstring to b.py" in briefs[0]
        assert "Files: b.py" in briefs[0]
        assert repl.tracker.plan is not None
        assert repl.tracker.plan.tasks[0].files == ["b.py"]
        assert repl.tracker.plan.tasks[0].title == \
            "Execute: add a docstring to b.py"
    finally:
        ctx.close()
        repl._loop.close()


def test_repl_dispatch_routes_edit_plan(tmp_path, monkeypatch) -> None:
    """R1-3d (review round 3): /edit-plan must dispatch through
    _dispatch — the previous test called _cmd_edit_plan directly,
    so the suite stayed green while the user-facing command
    answered "unknown command /edit-plan". This drives the command
    through _dispatch itself and asserts the recorded edit lands
    in the kernel."""
    from daemon.cli import _make_renderer
    from daemon.config import Config
    from daemon.context import build_context
    from daemon.events import EventStore
    from daemon.ui.repl import Repl

    workspace = _workspace(tmp_path)
    (workspace / "b.py").write_text(
        "def sub(a, b):\n    return a - b\n", encoding="utf-8")
    config = Config.load(workspace)
    renderer = _make_renderer(config, plain=True)
    ctx = build_context(workspace=workspace, config=config,
                        renderer=renderer, interactive=True,
                        profile_id="demo")
    repl = Repl(ctx, renderer)
    try:
        repl._loop.run_until_complete(
            repl._do_plan("add a docstring to a.py"))
        plan_id = repl._active_plan_id
        assert plan_id is not None
        assert repl._handler_for("/edit-plan") is not None
        monkeypatch.setenv(
            "EDITOR",
            f"{sys.executable} -c \"{_EDITOR_RETARGET_B}\"")
        assert repl._dispatch("/edit-plan") is False
        events = EventStore().read_by_type("plan.edited")
        assert any(event.payload.get("plan_id") == plan_id
                   for event in events)
        assert repl.tracker.plan is not None
        assert repl.tracker.plan.tasks[0].files == ["b.py"]
    finally:
        ctx.close()
        repl._loop.close()


def test_help_commands_all_have_handlers(tmp_path) -> None:
    """R1-3d (review round 3): every command /help advertises
    must resolve to a real handler through _dispatch's lookup.
    A command with no handler is a dead end the user can be told
    to use — as /edit-plan was. This pins the wiring, not the
    behaviour: handlers are checked, not invoked."""
    from daemon.cli import _make_renderer
    from daemon.config import Config
    from daemon.context import build_context
    from daemon.ui.repl import SLASH_COMMANDS, Repl

    workspace = _workspace(tmp_path)
    config = Config.load(workspace)
    renderer = _make_renderer(config, plain=True)
    ctx = build_context(workspace=workspace, config=config,
                        renderer=renderer, interactive=True,
                        profile_id="demo")
    repl = Repl(ctx, renderer)
    try:
        missing = [name for name in sorted(SLASH_COMMANDS)
                   if repl._handler_for(name) is None]
        assert missing == [], f"advertised but unreachable: {missing}"
        # /edit-plan is the only hyphenated command; its handler
        # must be the underscore method, not a missing lookup.
        assert repl._handler_for("/edit-plan").__func__ is \
            Repl._cmd_edit_plan
    finally:
        ctx.close()
        repl._loop.close()


def test_repl_build_refuses_unrecorded_edit(tmp_path) -> None:
    """R1-3 (round 2): the old flow — rewriting the plan file
    directly between /plan and /build, with no recorded edit —
    now refuses at /build: the file differs from the creation
    record and no plan.edited event moves the anchor."""
    from daemon.cli import _make_renderer
    from daemon.config import Config
    from daemon.context import build_context
    from daemon.ui.repl import Repl

    workspace = _workspace(tmp_path)
    (workspace / "b.py").write_text(
        "def sub(a, b):\n    return a - b\n", encoding="utf-8")
    config = Config.load(workspace)
    renderer = _make_renderer(config, plain=True)
    ctx = build_context(workspace=workspace, config=config,
                        renderer=renderer, interactive=True,
                        profile_id="demo")
    repl = Repl(ctx, renderer)
    try:
        repl._loop.run_until_complete(
            repl._do_plan("add a docstring to a.py"))
        plan_id = repl._active_plan_id
        assert plan_id is not None
        store = PlanStore(workspace)
        _forge_plan(store.path_for(store.load(plan_id)),
                    title="Execute: add a docstring to b.py",
                    description="Edited: operate on b.py.",
                    files=["b.py"])
        ctx.permissions.approver = lambda decision: (True, False)
        repl._loop.run_until_complete(repl._execute_plan(""))
        assert '"""Sub."""' not in (workspace / "b.py").read_text("utf-8")
        assert '"""Add."""' not in (workspace / "a.py").read_text("utf-8")
    finally:
        ctx.close()
        repl._loop.close()


# ------------------------------------------- review round 2, R1-3 (edit path)


def test_plan_edit_then_execute_succeeds(tmp_path, monkeypatch) -> None:
    """R1-3 (round 2): edit through the recorded edit path, then
    approve, then execute — the edited plan runs, and b.py (not
    a.py) gets the docstring. The plan.edited event is in the
    kernel, and the execution binds to it."""
    from daemon.events import EventStore

    workspace = _workspace(tmp_path)
    (workspace / "b.py").write_text(
        "def sub(a, b):\n    return a - b\n", encoding="utf-8")
    runner = CliRunner()
    created = runner.invoke(main, [
        "run", "add a docstring to a.py", "--plan", "--provider", "demo",
        "-w", str(workspace)])
    assert created.exit_code == 0, created.output
    plan_id = re.search(r"plan ([0-9a-f]+) created", created.output).group(1)
    monkeypatch.setattr(os, "isatty", lambda fd: True)
    monkeypatch.setenv(
        "EDITOR", f"{sys.executable} -c \"{_EDITOR_RETARGET_B}\"")
    edited = runner.invoke(main, [
        "plan", "--edit", plan_id, "-w", str(workspace)])
    assert edited.exit_code == 0, edited.output
    assert "recorded in the event kernel" in edited.output
    assert any(event.payload.get("plan_id") == plan_id
               for event in EventStore().read_by_type("plan.edited"))
    monkeypatch.setattr(Renderer, "approve",
                        lambda self, decision: (True, False))
    executed = runner.invoke(main, [
        "run", "--execute-plan", plan_id, "--provider", "demo",
        "-w", str(workspace)])
    assert executed.exit_code == 0, executed.output
    assert '"""Sub."""' in (workspace / "b.py").read_text("utf-8")
    assert '"""Add."""' not in (workspace / "a.py").read_text("utf-8")


def test_plan_edit_unchanged_records_nothing(tmp_path, monkeypatch) -> None:
    """The editor exits 0 without changing the content: no
    plan.edited event, and the plan still executes (the recorded
    version already matches)."""
    from daemon.events import EventStore

    workspace = _workspace(tmp_path)
    runner = CliRunner()
    created = runner.invoke(main, [
        "run", "add a docstring to a.py", "--plan", "--provider", "demo",
        "-w", str(workspace)])
    assert created.exit_code == 0, created.output
    plan_id = re.search(r"plan ([0-9a-f]+) created", created.output).group(1)
    monkeypatch.setattr(os, "isatty", lambda fd: True)
    monkeypatch.setenv("EDITOR", "true")
    edited = runner.invoke(main, [
        "plan", "--edit", plan_id, "-w", str(workspace)])
    assert edited.exit_code == 0, edited.output
    assert "unchanged" in edited.output
    assert not [event for event in EventStore().read_by_type("plan.edited")
                if event.payload.get("plan_id") == plan_id]
    monkeypatch.setattr(Renderer, "approve",
                        lambda self, decision: (True, False))
    executed = runner.invoke(main, [
        "run", "--execute-plan", plan_id, "--provider", "demo",
        "-w", str(workspace)])
    assert executed.exit_code == 0, executed.output
    assert '"""Add."""' in (workspace / "a.py").read_text("utf-8")


def test_plan_edit_refuses_without_tty(tmp_path, monkeypatch) -> None:
    """`daemon plan --edit` is a deliberate user act at a terminal:
    without a tty it refuses outright, so a scripted caller cannot
    re-record a forgery with a no-op EDITOR."""
    workspace = _workspace(tmp_path)
    runner = CliRunner()
    created = runner.invoke(main, [
        "run", "add a docstring to a.py", "--plan", "--provider", "demo",
        "-w", str(workspace)])
    assert created.exit_code == 0, created.output
    plan_id = re.search(r"plan ([0-9a-f]+) created", created.output).group(1)
    monkeypatch.setattr(os, "isatty", lambda fd: False)
    monkeypatch.setenv("EDITOR", "true")
    edited = runner.invoke(main, [
        "plan", "--edit", plan_id, "-w", str(workspace)])
    assert edited.exit_code == 2, edited.output
    assert "interactive terminal" in edited.output
    assert '"""Add."""' not in (workspace / "a.py").read_text("utf-8")


def test_plan_edit_unknown_plan_refuses(tmp_path, monkeypatch) -> None:
    workspace = _workspace(tmp_path)
    monkeypatch.setattr(os, "isatty", lambda fd: True)
    monkeypatch.setenv("EDITOR", "true")
    result = CliRunner().invoke(main, [
        "plan", "--edit", "deadbeef", "-w", str(workspace)])
    assert result.exit_code == 2, result.output
    assert "no stored plan" in result.output


def test_plan_edit_rejects_goal_and_execute(tmp_path) -> None:
    result = CliRunner().invoke(main, [
        "plan", "some goal", "--edit", "deadbeef", "-w", str(tmp_path)])
    assert result.exit_code != 0
    assert "--edit cannot be combined" in result.output


def test_always_grant_scoped_to_plan_content(tmp_path) -> None:
    """R1-3c: 'always this session' on a plan gate covers exactly
    the approved content — a tampered plan prompts again instead of
    silently re-executing."""
    from daemon.permissions import PermissionEngine

    workspace = _workspace(tmp_path)
    store = PlanStore(workspace)
    record = store.create(_sample_plan(), created_by="test")
    engine = PermissionEngine(workspace)
    calls: list[str] = []

    def _recording_approver(decision):
        calls.append(decision.tool)
        return True, False

    # First approval: the user picks "always this session".
    assert engine.approve_plan(
        plan_id=record.id, goal=record.goal, task_count=1,
        files=["a.py"], plan_hash=record.hash, tasks=list(record.tasks),
        approver=lambda decision: (True, True))
    # Same content again: the remembered grant applies, no prompt.
    assert engine.approve_plan(
        plan_id=record.id, goal=record.goal, task_count=1,
        files=["a.py"], plan_hash=record.hash, tasks=list(record.tasks),
        approver=_recording_approver)
    assert calls == []
    # Tampered content: a different hash means a different key —
    # the prompt must appear again.
    tampered = PlanRecord(
        id=record.id, goal=record.goal, summary=record.summary,
        tasks=[Task(id="1", title="Execute: add a docstring to b.py",
                    description="Tampered.", files=["b.py"],
                    verification="check the docstring", risk="low")])
    tampered_hash = tampered.compute_hash()
    assert tampered_hash != record.hash
    assert engine.approve_plan(
        plan_id=record.id, goal=record.goal, task_count=1,
        files=["b.py"], plan_hash=tampered_hash,
        tasks=list(tampered.tasks), approver=_recording_approver)
    assert calls == ["plan.execute"]


def test_repl_plan_build_gate(tmp_path) -> None:
    """REPL /plan persists and stops; /build re-hashes, asks the
    broker, and executes only on approval — and the approval lands
    in the kernel as plan.approved."""
    from daemon.cli import _make_renderer
    from daemon.config import Config
    from daemon.context import build_context
    from daemon.events import EventStore
    from daemon.ui.repl import Repl

    workspace = _workspace(tmp_path)
    config = Config.load(workspace)
    renderer = _make_renderer(config, plain=True)
    ctx = build_context(workspace=workspace, config=config,
                        renderer=renderer, interactive=True,
                        profile_id="demo")
    repl = Repl(ctx, renderer)
    try:
        repl._loop.run_until_complete(
            repl._do_plan("add a docstring to a.py"))
        assert repl._active_plan_id is not None
        assert '"""Add."""' not in (workspace / "a.py").read_text("utf-8")
        # Plain renderer: approve() refuses -> nothing runs.
        repl._loop.run_until_complete(repl._execute_plan(""))
        assert '"""Add."""' not in (workspace / "a.py").read_text("utf-8")
        # Approval granted -> executes, docstring lands.
        ctx.permissions.approver = lambda decision: (True, False)
        repl._loop.run_until_complete(repl._execute_plan(""))
        assert '"""Add."""' in (workspace / "a.py").read_text("utf-8")
        events = EventStore().read(ctx.session.meta.id)
        assert any(event.type == "plan.approved" for event in events)
        assert any(event.type == "plan.created" for event in events)
    finally:
        ctx.close()
        repl._loop.close()
