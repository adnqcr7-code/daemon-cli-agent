"""Tests for v0.9.0 §2.5: diff preview at decision, display after the
edit, and `daemon undo` through the kernel's checkpoint events."""

from __future__ import annotations

import asyncio
from pathlib import Path

from click.testing import CliRunner

from daemon.agent.runtime import AgentRuntime, AgentUI
from daemon.agent.session import Session, SessionMeta
from daemon.checkpoints import CheckpointManager
from daemon.cli import main
from daemon.config import Config
from daemon.diffs import preview_edit_diff, truncate_diff
from daemon.events import EventStore, project_checkpoints
from daemon.memory import MemoryStore
from daemon.permissions import PermissionEngine
from daemon.project import ProjectIndex
from daemon.providers.types import ModelCapabilities, RemoteModel, StreamEvent, ToolCall, Usage
from daemon.tools import default_registry
from daemon.tools.base import ToolContext
from daemon.tools.fs import EditFileTool, WriteFileTool


def _workspace(tmp_path: Path, *, content: str | None = None) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.py").write_text(
        content if content is not None else "def add(a, b):\n    return a + b\n",
        encoding="utf-8")
    return workspace


# ------------------------------------------------------------------ truncate


def test_truncate_diff() -> None:
    short = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-def add(a, b):\n+def add(a, b):\n"
    assert truncate_diff(short) == short
    lines = ["--- a/big.py", "+++ b/big.py"] + [f"+line {i}" for i in range(300)]
    big = "\n".join(lines)
    cut = truncate_diff(big)
    assert "… 102 more lines" in cut
    assert cut.count("\n") <= 201
    # Char budget: few lines, far too many chars.
    wide = "--- a/w.py\n+++ b/w.py\n+" + ("x" * 40_000)
    cut = truncate_diff(wide)
    assert "more line" in cut
    assert len(cut) < len(wide)


def test_preview_is_read_only_and_matches_post_edit(tmp_path) -> None:
    """The preview computes the edit without touching the file, and
    is byte-identical to the display the tool reports afterwards."""
    workspace = _workspace(tmp_path)
    original = (workspace / "a.py").read_text("utf-8")
    args = {"path": "a.py", "old_text": "def add(a, b):",
            "new_text": 'def add(a, b):\n    """Add."""'}
    preview = preview_edit_diff(workspace, "edit_file", args)
    assert preview is not None
    assert ' def add(a, b):' in preview  # context line, not removed
    assert '+    """Add."""' in preview
    # Nothing mutated by the preview.
    assert (workspace / "a.py").read_text("utf-8") == original
    # The tool's own display is the same computation.
    ctx = ToolContext(workspace, Config.load(workspace),
                      checkpoints=CheckpointManager(workspace))
    output = asyncio.run(EditFileTool().run(args, ctx))
    assert output.display == preview
    # write_file: same story for a new file.
    write_args = {"path": "b.py", "content": "x = 1\n"}
    write_preview = preview_edit_diff(workspace, "write_file", write_args)
    assert write_preview is not None and "+x = 1" in write_preview
    output = asyncio.run(WriteFileTool().run(write_args, ctx))
    assert output.display == write_preview
    assert (workspace / "b.py").read_text("utf-8") == "x = 1\n"
    # Uncomputable previews are None, never a crash or a wrong diff.
    assert preview_edit_diff(workspace, "edit_file",
                             {"path": "a.py", "old_text": "nope",
                              "new_text": "x"}) is None
    assert preview_edit_diff(workspace, "read_file",
                             {"path": "a.py"}) is None
    assert preview_edit_diff(workspace, "edit_file",
                             {"path": "../escape.py", "old_text": "x",
                              "new_text": "y"}) is None


# ------------------------------------------------------- ASK panel (runtime)


class _CapturingUI(AgentUI):
    def __init__(self) -> None:
        self.decisions = []

    async def approval(self, decision):
        self.decisions.append(decision)
        return True, False


