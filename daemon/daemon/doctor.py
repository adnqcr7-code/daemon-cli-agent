"""`daemon doctor`: diagnose installation, environment, providers, models,
tools and project integration. Optionally verify network reachability."""

from __future__ import annotations

import asyncio
import platform
import shutil
import sys
from pathlib import Path

from .config import Config, daemon_home
from .errors import DaemonError
from .log import get_logger

_logger = get_logger("doctor")

_OK, _WARN, _FAIL, _INFO = "ok", "warn", "fail", "info"


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []  # status, check, detail

    def add(self, status: str, check: str, detail: str = "") -> None:
        self.rows.append((status, check, detail))

    @property
    def failures(self) -> int:
        return sum(1 for status, _, _ in self.rows if status == _FAIL)

    @property
    def warnings(self) -> int:
        return sum(1 for status, _, _ in self.rows if status == _WARN)


def run_doctor(config: Config, workspace: Path, *,
               network: bool = False, fix: bool = False) -> Report:
    report = Report()
    _check_python(report)
    _check_terminal(report)
    home = _check_home(report, fix)
    _check_dependencies(report)
    _check_vault(report)
    _check_profiles(report)
    _check_models(report)
    _check_git(report)
    _check_shell(report)
    _check_devtools(report)
    _check_project(report, workspace)
    _check_mcp(report, config)
    if network:
        _check_network(report, config)
    return report


def _check_python(report: Report) -> None:
    version = sys.version_info
    detail = f"Python {platform.python_version()} on {platform.system()} " \
             f"{platform.release()} ({platform.machine()})"
    if version >= (3, 11):
        report.add(_OK, "python", detail)
    else:
        report.add(_FAIL, "python",
                   f"{detail} - Daemon requires Python 3.11 or newer")


def _check_terminal(report: Report) -> None:
    import os

    if os.environ.get("NO_COLOR"):
        report.add(_INFO, "terminal", "NO_COLOR set - plain output mode")
    elif sys.stdout.isatty():
        report.add(_OK, "terminal", "interactive colour terminal detected")
    else:
        report.add(_INFO, "terminal", "non-TTY output (piping?)")


def _check_home(report: Report, fix: bool) -> Path:
    home = daemon_home()
    try:
        home.mkdir(parents=True, exist_ok=True)
        probe = home / ".doctor-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        report.add(_OK, "daemon home", str(home))
    except OSError as exc:
        report.add(_FAIL, "daemon home",
                   f"{home} is not writable: {exc}"
                   + ("; set DAEMON_HOME to a writable path" if not fix else ""))
    return home


def _check_dependencies(report: Report) -> None:
    required = {"click": "8.1", "rich": "13.7", "httpx": "0.27"}
    for module, minimum in required.items():
        try:
            imported = __import__(module)
            version = getattr(imported, "__version__", "?")
        except ImportError:
            report.add(_FAIL, f"dependency {module}",
                       f"missing - pip install '{module}>={minimum}'")
            continue
        report.add(_OK, f"dependency {module}", version)
    optional = {
        "keyring": "OS keychain storage",
        "cryptography": "AES-256-GCM encrypted vault",
        "prompt_toolkit": "richer interactive input",
    }
    for module, purpose in optional.items():
        if _module_available(module):
            report.add(_OK, f"optional {module}", purpose)
        else:
            report.add(_WARN, f"optional {module}",
                       f"not installed - enables {purpose} "
                       f"(pip install 'daemon-agent[{module.split('_')[0]}]')")


def _module_available(name: str) -> bool:
    from importlib.util import find_spec

    try:
        return find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _check_vault(report: Report) -> None:
    import os

    forced = os.environ.get("DAEMON_VAULT_BACKEND")
    try:
        from .auth.vault import open_vault

        vault = open_vault(daemon_home())
        level = vault.security_level
        if vault.warning:
            report.add(_WARN, "credential storage",
                       f"{level} - {vault.warning}")
        else:
            report.add(_OK, "credential storage", level)
        if forced:
            report.add(_INFO, "credential storage",
                       f"forced via DAEMON_VAULT_BACKEND={forced}")
        vault.close()
    except DaemonError as exc:
        report.add(_FAIL, "credential storage", str(exc))


def _check_profiles(report: Report) -> None:
    try:
        from .auth.manager import AuthManager

        auth = AuthManager()
        profiles = auth.store.profiles()
        active = auth.store.active()
        for profile in profiles:
            marker = " [active]" if active and profile.id == active.id else ""
            endpoint = f" -> {profile.endpoint}" if profile.endpoint else ""
            source = f" (env ${profile.env_var})" if profile.env_var else ""
            report.add(_OK, f"profile {profile.provider}",
                       f"{profile.id}{marker}{source}{endpoint}")
        if not profiles:
            report.add(_WARN, "profiles",
                       "none configured - run 'daemon auth add'")
        auth.close()
    except DaemonError as exc:
        report.add(_WARN, "profiles", f"credential store issue: {exc}")


