"""Shared fixtures: isolated DAEMON_HOME, temp workspaces, mock providers."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from mocks import AnthropicMock, GeminiMock, OpenAICompatMock  # noqa: E402


def run(coro):
    """Run a coroutine to completion (no pytest-asyncio dependency)."""
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolate_daemon_home(tmp_path, monkeypatch):
    """Every test gets an isolated DAEMON_HOME - no test may write sessions,
    caches or logs into the developer's real state directory."""
    monkeypatch.setenv("DAEMON_HOME", str(tmp_path / "auto-daemon-home"))


@pytest.fixture(autouse=True)
def clean_globals():
    """Reset process-global state around every test."""
    from daemon.security import SecretRedactor, _GLOBAL_REDACTOR

    saved_known = set(_GLOBAL_REDACTOR._known)
    yield
    _GLOBAL_REDACTOR._known.clear()
    _GLOBAL_REDACTOR._known.update(saved_known)


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """Isolated DAEMON_HOME with the fast plain vault backend."""
    home_dir = tmp_path / "daemon-home"
    home_dir.mkdir()
    monkeypatch.setenv("DAEMON_HOME", str(home_dir))
    monkeypatch.setenv("DAEMON_VAULT_BACKEND", "plain")
    monkeypatch.delenv("DAEMON_PROVIDER", raising=False)
    monkeypatch.delenv("DAEMON_MODEL", raising=False)
    return home_dir


@pytest.fixture
def workspace(tmp_path) -> Path:
    """A small git repository with a deliberately buggy python project."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "bug.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "test_bug.py").write_text(
        "from bug import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "sample"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n',
        encoding="utf-8")
    (repo / "README.md").write_text("# sample\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"],
                   cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo,
                   check=True)
    return repo


@pytest.fixture
def mock_openai():
    server = OpenAICompatMock()
    yield server
    server.stop()


@pytest.fixture
def mock_anthropic():
    server = AnthropicMock()
    yield server
    server.stop()


@pytest.fixture
def mock_gemini():
    server = GeminiMock()
    yield server
    server.stop()


class ScriptedIO:
    """PromptIO with canned answers for auth-flow tests."""

    def __init__(self, *, secret: str = "gsk_" + "a" * 40,
                 choices: list[str] | None = None,
                 texts: list[str] | None = None,
                 confirms: bool = True) -> None:
        self.secret = secret
        self.choices = list(choices or [])
        self.texts = list(texts or [])
        self.confirms = confirms
        self.messages: list[str] = []

    def info(self, message: str) -> None:
        self.messages.append(f"info: {message}")

    def warn(self, message: str) -> None:
        self.messages.append(f"warn: {message}")

    def success(self, message: str) -> None:
        self.messages.append(f"success: {message}")

    def prompt_secret(self, prompt: str) -> str:
        self.messages.append(f"secret: {prompt}")
        return self.secret

    def ask_text(self, prompt: str, default: str | None = None) -> str:
        self.messages.append(f"text: {prompt}")
        return self.texts.pop(0) if self.texts else (default or "")

    def ask_choice(self, question: str, options) -> str:
        self.messages.append(f"choice: {question}")
        return self.choices.pop(0) if self.choices else options[0][0]

    def confirm(self, question: str, default: bool = False) -> bool:
        self.messages.append(f"confirm: {question}")
        return self.confirms


@pytest.fixture
def scripted_io():
    return ScriptedIO()


@pytest.fixture
def config(workspace, home):
    from daemon.config import Config

    return Config.load(workspace)


MCP_ECHO_SERVER = Path(__file__).parent / "mcp_echo_server.py"
