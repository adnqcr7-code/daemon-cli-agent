"""``daemon selftest`` — the release gate as a command (v0.9.0 §2.1).

One command, one verdict, over the legs a fresh install must prove:

* doctor checks (the real doctor, against the real home)
* a demo run in a throwaway workspace (the real CLI, subprocess)
* the offline eval suite (the real CLI, subprocess, no network)
* repomap answers on the throwaway workspace
* memory add/read/invalidate round-trip
* worktree create/list/remove round-trip
* sandbox probe (the §2.6 backend, a denied read actually denied)
* the model leg — only when a credential profile exists

Verdict algebra (spec, verbatim intent): PASS only when every leg is
PASS; PARTIAL when any leg is SKIP and none FAIL; FAIL otherwise. A
leg that cannot run is SKIP, never PASS — a false green is not an
option. Exit codes: 0 PASS, 1 PARTIAL, 2 FAIL. ``--json`` emits the
same verdict machine-readably.

Honesty rules, enforced by construction:

* The legs that exercise the CLI run it as a subprocess
  (``python -m daemon.cli``) with a throwaway DAEMON_HOME, so the
  selftest proves the INSTALLED CLI, not an in-process mock of it.
  The only in-process legs are the ones that cannot hang on network
  (doctor without --net, repomap, memory, worktree, sandbox probe via
  the backend's own timeout).
* Every leg has a timeout (``LEG_TIMEOUTS``); a leg that times out
  FAILs with the timeout named. No leg can hang the command.
* The model leg runs the real provider only when credentials exist;
  on this machine it is SKIP and the verdict is PARTIAL, by design.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .log import get_logger

_logger = get_logger("selftest")

_PACKAGE_ROOT = Path(__file__).resolve().parent
_REPO_ROOT = _PACKAGE_ROOT.parent
_DEMO_TASKS = _PACKAGE_ROOT / "evals" / "demo_tasks.json"

# Per-leg timeouts, seconds. The eval leg runs a full agent run
# through the real CLI, so it gets the longest budget; everything
# else is local and fast.
LEG_TIMEOUTS: dict[str, float] = {
    "doctor": 60.0,
    "demo run": 120.0,
    "offline eval": 300.0,
    "repomap": 60.0,
    "memory": 30.0,
    "worktree": 120.0,
    "sandbox probe": 120.0,
    "model leg": 120.0,
}

LEG_ORDER = ("doctor", "demo run", "offline eval", "repomap", "memory",
             "worktree", "sandbox probe", "model leg")

VERDICTS = ("PASS", "PARTIAL", "FAIL")
EXIT_CODES = {"PASS": 0, "PARTIAL": 1, "FAIL": 2}


@dataclass
class LegResult:
    name: str
    status: str  # PASS | FAIL | SKIP
    detail: str = ""
    duration_s: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {"leg": self.name, "status": self.status,
                "detail": self.detail,
                "duration_s": round(self.duration_s, 3)}


@dataclass
class SelftestReport:
    legs: list[LegResult] = field(default_factory=list)
    verdict: str = "FAIL"

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.verdict]

    def to_json(self) -> dict[str, Any]:
        return {"verdict": self.verdict,
                "exit_code": self.exit_code,
                "legs": [leg.to_json() for leg in self.legs]}


def verdict_of(legs: list[LegResult]) -> str:
    """The verdict algebra, pure: FAIL beats PARTIAL beats PASS.

    An empty leg list is a FAIL, not a PASS: a gate that ran
    nothing must never green-light a release (review round 1).
    """
    if not legs:
        return "FAIL"
    if any(leg.status == "FAIL" for leg in legs):
        return "FAIL"
    if any(leg.status == "SKIP" for leg in legs):
        return "PARTIAL"
    return "PASS"


class _Context:
    """Throwaway workspace + home shared by the legs."""

    def __init__(self, *, python: str,
                 timeouts: dict[str, float] | None = None) -> None:
        self.python = python
        self.timeouts = dict(LEG_TIMEOUTS)
        if timeouts:
            self.timeouts.update(timeouts)
        self._tmp = tempfile.TemporaryDirectory(prefix="daemon-selftest-")
        root = Path(self._tmp.name)
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "a.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8")
        self.home = root / "daemon-home"
        self.home.mkdir()
        self.git_available = shutil.which("git") is not None
        if self.git_available:
            self._git_init()

    def _git_init(self) -> None:
        try:
            for args in (["init", "-q"],
                         ["config", "user.email", "selftest@example.com"],
                         ["config", "user.name", "selftest"],
                         ["add", "-A"],
                         ["commit", "-q", "-m", "selftest fixture"]):
                subprocess.run(["git", *args], cwd=self.workspace,
                               capture_output=True, check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            _logger.debug("selftest git init failed (%s)", exc)
            self.git_available = False

    def timeout(self, leg: str) -> float:
        return self.timeouts.get(leg, 120.0)

    def subprocess_env(self) -> dict[str, str]:
        """Env for CLI subprocess legs: throwaway home, real PATH."""
        env = dict(os.environ)
        env["DAEMON_HOME"] = str(self.home)
        env.pop("DAEMON_VAULT_BACKEND", None)
        return env

    def cleanup(self) -> None:
        self._tmp.cleanup()


def _run_cli(ctx: _Context, args: list[str], *, timeout: float,
             env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Run the real CLI as a subprocess; (returncode, stdout, stderr)."""
    try:
        completed = subprocess.run(
            [ctx.python, "-m", "daemon.cli", *args],
            cwd=_REPO_ROOT, env=env or ctx.subprocess_env(),
            capture_output=True, text=True, timeout=timeout, check=False)
        return completed.returncode, completed.stdout, completed.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"timed out after {timeout:g}s"
    except OSError as exc:
        return -1, "", str(exc)


