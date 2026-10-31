"""Layered configuration: defaults <- user file <- project file <- environment.

* User config    ``~/.daemon/config.toml`` (or ``$DAEMON_HOME``)
* Project config ``<workspace>/.daemon/config.toml``
* Environment    ``DAEMON_<SECTION>_<KEY>`` overrides for selected keys.

Reading uses stdlib ``tomllib``. Writing uses a small serializer that only
supports the value shapes Daemon itself writes (strings, numbers, booleans,
lists of strings, nested tables) - we never emit arbitrary user TOML, which
keeps the writer simple and safe.
"""

from __future__ import annotations

import copy
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import tomllib

from .errors import ConfigError

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def daemon_home() -> Path:
    """Directory holding user-level state (config, credentials, sessions...)."""
    override = os.environ.get("DAEMON_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".daemon"


def project_dir(workspace: Path | str) -> Path:
    return Path(workspace) / ".daemon"


DEFAULTS: dict[str, Any] = {
    "default_provider": None,
    "default_model": None,
    "models": {
        "coding": None,
        "planning": None,
        "research": None,
        "review": None,
    },
    "ui": {
        "theme": "obsidian",
        "unicode": True,
        "spinner": True,
    },
    "limits": {
        "warn_cost_usd": 1.0,
        "max_cost_usd": None,
        "max_turns": 40,
        "tool_timeout_s": 120,
        "request_timeout_s": 180,
        "connect_timeout_s": 10,
        "retries": 3,
    },
    "permissions": {
        # Extra rules layered on top of the built-in defaults.
        "rules": [],
        # Only honoured when the user passes --assume-yes explicitly.
        "assume_yes": False,
    },
    "research": {
        "search_backend": "duckduckgo",
        "searxng_url": None,
        "max_results": 8,
        "fetch_limit_kb": 48,
    },
    "index": {
        "extra_ignore": [],
        "max_files": 20000,
    },
    "mcp": {
        "servers": {},  # id -> {transport, command/args or url, env, enabled, trusted, headers}
    },
    "plugins": {
        "enabled": [],  # opt-in local plugin modules under .daemon/plugins/
    },
    "sessions": {
        "keep": 50,
    },
    "optimization": {
        "enabled": True,
        "context": {
            "enabled": True,
            "preserve_recent_turns": 4,
            "dedupe_tool_results": True,
            "summarize_old_turns": True,
            "max_summary_length": 2000,
        },
        "tools": {
            "enabled": False,
            "min_score": 0.15,
            "max_tools": 20,
        },
        "failure_detection": {
            "enabled": False,
            "max_failures": 3,
            "window_turns": 10,
        },
        "memory": {
            "verified_facts_in_prompt": False,
            "max_verified_facts": 10,
        },
        "reporting": {
            "enabled": False,
        },
    },
}

# Environment overrides: env var name -> config path.
_ENV_OVERRIDES: dict[str, str] = {
    "DAEMON_PROVIDER": "default_provider",
    "DAEMON_MODEL": "default_model",
    "DAEMON_THEME": "ui.theme",
    "DAEMON_SEARCH_BACKEND": "research.search_backend",
    "DAEMON_TOOL_TIMEOUT": "limits.tool_timeout_s",
    "DAEMON_MAX_COST": "limits.max_cost_usd",
}


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _set_path(target: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = target
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


class Config:
    """Read-only merged view of Daemon settings with typed accessors."""

    def __init__(self, data: dict[str, Any], *, user_path: Path,
                 project_path: Path | None) -> None:
        self._data = data
        self.user_path = user_path
        self.project_path = project_path

    # -- construction ------------------------------------------------------

    @classmethod
    def load(cls, workspace: Path | str | None = None) -> "Config":
        home = daemon_home()
        user_path = home / "config.toml"
        project_path = None
        if workspace is not None:
            candidate = project_dir(workspace) / "config.toml"
            if candidate.exists():
                project_path = candidate

        data = copy.deepcopy(DEFAULTS)
        for path in (user_path, project_path):
            if path is not None and path.exists():
                data = _deep_merge(data, _read_toml(path))
        for env_name, dotted in _ENV_OVERRIDES.items():
            raw = os.environ.get(env_name)
            if raw is None or raw == "":
                continue
            value: Any = raw
            if dotted in ("limits.tool_timeout_s", "limits.max_cost_usd"):
                try:
                    value = float(raw) if "." in raw else int(raw)
                except ValueError as exc:
                    raise ConfigError(f"Bad value for {env_name}: {raw!r}") from exc
            _set_path(data, dotted, value)
        return cls(data, user_path=user_path, project_path=project_path)

    # -- access ------------------------------------------------------------

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, dotted: str) -> Any:
        value = self.get(dotted)
        if value is None:
            raise ConfigError(f"Missing required configuration: {dotted}")
        return value

    @property
    def raw(self) -> dict[str, Any]:
        return self._data

    # -- typed helpers -----------------------------------------------------

    def theme_name(self) -> str:
        return str(self.get("ui.theme") or "obsidian")

    def tool_timeout(self) -> int:
        return int(self.get("limits.tool_timeout_s") or 120)

    def request_timeouts(self) -> tuple[float, float]:
        return (
            float(self.get("limits.connect_timeout_s") or 10),
            float(self.get("limits.request_timeout_s") or 180),
        )

    def max_turns(self) -> int:
        return int(self.get("limits.max_turns") or 40)

    def retries(self) -> int:
        return int(self.get("limits.retries") or 3)

    # -- persistence -------------------------------------------------------

    def set_user(self, dotted: str, value: Any) -> None:
        _set_path(self._data, dotted, value)
        self.save_user()

    def save_user(self) -> None:
        self.user_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.user_path, toml_dumps(self._data))

    def set_project(self, workspace: Path, dotted: str, value: Any) -> None:
        _set_path(self._data, dotted, value)
        path = project_dir(workspace) / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        existing: dict[str, Any] = {}
        if path.exists():
            existing = _read_toml(path)
        _set_path(existing, dotted, value)
        _atomic_write(path, toml_dumps(existing))
        self.project_path = path


