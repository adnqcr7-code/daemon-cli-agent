"""REPL smoke test: slash-command dispatch and startup wiring with a
scripted input queue (no terminal needed)."""

import asyncio
from pathlib import Path

import pytest

from daemon.config import Config
from daemon.context import build_context
from daemon.ui.render import Renderer
from daemon.ui.repl import Repl
from daemon.ui.themes import resolve_theme


@pytest.fixture
def repl_ctx(mock_openai, home, workspace):
    mock_openai.stages = [{"text": "ok"}]
    config = Config.load(workspace)

    class _IO:
        def info(self, m): pass
        def warn(self, m): pass
        def success(self, m): pass
        def prompt_secret(self, p): return "test-key-1234567890"
        def ask_text(self, p, default=None): return ""
        def ask_choice(self, q, options): return options[0][0]
        def confirm(self, q, default=False): return True

    from daemon.auth.manager import AuthManager

    auth = AuthManager(config)
    auth.add(_IO(), key="test-key-1234567890",
             base_url=mock_openai.url + "/v1")
    auth.close()

    ctx = build_context(workspace=workspace, config=config,
                        renderer=None, interactive=False)
    return ctx, mock_openai


def test_repl_slash_commands_dispatch(capsys, repl_ctx):
    ctx, mock = repl_ctx
    renderer = Renderer(resolve_theme("obsidian"), plain=True)
    repl = Repl(ctx, renderer)
    script = ["/help", "/tools", "/models", "/agents", "/permissions",
              "/memory", "/stats", "/session", "/skills", "/mcp", "/tasks",
              "/theme", "/exit"]
    queue = iter(script)

    def fake_read():
        try:
            return next(queue)
        except StopIteration:
            raise EOFError from None

    repl._read_line = fake_read
    repl.start()
    output = capsys.readouterr().out
    assert "read_file" in output          # /tools listed tools
    assert "mock-coder" in output         # /models listed models
    assert "architect" in output          # /agents listed roles
    assert "ALLOW" in output or "ASK" in output  # /permissions
    assert "daemon" in output.lower()


def test_repl_agent_turn_and_exit(capsys, repl_ctx):
    ctx, mock = repl_ctx
    renderer = Renderer(resolve_theme("obsidian"), plain=True)
    repl = Repl(ctx, renderer)
    queue = iter(["hello agent", "/exit"])

    def fake_read():
        try:
            return next(queue)
        except StopIteration:
            raise EOFError from None

    repl._read_line = fake_read
    repl.start()
    output = capsys.readouterr().out
    assert "ok" in output                  # the model's streamed answer


def test_repl_model_switch(capsys, repl_ctx):
    ctx, mock = repl_ctx
    renderer = Renderer(resolve_theme("obsidian"), plain=True)
    repl = Repl(ctx, renderer)
    queue = iter(["/model mock-fast", "/exit"])

    def fake_read():
        try:
            return next(queue)
        except StopIteration:
            raise EOFError from None

    repl._read_line = fake_read
    repl.start()
    assert ctx.model.model_id == "mock-fast"


def test_repl_remember_and_memory(capsys, repl_ctx):
    ctx, mock = repl_ctx
    renderer = Renderer(resolve_theme("obsidian"), plain=True)
    repl = Repl(ctx, renderer)
    queue = iter(["/remember always run tests before reporting done",
                  "/memory tests", "/exit"])

    def fake_read():
        try:
            return next(queue)
        except StopIteration:
            raise EOFError from None

    repl._read_line = fake_read
    repl.start()
    output = capsys.readouterr().out
    assert "always run tests" in output


def test_repl_compact_command(capsys, repl_ctx):
    ctx, mock = repl_ctx
    renderer = Renderer(resolve_theme("obsidian"), plain=True)
    repl = Repl(ctx, renderer)
    queue = iter(["/compact", "/exit"])

    def fake_read():
        try:
            return next(queue)
        except StopIteration:
            raise EOFError from None

    repl._read_line = fake_read
    repl.start()
    output = capsys.readouterr().out
    assert "compact" in output.lower()