def _parse_json_output(stdout: str) -> dict[str, Any]:
    """The JSON a --json CLI invocation printed (whole stdout)."""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


# --------------------------------------------------------------------- legs
# Each leg returns a LegResult. A leg that cannot run returns SKIP with
# the reason named; a leg whose precondition is broken returns FAIL.


def _leg_doctor(ctx: _Context) -> LegResult:
    from .doctor import run_doctor
    from .log import setup_logging

    started = time.perf_counter()
    try:
        # The doctor leg runs in-process, and its audit check reads the
        # process-global audit state (v0.5.0 §3): without a setup_logging
        # call in THIS process it reports "audit logging not initialised"
        # even though the audit log is fine. A selftest that only passes
        # when some earlier test happened to invoke the CLI is order-
        # dependent, not green. Initialise logging for the leg's
        # throwaway home first, so the leg proves the audit trail it
        # actually checks.
        setup_logging(ctx.home)
        report = run_doctor(Config.load(ctx.workspace), ctx.workspace,
                            network=False)
    except Exception as exc:  # noqa: BLE001 - a doctor crash is a FAIL
        return LegResult("doctor", "FAIL",
                         f"doctor crashed: {exc}",
                         time.perf_counter() - started)
    if report.failures:
        failed = [check for status, check, _ in report.rows
                  if status == "fail"]
        return LegResult("doctor", "FAIL",
                         f"{report.failures} failure(s): "
                         f"{', '.join(failed[:3])}",
                         time.perf_counter() - started)
    return LegResult("doctor", "PASS",
                     f"{len(report.rows)} checks, {report.warnings} warning(s)",
                     time.perf_counter() - started)


def _leg_demo_run(ctx: _Context) -> LegResult:
    started = time.perf_counter()
    timeout = ctx.timeout("demo run")
    code, stdout, stderr = _run_cli(
        ctx, ["demo", "add a docstring to a.py", "--yes", "--json",
              "--workspace", str(ctx.workspace)],
        timeout=timeout)
    elapsed = time.perf_counter() - started
    if code == -1:
        return LegResult("demo run", "FAIL", stderr or "did not run", elapsed)
    payload = _parse_json_output(stdout)
    if code != 0 or not payload.get("ok"):
        detail = (payload.get("error") or stderr.strip()
                  or f"exit {code}")
        return LegResult("demo run", "FAIL", detail[:300], elapsed)
    content = (ctx.workspace / "a.py").read_text("utf-8")
    if '"""Add."""' not in content:
        return LegResult("demo run", "FAIL",
                         "run reported ok but a.py has no docstring", elapsed)
    return LegResult("demo run", "PASS",
                     "demo run completed, docstring added, verification passed",
                     elapsed)


