"""Tier 1 hard-verify loop: a failed verification is fed back to the
agent, which repairs the change before the turn is allowed to finish.
Also asserts the per-session optimization report is written.

Uses a scripted in-process adapter (the HTTP mocks index their script
by tool-round, which cannot express a second turn in one session).
"""

import asyncio
import json

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
    Usage,
)
from daemon.tools import default_registry

SCRIPT = [
    # First attempt: a wrong "fix" — the test still fails.
    [StreamEvent(kind="tool_calls", tool_calls=[ToolCall(
        id="c1", name="edit_file",
        arguments={"path": "bug.py", "old_text": "return a - b",
                   "new_text": "return a * b"})]),
     StreamEvent(kind="usage", usage=Usage(input_tokens=100,
                                           output_tokens=10))],
    [StreamEvent(kind="text", text="Changed add() to multiply."),
     StreamEvent(kind="usage", usage=Usage(input_tokens=200,
                                           output_tokens=10))],
    # Repair turn (driven by the verification failure prompt).
    [StreamEvent(kind="tool_calls", tool_calls=[ToolCall(
        id="c2", name="edit_file",
        arguments={"path": "bug.py", "old_text": "return a * b",
                   "new_text": "return a + b"})]),
     StreamEvent(kind="usage", usage=Usage(input_tokens=300,
                                           output_tokens=10))],
    [StreamEvent(kind="text", text="Fixed add() to add."),
     StreamEvent(kind="usage", usage=Usage(input_tokens=400,
                                           output_tokens=10))],
]


class ScriptedAdapter:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, request):
        events = SCRIPT[min(self.calls, len(SCRIPT) - 1)]
        self.calls += 1
        for event in events:
            yield event

    async def close(self) -> None:
        pass


def test_verification_failure_triggers_repair(workspace):
    adapter = ScriptedAdapter()
    model = RemoteModel(
        model_id="mock-coder", provider="mock",
        capabilities=ModelCapabilities(context_window=128_000,
                                       tool_calling=True))
    session = Session(meta=SessionMeta(workspace=str(workspace),
                                       provider="mock", model="mock-coder"))
    index = ProjectIndex(workspace)
    index.build()
    runtime = AgentRuntime(
        adapter=adapter, model=model, registry=default_registry(),
        permissions=PermissionEngine(workspace, assume_yes=True),
        session=session, config=Config.load(workspace),
        workspace=workspace, memory=MemoryStore(workspace),
        checkpoints=CheckpointManager(workspace), index=index)

    result = asyncio.run(runtime.run("Fix the failing test."))

    # The repair actually landed and the final verification passed.
    assert "return a + b" in (workspace / "bug.py").read_text()
    assert "verification passed" in result.verification
    # The failure was fed back to the agent as a user turn.
    feedback = [m for m in session.messages
                if m.role == "user" and m.text
                and m.text.startswith("Verification failed:")]
    assert feedback, "repair prompt must be fed back to the agent"
    # The optimization report for this session was persisted.
    report_path = (workspace / ".daemon" / "reports"
                   / f"{session.meta.id}.json")
    assert report_path.exists()
    report = json.loads(report_path.read_text())
    assert report["files_changed"] == 1
    assert report["verification_passed"] is True
    assert report["total_tool_calls"] == 2
