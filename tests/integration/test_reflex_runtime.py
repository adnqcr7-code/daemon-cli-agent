"""Reflex Phase 1 runtime integration (design v4 section 3.1).

A scripted session where a certified structural fact already exists and
the agent heads toward re-deriving it: exactly one [reflex] note must
appear, at the tail of exactly one request, the system prompt must stay
byte-identical across every request (the prompt-cache invariant), and
the session transcript must never contain the note.
"""

import asyncio
import json

from daemon.agent.reflex import ReflexEngine
from daemon.agent.runtime import AgentRuntime
from daemon.agent.session import Session, SessionMeta
from daemon.checkpoints import CheckpointManager
from daemon.config import Config
from daemon.memory import MemoryStore
from daemon.models import RemoteModel
from daemon.permissions import PermissionEngine
from daemon.project import ProjectIndex
from daemon.providers.types import (
    ModelCapabilities,
    StreamEvent,
    ToolCall,
    ToolResult,
    Usage,
)
from daemon.tools import default_registry

SCRIPT = [
    # The agent heads for the symbol lookup the certified fact covers.
    [StreamEvent(kind="tool_calls", tool_calls=[ToolCall(
        id="c1", name="find_symbols", arguments={"path": "bug.py"})]),
     StreamEvent(kind="usage", usage=Usage(input_tokens=100,
                                           output_tokens=10))],
    [StreamEvent(kind="text", text="add is defined in bug.py."),
     StreamEvent(kind="usage", usage=Usage(input_tokens=200,
                                           output_tokens=10))],
]


class CapturingAdapter:
    """Scripted adapter that records every request it is given."""

    def __init__(self) -> None:
        self.calls = 0
        self.requests = []

    async def chat(self, request):
        self.requests.append(request)
        events = SCRIPT[min(self.calls, len(SCRIPT) - 1)]
        self.calls += 1
        for event in events:
            yield event

    async def close(self) -> None:
        pass


def _runtime(workspace, adapter, session):
    index = ProjectIndex(workspace)
    index.build()
    return AgentRuntime(
        adapter=adapter,
        model=RemoteModel(
            model_id="mock-coder", provider="mock",
            capabilities=ModelCapabilities(context_window=128_000,
                                           tool_calling=True)),
        registry=default_registry(),
        permissions=PermissionEngine(workspace, assume_yes=True),
        session=session, config=Config.load(workspace),
        workspace=workspace, memory=MemoryStore(workspace),
        checkpoints=CheckpointManager(workspace), index=index)


def _seed_certified_fact(workspace) -> None:
    """A prior session established the fact by execution."""
    engine = ReflexEngine(Config.load(workspace), workspace=workspace)
    engine.record_tool_result(
        "find_symbols", {"path": "bug.py"},
        "bug.py:1 function add(a, b)\n[analysis: ast]", is_error=False,
        metadata={"method": "ast", "symbols": [
            {"name": "add", "kind": "function", "path": "bug.py",
             "line": 1}]})
    entries = engine.store.entries(status="certified")
    assert len(entries) == 1


def test_reflex_note_fires_once_at_tail(workspace):
    _seed_certified_fact(workspace)
    adapter = CapturingAdapter()
    session = Session(meta=SessionMeta(workspace=str(workspace),
                                       provider="mock", model="mock-coder"))
    runtime = _runtime(workspace, adapter, session)

    asyncio.run(runtime.run("Where is add defined?"))

    # Exactly one [reflex] note across every request the model saw.
    noted = [(index, message)
             for index, request in enumerate(adapter.requests)
             for message in request.messages
             if message.text and "[reflex]" in message.text]
    assert len(noted) == 1
    request_index, note_message = noted[0]
    # It sits at the tail of its request, and it fired on the request
    # built after the agent's find_symbols call entered the trajectory.
    assert adapter.requests[request_index].messages[-1] is note_message
    assert request_index == 1
    # Phase 2: the certified fact comes from structured metadata rows
    # ({name, kind, path, line} - no signature), so the note carries
    # the row shape "bug.py:1 function add".
    assert "bug.py:1 function add" in note_message.text
    # The system prompt is byte-identical on every request, firing or
    # not - the prompt-cache invariant.
    systems = {request.system for request in adapter.requests}
    assert len(systems) == 1
    # The note is delivery-only: the session transcript never stores it.
    assert all("[reflex]" not in (message.text or "")
               for message in session.messages)
    # The run report carries the event counters.
    report = json.loads(
        (workspace / ".daemon" / "reports"
         / f"{session.meta.id}.json").read_text())
    assert report["reflex_firings"] == 1
    assert report["reflex_follows"] == 1  # agent never re-read the file
    assert report["reflex_overrides"] == 0


def test_system_prompt_byte_identical_across_a_firing(workspace):
    _seed_certified_fact(workspace)
    adapter = CapturingAdapter()
    session = Session(meta=SessionMeta(workspace=str(workspace),
                                       provider="mock", model="mock-coder"))
    runtime = _runtime(workspace, adapter, session)

    # Build the trajectory by hand: user task, an assistant tool call,
    # its result - the state _build_request sees mid-loop.
    session.add_user("Where is add defined?")
    runtime._current_task = "Where is add defined?"
    session.add_assistant(None, [ToolCall(
        id="c1", name="find_symbols", arguments={"path": "bug.py"})])
    session.add_tool_results([ToolResult(
        tool_call_id="c1", tool_name="find_symbols",
        content="bug.py:1 function add(a, b)", is_error=False)])

    before = runtime.system_prompt()
    request = runtime._build_request()

    assert request.messages[-1].text.startswith("[reflex]")
    assert request.system == before
    assert runtime.system_prompt() == before


def test_build_request_survives_reflex_trajectory_failure(workspace):
    # Review finding 8: _reflex_trajectory ran unguarded inside
    # _build_request - the one call that could break a run the design
    # says must never break. A throw there now just skips the signal.
    adapter = CapturingAdapter()
    session = Session(meta=SessionMeta(workspace=str(workspace),
                                       provider="mock", model="mock-coder"))
    runtime = _runtime(workspace, adapter, session)
    session.add_user("Where is add defined?")

    def boom():
        raise RuntimeError("trajectory exploded")

    runtime._reflex_trajectory = boom  # type: ignore[method-assign]
    request = runtime._build_request()

    assert request.messages
    assert all("[reflex]" not in (message.text or "")
               for message in request.messages)
