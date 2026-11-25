"""v0.5.0 §1: the built-in scripted demo provider.

The demo provider is NOT a model: these tests pin the honesty contract
(labeled scripted everywhere, deterministic, no network) and prove a
full run completes with zero credentials through the real tool /
permission / checkpoint path.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from click.testing import CliRunner

from daemon.agent.runtime import AgentRuntime
from daemon.agent.session import Session, SessionMeta
from daemon.checkpoints import CheckpointManager
from daemon.cli import main
from daemon.context import build_context
from daemon.memory import MemoryStore
from daemon.permissions import PermissionEngine
from daemon.project import ProjectIndex
from daemon.providers.catalog import provider_by_id
from daemon.providers.demo import DEMO_MODEL_ID, DemoAdapter
from daemon.providers.registry import AdapterRegistry
from daemon.providers.types import ChatRequest, Message, ToolResult, ToolSpec
from daemon.tools import default_registry

TOOLS = [ToolSpec(name="read_file", description="read"),
         ToolSpec(name="edit_file", description="edit")]

SOURCE = "def add(a, b):\n    return a + b\n"


def _adapter() -> DemoAdapter:
    adapter = AdapterRegistry().create_by_id("demo")
    assert isinstance(adapter, DemoAdapter)
    return adapter


def _events(adapter, request):
    async def collect():
        return [event async for event in adapter.chat(request)]

    return asyncio.run(collect())


def _request(messages, tools=TOOLS):
    return ChatRequest(model=DEMO_MODEL_ID, messages=messages, tools=tools)


def test_catalog_row_is_keyless_and_labeled_scripted():
    descriptor = provider_by_id("demo")
    assert descriptor is not None
    assert descriptor.requires_key is False
    assert descriptor.protocol == "demo"
    assert "scripted" in descriptor.summary()


def test_list_models_is_one_scripted_model():
    models = asyncio.run(_adapter().list_models())
    assert [model.model_id for model in models] == [DEMO_MODEL_ID]
    assert models[0].provider == "demo"
    assert "scripted" in models[0].display_name.lower()


def test_no_file_named_answers_text_only_and_labeled():
    events = _events(_adapter(), _request(
        [Message.user("Explain what a hash map is.")]))
    kinds = [event.kind for event in events]
    assert "tool_calls" not in kinds
    text = "".join(event.text or "" for event in events)
    assert "scripted" in text.lower()
    assert "not a" in text.lower() and "model" in text.lower()


def test_file_task_starts_with_read_file(tmp_path):
    # V11: a token counts as a file only when it resolves to an
    # existing file inside the workspace - a.py really exists here.
    (tmp_path / "a.py").write_text(SOURCE, encoding="utf-8")
    adapter = _adapter()
    adapter.workspace = tmp_path
    events = _events(adapter, _request(
        [Message.user("add a docstring to a.py")]))
    calls = [call for event in events for call in event.tool_calls]
    assert [(call.name, call.arguments) for call in calls] == [
        ("read_file", {"path": "a.py"})]


def test_dotted_non_file_tokens_are_not_read(tmp_path):
    # Review round 2, V11 (the reviewer's probes): none of these may
    # produce a read_file call - example.com is not a file, the
    # parent-relative and absolute paths are outside the workspace,
    # and ".hidden.py" does not make "hidden.py" exist.
    (tmp_path / "ws").mkdir()
    workspace = tmp_path / "ws"
    (tmp_path / "outside.py").write_text("x = 1\n", encoding="utf-8")
    (workspace / ".hidden.py").write_text("x = 1\n", encoding="utf-8")
    adapter = _adapter()
    adapter.workspace = workspace
    for task in ("what is the weather on example.com",
                 "read ../outside.py",
                 f"read {tmp_path / 'outside.py'}",
                 "read .hidden.py"):
        events = _events(adapter, _request([Message.user(task)]))
        calls = [call for event in events for call in event.tool_calls]
        assert calls == [], task


def test_real_file_after_dotted_non_file_is_found(tmp_path):
    (tmp_path / "a.py").write_text(SOURCE, encoding="utf-8")
    adapter = _adapter()
    adapter.workspace = tmp_path
    events = _events(adapter, _request(
        [Message.user("check example.com then add a docstring to a.py")]))
    calls = [call for event in events for call in event.tool_calls]
    assert [(call.name, call.arguments) for call in calls] == [
        ("read_file", {"path": "a.py"})]


def test_after_read_proposes_docstring_edit():
    read_result = ToolResult(
        tool_call_id="demo-1", tool_name="read_file",
        content=f"[a.py | lines 1-2 of 2]\n{SOURCE}")
    messages = [
        Message.user("add a docstring to a.py"),
        Message.assistant(tool_calls=[]),
        Message.with_tool_results([read_result]),
    ]
    # rebuild the assistant turn with the actual call so the path resolves
    from daemon.providers.types import ToolCall

    messages[1] = Message.assistant(tool_calls=[
        ToolCall(id="demo-1", name="read_file", arguments={"path": "a.py"})])
    events = _events(_adapter(), _request(messages))
    calls = [call for event in events for call in event.tool_calls]
    assert len(calls) == 1
    assert calls[0].name == "edit_file"
    assert calls[0].arguments["path"] == "a.py"
    assert calls[0].arguments["old_text"] == "def add(a, b):"
    assert '"""Add."""' in calls[0].arguments["new_text"]


def test_after_edit_finishes_with_labeled_summary():
    messages = [
        Message.user("add a docstring to a.py"),
        Message.with_tool_results([ToolResult(
            tool_call_id="demo-2", tool_name="edit_file",
            content="Edited a.py: replaced 1 occurrence(s).")]),
    ]
    events = _events(_adapter(), _request(messages))
    assert not [call for event in events for call in event.tool_calls]
    text = "".join(event.text or "" for event in events)
    assert "scripted" in text.lower()


def test_read_error_ends_scenario_without_edits():
    messages = [
        Message.user("add a docstring to missing.py"),
        Message.with_tool_results([ToolResult(
            tool_call_id="demo-1", tool_name="read_file",
            content="File not found: missing.py", is_error=True)]),
    ]
    events = _events(_adapter(), _request(messages))
    assert not [call for event in events for call in event.tool_calls]


def test_already_documented_file_gets_no_edit():
    documented = 'def add(a, b):\n    """Add."""\n    return a + b\n'
    messages = [
        Message.user("add a docstring to a.py"),
        Message.with_tool_results([ToolResult(
            tool_call_id="demo-1", tool_name="read_file",
            content=f"[a.py | lines 1-3 of 3]\n{documented}")]),
    ]
    events = _events(_adapter(), _request(messages))
    assert not [call for event in events for call in event.tool_calls]


def test_toolless_request_never_emits_tool_calls():
    # Compaction/summarization calls offer no tools: text only, always.
    events = _events(_adapter(), _request(
        [Message.user("Summarize: the user edited a.py and b.py")],
        tools=[]))
    assert not [call for event in events for call in event.tool_calls]
    assert any(event.kind == "text" for event in events)


def test_deterministic_same_conversation_same_events(tmp_path):
    (tmp_path / "a.py").write_text(SOURCE, encoding="utf-8")
    adapter = _adapter()
    adapter.workspace = tmp_path
    request = _request([Message.user("add a docstring to a.py")])
    first = [(e.kind, e.text, [(c.name, c.arguments) for c in e.tool_calls])
             for e in _events(adapter, request)]
    second = [(e.kind, e.text, [(c.name, c.arguments) for c in e.tool_calls])
              for e in _events(adapter, request)]
    assert first == second


def test_build_context_demo_needs_no_profiles(home, workspace):
    ctx = build_context(workspace=workspace, interactive=False,
                        profile_id="demo")
    try:
        assert isinstance(ctx.adapter, DemoAdapter)
        assert ctx.model is not None
        assert ctx.model.provider == "demo"
        assert ctx.model.model_id == DEMO_MODEL_ID
        assert ctx.session.meta.provider == "demo"
    finally:
        ctx.close()


def test_full_runtime_run_adds_docstring_via_real_path(workspace):
    (workspace / "a.py").write_text(SOURCE, encoding="utf-8")
    adapter = _adapter()
    model = asyncio.run(adapter.list_models())[0]
    session = Session(meta=SessionMeta(workspace=str(workspace),
                                       provider="demo", model=DEMO_MODEL_ID))
    index = ProjectIndex(workspace)
    index.build()
    runtime = AgentRuntime(
        adapter=adapter, model=model, registry=default_registry(),
        permissions=PermissionEngine(workspace, assume_yes=True),
        session=session, workspace=workspace,
        memory=MemoryStore(workspace),
        checkpoints=CheckpointManager(workspace), index=index)

    result = asyncio.run(runtime.run("add a docstring to a.py"))

    content = (workspace / "a.py").read_text(encoding="utf-8")
    assert '"""Add."""' in content
    assert result.tool_calls == 2  # read_file + edit_file
    assert result.files_changed == ["a.py"]
    assert "scripted" in result.text.lower()
    # the edit went through the checkpoint path
    latest = runtime.checkpoints.latest()
    assert latest is not None and "a.py" in latest.files


def test_cli_demo_command_runs_offline(home, workspace):
    (workspace / "a.py").write_text(SOURCE, encoding="utf-8")
    result = CliRunner().invoke(main, [
        "demo", "add a docstring to a.py",
        "--workspace", str(workspace), "--yes", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["provider"] == "demo"
    assert payload["model"] == DEMO_MODEL_ID
    assert '"""Add."""' in (workspace / "a.py").read_text(encoding="utf-8")


def test_cli_run_provider_demo_flag(home, workspace):
    (workspace / "a.py").write_text(SOURCE, encoding="utf-8")
    result = CliRunner().invoke(main, [
        "run", "add a docstring to a.py", "--provider", "demo",
        "--workspace", str(workspace), "--yes"])
    assert result.exit_code == 0, result.output
    assert "SCRIPTED" in result.output
    assert '"""Add."""' in (workspace / "a.py").read_text(encoding="utf-8")


def test_eval_demo_target_runs_fully_offline(home):
    from daemon.evaluation import run_evaluations

    task_file = (Path(__file__).resolve().parents[2] / "daemon" / "evals"
                 / "demo_tasks.json")
    report = run_evaluations((f"demo={DEMO_MODEL_ID}",), task_file=task_file)
    assert report["runs"], "demo eval produced no runs"
    run = report["runs"][0]
    assert run["passed"], run
