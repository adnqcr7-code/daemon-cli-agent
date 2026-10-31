"""Configuration layering, env overrides, TOML round-trips."""

import tomllib

from daemon.config import Config, toml_dumps
from daemon.errors import ConfigError


def test_defaults_present(home):
    config = Config.load()
    assert config.get("ui.theme") == "obsidian"
    assert config.get("limits.max_turns") == 40
    assert config.get("permissions.assume_yes") is False


def test_user_file_overlay(home):
    (home / "config.toml").write_text(
        '[ui]\ntheme = "archive"\n[limits]\nmax_turns = 12\n')
    config = Config.load()
    assert config.theme_name() == "archive"
    assert config.max_turns() == 12
    assert config.get("limits.tool_timeout_s") == 120  # untouched default


def test_project_file_overlays_user(home, tmp_path):
    (home / "config.toml").write_text('[ui]\ntheme = "minimal"\n')
    project = tmp_path / "proj"
    (project / ".daemon").mkdir(parents=True)
    (project / ".daemon" / "config.toml").write_text('[ui]\ntheme = "classic"\n')
    config = Config.load(project)
    assert config.theme_name() == "classic"


def test_env_overrides(home, monkeypatch):
    monkeypatch.setenv("DAEMON_THEME", "archive")
    monkeypatch.setenv("DAEMON_TOOL_TIMEOUT", "45")
    config = Config.load()
    assert config.theme_name() == "archive"
    assert config.tool_timeout() == 45


def test_set_user_persists(home):
    config = Config.load()
    config.set_user("models.coding", "gpt-4o")
    config.set_user("research.search_backend", "searxng")
    reread = Config.load()
    assert reread.get("models.coding") == "gpt-4o"
    assert reread.get("research.search_backend") == "searxng"


def test_set_project_writes_only_project_file(home, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    config = Config.load(project)
    config.set_project(project, "ui.theme", "minimal")
    assert (project / ".daemon" / "config.toml").exists()
    assert not (home / "config.toml").exists()
    payload = tomllib.loads(
        (project / ".daemon" / "config.toml").read_text())
    assert payload["ui"]["theme"] == "minimal"


def test_toml_roundtrip():
    data = {
        "name": "quoted \"string\"", "count": 3, "ratio": 1.5, "flag": True,
        "list": ["a", "b"],
        "nested": {"deeper": {"x": 1}},
        "rules": [{"tool": "run_command", "effect": "allow"}],
    }
    parsed = tomllib.loads(toml_dumps(data))
    assert parsed == data


def test_invalid_toml_raises_config_error(home):
    (home / "config.toml").write_text("this is [ not toml")
    import pytest

    with pytest.raises(ConfigError):
        Config.load()


def test_mcp_servers_roundtrip(home):
    config = Config.load()
    config.set_user("mcp.servers", {
        "echo": {"transport": "stdio", "command": "python",
                 "args": ["echo.py"], "trusted": True}})
    reread = Config.load()
    servers = reread.get("mcp.servers")
    assert servers["echo"]["command"] == "python"
    assert servers["echo"]["trusted"] is True
