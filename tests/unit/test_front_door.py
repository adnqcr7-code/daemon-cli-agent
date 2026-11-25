"""Tests for the v0.9.0 §2.2 front door: credential-free REPL,
labeled demo offer, `daemon init` scaffolding, and `daemon plan
--provider demo` (the Phase 1 gap)."""

from __future__ import annotations

import tomllib

from click.testing import CliRunner

from daemon.agent.planner import parse_plan
from daemon.agent.prompts import planner_prompt
from daemon.cli import _launch_repl, main
from daemon.config import daemon_home
from daemon.providers.demo import _decide
from daemon.providers.types import ChatRequest, Message
from daemon.ui.repl import ask_demo_offer

# --------------------------------------------------------------------- init


def test_init_scaffolds_and_never_overwrites(tmp_path) -> None:
    workspace = tmp_path / "project"
    runner = CliRunner()
    result = runner.invoke(main, ["init", str(workspace)])
    assert result.exit_code == 0, result.output
    daemon_dir = workspace / ".daemon"
    for sub in ("memory", "skills", "checkpoints"):
        assert (daemon_dir / sub).is_dir()
    config = daemon_dir / "config.toml"
    assert config.exists()
    # Commented scaffold: parses as TOML, changes nothing by itself.
    assert tomllib.loads(config.read_text("utf-8")) == {}
    assert "# [model]" in config.read_text("utf-8")
    agents = workspace / "AGENTS.md"
    assert agents.exists()
    assert "Ask before anything destructive" in agents.read_text("utf-8")
    assert (workspace / ".gitignore").read_text("utf-8").count(
        ".daemon/checkpoints") == 1

    # Second run: customised files are never overwritten.
    config.write_text("[limits]\nmax_cost_usd = 1.0\n", encoding="utf-8")
    agents.write_text("# Mine\n", encoding="utf-8")
    result = runner.invoke(main, ["init", str(workspace)])
    assert result.exit_code == 0, result.output
    assert config.read_text("utf-8") == "[limits]\nmax_cost_usd = 1.0\n"
    assert agents.read_text("utf-8") == "# Mine\n"
    assert (workspace / ".gitignore").read_text("utf-8").count(
        ".daemon/checkpoints") == 1


# -------------------------------------------------------------- demo offer


def test_ask_demo_offer_defaults_yes_and_honours_no() -> None:
    assert ask_demo_offer(lambda _: "") is True
    assert ask_demo_offer(lambda _: "y") is True
    assert ask_demo_offer(lambda _: "yes") is True
    assert ask_demo_offer(lambda _: "n") is False
    assert ask_demo_offer(lambda _: "no") is False

    def _eof(_prompt: str):
        raise EOFError

    assert ask_demo_offer(_eof) is False


# ------------------------------------------------------- demo planner branch


def test_demo_provider_answers_planner_prompts(tmp_path) -> None:
    (tmp_path / "a.py").write_text("def add(a, b):\n    return a + b\n",
                                   encoding="utf-8")
    request = ChatRequest(
        model="demo-scripted",
        messages=[Message.user(
            planner_prompt("add a docstring to a.py", ""))],
        stream=True, max_tokens=2000)
    text, calls = _decide(request, tmp_path)
    assert not calls
    assert text is not None
    plan = parse_plan("add a docstring to a.py", text)
    assert len(plan.tasks) == 1
    assert plan.tasks[0].files == ["a.py"]
    assert "scripted" in plan.summary.lower()


# ------------------------------------------------------------ REPL front door


def test_bare_daemon_starts_demo_mode_without_credentials(
        tmp_path, monkeypatch) -> None:
    """No credentials -> the REPL starts on the demo provider instead
    of dying on AuthError, and nothing is persisted as a profile."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    seen: dict = {}

    class FakeRepl:
        def __init__(self, ctx, renderer, initial_prompt=None,
                     demo_mode=False):
            seen["ctx"] = ctx
            seen["demo_mode"] = demo_mode

        def start(self):
            seen["started"] = True

    monkeypatch.setattr("daemon.ui.repl.Repl", FakeRepl)
    _launch_repl(workspace=workspace, model=None, theme=None,
                 session_id=None, plain=True, check_updates=False)
    assert seen["started"] is True
    assert seen["demo_mode"] is True
    assert seen["ctx"].model.provider == "demo"
    assert not (daemon_home() / "auth.json").exists()


# ------------------------------------------------------------- plan --provider


def test_plan_with_demo_provider(tmp_path) -> None:
    """The Phase 1 gap, closed: `daemon plan` runs against the demo
    provider and prints a plan."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.py").write_text("def add(a, b):\n    return a + b\n",
                                    encoding="utf-8")
    result = CliRunner().invoke(main, [
        "plan", "add a docstring to a.py", "--provider", "demo",
        "-w", str(workspace)])
    assert result.exit_code == 0, result.output
    assert "Execute:" in result.output
    assert "a.py" in result.output


def test_plan_rejects_other_providers() -> None:
    result = CliRunner().invoke(main, [
        "plan", "do a thing", "--provider", "openai"])
    assert result.exit_code == 2
    assert "demo" in result.output
