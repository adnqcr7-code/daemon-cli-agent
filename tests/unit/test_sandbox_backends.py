"""Tests for v0.9.0 §2.6: the pluggable sandbox backends."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import daemon.sandbox as sandbox_module
import daemon.sandbox_backends as backends
from daemon.errors import SandboxError
from daemon.sandbox_backends import (
    BwrapBackend,
    ContainerBackend,
    LandlockBackend,
    SandboxResult,
    SeatbeltBackend,
    select_backend,
)
from daemon.selftest import _Context, _leg_sandbox

# ------------------------------------------------------- container backend


class _FakeRun:
    """Records subprocess argv; answers like a working podman."""

    def __init__(self, *, probe_code: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.probe_code = probe_code

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if len(argv) > 1 and argv[1] == "stop":
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "sleep" in argv[-1]:
            raise subprocess.TimeoutExpired(argv, 30, output="partial")
        return subprocess.CompletedProcess(argv, self.probe_code, "", "")


def _patch_which(monkeypatch, present: set[str]):
    # Only the container CLIs are faked; every other lookup (notably
    # bwrap) delegates to the real which, or the bwrap fallback tests
    # would see a world with no bwrap at all.
    real_which = shutil.which
    monkeypatch.setattr(
        backends.shutil, "which",
        lambda exe: (f"/usr/bin/{exe}" if exe in present else
                     None if exe in ("podman", "docker") else
                     real_which(exe)))


def test_container_argv_is_locked_down(tmp_path, monkeypatch) -> None:
    """--network=none, read-only root, workspace rw, scrubbed env."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    fake = _FakeRun()
    monkeypatch.setattr(backends.subprocess, "run", fake)
    _patch_which(monkeypatch, {"podman"})
    backend = ContainerBackend(cli="podman", image="img:latest")
    assert backend.available()
    ok, _detail = backend.probe()
    assert ok
    assert fake.calls[0] == ["podman", "run", "--rm", "--network=none",
                             "img:latest", "/bin/true"]
    result = backend.run(
        "echo hi", cwd=workspace,
        env={"MY_TOKEN": "leak-me", "MY_NOTE": "keep-me"})
    assert result == SandboxResult(0, "", "", False)
    argv = fake.calls[-1]
    assert argv[:4] == ["podman", "run", "--rm", "--name"]
    assert "--network=none" in argv
    assert "--read-only" in argv
    assert "-v" in argv
    assert argv[argv.index("-v") + 1] == f"{workspace}:{workspace}:rw"
    assert "img:latest" in argv
    assert argv[-3:] == ["/bin/sh", "-c", "echo hi"]
    # The secret is stripped; the harmless var survives.
    env_flags = [argv[i + 1] for i, item in enumerate(argv)
                 if item == "--env"]
    assert not any("leak-me" in flag or "MY_TOKEN" in flag
                   for flag in env_flags)
    assert "MY_NOTE=keep-me" in env_flags
    assert f"HOME={workspace}" in env_flags
    # network_policy="allow" drops the --network flag.
    backend.run("echo hi", cwd=workspace, network_policy="allow")
    assert "--network=none" not in fake.calls[-1]


