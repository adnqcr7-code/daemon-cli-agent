"""Tests for the malformed tool-call repair ladder (v0.9.0 Phase 3):
validate -> repair -> retry -> fallback to plan-only, no tools."""

from __future__ import annotations

import asyncio

from daemon.agent.runtime import AgentRuntime
from daemon.agent.session import Session, SessionMeta
from daemon.agent.toolcall_repair import (
    repair_call,
    repair_calls,
    validate_call,
)
from daemon.checkpoints import CheckpointManager
from daemon.config import Config
from daemon.memory import MemoryStore
from daemon.permissions import PermissionEngine
from daemon.project import ProjectIndex
from daemon.providers.types import (
    ModelCapabilities,
    RemoteModel,
    StreamEvent,
    ToolCall,
    Usage,
)
from daemon.tools import default_registry


def _registry():
    return default_registry()


def test_valid_call_passes() -> None:
    call = ToolCall(id="c1", name="read_file", arguments={"path": "bug.py"})
    assert validate_call(call, _registry()) == []


def test_unknown_tool_is_an_error_and_never_renamed() -> None:
    call = ToolCall(id="c1", name="readfile", arguments={"path": "bug.py"})
    assert validate_call(call, _registry())
    assert repair_call(call, _registry()) is None


def test_raw_truncated_json_is_salvaged() -> None:
    call = ToolCall(id="c1", name="read_file",
                    arguments={"_raw": '{"path": "bug.py"'})
    assert validate_call(call, _registry())
    repaired = repair_call(call, _registry())
    assert repaired is not None
    assert repaired.arguments == {"path": "bug.py"}
    assert validate_call(repaired, _registry()) == []


def test_raw_garbage_cannot_be_repaired() -> None:
    call = ToolCall(id="c1", name="read_file",
                    arguments={"_raw": "not json at all {{{"})
    assert repair_call(call, _registry()) is None


def test_json_string_arguments_are_parsed() -> None:
    call = ToolCall(id="c1", name="read_file",
                    arguments='{"path": "bug.py"}')  # type: ignore[arg-type]
    repaired = repair_call(call, _registry())
    assert repaired is not None
    assert repaired.arguments == {"path": "bug.py"}


def test_missing_required_parameter_cannot_be_invented() -> None:
    call = ToolCall(id="c1", name="read_file", arguments={})
    assert validate_call(call, _registry())
    assert repair_call(call, _registry()) is None


def test_scalar_is_coerced_to_the_declared_type() -> None:
    call = ToolCall(id="c1", name="read_file", arguments={"path": 42})
    assert validate_call(call, _registry())
    repaired = repair_call(call, _registry())
    assert repaired is not None
    assert repaired.arguments == {"path": "42"}


def test_repair_calls_is_all_or_nothing() -> None:
    good = ToolCall(id="c1", name="read_file", arguments={"path": "bug.py"})
    bad = ToolCall(id="c2", name="nope", arguments={})
    _, errors = repair_calls([good, bad], _registry())
    assert errors  # the unrepairable call blocks the whole turn


# ---------------------------------------------------------------------------
# Runtime ladder: a scripted adapter that emits malformed calls.
# ---------------------------------------------------------------------------


class _ScriptedAdapter:
    def __init__(self, stages) -> None:
        self._stages = stages
        self.calls = 0

    async def chat(self, request):
        events = self._stages[min(self.calls, len(self._stages) - 1)]
        self.calls += 1
        for event in events:
            yield event

    async def close(self) -> None:
        pass


def _runtime(adapter, workspace, *, repair_attempts: int | None = None):
    model = RemoteModel(
        model_id="mock-coder", provider="mock",
        capabilities=ModelCapabilities(context_window=128_000,
                                       tool_calling=True))
    session = Session(meta=SessionMeta(workspace=str(workspace),
                                       provider="mock", model="mock-coder"))
    index = ProjectIndex(workspace)
    index.build()
    config = Config.load(workspace)
    if repair_attempts is not None:
        config.set_user("tool_call.repair_attempts", repair_attempts)
    return AgentRuntime(
        adapter=adapter, model=model, registry=default_registry(),
        permissions=PermissionEngine(workspace, assume_yes=True),
        session=session, config=config,
        workspace=workspace, memory=MemoryStore(workspace),
        checkpoints=CheckpointManager(workspace), index=index), session


def _events(calls=None, text=None):
    events = []
    if text is not None:
        events.append(StreamEvent(kind="text", text=text))
    if calls is not None:
        events.append(StreamEvent(kind="tool_calls", tool_calls=calls))
    events.append(StreamEvent(kind="usage", usage=Usage()))
    events.append(StreamEvent(kind="done",
                             finish_reason="tool_calls" if calls else "stop"))
    return events


def test_deterministic_repair_executes_without_a_retry(workspace) -> None:
    """A truncated _raw payload is salvaged mechanically; the model is
    never re-prompted and the tool executes."""
    adapter = _ScriptedAdapter([
        _events([ToolCall(id="c1", name="read_file",
                          arguments={"_raw": '{"path": "bug.py"'})]),
        _events(text="Read it."),
    ])
    runtime, _ = _runtime(adapter, workspace)
    result = asyncio.run(runtime.run("Read bug.py."))
    assert result.tool_calls == 1
    assert adapter.calls == 2
    assert "Read it." in result.text


def test_retry_then_valid_call_executes(workspace) -> None:
    """An unrepairable call (unknown tool) triggers one repair prompt;
    the model's corrected call then executes."""
    adapter = _ScriptedAdapter([
        _events([ToolCall(id="c1", name="readfile",
                          arguments={"path": "bug.py"})]),
        _events([ToolCall(id="c2", name="read_file",
                          arguments={"path": "bug.py"})]),
        _events(text="Done."),
    ])
    runtime, session = _runtime(adapter, workspace)
    result = asyncio.run(runtime.run("Read bug.py."))
    assert result.tool_calls == 1
    assert "Done." in result.text
    repair_prompts = [m for m in session.messages
                      if m.role == "user" and m.text
                      and m.text.startswith(
                          "Your last tool call could not be used")]
    assert len(repair_prompts) == 1
    assert "readfile" in repair_prompts[0].text


def test_fallback_is_plan_only_with_zero_tool_calls(workspace) -> None:
    """Still malformed after the retry: the turn ends with a text-only
    plan and nothing executes."""
    adapter = _ScriptedAdapter([
        _events([ToolCall(id="c1", name="nope", arguments={})]),
        _events([ToolCall(id="c2", name="nope", arguments={})]),
    ])
    runtime, session = _runtime(adapter, workspace)
    result = asyncio.run(runtime.run("Fix bug.py."))
    assert result.tool_calls == 0
    assert "Plan only" in result.text
    assert "no tools were executed" in result.text
    assert "return a - b" in (workspace / "bug.py").read_text()  # unchanged
    assert not any(message.tool_calls for message in session.messages)
    assert not any(message.tool_results for message in session.messages)


def test_zero_repair_attempts_falls_back_immediately(workspace) -> None:
    adapter = _ScriptedAdapter([
        _events([ToolCall(id="c1", name="nope", arguments={})]),
    ])
    runtime, _ = _runtime(adapter, workspace, repair_attempts=0)
    result = asyncio.run(runtime.run("Fix bug.py."))
    assert result.tool_calls == 0
    assert adapter.calls == 1  # no retry was attempted
    assert "Plan only" in result.text
