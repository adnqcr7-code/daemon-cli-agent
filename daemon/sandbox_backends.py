"""Pluggable sandbox backends (v0.9.0 §2.6).

The interface every backend implements:

    run(command, cwd, env, timeout, network_policy)
        -> SandboxResult(exit_code, stdout, stderr, timed_out)
    available() -> bool     (cheap: is the mechanism present at all)
    probe() -> (ok, detail) (actually runs the mechanism; cached)

Selection order: container when available -> bwrap -> none.

Backends:

- ContainerBackend: podman or docker CLI, whichever is present.
  No network by default (``--network=none``), read-only root, the
  workspace mounted read-write, a minimal scrubbed environment
  (the v0.5.0 segment-based secret stripper), no host secrets.
- BwrapBackend: the v0.5.0 bubblewrap implementation, re-homed
  behind the interface - unchanged behaviour, same probe.
- SeatbeltBackend / LandlockBackend: stubs per spec (not available
  on this platform); ``available()`` is False and ``run()`` refuses.

No backend available: the selftest sandbox leg is SKIP, and shell
commands proceed under the permission engine with dangerous
commands still denied (fail-closed) - that fallback lives in the
callers (the shell tool's classifier), not here.

Probing is honest, everywhere: ``available()`` never claims a
backend works; ``probe()`` actually runs the mechanism once and
caches the result. On this dev host the container backend is
unavailable - podman is not installed system-wide, and the sandbox
kernel rejects the namespaces a container runtime needs (crun
fails on SIOCSIFFLAGS, runc on setns; even ``--network=host``
wedges) - so selection falls through to bwrap, which is
filesystem-only here because the kernel also rejects
``--unshare-net``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from .errors import SandboxError
from .log import get_logger
from .security import strip_secret_env

_logger = get_logger("sandbox_backends")

_PROBE_TIMEOUT_S = 30
_DEFAULT_IMAGE = "docker.io/library/alpine:latest"
# The container's base environment: minimal on purpose. The host env
# is not carried in (a host LD_PRELOAD / PYTHONPATH would point at
# paths that do not exist inside the image); the caller supplies
# anything else through run()'s env argument.
_CONTAINER_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool


class SandboxBackend(ABC):
    """One sandbox mechanism behind the §2.6 interface."""

    name: str = "none"
    # Whether the default (deny) network policy actually isolates
    # the network. The selftest egress assertion is gated on this:
    # a backend that cannot isolate must not claim it did.
    network_isolated: bool = False

    @abstractmethod
    def available(self) -> bool:
        """Cheap presence check (CLI on PATH, platform right)."""

    @abstractmethod
    def probe(self) -> tuple[bool, str]:
        """Actually run the mechanism once; (ok, human detail)."""

    @abstractmethod
    def run(self, command: str, *, cwd: Path,
            env: dict[str, str] | None = None,
            timeout: float = 120.0,
            network_policy: str = "deny") -> SandboxResult:
        """Run *command* (a shell string) inside the backend.

        cwd is the workspace root: it is the working directory and
        the one host tree mounted read-write. env is extra
        environment for the command, scrubbed by the same segment
        stripper as the base environment - a sandbox never receives
        a secret, however it is passed in. network_policy "deny"
        (the default) means no network; "allow" means the backend's
        default networking. A timeout kills the command (and the
        container, for the container backend).
        """


def _check_workspace_hardlinks(workspace: Path) -> None:
    """Refuse a sandbox run when the workspace holds a hardlink to
    outside content (review round 1, R1-6).

    A bind mount carries the inode, so the container backend has
    the same hole as bwrap: a hardlink planted in the workspace
    names the same inode as a file outside it, and reading it inside
    the box returns the outside file's contents. The check is the
    shared one in sandbox.py (also used by plan_for, the shell
    tool's path); it raises before anything starts.
    """
    from .sandbox import _hardlink_refusal, find_outside_hardlinks

    offenders = find_outside_hardlinks(workspace)
    if offenders:
        raise SandboxError(_hardlink_refusal(offenders))


def _detect_cli() -> str | None:
    for cli in ("podman", "docker"):
        if shutil.which(cli) is not None:
            return cli
    return None


class ContainerBackend(SandboxBackend):
    """Docker or Podman CLI (spec default)."""

    name = "container"
    network_isolated = True

    def __init__(self, cli: str | None = None,
                 image: str = _DEFAULT_IMAGE) -> None:
        self.cli = cli or _detect_cli()
        self.image = image
        self._probe: tuple[bool, str] | None = None

    def available(self) -> bool:
        return self.cli is not None and shutil.which(self.cli) is not None

    def probe(self) -> tuple[bool, str]:
        if self._probe is not None:
            return self._probe
        if not self.available():
            self._probe = (False, "no container CLI (podman/docker) "
                                  "on PATH")
            return self._probe
        try:
            completed = subprocess.run(
                [self.cli, "run", "--rm", "--network=none",
                 self.image, "/bin/true"],
                capture_output=True, text=True,
                timeout=_PROBE_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._probe = (False, f"{self.cli} run failed: {exc}")
            return self._probe
        if completed.returncode == 0:
            self._probe = (True, f"{self.cli} runs {self.image}")
            return self._probe
        detail = (completed.stderr or completed.stdout or "").strip()
        last = detail.splitlines()[-1] if detail else \
            f"exit {completed.returncode}"
        self._probe = (False, f"{self.cli} run failed: {last}")
        return self._probe

    def run(self, command: str, *, cwd: Path,
            env: dict[str, str] | None = None,
            timeout: float = 120.0,
            network_policy: str = "deny") -> SandboxResult:
        if not self.available():
            raise SandboxError(
                f"container backend ({self.cli or 'podman/docker'}) "
                f"is not available on this host")
        assert self.cli is not None
        workspace = Path(cwd).resolve()
        _check_workspace_hardlinks(workspace)
        container = f"daemon-sandbox-{uuid.uuid4().hex[:12]}"
        argv = [self.cli, "run", "--rm", "--name", container]
        if network_policy == "deny":
            argv += ["--network=none"]
        argv += ["--read-only", "--tmpfs", "/tmp",
                 "-v", f"{workspace}:{workspace}:rw",
                 "-w", str(workspace)]
        # The base env is minimal and secret-free by construction;
        # the caller's env goes through the same segment stripper -
        # a sandbox never receives a secret, however it is passed in.
        base_env = {"PATH": _CONTAINER_PATH,
                    "HOME": str(workspace),
                    "TERM": os.environ.get("TERM", "xterm")}
        if env:
            base_env.update(strip_secret_env(dict(env)))
        for key, value in sorted(base_env.items()):
            argv += ["--env", f"{key}={value}"]
        argv += [self.image, "/bin/sh", "-c", command]
        try:
            completed = subprocess.run(
                argv, capture_output=True, text=True,
                timeout=timeout, errors="replace")
        except subprocess.TimeoutExpired as exc:
            self._stop(container)
            stdout = exc.stdout if isinstance(exc.stdout, str) \
                else (exc.stdout or b"").decode("utf-8", "replace")
            return SandboxResult(-1, stdout, "", True)
        return SandboxResult(completed.returncode, completed.stdout,
                             completed.stderr, False)

    def _stop(self, container: str) -> None:
        """Best-effort stop after a timeout (the client was killed)."""
        assert self.cli is not None
        try:
            subprocess.run([self.cli, "stop", "-t", "0", container],
                           capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass


class BwrapBackend(SandboxBackend):
    """The v0.5.0 bubblewrap implementation, re-homed (§2.6)."""

    name = "bwrap"

    def __init__(self) -> None:
        self._probe: tuple[bool, str] | None = None

    @property
    def network_isolated(self) -> bool:  # type: ignore[override]
        from .sandbox import _probe

        return _probe()[1]

    def available(self) -> bool:
        import sys

        from .sandbox import _bwrap_path

        return (sys.platform.startswith("linux")
                and _bwrap_path() is not None)

    def probe(self) -> tuple[bool, str]:
        if self._probe is not None:
            return self._probe
        if not self.available():
            self._probe = (False, "bwrap not on PATH "
                                  "(bubblewrap is Linux-only)")
            return self._probe
        from .sandbox import _probe

        fs_ok, net_ok = _probe()
        if not fs_ok:
            self._probe = (False, "bwrap is installed but cannot "
                                  "create a sandbox on this host "
                                  "(probe failed)")
        elif not net_ok:
            self._probe = (True, "filesystem isolation only "
                                 "(network namespace unavailable on "
                                 "this host)")
        else:
            self._probe = (True, "filesystem + network namespace "
                                 "isolation")
        return self._probe

    def run(self, command: str, *, cwd: Path,
            env: dict[str, str] | None = None,
            timeout: float = 120.0,
            network_policy: str = "deny") -> SandboxResult:
        from .sandbox import SandboxPlan
        from .tools.shell import shell_argv

        workspace = Path(cwd).resolve()
        _check_workspace_hardlinks(workspace)
        plan = SandboxPlan(
            workspace=workspace,
            network_isolated=(network_policy == "deny"
                              and self.network_isolated))
        argv = plan.wrap(shell_argv() + [command], workspace)
        merged = strip_secret_env(None)
        if env:
            merged.update(strip_secret_env(dict(env)))
        try:
            completed = subprocess.run(
                argv, cwd=str(workspace), env=merged,
                capture_output=True, text=True,
                timeout=timeout, errors="replace")
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) \
                else (exc.stdout or b"").decode("utf-8", "replace")
            return SandboxResult(-1, stdout, "", True)
        return SandboxResult(completed.returncode, completed.stdout,
                             completed.stderr, False)


class _StubBackend(SandboxBackend):
    """A backend named by the spec but not implemented here."""

    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self._reason = reason

    def available(self) -> bool:
        return False

    def probe(self) -> tuple[bool, str]:
        return False, f"{self.name} backend is a stub ({self._reason})"

    def run(self, command: str, *, cwd: Path,
            env: dict[str, str] | None = None,
            timeout: float = 120.0,
            network_policy: str = "deny") -> SandboxResult:
        raise SandboxError(
            f"{self.name} sandbox backend is not implemented: "
            f"{self._reason}")


class SeatbeltBackend(_StubBackend):
    """macOS Seatbelt - stub per spec (not implemented)."""

    def __init__(self) -> None:
        super().__init__("seatbelt", "macOS-only, not implemented")


class LandlockBackend(_StubBackend):
    """Linux Landlock - stub per spec (not implemented)."""

    def __init__(self) -> None:
        super().__init__("landlock", "not implemented")


def select_backend(config=None) -> SandboxBackend | None:
    """The §2.6 selection: container when available -> bwrap -> none.

    A backend is selected only when its probe actually runs - a CLI
    on PATH whose containers fail to start is not selected.
    """
    image = _DEFAULT_IMAGE
    if config is not None:
        image = config.get("sandbox.container_image", _DEFAULT_IMAGE)
    for cli in ("podman", "docker"):
        backend = ContainerBackend(cli=cli, image=image)
        if backend.available() and backend.probe()[0]:
            return backend
    backend = BwrapBackend()
    if backend.available() and backend.probe()[0]:
        return backend
    return None