def _check_models(report: Report) -> None:
    try:
        from .auth.manager import AuthManager
        from .models import ModelRegistry

        auth = AuthManager()
        active = auth.store.active()
        if active is None:
            return
        registry = ModelRegistry(daemon_home())
        models = registry.cached(active.id)
        if models:
            caps = sum(1 for model in models
                       if model.capabilities.tool_calling is not None)
            report.add(_OK, "models",
                       f"{len(models)} cached for {active.id} "
                       f"({caps} with known capabilities)")
        else:
            report.add(_INFO, "models",
                       f"none cached for {active.id} - run "
                       f"'daemon models list' (needs network)")
        auth.close()
    except DaemonError as exc:
        report.add(_WARN, "models", str(exc))


def _check_git(report: Report) -> None:
    path = shutil.which("git")
    if not path:
        report.add(_WARN, "git", "not installed - git tools disabled")
        return
    import subprocess

    version = subprocess.run([path, "--version"], capture_output=True,
                             text=True).stdout.strip()
    report.add(_OK, "git", version)


def _check_shell(report: Report) -> None:
    if sys.platform.startswith("win"):
        for executable in ("pwsh", "powershell"):
            if shutil.which(executable):
                report.add(_OK, "shell", f"{executable} detected")
                return
        report.add(_WARN, "shell", "no PowerShell found; cmd.exe fallback")
    else:
        for shell in ("/bin/bash", "/bin/sh"):
            if Path(shell).exists():
                report.add(_OK, "shell", shell)
                return
        report.add(_FAIL, "shell", "neither bash nor sh found")


def _check_devtools(report: Report) -> None:
    tools = {"ruff": "lint+format", "black": "format", "mypy": "types",
             "prettier": "js/ts format", "pytest": "test runner",
             "node": "js runtime", "go": "go toolchain", "cargo": "rust"}
    found = []
    for tool in tools:
        if shutil.which(tool):
            found.append(tool)
    if found:
        report.add(_OK, "dev tools", ", ".join(found))
    else:
        report.add(_INFO, "dev tools",
                   "none of ruff/black/mypy/prettier/pytest found - "
                   "diagnostics and formatting stay limited")


def _check_project(report: Report, workspace: Path) -> None:
    if not workspace.exists():
        report.add(_FAIL, "workspace", f"{workspace} does not exist")
        return
    try:
        from .project import ProjectIndex

        index = ProjectIndex(workspace)
        index.build()
        report.add(_OK, "workspace",
                   f"{len(index.data.get('files') or {})} files indexed · " +
                   ", ".join(f"{name} {count}"
                             for name, count in
                             list((index.data.get("languages") or {}).items())
                             [:3]))
    except OSError as exc:
        report.add(_WARN, "workspace", f"index failed: {exc}")


def _check_mcp(report: Report, config: Config) -> None:
    servers = config.get("mcp.servers") or {}
    if not servers:
        report.add(_INFO, "mcp", "no servers configured")
        return
    for server_id, entry in servers.items():
        trusted = "trusted" if entry.get("trusted") else "UNREVIEWED"
        enabled = "enabled" if entry.get("enabled", True) else "disabled"
        report.add(_WARN if not entry.get("trusted") else _OK,
                   f"mcp {server_id}",
                   f"{entry.get('transport')} · {enabled} · {trusted}")


def _check_network(report: Report, config: Config) -> None:
    try:
        from .auth.manager import AuthManager

        auth = AuthManager()
        active = auth.store.active()
        profiles = auth.store.profiles() if active is None else [active]
        for profile in profiles:
            from .providers.catalog import provider_by_id
            from .providers.registry import custom_descriptor

            descriptor = provider_by_id(profile.provider) or custom_descriptor(
                "Custom", profile.endpoint or "")
            url = (profile.endpoint or descriptor.base_url).rstrip("/")
            if "{account_id}" in url or "{resource}" in url:
                report.add(_INFO, f"net {profile.provider}",
                           "endpoint needs account data; skipped")
                continue
            try:
                import httpx

                async def _probe() -> int:
                    async with httpx.AsyncClient(
                            timeout=httpx.Timeout(10.0)) as client:
                        response = await client.get(url)
                        return response.status_code

                status = asyncio.run(_probe())
                report.add(_OK, f"net {profile.provider}",
                           f"HTTP {status} from {url}")
            except Exception as exc:  # noqa: BLE001
                report.add(_FAIL, f"net {profile.provider}",
                           f"{url} unreachable: {exc}")
        auth.close()
    except DaemonError as exc:
        report.add(_WARN, "network", f"profile issue: {exc}")
