"""v0.9.0 Phase 1: the minimal append-only event kernel.

Pins the kernel contract: append-only (no mutation API), per-run
monotonic seq, the closed type set, redaction, best-effort failure
(a dead store never breaks a run), projections, and `daemon replay`
determinism + divergence reporting. The lifecycle test drives a real
demo-provider run through the real context/runtime (no mocks of the
thing under test) and asserts all twelve event types land for one
run id.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from daemon.agent.planner import parse_plan
from daemon.context import build_context
from daemon.events import (
    EVENT_TYPES,
    Event,
    EventStore,
    emit,
    emit_plan_approved,
    emit_plan_created,
    emit_plan_edited,
    find_plan_recorded,
    project_checkpoints,
    project_messages,
    project_plan,
    replay_report,
)
from daemon.plans import PlanStore, edit_plan_record
from daemon.security import redactor

SOURCE = "def add(a, b):\n    return a + b\n"

CANNED_PLAN = json.dumps({
    "summary": "Add docstrings to the module.",
    "tasks": [
        {"id": "1", "title": "Add a docstring to a.py",
         "description": "Add a module-level docstring.",
         "files": ["a.py"], "verification": "python -m compileall a.py",
         "risk": "low"},
        {"id": "2", "title": "Run the test suite",
         "description": "Prove nothing broke.",
         "files": ["test_a.py"], "verification": "pytest -q",
         "risk": "low"},
    ],
})


def _types(store: EventStore, run_id: str) -> set[str]:
    return {event.type for event in store.read(run_id)}


def test_no_mutation_api_exists():
    store = EventStore()
    for name in ("update", "delete", "remove", "clear", "purge", "rewrite"):
        assert not hasattr(store, name), f"EventStore must not offer {name}"


def test_seq_monotonic_per_run_and_independent_across_runs(tmp_path):
    store = EventStore(home=tmp_path)
    assert store.append("run-a", "run.started", {"task": "x"}) == 1
    assert store.append("run-a", "message.user", {"message": {}}) == 2
    assert store.append("run-a", "run.finished", {}) == 3
    assert store.append("run-b", "run.started", {"task": "y"}) == 1
    assert store.append("run-b", "run.finished", {}) == 2
    assert [event.seq for event in store.read("run-a")] == [1, 2, 3]
    assert [event.seq for event in store.read("run-b")] == [1, 2]


def test_unknown_type_refused(tmp_path):
    store = EventStore(home=tmp_path)
    assert store.append("run-a", "run.exploded", {}) is None
    assert store.read("run-a") == []
    assert not store.has_run("run-a")


def test_payload_redacted(tmp_path):
    secret = "TEST-SECRET-VALUE-12345"
    redactor().register(secret)
    store = EventStore(home=tmp_path)
    store.append("run-a", "run.started",
                 {"task": f"do it with {secret} inside"})
    stored = store.read("run-a")[0].payload["task"]
    assert secret not in stored
    assert "***REDACTED***" in stored


def test_append_survives_unwritable_home(tmp_path):
    # events.db as a directory: sqlite cannot open it, so append
    # must fail soft (None) instead of raising.
    (tmp_path / "events.db").mkdir()
    store = EventStore(home=tmp_path)
    assert store.append("run-a", "run.started", {}) is None
    assert store.read("run-a") == []


def test_emit_never_raises_without_run_id():
    assert emit(None, "run.started", {}) is None
    assert emit("", "run.started", {}) is None


def test_projections_are_pure_and_group_tool_results():
    events = [
        Event(1, "r", 1, "message.user",
              {"message": {"role": "user", "text": "hi",
                           "tool_calls": [], "tool_results": []}}, "t"),
        Event(2, "r", 2, "message.assistant",
              {"message": {"role": "assistant", "text": "ok",
                           "tool_calls": [{"id": "c1", "name": "read_file",
                                           "arguments": {}}],
                           "tool_results": []}}, "t"),
        Event(3, "r", 3, "tool.called",
              {"tool_call_id": "c1", "tool_name": "read_file"}, "t"),
        Event(4, "r", 4, "tool.result",
              {"tool_call_id": "c1", "tool_name": "read_file",
               "content": "x=1", "is_error": False}, "t"),
        Event(5, "r", 5, "checkpoint.created",
              {"checkpoint_id": "cp1", "files": ["a.py"]}, "t"),
        Event(6, "r", 6, "plan.created",
              {"goal": "g", "tasks": [{"id": "1"}]}, "t"),
        Event(7, "r", 7, "plan.approved",
              {"goal": "g", "via": "--execute"}, "t"),
    ]
    messages = project_messages(events)
    assert [message["role"] for message in messages] == [
        "user", "assistant", "user"]
    assert messages[2]["tool_results"][0]["tool_call_id"] == "c1"
    plan = project_plan(events)
    assert plan["created"]["goal"] == "g"
    assert plan["approved"] is True
    assert plan["approved_via"] == "--execute"
    assert project_checkpoints(events)[0]["checkpoint_id"] == "cp1"
    # A second plan replaces the first and resets approval.
    later = events + [Event(8, "r", 8, "plan.created",
                            {"goal": "g2", "tasks": []}, "t")]
    assert project_plan(later)["approved"] is False


def test_plan_edited_replaces_recorded_version_and_resets_approval(
        tmp_path) -> None:
    """Review round 2, R1-3: plan.edited is the only event that
    moves the recorded version, and the projection must not claim
    an edited plan is still approved under the old approval."""
    from daemon.agent.planner import Plan, Task

    plan = Plan(goal="g", summary="s",
                tasks=[Task(id="1", title="t1", description="d",
                            files=["a.py"], verification="v", risk="low")])
    edited = Plan(goal="g", summary="s",
                  tasks=[Task(id="1", title="t1 edited", description="d",
                              files=["b.py"], verification="v", risk="low")])
    assert emit_plan_created("r", plan, plan_id="abc",
                             plan_hash="hash-1") is not None
    assert emit_plan_approved("r", plan, via="--execute", plan_id="abc",
                              plan_hash="hash-1") is not None
    assert find_plan_recorded("abc")["plan_hash"] == "hash-1"
    assert project_plan(EventStore().read("r"))["approved"] is True
    assert emit_plan_edited("r", edited, plan_id="abc",
                            plan_hash="hash-2") is not None
    assert find_plan_recorded("abc")["plan_hash"] == "hash-2"
    projected = project_plan(EventStore().read("r"))
    assert projected["approved"] is False
    assert projected["created"]["tasks"][0]["title"] == "t1 edited"
    assert find_plan_recorded("nope") is None


def _drive_demo_run(home, workspace) -> str:
    """One real demo-provider run + a real plan leg, one run id.

    Returns the session id. The demo run is the v0.5.0 smoke path
    (real context, real runtime, real tools); the plan leg drives
    parse_plan + the same emit helpers the CLI and REPL call, with
    an edit-before-approve through the recorded edit path (a
    scripted editor retargets the first task's title, so the
    plan.edited event is real, not hand-emitted).
    """
    (workspace / "a.py").write_text(SOURCE, encoding="utf-8")
    ctx = build_context(workspace=workspace, interactive=False,
                        profile_id="demo", assume_yes=True)
    try:
        runtime = ctx.build_runtime()
        result = asyncio.run(runtime.run("add a docstring to a.py"))
        assert '"""Add."""' in (workspace / "a.py").read_text(encoding="utf-8")
        assert result.tool_calls >= 2
        run_id = ctx.session.meta.id
        plan = parse_plan("Add docstrings", CANNED_PLAN)
        store = PlanStore(workspace)
        record = store.create(plan, created_by="test")
        assert emit_plan_created(run_id, plan, plan_id=record.id,
                                 plan_hash=record.hash) is not None
        editor = [sys.executable, "-c",
                  "import json,sys; p=sys.argv[1]; d=json.load(open(p)); "
                  "d['tasks'][0]['title']='Add a docstring to a.py "
                  "(edited)'; json.dump(d, open(p, 'w'))"]
        record, changed = edit_plan_record(
            store, record.id, run_id=run_id, editor=editor)
        assert changed
        assert emit_plan_approved(run_id, record.plan, via="--execute",
                                  plan_id=record.id,
                                  plan_hash=record.hash) is not None
        return run_id
    finally:
        ctx.close()


def test_full_run_emits_all_twelve_types(home, workspace):
    run_id = _drive_demo_run(home, workspace)
    store = EventStore()
    found = _types(store, run_id)
    assert found == EVENT_TYPES, (
        f"missing event types: {sorted(EVENT_TYPES - found)}; "
        f"extra: {sorted(found - EVENT_TYPES)}")
    events = store.read(run_id)
    assert events[0].type == "run.started"
    # The plan leg runs after the demo run finishes, so plan.approved
    # is legitimately last; the run's own lifecycle is ordered within
    # itself: started < messages < tools < verification < finished.
    order = [event.type for event in events]
    assert order.index("run.started") < order.index("message.user")
    assert order.index("tool.called") < order.index("tool.result")
    assert order.index("verification.result") < order.index("run.finished")
    assert order.index("run.finished") < order.index("plan.created")
    assert order.index("plan.created") < order.index("plan.edited")
    assert order.index("plan.edited") < order.index("plan.approved")


def test_projections_match_session_store_after_run(home, workspace):
    run_id = _drive_demo_run(home, workspace)
    store = EventStore()
    events = store.read(run_id)
    projected = project_messages(events)
    transcript = [
        json.loads(line)
        for line in (store.home / "sessions" / run_id
                     / "transcript.jsonl").read_text("utf-8").splitlines()
        if line.strip()
    ]
    # The kernel records only this run's messages, so the projection
    # is the transcript's tail (the whole file for a fresh session).
    assert projected == transcript[-len(projected):]
    assert project_plan(events)["approved"] is True
    checkpoints = project_checkpoints(events)
    assert checkpoints, "the edit must have checkpointed"
    for record in checkpoints:
        assert (workspace / ".daemon" / "checkpoints"
                / record["checkpoint_id"]).is_dir()


def test_replay_identical_twice_byte_identical(home, workspace):
    run_id = _drive_demo_run(home, workspace)
    first, code_first = replay_report(run_id)
    second, code_second = replay_report(run_id)
    assert code_first == 0 and code_second == 0
    assert first == second
    assert first[-1] == "identical"
    assert first[0] == f"run {run_id}"


def test_replay_reports_bogus_transcript_line(home, workspace):
    run_id = _drive_demo_run(home, workspace)
    transcript = Path(home) / "sessions" / run_id / "transcript.jsonl"
    with open(transcript, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "role": "user", "text": "a forged turn",
            "tool_calls": [], "tool_results": []}) + "\n")
    lines, code = replay_report(run_id)
    assert code == 1
    assert lines[-2] == "DIVERGENT:"
    assert any("message 0" in line for line in lines)


def test_replay_reports_torn_tail(home, workspace):
    run_id = _drive_demo_run(home, workspace)
    transcript = Path(home) / "sessions" / run_id / "transcript.jsonl"
    with open(transcript, "a", encoding="utf-8") as handle:
        handle.write('{"role": "user", "text": "unfin')
    lines, code = replay_report(run_id)
    assert code == 1
    assert any("torn final line" in line for line in lines)


def test_replay_no_event_record(tmp_path):
    lines, code = replay_report("does-not-exist")
    assert code == 2
    assert "no event record" in lines[-1]


def test_run_survives_dead_event_store(home, workspace, monkeypatch):
    """Best-effort contract: a broken events.db never breaks a run."""
    (workspace / "a.py").write_text(SOURCE, encoding="utf-8")
    broken = Path(home) / "broken-home"
    broken.mkdir()
    (broken / "events.db").mkdir()  # sqlite cannot open a directory
    monkeypatch.setenv("DAEMON_HOME", str(broken))
    monkeypatch.setenv("DAEMON_VAULT_BACKEND", "plain")
    ctx = build_context(workspace=workspace, interactive=False,
                        profile_id="demo", assume_yes=True)
    try:
        result = asyncio.run(
            ctx.build_runtime().run("add a docstring to a.py"))
    finally:
        ctx.close()
    assert '"""Add."""' in (workspace / "a.py").read_text(encoding="utf-8")
    assert result.tool_calls >= 2
    assert EventStore(home=broken).read(ctx.session.meta.id) == []
