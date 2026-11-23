"""Tier 1 CLI wiring: worktree / repomap / optimize / verified memory.

Drives the real click CLI against the shared git `workspace` fixture —
no model or network involved.
"""

from click.testing import CliRunner

from daemon.agent.reporting import OptimizationReport, save_report
from daemon.cli import main
from daemon.memory import MemoryStore


def _run(*args: str):
    result = CliRunner().invoke(main, list(args))
    assert result.exit_code == 0, result.output
    return result.output


def test_worktree_cli_lifecycle(workspace):
    out = _run("worktree", "create", "Fix the bug!", "-w", str(workspace))
    assert "daemon/fix-the-bug-" in out
    out = _run("worktree", "list", "-w", str(workspace))
    assert "daemon/fix-the-bug-" in out

    from daemon.worktree import WorktreeManager

    info = WorktreeManager().list_worktrees(workspace)[0]
    (info.path / "bug.py").write_text("def add(a, b):\n    return a + b\n")
    out = _run("worktree", "status", "fix-the-bug", "-w", str(workspace))
    assert "1 changed file(s)" in out
    assert "bug.py" in out

    # Dirty worktrees refuse removal without --force (by design).
    result = CliRunner().invoke(
        main, ["worktree", "remove", "fix-the-bug", "-w", str(workspace)])
    assert result.exit_code == 1
    out = _run("worktree", "remove", "fix-the-bug", "--force",
               "-w", str(workspace))
    assert "Removed" in out
    assert "deleted branch" in out
    out = _run("worktree", "list", "-w", str(workspace))
    assert "No daemon worktrees." in out


def test_repomap_cli(workspace):
    out = _run("repomap", "add", "-w", str(workspace))
    assert "bug.py" in out
    out = _run("repomap", "add", "--scores", "-w", str(workspace))
    assert "bug.py" in out


def test_memory_verified_cli(workspace):
    out = _run("memory", "add-verified", "add() must add its arguments",
               "--location", "bug.py:2", "--confidence", "0.95",
               "-w", str(workspace))
    assert "Verified fact saved" in out
    out = _run("memory", "verified", "-w", str(workspace))
    assert "add() must add" in out
    assert "0.95" in out

    entry_id = MemoryStore(workspace).get_verified_facts()[0].id
    out = _run("memory", "invalidate", entry_id, "--reason", "wrong",
               "-w", str(workspace))
    assert "Invalidated" in out
    out = _run("memory", "verified", "-w", str(workspace))
    assert "No verified facts yet" in out


def test_optimize_cli(workspace):
    save_report(OptimizationReport(
        session_id="sess-a", model="mock-coder", total_input_tokens=100,
        total_output_tokens=10, context_tokens_saved=40), workspace)
    save_report(OptimizationReport(
        session_id="sess-b", model="mock-coder", optimization_enabled=False,
        total_input_tokens=200, total_output_tokens=10), workspace)

    out = _run("optimize", "list", "-w", str(workspace))
    assert "sess-a" in out and "sess-b" in out
    out = _run("optimize", "show", "sess-a", "-w", str(workspace))
    assert "sess-a" in out
    out = _run("optimize", "compare", "sess-b", "sess-a", "-w", str(workspace))
    assert "sess" in out
    out = _run("optimize", "show", "missing", "-w", str(workspace))
    assert "No report" in out
