"""Tests for cache discipline: the stable system prefix and
append-only history (v0.9.0 Phase 3)."""

from __future__ import annotations

import asyncio

import pytest

from daemon.agent.prefix import PrefixGuard, PrefixViolation
from daemon.agent.runtime import AgentRuntime
from daemon.agent.session import Session, SessionMeta
from daemon.checkpoints import CheckpointManager
from daemon.config import Config
from daemon.memory import MemoryStore
from daemon.permissions import PermissionEngine
from daemon.project import ProjectIndex
from daemon.providers.demo import DEMO_MODEL_ID, DemoAdapter
from daemon.providers.registry import AdapterRegistry
from daemon.providers.types import Message
from daemon.tools import default_registry


def test_guard_accepts_appends() -> None:
    guard = PrefixGuard()
    first = guard.check("system", [Message.user("hello")])
    second = guard.check("system", [Message.user("hello"),
                                    Message.assistant("hi")])
    assert first.system_hash == second.system_hash
    assert second.message_count == 2
    assert guard.checks == 2


def test_guard_rejects_an_in_place_rewrite() -> None:
    guard = PrefixGuard()
    guard.check("system", [Message.user("hello")])
    with pytest.raises(PrefixViolation, match="message 0"):
        guard.check("system", [Message.user("HELLO")])
    # Appending after a failed check still works from the baseline.
    guard.check("system", [Message.user("hello"),
                           Message.assistant("hi")])


def test_guard_rejects_a_system_prompt_change() -> None:
    guard = PrefixGuard()
    guard.check("system v1", [Message.user("hello")])
    with pytest.raises(PrefixViolation, match="system prompt changed"):
        guard.check("system v2", [Message.user("hello")])


def test_guard_reset_rebaselines_after_a_sanctioned_rewrite() -> None:
    guard = PrefixGuard()
    guard.check("system", [Message.user("hello")])
    guard.reset("compaction rewrote history")
    snapshot = guard.check("system", [Message.user("summary only")])
    assert snapshot.message_count == 1


def test_prefix_hash_changes_only_when_the_prefix_changes() -> None:
    guard = PrefixGuard()
    first = guard.check("system", [Message.user("hello")])
    second = guard.check("system", [Message.user("hello"),
                                    Message.assistant("hi")])
    assert first.prefix_hash != second.prefix_hash
    assert first.message_hashes == second.message_hashes[:1]


def test_scripted_run_keeps_the_prefix_stable_across_turns(
        workspace) -> None:
    """The plan's acceptance: in a real multi-turn demo run, the
    system hash is identical at every request and each turn's
    history extends the previous one — the prefix hash is unchanged
    except by appends."""
    (workspace / "a.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8")
    adapter = AdapterRegistry().create_by_id("demo")
    assert isinstance(adapter, DemoAdapter)
    adapter.workspace = workspace
    model = asyncio.run(adapter.list_models())[0]
    session = Session(meta=SessionMeta(workspace=str(workspace),
                                       provider="demo", model=DEMO_MODEL_ID))
    index = ProjectIndex(workspace)
    index.build()
    runtime = AgentRuntime(
        adapter=adapter, model=model, registry=default_registry(),
        permissions=PermissionEngine(workspace, assume_yes=True),
        session=session, config=Config.load(workspace),
        workspace=workspace, memory=MemoryStore(workspace),
        checkpoints=CheckpointManager(workspace), index=index)

    result = asyncio.run(runtime.run("add a docstring to a.py"))
    assert result.tool_calls == 2  # read_file + edit_file: two turns

    snapshots = runtime._prefix_guard.snapshots
    assert len(snapshots) >= 2  # one check per model request
    systems = {snapshot.system_hash for snapshot in snapshots}
    assert len(systems) == 1  # the system prompt never changed
    for earlier, later in zip(snapshots, snapshots[1:]):
        assert later.message_hashes[:earlier.message_count] \
            == earlier.message_hashes
        assert later.message_count > earlier.message_count


def test_runtime_raises_when_history_is_rewritten_mid_run(
        workspace) -> None:
    """A bug that edits an earlier message in place fails the run
    loudly instead of silently serving a stale cached prefix."""
    (workspace / "a.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8")
    adapter = AdapterRegistry().create_by_id("demo")
    assert isinstance(adapter, DemoAdapter)
    adapter.workspace = workspace
    model = asyncio.run(adapter.list_models())[0]
    session = Session(meta=SessionMeta(workspace=str(workspace),
                                       provider="demo", model=DEMO_MODEL_ID))
    index = ProjectIndex(workspace)
    index.build()
    runtime = AgentRuntime(
        adapter=adapter, model=model, registry=default_registry(),
        permissions=PermissionEngine(workspace, assume_yes=True),
        session=session, config=Config.load(workspace),
        workspace=workspace, memory=MemoryStore(workspace),
        checkpoints=CheckpointManager(workspace), index=index)
    runtime._run_system_prompt = runtime.system_prompt()
    runtime._prefix_guard.check(runtime._run_system_prompt,
                                list(session.messages))
    session.messages.append(Message.user("hello"))
    runtime._prefix_guard.check(runtime._run_system_prompt,
                                list(session.messages))
    # The in-place rewrite: an earlier message's text changes.
    session.messages[0].text = "TAMPERED"
    with pytest.raises(PrefixViolation):
        runtime._build_request()