class _BigWriteAdapter:
    """One huge write_file call, then a final text."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    async def chat(self, request):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(kind="tool_calls", tool_calls=[ToolCall(
                id="c1", name="write_file",
                arguments={"path": "big.py", "content": self.content})])
            yield StreamEvent(kind="usage", usage=Usage())
        else:
            yield StreamEvent(kind="text", text="Wrote big.py.")
            yield StreamEvent(kind="usage", usage=Usage())

    async def close(self) -> None:
        pass


def test_large_edit_prompt_shows_truncated_diff(tmp_path) -> None:
    """The acceptance case: a large edit's ASK panel shows the diff
    truncated to the budget, with the marker — and the file is only
    written after approval."""
    workspace = _workspace(tmp_path)
    content = "".join(f"value_{i} = {i}\n" for i in range(300))
    ui = _CapturingUI()
    session = Session(meta=SessionMeta(workspace=str(workspace),
                                       provider="mock", model="mock"))
    runtime = AgentRuntime(
        adapter=_BigWriteAdapter(content),
        model=RemoteModel(model_id="mock", provider="mock",
                          capabilities=ModelCapabilities(
                              context_window=128_000, tool_calling=True)),
        registry=default_registry(),
        permissions=PermissionEngine(
            workspace,
            rules=[{"tool": "write_file", "effect": "ask"}]),
        session=session, config=Config.load(workspace),
        workspace=workspace, memory=MemoryStore(workspace),
        checkpoints=CheckpointManager(workspace),
        index=ProjectIndex(workspace), ui=ui)
    result = asyncio.run(runtime.run("Write a big file."))
    assert len(ui.decisions) == 1
    preview = ui.decisions[0].diff_preview
    assert preview is not None
    assert "… " in preview and "more lines" in preview
    assert "+value_0 = 0" in preview
    assert "+value_299 = 299" not in preview  # truncated away
    assert (workspace / "big.py").read_text("utf-8") == content
    assert result.permission_prompts == 1


def test_small_edit_prompt_shows_full_diff(tmp_path) -> None:
    """A small edit's panel shows the whole diff (no marker)."""
    workspace = _workspace(tmp_path)
    ui = _CapturingUI()
    session = Session(meta=SessionMeta(workspace=str(workspace),
                                       provider="mock", model="mock"))
    from daemon.providers.registry import AdapterRegistry

    runtime = AgentRuntime(
        adapter=AdapterRegistry().create_by_id("demo"),
        model=RemoteModel(model_id="demo-scripted", provider="demo",
                          capabilities=ModelCapabilities(
                              context_window=128_000, tool_calling=True)),
        registry=default_registry(),
        permissions=PermissionEngine(
            workspace,
            rules=[{"tool": "edit_file", "effect": "ask"}]),
        session=session, config=Config.load(workspace),
        workspace=workspace, memory=MemoryStore(workspace),
        checkpoints=CheckpointManager(workspace),
        index=ProjectIndex(workspace), ui=ui)
    asyncio.run(runtime.run("add a docstring to a.py"))
    assert len(ui.decisions) == 1
    preview = ui.decisions[0].diff_preview
    assert preview is not None
    assert "+    \"\"\"Add.\"\"\"" in preview
    assert "more lines" not in preview


# ---------------------------------------------------------------------- undo


def _demo_run(workspace: Path):
    result = CliRunner().invoke(main, [
        "demo", "add a docstring to a.py", "--yes", "-w", str(workspace)])
    assert result.exit_code == 0, result.output
    assert '"""Add."""' in (workspace / "a.py").read_text("utf-8")


def test_undo_list_matches_checkpoint_events(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    _demo_run(workspace)
    from daemon.agent.session import Session as SessionCls

    metas = SessionCls.list_sessions(10)
    assert metas, "no sessions recorded"
    run_id = metas[0].id
    expected = project_checkpoints(EventStore().read(run_id))
    assert expected, "no checkpoint events for the run"
    listed = CliRunner().invoke(main, ["undo", "--list",
                                       "-w", str(workspace)])
    assert listed.exit_code == 0, listed.output
    expected_ids = {item["checkpoint_id"] for item in expected}
    for checkpoint_id in expected_ids:
        assert checkpoint_id in listed.output
    # And --list shows the files the checkpoint holds.
    assert "a.py" in listed.output


def test_undo_restores_exact_bytes(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    original = (workspace / "a.py").read_text("utf-8")
    _demo_run(workspace)
    result = CliRunner().invoke(main, ["undo", "--yes",
                                       "-w", str(workspace)])
    assert result.exit_code == 0, result.output
    assert "Restored checkpoint" in result.output
    assert (workspace / "a.py").read_text("utf-8") == original


def test_undo_specific_id_and_unknown_id(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    original = (workspace / "a.py").read_text("utf-8")
    _demo_run(workspace)
    first_id = CliRunner().invoke(main, ["undo", "--list",
                                         "-w", str(workspace)]).output \
        .split()[0]
    # A second, different edit -> a second checkpoint. Append the new
    # function to the CURRENT content (run 1 already added Add's
    # docstring), so the demo documents sub() next.
    current = (workspace / "a.py").read_text("utf-8")
    (workspace / "a.py").write_text(
        current + "\ndef sub(a, b):\n    return a - b\n", encoding="utf-8")
    _demo_run(workspace)
    assert '"""Sub."""' in (workspace / "a.py").read_text("utf-8")
    # Undoing the FIRST checkpoint restores the original bytes.
    result = CliRunner().invoke(main, ["undo", first_id, "--yes",
                                       "-w", str(workspace)])
    assert result.exit_code == 0, result.output
    assert (workspace / "a.py").read_text("utf-8") == original
    # An unknown id refuses.
    result = CliRunner().invoke(main, ["undo", "nope-nope", "--yes",
                                       "-w", str(workspace)])
    assert result.exit_code != 0
    assert "No checkpoint" in result.output


def test_undo_with_no_checkpoints(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    listed = CliRunner().invoke(main, ["undo", "--list",
                                       "-w", str(workspace)])
    assert listed.exit_code == 0
    assert "No checkpoints recorded" in listed.output
    result = CliRunner().invoke(main, ["undo", "--yes",
                                       "-w", str(workspace)])
    assert result.exit_code != 0
    assert "No checkpoints recorded" in result.output