# ---------------------------------------------------------------------------
# TOML reading / writing helpers
# ---------------------------------------------------------------------------


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from exc


def toml_dumps(data: dict[str, Any]) -> str:
    """Serialize a dictionary of primitives into TOML text.

    Supports: str, int, float, bool, list of str/int/float, nested dicts.
    Tables are emitted after scalars, recursively. Values of ``None`` and
    empty dicts/lists are skipped.
    """
    lines: list[str] = _dump_table(data, prefix=[])
    return "\n".join(lines) + "\n"


def _dump_table(data: dict[str, Any], prefix: list[str]) -> list[str]:
    lines: list[str] = []
    scalars: list[str] = []
    tables: list[tuple[str, dict[str, Any]]] = []
    list_of_tables: list[tuple[str, list[Any]]] = []

    for key, value in data.items():
        if value is None or value == {} or value == []:
            continue
        if isinstance(value, dict):
            tables.append((key, value))
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            list_of_tables.append((key, value))
        else:
            scalars.append(f"{_key(key)} = {_value(value)}")

    if prefix and (scalars or tables or list_of_tables):
        lines.append("[" + ".".join(_key(part) for part in prefix) + "]")
    elif prefix:  # explicit empty table
        lines.append("[" + ".".join(_key(part) for part in prefix) + "]")
    lines.extend(scalars)

    for key, rows in list_of_tables:
        for row in rows:
            lines.append("")
            lines.append("[[" + ".".join(_key(part) for part in prefix + [key]) + "]]")
            lines.extend(f"{_key(k)} = {_value(v)}" for k, v in row.items())
    for key, sub in tables:
        lines.append("")
        lines.extend(_dump_table(sub, prefix=prefix + [key]))
    return lines


_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(name: str) -> str:
    if _BARE_KEY.match(name):
        return name
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        return f'"{escaped}"'
    if isinstance(value, (list, tuple)):
        if not value:
            return "[]"
        if all(isinstance(item, (int, float)) for item in value):
            return "[" + ", ".join(repr(item) for item in value) + "]"
        return "[" + ", ".join(_value(item) for item in value) + "]"
    raise ConfigError(f"Cannot serialize value of type {type(value).__name__} to TOML")


def _atomic_write(path: Path, text: str, *, mode: int = 0o644) -> None:
    """Write via a temp file + rename so a crash never leaves a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, str(path))
    except BaseException:
        try:
            os.unlink(tmp_name)
        finally:
            raise