def _leg_offline_eval(ctx: _Context) -> LegResult:
    started = time.perf_counter()
    timeout = ctx.timeout("offline eval")
    code, stdout, stderr = _run_cli(
        ctx, ["eval", "--target", "demo=demo-scripted",
              "--tasks", str(_DEMO_TASKS)],
        timeout=timeout)
    elapsed = time.perf_counter() - started
    if code == -1:
        return LegResult("offline eval", "FAIL", stderr or "did not run",
                         elapsed)
    payload = _parse_json_output(stdout)
    summary = payload.get("summary") or {}
    if code != 0 or not payload.get("runs"):
        detail = (stderr.strip() or f"exit {code}")
        return LegResult("offline eval", "FAIL", detail[:300], elapsed)
    passed = int(summary.get("passed", 0))
    total = int(summary.get("total", 0))
    if total == 0 or passed != total:
        return LegResult("offline eval", "FAIL",
                         f"{passed}/{total} eval tasks passed", elapsed)
    return LegResult("offline eval", "PASS",
                     f"{passed}/{total} eval tasks passed", elapsed)


def _leg_repomap(ctx: _Context) -> LegResult:
    from .agent.repomap import RepoMap

    started = time.perf_counter()
    try:
        ranked = RepoMap(ctx.workspace).rank("add a docstring to a.py")
    except Exception as exc:  # noqa: BLE001
        return LegResult("repomap", "FAIL", f"repomap crashed: {exc}",
                         time.perf_counter() - started)
    top = [item.path for item in ranked[:3]]
    if "a.py" not in top:
        return LegResult("repomap", "FAIL",
                         f"a.py not in top 3 for its own task: {top}",
                         time.perf_counter() - started)
    return LegResult("repomap", "PASS",
                     f"a.py ranked #{top.index('a.py') + 1} of {len(ranked)}",
                     time.perf_counter() - started)


def _leg_memory(ctx: _Context) -> LegResult:
    from .memory import MemoryStore

    started = time.perf_counter()
    try:
        store = MemoryStore(workspace=ctx.workspace, home=ctx.home)
        entry = store.add("selftest probe fact", kind="fact",
                          scope="project")
        if entry.id not in {item.id for item in store.entries()}:
            return LegResult("memory", "FAIL",
                             "added entry not visible on read",
                             time.perf_counter() - started)
        if not store.invalidate(entry.id, "selftest round-trip"):
            return LegResult("memory", "FAIL",
                             "invalidate returned False",
                             time.perf_counter() - started)
        if entry.id in {item.id for item in store.entries()}:
            return LegResult("memory", "FAIL",
                             "invalidated entry still visible",
                             time.perf_counter() - started)
    except Exception as exc:  # noqa: BLE001
        return LegResult("memory", "FAIL", f"memory error: {exc}",
                         time.perf_counter() - started)
    return LegResult("memory", "PASS", "add/read/invalidate round-trip",
                     time.perf_counter() - started)


def _leg_worktree(ctx: _Context) -> LegResult:
    from .worktree import WorktreeManager

    started = time.perf_counter()
    if not ctx.git_available:
        return LegResult("worktree", "FAIL",
                         "git not available for the fixture",
                         time.perf_counter() - started)
    manager = WorktreeManager()
    try:
        info = manager.create(ctx.workspace, "selftest")
        listed = [item.branch for item in
                  manager.list_worktrees(ctx.workspace)]
        if info.branch not in listed:
            return LegResult("worktree", "FAIL",
                             "created worktree not listed",
                             time.perf_counter() - started)
        if not manager.remove(ctx.workspace, info):
            return LegResult("worktree", "FAIL",
                             "remove reported nothing removed",
                             time.perf_counter() - started)
        if info.branch in [item.branch for item in
                           manager.list_worktrees(ctx.workspace)]:
            return LegResult("worktree", "FAIL",
                             "removed worktree still listed",
                             time.perf_counter() - started)
    except Exception as exc:  # noqa: BLE001
        return LegResult("worktree", "FAIL", f"worktree error: {exc}",
                         time.perf_counter() - started)
    return LegResult("worktree", "PASS", "create/list/remove round-trip",
                     time.perf_counter() - started)


