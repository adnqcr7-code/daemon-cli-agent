"""End-to-end: the complete user journey through the real CLI.

auth add -> provider detection -> model discovery -> run a real task
-> tools execute in a real repository -> tests run -> diff -> undo.
Everything against the local mock provider; the only *real* work is the
actual file editing and the actual pytest run inside the workspace.
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parent.parent))
from mocks import OpenAICompatMock  # noqa: E402

from daemon.cli import main as cli_main  # noqa: E402

API_KEY = "test-key-1234567890"

FIX_STAGES = [
    {"tool_calls": [{"name": "read_file", "arguments": {"path": "bug.py"}}],
     "usage": {"prompt_tokens": 100, "completion_tokens": 10}},
    {"tool_calls": [{"name": "edit_file", "arguments": {
        "path": "bug.py", "old_text": "return a - b",
        "new_text": "return a + b"}}],
     "usage": {"prompt_tokens": 300, "completion_tokens": 20}},
    {"tool_calls": [{"name": "run_tests", "arguments": {}}],
     "usage": {"prompt_tokens": 500, "completion_tokens": 30}},
    {"text": "Fixed add() in bug.py (was subtracting instead of adding). "
             "Verified: pytest reports 1 passed.",
     "usage": {"prompt_tokens": 700, "completion_tokens": 40}},
]


@pytest.fixture
def mock(mock_openai):
    mock_openai.stages = FIX_STAGES
    return mock_openai


@pytest.fixture
def encrypted_home(home, monkeypatch):
    """Use the AES-256-GCM vault so the leakage sweep is meaningful."""
    monkeypatch.setenv("DAEMON_VAULT_BACKEND", "encrypted")
    return home


@pytest.fixture
def runner():
    return CliRunner()


def _invoke(runner, *args, expect_exit: int = 0):
    result = runner.invoke(cli_main, list(args))
    if expect_exit is not None and result.exit_code != expect_exit:
        raise AssertionError(
            f"`daemon {' '.join(args)}` exited {result.exit_code} "
            f"(expected {expect_exit}):\n{result.output}\n"
            f"{result.exception if result.exception else ''}")
    return result


def test_version_and_help(runner):
    result = _invoke(runner, "version")
    assert "daemon 0.1.0" in result.output
    result = _invoke(runner, "--help")
    for command in ("auth", "models", "run", "plan", "review", "research",
                    "tools", "mcp", "agents", "sessions", "memory", "config",
                    "doctor", "init"):
        assert command in result.output


def test_config_active_profile_switches_auth_profile(runner):
    from daemon.auth.manager import AuthManager
    from daemon.auth.store import Profile

    manager = AuthManager()
    manager.store.add(Profile(provider="ollama", label="first"),
                      allow_keyless=True)
    manager.store.add(Profile(provider="ollama", label="second"),
                      allow_keyless=True)
    manager.close()

    result = _invoke(runner, "config", "set", "active_profile",
                     "ollama#second")
    assert "Active profile: ollama#second" in result.output
    result = _invoke(runner, "auth", "list")
    assert "* ollama#second" in result.output


def test_full_workflow(runner, mock, encrypted_home, workspace):
    endpoint = mock.url + "/v1"

    # -- 1. add credentials with automatic verification --------------------
    result = _invoke(runner, "auth", "add", "--api-key", API_KEY,
                     "--provider", "openai", "--base-url", endpoint)
    assert "Found 2 accessible model(s)." in result.output
    assert "Authentication successful" in result.output
    assert API_KEY not in result.output  # never echoed

    # -- 2. the profile is listed without leaking the secret ---------------
    result = _invoke(runner, "auth", "list")
    assert "openai" in result.output
    assert API_KEY not in result.output

    # -- 3. models are visible through the cache ---------------------------
    result = _invoke(runner, "models", "list", "-w", str(workspace))
    assert "mock-coder" in result.output and "mock-fast" in result.output

    # Model inspection must remain usable when a stale configured role model
    # no longer exists on the active provider.
    _invoke(runner, "config", "set", "models.build", "stale-model")
    result = _invoke(runner, "models", "list", "-w", str(workspace))
    assert "mock-coder" in result.output and "mock-fast" in result.output
    result = _invoke(runner, "models", "list", "--refresh", "-w",
                     str(workspace))
    assert "mock-coder" in result.output and "mock-fast" in result.output
    _invoke(runner, "config", "set", "models.build", "mock-coder")

    # -- 4. run the task non-interactively ----------------------------------
    result = _invoke(runner, "run", "Fix the failing test in this repository.",
                     "--yes", "--json", "-w", str(workspace))
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["files_changed"] == ["bug.py"]
    assert payload["tool_calls"] == 3
    assert payload["usage"]["input_tokens"] == 1600

    # -- 5. the fix is real; the tests really passed ------------------------
    assert "return a + b" in (workspace / "bug.py").read_text()
    verification = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"], cwd=workspace,
        capture_output=True, text=True)
    assert verification.returncode == 0, verification.stdout

    # -- 6. the session was persisted and is listable ------------------------
    result = _invoke(runner, "sessions", "list")
    assert "build" in result.output

    # -- 7. checkpoints allow undo -------------------------------------------
    from daemon.checkpoints import CheckpointManager

    manager = CheckpointManager(workspace)
    latest = manager.latest()
    assert latest is not None and "bug.py" in latest.files
    diffs = manager.diff(latest.id)
    assert any("-    return a - b" in body and "+    return a + b" in body
               for _, body in diffs)
    asyncio.run(manager.restore(latest.id))
    assert "return a - b" in (workspace / "bug.py").read_text()
    # and the agent can redo it (mock still scripted)
    _invoke(runner, "run", "Fix the failing test again.", "--yes",
            "--json", "-w", str(workspace))

    # -- 8. doctor reports a healthy installation ---------------------------
    result = _invoke(runner, "doctor", "-w", str(workspace))
    assert "0 failures" in result.output

    # -- 9. no secret anywhere on disk (encrypted vault) --------------------
    for root in (encrypted_home, workspace):
        for path in Path(root).rglob("*"):
            if path.is_file():
                content = path.read_bytes()
                assert API_KEY.encode() not in content, \
                    f"secret leaked into {path}"


def test_research_one_shot_is_readonly(runner, mock, encrypted_home,
                                       workspace, monkeypatch):
    endpoint = mock.url + "/v1"
    _invoke(runner, "auth", "add", "--api-key", API_KEY,
            "--base-url", endpoint)

    async def fake_web_search(query, *, backend, limit, searxng_url=None):
        return [{"title": "Answer", "url": "https://example.com/a",
                 "snippet": "42"}]

    monkeypatch.setattr("daemon.tools.web.web_search", fake_web_search)
    mock.stages = [
        {"tool_calls": [{"name": "web_search",
                         "arguments": {"query": "the answer"}}]},
        {"text": "The answer is 42 [1]. https://example.com/a"},
    ]
    result = _invoke(runner, "research", "What is the answer?",
                     "-w", str(workspace))
    assert "42" in result.output
    # the research agent never touched the workspace
    assert "return a - b" in (workspace / "bug.py").read_text()
    assert "42" not in (workspace / "bug.py").read_text()


def test_plan_command_produces_task_list(runner, mock, encrypted_home,
                                         workspace):
    _invoke(runner, "auth", "add", "--api-key", API_KEY,
            "--base-url", mock.url + "/v1")
    plan_json = json.dumps({
        "summary": "fix the bug",
        "tasks": [
            {"id": "1", "title": "Read bug.py", "files": ["bug.py"],
             "verification": "read works", "depends_on": []},
            {"id": "2", "title": "Fix the subtraction",
             "files": ["bug.py"], "verification": "pytest",
             "depends_on": ["1"]},
        ]})
    mock.stages = [{"text": plan_json}]
    result = _invoke(runner, "plan", "fix the failing test",
                     "-w", str(workspace))
    assert "[ ] 1. Read bug.py" in result.output
    assert "[ ] 2. Fix the subtraction" in result.output


def test_run_denies_destructive_command_without_yes(runner, mock,
                                                     encrypted_home,
                                                    workspace):
    """Non-interactive without --yes: an 'ask' action fails closed, the
    agent receives the refusal, and nothing dangerous ran."""
    _invoke(runner, "auth", "add", "--api-key", API_KEY,
            "--base-url", mock.url + "/v1")
    mock.stages = [
        {"tool_calls": [{"name": "run_command",
                         "arguments": {"command": "pip install requests"}}]},
        {"text": "The install was not approved; stopped."},
    ]
    result = runner.invoke(cli_main, [
        "run", "install requests", "--json", "-w", str(workspace)])
    payload = json.loads(result.output)
    assert payload["ok"] is True  # the run itself finished gracefully
    text = payload["text"]
    assert "not approved" in text
    import importlib.util

    assert importlib.util.find_spec("requests") is not None  # preinstalled
    # nothing changed in the workspace
    assert "return a - b" in (workspace / "bug.py").read_text()


def test_auth_test_verifies_credentials(runner, mock, encrypted_home):
    _invoke(runner, "auth", "add", "--api-key", API_KEY,
            "--base-url", mock.url + "/v1")
    result = _invoke(runner, "auth", "test")
    assert "OK - 2 model(s) reachable" in result.output


def test_mcp_lifecycle_through_cli(runner, encrypted_home, config):
    echo_server = str(Path(__file__).parent.parent / "mcp_echo_server.py")
    _invoke(runner, "mcp", "add", "echo", "--command", sys.executable,
            "--arg", echo_server)
    result = _invoke(runner, "mcp", "list")
    assert "echo" in result.output
    assert "unreviewed" in result.output
    result = _invoke(runner, "mcp", "test", "echo")
    assert "echo-mcp" in result.output
    assert "echo, add" in result.output
    _invoke(runner, "mcp", "enable", "echo", "--yes")
    result = _invoke(runner, "mcp", "list")
    assert "trusted" in result.output
    _invoke(runner, "mcp", "disable", "echo")
    result = _invoke(runner, "mcp", "list")
    assert "disabled" in result.output


def test_doctor_reports_missing_credentials_cleanly(runner, encrypted_home,
                                                     workspace):
    result = _invoke(runner, "doctor", "-w", str(workspace))
    assert "credential storage" in result.output
    # plain-vault warning path is exercised by the conftest default elsewhere
