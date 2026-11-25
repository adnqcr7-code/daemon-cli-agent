"""Layered configuration: defaults <- user file <- env fragment <-
project file <- environment.

* User config    ``~/.daemon/config.toml`` (or ``$DAEMON_HOME``)
* Env fragment   ``~/.daemon/envs/<name>/config.toml`` (active env only)
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
import tomllib
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .log import get_logger

_logger = get_logger("config")

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


# ---------------------------------------------------------------------------
# Environments (v0.9.0 §2.4): the active env is a single file under the
# home dir, so `daemon env use` takes effect on the next Config.load
# with no process state. These helpers live here (not in envs.py) so
# the config loader can use them without an import cycle: envs.py
# imports this module, never the reverse.
# ---------------------------------------------------------------------------

ACTIVE_ENV_FILE = "active_env"


def envs_root(home: Path | str | None = None) -> Path:
    return (Path(home) if home is not None else daemon_home()) / "envs"


def active_env_name(home: Path | str | None = None) -> str | None:
    """Name of the active environment, or None when no env is active."""
    path = (Path(home) if home is not None else daemon_home()) \
        / ACTIVE_ENV_FILE
    try:
        name = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return name or None


def active_env_dir(home: Path | str | None = None) -> Path | None:
    """Directory of the active environment, or None."""
    name = active_env_name(home)
    if name is None:
        return None
    return envs_root(home) / name


def set_active_env(name: str | None,
                   home: Path | str | None = None) -> None:
    """Persist (or clear, with None) the active environment."""
    home_path = Path(home) if home is not None else daemon_home()
    path = home_path / ACTIVE_ENV_FILE
    if name is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    home_path.mkdir(parents=True, exist_ok=True)
    path.write_text(name + "\n", encoding="utf-8")


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
        # Diff display budget (§2.5): the permission ASK preview and
        # the post-edit display are truncated past this, with a
        # "… N more lines" marker. Tokens are ~4 chars each.
        "diff_max_lines": 200,
        "diff_max_tokens": 8000,
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
    "sandbox": {
        # v0.5.0 §5: run shell commands inside bubblewrap (Linux).
        # OFF by default - opt-in until it has survived real use.
        # When on but unavailable (no bwrap, probe fails, non-Linux),
        # commands run unsandboxed with a loud one-time warning and
        # doctor reports the gap; availability is probed, never faked.
        "shell": False,
        # v0.9.0 §2.6: image for the container backend. Only used
        # when a container CLI is present and the image exists
        # locally - the backend never pulls: a missing image means
        # the backend is unavailable and selection falls through to
        # bwrap. Set this to an image you have (e.g. a local build)
        # to use the container backend.
        "container_image": "docker.io/library/alpine:latest",
    },
    "verification": {
        # After an agent turn that changed files, verify before reporting
        # done: byte-compile changed Python files, then run the project's
        # test runner scoped to the touched package. A failure is reported,
        # never silently ignored; set enabled=false to opt out.
        "enabled": True,
        "run_tests": True,
        "lint": True,
        "typecheck": True,
        # On a verification failure, feed the failures back to the agent
        # for this many repair attempts before reporting the turn done.
        "repair_attempts": 1,
        "timeout_s": 300,
    },
    "repomap": {
        # Ranked repo map (most relevant files for the current task)
        # injected into the system prompt, Aider-style.
        "enabled": True,
        "token_budget": 512,
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
    "code_intelligence": {
        "lsp": {
            "servers": {},
        },
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
    "updates": {
        "check": True,
    },
    "optimization": {
        "enabled": True,
        "context": {
            "enabled": True,
            "summarize_threshold": 0.9,
            "preserve_recent_turns": 4,
            "dedupe_tool_results": True,
            "summarize_old_turns": True,
            "max_summary_length": 2000,
        },
        "tools": {
            "enabled": True,
            "min_score": 0.15,
            "max_tools": 20,
        },
        "failure_detection": {
            "enabled": True,
            "max_failures": 3,
            "window_turns": 10,
        },
        "reflex": {
            "enabled": True,
            # Phase 2 fence (design v4 section 3.2): deterministic
            # artifact serves are not implemented in Phase 1; the key
            # exists so projects can pin the intended value now.
            "hard_signals": False,
            "max_signals_per_turn": 1,
            "entry_ttl_days": None,
        },
        "memory": {
            "verified_facts_in_prompt": False,
            "max_verified_facts": 10,
        },
        "reporting": {
            "enabled": True,
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


def _tag_env_rules(layer: dict[str, Any]) -> None:
    """Mark an env fragment's rules with origin "env" (in place).

    The broker treats origin "env" as unable to relax the classifier
    floor (review round 1, R1-4): without the tag, env rules arrived
    as origin "user" and a path ALLOW could widen the
    outside-workspace ASK floor to ALLOW. The tag overwrites any
    origin the fragment declares, so a fragment cannot smuggle a
    user-origin rule past the broker.
    """
    permissions = layer.get("permissions")
    if not isinstance(permissions, dict):
        return
    rules = permissions.get("rules")
    if not isinstance(rules, list):
        return
    for raw in rules:
        if isinstance(raw, dict):
            raw["origin"] = "env"


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
    def load(cls, workspace: Path | str | None = None) -> Config:
        home = daemon_home()
        user_path = home / "config.toml"
        project_path = None
        if workspace is not None:
            candidate = project_dir(workspace) / "config.toml"
            if candidate.exists():
                project_path = candidate

        data = copy.deepcopy(DEFAULTS)
        # Env fragment layer (§2.4): between the user file and the
        # project file — the env scopes the run, the project is the
        # most local and wins. A dangling active-env pointer (env
        # directory deleted by hand) warns and continues without the
        # fragment rather than breaking every command.
        env_path = None
        env_name = active_env_name(home)
        if env_name is not None:
            candidate = envs_root(home) / env_name / "config.toml"
            if candidate.exists():
                env_path = candidate
            else:
                _logger.warning(
                    "active environment %r has no config.toml; "
                    "continuing without it", env_name)
        for path, is_env in ((user_path, False), (env_path, True),
                             (project_path, False)):
            if path is not None and path.exists():
                layer = _read_toml(path)
                if is_env:
                    # Re-validate on every load (review round 1,
                    # R1-4): a fragment hand-edited after `env use`
                    # bypassed the create/use-time screen. An invalid
                    # fragment is skipped, with a warning — the env's
                    # rules never apply, so the mistake fails closed
                    # instead of widening the floor mid-run. The
                    # lazy import avoids a config <-> envs cycle
                    # (envs imports this module at its top level).
                    from .envs import EnvError, validate_fragment

                    try:
                        validate_fragment(layer)
                    except EnvError as exc:
                        _logger.warning(
                            "active environment %r fragment is invalid "
                            "(%s); continuing without it", env_name, exc)
                        continue
                    _tag_env_rules(layer)
                data = _deep_merge(data, layer)
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
