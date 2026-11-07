"""Local plugins and distributable plugin entry points.

This module is the loader the docs describe: it was missing from the
codebase, so ``[plugins] enabled = [...]`` in config and the
``daemon.tools`` entry-point group were documented but did nothing.

Two sources, both opt-in and both failure-tolerant (a broken plugin logs
a warning and is skipped — it never crashes startup):

* **Local plugins**: modules under ``<workspace>/.daemon/plugins/``,
  listed by name in ``[plugins] enabled``. The module must define a
  ``register(registry)`` hook. Local code executes with Daemon's
  permissions, so nothing is auto-loaded — only listed names.
* **Distributable plugins**: installed packages exposing a
  ``daemon.tools`` entry point. Each callable receives the registry.

``daemon.providers`` / ``daemon.agents`` entry-point groups are *not*
loaded yet (the docs used to claim they were); see
``docs/tool-development.md``.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .log import get_logger

_logger = get_logger("plugins")

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class PluginLoadResult:
    """What plugin loading did: names loaded, and (name, reason) failures."""

    loaded: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    def extend(self, other: PluginLoadResult) -> PluginLoadResult:
        self.loaded.extend(other.loaded)
        self.failed.extend(other.failed)
        return self


def load_local_plugins(workspace: Path, enabled: list[str],
                       registry: Any) -> PluginLoadResult:
    """Load ``.daemon/plugins/<name>.py`` modules listed in ``enabled``."""
    result = PluginLoadResult()
    plugin_dir = Path(workspace) / ".daemon" / "plugins"
    for name in enabled or ():
        if not isinstance(name, str) or not _NAME.match(name):
            result.failed.append((str(name), "not a valid module name"))
            continue
        module_path = plugin_dir / f"{name}.py"
        if not module_path.is_file():
            result.failed.append(
                (name, f"not found: {module_path}"))
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                f"daemon_plugin_{name}", module_path)
            if spec is None or spec.loader is None:
                raise ImportError(f"could not load {module_path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 - a broken plugin must not crash startup
            result.failed.append((name, f"import failed: {exc}"))
            _logger.warning("plugin %s failed to import: %s", name, exc)
            continue
        register = getattr(module, "register", None)
        if not callable(register):
            result.failed.append(
                (name, "no register(registry) hook found"))
            continue
        try:
            register(registry)
        except Exception as exc:  # noqa: BLE001
            result.failed.append((name, f"register() failed: {exc}"))
            _logger.warning("plugin %s register() failed: %s", name, exc)
            continue
        result.loaded.append(name)
    return result


def load_entry_point_plugins(registry: Any) -> PluginLoadResult:
    """Load installed distributions exposing ``daemon.tools`` entry points."""
    result = PluginLoadResult()
    try:
        entry_points = importlib.metadata.entry_points(group="daemon.tools")
    except Exception as exc:  # noqa: BLE001 - metadata backends differ
        _logger.warning("could not read daemon.tools entry points: %s", exc)
        return result
    for entry_point in entry_points:
        try:
            register = entry_point.load()
            register(registry)
        except Exception as exc:  # noqa: BLE001
            result.failed.append((entry_point.name, f"load failed: {exc}"))
            _logger.warning("entry-point plugin %s failed: %s",
                            entry_point.name, exc)
            continue
        result.loaded.append(f"entry-point:{entry_point.name}")
    return result


def load_plugins(workspace: Path, config: Any, registry: Any) -> PluginLoadResult:
    """Load every plugin source. ``config`` may be None (defaults apply)."""
    enabled = []
    if config is not None:
        enabled = config.get("plugins.enabled", []) or []
    result = load_local_plugins(workspace, enabled, registry)
    result.extend(load_entry_point_plugins(registry))
    for name, reason in result.failed:
        _logger.warning("plugin %s not loaded: %s", name, reason)
    if result.loaded:
        _logger.info("loaded plugins: %s", ", ".join(result.loaded))
    return result