def test_container_timeout_stops_the_container(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    fake = _FakeRun()
    monkeypatch.setattr(backends.subprocess, "run", fake)
    _patch_which(monkeypatch, {"podman"})
    backend = ContainerBackend(cli="podman", image="img:latest")
    result = backend.run("sleep 999", cwd=workspace, timeout=30)
    assert result.timed_out
    assert result.exit_code == -1
    assert result.stdout == "partial"
    stops = [call for call in fake.calls if call[1] == "stop"]
    assert len(stops) == 1


def test_container_unavailable_without_cli(monkeypatch) -> None:
    _patch_which(monkeypatch, set())
    backend = ContainerBackend()
    assert not backend.available()
    ok, detail = backend.probe()
    assert not ok and "PATH" in detail
    with pytest.raises(SandboxError):
        backend.run("echo hi", cwd=Path("/tmp"))


def test_container_probe_failure_is_honest(monkeypatch) -> None:
    fake = _FakeRun(probe_code=125)
    monkeypatch.setattr(backends.subprocess, "run", fake)
    _patch_which(monkeypatch, {"podman"})
    backend = ContainerBackend(cli="podman", image="missing:latest")
    ok, detail = backend.probe()
    assert not ok
    assert "missing:latest" in detail or "125" in detail


# ----------------------------------------------------------- bwrap backend


def test_bwrap_backend_live(tmp_path) -> None:
    """The real bwrap backend on this host: denied read, secret env
    absent, trivial command works. (Filesystem-only here: the kernel
    rejects --unshare-net, so network_isolated is False.)"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    backend = BwrapBackend()
    assert backend.available()
    ok, _detail = backend.probe()
    assert ok
    assert backend.network_isolated is False
    secret = tmp_path / "outside-secret.txt"
    secret.write_text("bwrap-secret-marker", encoding="utf-8")
    result = backend.run(f"cat {secret}", cwd=workspace, timeout=30)
    assert "bwrap-secret-marker" not in result.stdout + result.stderr
    assert result.exit_code != 0
    # A secret env var is absent inside, via the host env and via
    # the run's env argument.
    os.environ["DAEMON_TEST_API_KEY"] = "bwrap-secret-marker"
    try:
        result = backend.run("env", cwd=workspace, timeout=30)
    finally:
        os.environ.pop("DAEMON_TEST_API_KEY", None)
    assert "bwrap-secret-marker" not in result.stdout
    result = backend.run(
        "env", cwd=workspace, timeout=30,
        env={"DAEMON_TEST_API_KEY": "bwrap-secret-marker"})
    assert "bwrap-secret-marker" not in result.stdout
    result = backend.run("echo sandbox-ok", cwd=workspace, timeout=30)
    assert result.exit_code == 0
    assert "sandbox-ok" in result.stdout


def test_bwrap_backend_refuses_outside_hardlink(tmp_path) -> None:
    """Review round 1, R1-6: a hardlink planted in the workspace to
    an outside secret makes the backend refuse the run outright —
    the secret is never returned. A clean workspace still runs."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    backend = BwrapBackend()
    assert backend.available()
    secret = tmp_path / "outside-secret.txt"
    secret.write_text("bwrap-secret-marker", encoding="utf-8")
    os.link(secret, workspace / "hard-out.txt")
    with pytest.raises(SandboxError, match="hardlinks to files outside"):
        backend.run("cat hard-out.txt", cwd=workspace, timeout=30)
    (workspace / "hard-out.txt").unlink()
    result = backend.run("echo sandbox-ok", cwd=workspace, timeout=30)
    assert result.exit_code == 0
    assert "sandbox-ok" in result.stdout


# ------------------------------------------------------------------ stubs


def test_stubs_are_unavailable_and_refuse() -> None:
    for backend in (SeatbeltBackend(), LandlockBackend()):
        assert not backend.available()
        ok, detail = backend.probe()
        assert not ok and "stub" in detail
        with pytest.raises(SandboxError):
            backend.run("echo hi", cwd=Path("/tmp"))


# --------------------------------------------------------------- selection


def test_select_backend_prefers_container(monkeypatch) -> None:
    monkeypatch.setattr(backends.subprocess, "run", _FakeRun())
    _patch_which(monkeypatch, {"podman"})
    backend = select_backend()
    assert isinstance(backend, ContainerBackend)
    assert backend.cli == "podman"


def test_select_backend_falls_back_to_bwrap(monkeypatch) -> None:
    _patch_which(monkeypatch, set())
    backend = select_backend()
    assert isinstance(backend, BwrapBackend)


def test_select_backend_none_when_nothing_works(monkeypatch) -> None:
    _patch_which(monkeypatch, set())
    monkeypatch.setattr(sandbox_module, "_probe_result", (False, False))
    assert select_backend() is None


# ------------------------------------------------------ the selftest leg


class _FakeIsolatedBackend:
    """A network-isolated backend with scripted answers."""

    name = "fake"
    network_isolated = True

    def __init__(self, *, egress_code: int = 1, leak: bool = False,
                 no_tool: bool = False) -> None:
        self.egress_code = egress_code
        self.leak = leak
        self.no_tool = no_tool

    def probe(self):
        return True, "fake isolated"

    def run(self, command, *, cwd, env=None, timeout=30,
            network_policy="deny"):
        if command.startswith("cat "):
            return SandboxResult(1, "", "cat: no such file", False)
        if command == "env":
            if self.leak:
                return SandboxResult(
                    0, "DAEMON_SELFTEST_API_KEY=selftest-secret-marker\n",
                    "", False)
            return SandboxResult(0, "PATH=/usr/bin\nHOME=/tmp\n", "", False)
        if command == "echo sandbox-ok":
            return SandboxResult(0, "sandbox-ok\n", "", False)
        if "example.com" in command:
            if self.no_tool:
                return SandboxResult(0, "NO_EGRESS_TOOL\n", "", False)
            return SandboxResult(self.egress_code, "", "", False)
        raise AssertionError(f"unexpected command: {command}")


def _leg_with_fake(monkeypatch, fake):
    monkeypatch.setattr(backends, "select_backend", lambda *a, **k: fake)
    ctx = _Context(python=sys.executable)
    try:
        return _leg_sandbox(ctx)
    finally:
        ctx.cleanup()


def test_selftest_leg_passes_with_isolated_backend(monkeypatch) -> None:
    result = _leg_with_fake(monkeypatch, _FakeIsolatedBackend())
    assert result.status == "PASS"
    assert "fake" in result.detail
    assert "network isolated" in result.detail


def test_selftest_leg_fails_when_egress_succeeds(monkeypatch) -> None:
    result = _leg_with_fake(
        monkeypatch, _FakeIsolatedBackend(egress_code=0))
    assert result.status == "FAIL"
    assert "egress SUCCEEDED" in result.detail


def test_selftest_leg_fails_when_no_egress_tool(monkeypatch) -> None:
    result = _leg_with_fake(
        monkeypatch, _FakeIsolatedBackend(no_tool=True))
    assert result.status == "FAIL"
    assert "no egress probe tool" in result.detail


def test_selftest_leg_fails_when_secret_env_leaks(monkeypatch) -> None:
    result = _leg_with_fake(monkeypatch, _FakeIsolatedBackend(leak=True))
    assert result.status == "FAIL"
    assert "leaked" in result.detail


def test_selftest_leg_live_on_this_host() -> None:
    """The real leg: bwrap on this host, filesystem-only, so the
    egress assertion is skipped and the leg passes on the other
    three."""
    ctx = _Context(python=sys.executable)
    try:
        result = _leg_sandbox(ctx)
    finally:
        ctx.cleanup()
    assert result.status == "PASS", result.detail
    assert "bwrap" in result.detail
    assert "filesystem isolation only" in result.detail


def test_selftest_leg_skips_with_no_backend(monkeypatch) -> None:
    monkeypatch.setattr(backends, "select_backend",
                        lambda *a, **k: None)
    ctx = _Context(python=sys.executable)
    try:
        result = _leg_sandbox(ctx)
    finally:
        ctx.cleanup()
    assert result.status == "SKIP"
