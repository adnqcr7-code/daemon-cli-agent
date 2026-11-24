"""Reflex CLI: list / proposals / approve / purge (design v4 Phase 1)."""

from click.testing import CliRunner

from daemon.agent.reflex import ReflexEngine
from daemon.cli import main


def _run(*args: str):
    result = CliRunner().invoke(main, list(args))
    assert result.exit_code == 0, result.output
    return result.output


def _seed_proposal(tmp_path) -> str:
    engine = ReflexEngine(workspace=tmp_path)
    entry = engine.propose(
        "agent:model",
        {"files": ["bug.py"], "symbols": ["add"], "tools": []},
        "bug.py:1 function add(a, b)",
        derivation="model derivation, unverified")
    assert entry is not None
    return entry.id


def test_reflex_cli_lifecycle(tmp_path):
    entry_id = _seed_proposal(tmp_path)

    out = _run("reflex", "proposals", "-w", str(tmp_path))
    assert entry_id in out
    assert "add(a, b)" in out

    out = _run("reflex", "list", "-w", str(tmp_path))
    assert "proposal" in out

    out = _run("reflex", "approve", entry_id, "-w", str(tmp_path))
    assert "Certified" in out

    out = _run("reflex", "list", "-w", str(tmp_path))
    assert "certified" in out
    assert "user_approval" in out
    out = _run("reflex", "proposals", "-w", str(tmp_path))
    assert "No pending reflex proposals." in out

    out = _run("reflex", "purge", "-w", str(tmp_path))
    assert "Purged 1" in out
    out = _run("reflex", "list", "-w", str(tmp_path))
    assert "No reflex entries yet" in out


def test_reflex_approve_unknown_id(tmp_path):
    out = _run("reflex", "approve", "missing-id", "-w", str(tmp_path))
    assert "No pending proposal" in out