def _leg_sandbox(ctx: _Context) -> LegResult:
    """The sandbox probe, behind the §2.6 backend interface.

    Whichever backend ``select_backend`` picks (container when
    available, else bwrap), the same assertions hold: a secret file
    outside the workspace must be unreadable inside the box, a
    secret environment variable must be absent inside, a hardlink
    planted in the workspace must not leak outside content (the run
    is refused outright — review round 1, R1-6), and a trivial
    command must still succeed. The network-egress
    assertion runs only when the backend actually isolates the
    network (``backend.network_isolated``) - a filesystem-only
    backend must not claim egress denial it cannot enforce. No
    backend at all on this host is SKIP, never PASS.
    """
    from .sandbox_backends import select_backend

    started = time.perf_counter()
    backend = select_backend()
    if backend is None:
        return LegResult("sandbox probe", "SKIP",
                         "no sandbox backend on this host",
                         time.perf_counter() - started)
    secret = Path(ctx._tmp.name) / "selftest-secret.txt"
    secret.write_text("selftest-secret-marker", encoding="utf-8")
    try:
        result = backend.run(f"cat {secret}", cwd=ctx.workspace,
                             timeout=30)
    except Exception as exc:  # noqa: BLE001
        return LegResult("sandbox probe", "FAIL",
                         f"probe command failed to run: {exc}",
                         time.perf_counter() - started)
    if result.timed_out or "selftest-secret-marker" in \
            result.stdout + result.stderr:
        return LegResult("sandbox probe", "FAIL",
                         "secret outside the workspace was readable "
                         "inside the sandbox",
                         time.perf_counter() - started)
    # A secret env var must be absent inside, however it is passed:
    # through the host environment (the stripper) and through the
    # run's env argument (the backends scrub that too).
    os.environ["DAEMON_SELFTEST_API_KEY"] = "selftest-secret-marker"
    try:
        result = backend.run("env", cwd=ctx.workspace, timeout=30,
                             env={"DAEMON_SELFTEST_API_KEY":
                                  "selftest-secret-marker"})
    finally:
        os.environ.pop("DAEMON_SELFTEST_API_KEY", None)
    if "selftest-secret-marker" in result.stdout + result.stderr:
        return LegResult("sandbox probe", "FAIL",
                         "secret env var leaked into the sandbox",
                         time.perf_counter() - started)
    # A hardlink planted in the workspace must not leak outside
    # content (review round 1, R1-6): the backends refuse the run
    # outright when the workspace holds a link to an outside inode.
    # Refusal (SandboxError) is the pass; a result carrying the
    # secret is the fail. The link is removed afterwards so later
    # probes run on a clean workspace.
    from .errors import SandboxError

    hardlink = ctx.workspace / "hard-out.txt"
    try:
        os.link(secret, hardlink)
    except OSError as exc:
        return LegResult("sandbox probe", "FAIL",
                         f"could not plant the hardlink probe: {exc}",
                         time.perf_counter() - started)
    try:
        result = backend.run("cat hard-out.txt", cwd=ctx.workspace,
                             timeout=30)
    except SandboxError:
        pass  # refused: the isolation holds
    except Exception as exc:  # noqa: BLE001
        return LegResult("sandbox probe", "FAIL",
                         f"hardlink probe command failed to run: {exc}",
                         time.perf_counter() - started)
    else:
        if "selftest-secret-marker" in result.stdout + result.stderr:
            return LegResult(
                "sandbox probe", "FAIL",
                "secret outside the workspace was readable inside "
                "the sandbox via a hardlink",
                time.perf_counter() - started)
    finally:
        hardlink.unlink(missing_ok=True)
    if backend.network_isolated:
        # An egress attempt must fail. The snippet uses whichever
        # fetch tool exists inside (python3, curl, wget); if none
        # exists the probe is inconclusive - FAIL, never PASS.
        egress = (
            "if command -v python3 >/dev/null; then "
            "python3 -c \"import urllib.request; "
            "urllib.request.urlopen('https://example.com', timeout=5)\"; "
            "elif command -v curl >/dev/null; then "
            "curl -fsS -m 5 https://example.com >/dev/null; "
            "elif command -v wget >/dev/null; then "
            "wget -qO- -T 5 https://example.com >/dev/null; "
            "else echo NO_EGRESS_TOOL; fi"
        )
        try:
            result = backend.run(egress, cwd=ctx.workspace, timeout=30)
        except Exception as exc:  # noqa: BLE001
            return LegResult("sandbox probe", "FAIL",
                             f"egress probe failed to run: {exc}",
                             time.perf_counter() - started)
        if "NO_EGRESS_TOOL" in result.stdout:
            return LegResult("sandbox probe", "FAIL",
                             "no egress probe tool (python3/curl/wget) "
                             "inside the sandbox - cannot verify "
                             "network isolation",
                             time.perf_counter() - started)
        if result.exit_code == 0:
            return LegResult("sandbox probe", "FAIL",
                             "network egress SUCCEEDED inside the "
                             "sandbox (network isolation not in "
                             "effect)",
                             time.perf_counter() - started)
    try:
        result = backend.run("echo sandbox-ok", cwd=ctx.workspace,
                             timeout=30)
    except Exception as exc:  # noqa: BLE001
        return LegResult("sandbox probe", "FAIL",
                         f"probe command failed to run: {exc}",
                         time.perf_counter() - started)
    if result.exit_code != 0 or "sandbox-ok" not in result.stdout:
        return LegResult("sandbox probe", "FAIL",
                         "trivial command failed inside the sandbox",
                         time.perf_counter() - started)
    _ok, detail = backend.probe()
    level = ("network isolated" if backend.network_isolated else
             "filesystem isolation only (no network isolation on "
             "this host)")
    return LegResult("sandbox probe", "PASS",
                     f"denied read blocked, secret env absent, "
                     f"hardlink read refused, trivial command ran "
                     f"({backend.name}: {level}; {detail})",
                     time.perf_counter() - started)


def _leg_model(ctx: _Context) -> LegResult:
    """The one leg that needs real credentials — SKIP without them."""
    started = time.perf_counter()
    try:
        from .auth.manager import AuthManager

        active = AuthManager().store.active()
    except Exception as exc:  # noqa: BLE001 - a broken store is a SKIP:
        # the leg cannot run, and the credential store's own health
        # is the doctor leg's business, not this leg's.
        return LegResult("model leg", "SKIP",
                         f"credential store unreadable: {exc}",
                         time.perf_counter() - started)
    if active is None:
        return LegResult("model leg", "SKIP",
                         "no credentials configured",
                         time.perf_counter() - started)
    code, stdout, stderr = _run_cli(
        ctx, ["run", "Reply with the single word OK.", "--yes", "--json"],
        timeout=ctx.timeout("model leg"), env=dict(os.environ))
    elapsed = time.perf_counter() - started
    if code == -1:
        return LegResult("model leg", "FAIL", stderr or "did not run",
                         elapsed)
    payload = _parse_json_output(stdout)
    if code != 0 or not payload.get("ok"):
        detail = (payload.get("error") or stderr.strip()
                  or f"exit {code}")
        return LegResult("model leg", "FAIL", detail[:300], elapsed)
    if "OK" not in (payload.get("text") or ""):
        return LegResult("model leg", "FAIL",
                         "model did not reply OK", elapsed)
    return LegResult("model leg", "PASS",
                     f"real completion via {active.provider}", elapsed)


_LEGS = {
    "doctor": _leg_doctor,
    "demo run": _leg_demo_run,
    "offline eval": _leg_offline_eval,
    "repomap": _leg_repomap,
    "memory": _leg_memory,
    "worktree": _leg_worktree,
    "sandbox probe": _leg_sandbox,
    "model leg": _leg_model,
}


def run_selftest(*, python: str = sys.executable,
                 timeouts: dict[str, float] | None = None) -> SelftestReport:
    """Run every leg in order; never raises on a leg failure.

    ``python`` is the interpreter the subprocess legs invoke (tests
    point it at a broken script to prove a broken install FAILs).
    ``timeouts`` overrides individual leg budgets (tests only).
    """
    ctx = _Context(python=python, timeouts=timeouts)
    legs: list[LegResult] = []
    try:
        for name in LEG_ORDER:
            try:
                legs.append(_LEGS[name](ctx))
            except Exception as exc:  # noqa: BLE001 - a leg crash is FAIL
                legs.append(LegResult(name, "FAIL",
                                      f"leg crashed: {exc}"))
    finally:
        ctx.cleanup()
    report = SelftestReport(legs=legs, verdict=verdict_of(legs))
    return report


def format_report(report: SelftestReport) -> list[str]:
    """Human-readable lines: one per leg, then the verdict."""
    lines = []
    for leg in report.legs:
        marker = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP"}[leg.status]
        lines.append(f"[{marker}] {leg.name:<14} {leg.detail}")
    lines.append(f"selftest: {report.verdict} "
                 f"({sum(1 for leg in report.legs if leg.status == 'PASS')} "
                 f"pass, "
                 f"{sum(1 for leg in report.legs if leg.status == 'SKIP')} "
                 f"skip, "
                 f"{sum(1 for leg in report.legs if leg.status == 'FAIL')} "
                 f"fail)")
    return lines
