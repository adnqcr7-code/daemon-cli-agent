"""Enforced verification: after an agent turn changes files, prove the
change works before the agent claims it is done.

Checks, reported when they run:

* ``compileall`` on every changed ``.py`` file (fast, no dependencies).
* Lint and typecheck, when the project is configured for a tool *and*
  that tool is runnable on this machine (ruff / mypy / pyright /
  ``npm run lint`` / ``go vet`` / ``tsc``). An unavailable tool is
  skipped silently — it never makes verification fail.
* The project's own test runner, scoped to the touched package
  (``pytest`` for Python, ``npm test`` / ``go test`` / ``cargo test``
  for other stacks), when one is detectable.

A verification failure does not roll anything back — the edits stay, the
user sees exactly what failed, and the agent's final message says so.
``VerificationResult.repair_prompt()`` turns a failure into a message
that can be fed back to the agent for a repair attempt.

Every command verification runs goes through the SAME path as the
shell tool (review round 2, V3): a permission-engine ``check()``
(which audit-logs the decision) resolved through the engine, then
the shared ``run_shell`` executor — secret-stripped environment and,
when ``sandbox.shell`` is enabled, the bubblewrap wrap. Verification
is automatic and runs project code after every edit turn, so it must
never be a broker blind spot: no raw subprocess, no inherited
environment, no unlogged execution. The check classifies each
command's LOGICAL form (``python -m pytest -q .``); the argv that
executes is normalized to this interpreter's absolute path, which
the classifier would rate "unknown" and a non-interactive run would
deny (round 3, R2-1).

Classified and executed must also be the same PROGRAM (round 4,
R3-1): ``python -m <tool>`` resolves ``<tool>`` with the workspace
first on ``sys.path``, so a ``pytest.py`` planted at the workspace
root used to execute in place of the real tool and mint a green
"verification passed" under an ALLOW naming the real one. Before
any ``python -m X`` verification command runs, :func:`tool_module_block`
fails the check closed when the workspace root carries a shadowing
entry for X - decided with plain pathlib in this process, never by
an interpreter started inside the workspace (round 5, R4-1: a
planted ``json.py`` blinded the round-4 probe itself) - and an
isolated (``python -I``) probe of the same interpreter must still
confirm a clean origin for X outside the workspace.
"""

from __future__ import annotations

import importlib.util
import json
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..permissions import Effect
from ..security import strip_secret_env


@dataclass
class VerificationCheck:
    """One check and its outcome."""

    label: str
    passed: bool
    detail: str = ""

    def __str__(self) -> str:
        status = "passed" if self.passed else "FAILED"
        return f"{self.label}: {status}" + (f" ({self.detail})" if self.detail else "")


@dataclass
class VerificationResult:
    """Everything verification ran, and whether it all passed."""

    checks: list[VerificationCheck] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def ran_anything(self) -> bool:
        return bool(self.checks)

    def summary(self) -> str:
        if not self.checks:
            return "no checks applied (no Python files changed, no test runner found)"
        parts = [str(check) for check in self.checks]
        verdict = "verification passed" if self.passed else "verification FAILED"
        return f"{verdict} — " + "; ".join(parts)

    def failed_labels(self) -> list[str]:
        """Labels of the checks that failed, in run order."""
        return [check.label for check in self.checks if not check.passed]

    def repair_prompt(self) -> str:
        """A message to feed back to the agent after a failed verification.

        Empty when verification passed (or nothing ran). Otherwise names
        each failed check with its detail (the stored detail is already
        the check output's last line) and instructs the agent to fix the
        failures instead of claiming completion.
        """
        if self.passed:
            return ""
        lines = ["Verification failed:"]
        for check in self.checks:
            if check.passed:
                continue
            lines.append(f"- {check.label}: {check.detail}" if check.detail
                         else f"- {check.label}")
        lines.append(
            "Fix these failures and finish again. Do not claim the task is "
            "complete while verification fails.")
        return "\n".join(lines)


def detect_test_command(workspace: Path, changed: list[str]) -> str | None:
    """Best test command for the changed files, or None if none applies.

    Mirrors the ``run_tests`` tool's detection, but scopes pytest to the
    touched top-level package so a turn that edits one module does not
    re-run an unrelated suite.
    """
    root = Path(workspace)
    py_files = [rel for rel in changed if rel.endswith(".py")]
    if py_files and (
        (root / "pyproject.toml").exists() or (root / "pytest.ini").exists()
        or (root / "tests").is_dir() or list(root.glob("test_*.py"))
    ):
        # Scope to the touched top-level package(s): "daemon/foo.py" -> "daemon",
        # a root-level file -> the workspace itself (".").
        targets = sorted({rel.split("/")[0] if "/" in rel else "."
                          for rel in py_files})
        return "python -m pytest -q " + " ".join(targets)
    js_files = [rel for rel in changed
                if rel.endswith((".js", ".ts", ".jsx", ".tsx"))]
    if js_files and (root / "package.json").exists():
        return "npm test"
    if any(rel.endswith(".go") for rel in changed) and (root / "go.mod").exists():
        return "go test ./..."
    if any(rel.endswith(".rs") for rel in changed) and (root / "Cargo.toml").exists():
        return "cargo test"
    return None


def _file_mentions(path: Path, needle: str) -> bool:
    """True when ``path`` is a readable text file mentioning ``needle``."""
    try:
        return needle in path.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False


def _runnable(binary: str, module: str | None = None) -> bool:
    """True when a tool can run here: on PATH, or importable as a module."""
    if shutil.which(binary) is not None:
        return True
    return module is not None and importlib.util.find_spec(module) is not None


def _has_lint_script(package_json: Path) -> bool:
    try:
        data = json.loads(package_json.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return False
    scripts = data.get("scripts") if isinstance(data, dict) else None
    return isinstance(scripts, dict) and "lint" in scripts


def detect_lint_command(workspace: Path, changed: list[str]) -> str | None:
    """Best lint command for the changed files, or None if none applies.

    A linter only applies when the project is configured for it *and*
    the tool is runnable on this machine; an unavailable tool returns
    None so verification skips it silently instead of failing.
    """
    root = Path(workspace)
    py_files = [rel for rel in changed if rel.endswith(".py")]
    if py_files:
        configured = (
            (root / "ruff.toml").exists() or (root / ".ruff.toml").exists()
            or _file_mentions(root / "pyproject.toml", "ruff")
            or _file_mentions(root / "setup.cfg", "ruff")
            or _file_mentions(root / "tox.ini", "ruff"))
        if configured and _runnable("ruff", "ruff"):
            return "ruff check " + " ".join(py_files)
    js_files = [rel for rel in changed
                if rel.endswith((".js", ".ts", ".jsx", ".tsx"))]
    if js_files and (root / "package.json").exists() \
            and _has_lint_script(root / "package.json") \
            and shutil.which("npm") is not None:
        return "npm run lint"
    if any(rel.endswith(".go") for rel in changed) and (root / "go.mod").exists() \
            and shutil.which("go") is not None:
        return "go vet ./..."
    return None


def detect_typecheck_command(workspace: Path, changed: list[str]) -> str | None:
    """Best typecheck command for the changed files, or None.

    Same contract as :func:`detect_lint_command`: configured for the
    project and runnable here, otherwise skipped silently.
    """
    root = Path(workspace)
    py_files = [rel for rel in changed if rel.endswith(".py")]
    if py_files:
        mypy_configured = (
            (root / "mypy.ini").exists() or (root / ".mypy.ini").exists()
            or _file_mentions(root / "pyproject.toml", "tool.mypy")
            or _file_mentions(root / "setup.cfg", "mypy"))
        if mypy_configured and _runnable("mypy", "mypy"):
            return "mypy " + " ".join(py_files)
        if (root / "pyrightconfig.json").exists() \
                and shutil.which("pyright") is not None:
            return "pyright " + " ".join(py_files)
    ts_files = [rel for rel in changed if rel.endswith((".ts", ".tsx"))]
    if ts_files and (root / "tsconfig.json").exists() \
            and shutil.which("npx") is not None:
        return "npx tsc --noEmit"
    return None


def _command_argv(command: str) -> list[str]:
    """Split a detected command, preferring ``python -m`` for module tools.

    When ruff/mypy is importable but has no binary on PATH, run it
    through this interpreter so the detected command still executes.
    """
    argv = command.split()
    if argv and argv[0] in ("ruff", "mypy") and shutil.which(argv[0]) is None \
            and importlib.util.find_spec(argv[0]) is not None:
        return [sys.executable, "-m", argv[0], *argv[1:]]
    return argv


# The only modules verification ever executes via ``python -m``
# (round 4, R3-1). The origin probe below refuses to resolve any
# other name: the set is daemon's own, never project input.
_MODULE_TOOLS = frozenset({"pytest", "ruff", "mypy", "compileall"})

# find_spec() locates a module WITHOUT executing it (for a top-level
# name it runs no project code at all). The probe runs the SAME
# interpreter in ISOLATED mode (``python -I``: implies -E and -s, and
# the script's directory / cwd / PYTHONPATH are excluded from
# sys.path), so the probe's OWN imports - importlib, json - resolve
# from the real stdlib and can never be shadowed by the workspace
# under review (round 5, R4-1: a planted ``json.py`` used to blind
# the round-4 probe, whose cwd WAS the workspace, and the planted
# tool then executed). The probe therefore only ever reports the
# CLEAN origin of a module; whether the workspace shadows it is
# decided separately, in this process, by _workspace_shadow().
_PROBE_PROGRAM = (
    "import importlib.util, json, sys\n"
    "spec = importlib.util.find_spec(sys.argv[1])\n"
    "paths = []\n"
    "if spec is not None:\n"
    "    if spec.origin:\n"
    "        paths.append(spec.origin)\n"
    "    if spec.submodule_search_locations:\n"
    "        paths.extend(spec.submodule_search_locations)\n"
    "print(json.dumps(paths))\n"
)


def _workspace_shadow(root: Path, module: str) -> Path | None:
    """The workspace entry that would shadow ``module``, if any.

    Decided HERE, in the daemon process, with plain pathlib - no
    interpreter is started in the workspace and no workspace code
    is imported, so nothing the workspace contains can influence
    the answer (round 5, R4-1). ``python -m X`` puts exactly one
    workspace directory first on ``sys.path``: the process's cwd,
    which for verification is the workspace root. A top-level name
    X resolves from that directory via exactly three entry shapes,
    and all three are checked: a module file ``X.py``, a sourceless
    module ``X.pyc``, and a package directory ``X/`` - with an
    ``__init__.py`` (a regular package) or without one (a namespace
    package portion, which still wins over an installed REGULAR
    package at execution time only if it supplies the module - and
    for ``-m`` execution a namespace portion with a ``__main__``
    does run; failing closed on any ``X/`` is the safe reading).
    Returns the shadowing path to name in the refusal, else None.
    """
    module_file = root / f"{module}.py"
    if module_file.is_file():
        return module_file
    bytecode_file = root / f"{module}.pyc"
    if bytecode_file.is_file():
        return bytecode_file
    package_dir = root / module
    if package_dir.is_dir():
        init_file = package_dir / "__init__.py"
        return init_file if init_file.is_file() else package_dir
    return None


def tool_module_block(python: str, module: str,
                      workspace: Path) -> str | None:
    """Why ``python -m module`` must not execute from ``workspace``.

    Returns a failure detail when the command is blocked, None when
    it may run. ``python -m X`` puts the process's CWD first on
    ``sys.path`` and verification runs with cwd = the workspace under
    review, so a workspace file named after the tool shadows the
    real one and its exit code would decide the check (R3-1).

    Two independent halves, neither trusting the workspace:

    * Shadow DETECTION is _workspace_shadow() above - plain pathlib
      in this process. It cannot be blinded by a planted stdlib
      module (round 5, R4-1: the round-4 design asked a probe
      interpreter started INSIDE the workspace where the module
      resolves, and a planted ``json.py`` decided what that probe
      "learned").
    * The probe supplies the CLEAN origin: a fresh subprocess of
      the SAME interpreter the command would use, run isolated
      (``python -I``) so its own import machinery and stdlib are
      beyond the workspace's reach, resolving X via find_spec
      without executing it. A probe that cannot answer (interpreter
      error, timeout, unparseable output) blocks: a tool whose
      origin cannot be confirmed is not run on faith. An origin
      landing inside the workspace even under isolation also
      blocks (defense in depth - e.g. an interpreter whose own
      installation lives in the workspace).
    """
    if module not in _MODULE_TOOLS:
        return f"unrecognized verification module {module!r}"
    root = Path(workspace).resolve()
    shadow = _workspace_shadow(root, module)
    if shadow is not None:
        return f"verification tool shadowed by workspace " \
               f"file {shadow}"
    try:
        proc = subprocess.run(
            [python, "-I", "-c", _PROBE_PROGRAM, module],
            cwd=root, env=strip_secret_env(None),
            capture_output=True, text=True, timeout=30)
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        paths = json.loads(lines[-1])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return f"verification tool origin could not be confirmed " \
               f"for {module}"
    for raw in paths:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve()
        if candidate == root or root in candidate.parents:
            return f"verification tool shadowed by workspace " \
                   f"file {candidate}"
    return None


async def _run_command(command: str, *, cwd: Path, timeout_s: float,
                       tool: str, permissions, sandbox_plan,
                       check_command: str | None = None
                       ) -> tuple[int, str, bool]:
    """Run one verification command through the broker + shell path.

    Returns (exit code, output, timed out). When a permission engine
    is supplied, the command is checked (and the decision audit-
    logged) and resolved through it first; a command the broker does
    not ALLOW is never executed - the check simply fails with the
    recorded reasons. Execution itself is the shell tool's shared
    ``run_shell``: secret-stripped environment, sandbox wrap when a
    plan is given.

    The broker classifies ``check_command`` when given - the
    command's LOGICAL form (``python -m pytest -q .``) - while
    ``command`` is the normalized argv that actually executes
    (``/path/to/venv/bin/python -m pytest -q .``). The two are the
    same command semantically, but the classifier rates any absolute
    interpreter path "unknown" (review round 3, R2-1): round 2
    checked the rewritten string, so daemon's own housekeeping
    commands hit the ASK floor and were denied in every
    non-interactive run - verification "failed" on correct edits
    without ever running a test.
    """
    if permissions is not None:
        decision = permissions.check(
            tool, {"command": check_command or command},
            mutating=True)
        decision = permissions.resolve(decision)
        if decision.effect is not Effect.ALLOW:
            reasons = "; ".join(decision.reasons) or decision.effect.value
            return 126, f"verification command not permitted: {reasons}", False
    from ..tools.shell import run_shell

    return await run_shell(command, cwd=cwd, timeout_s=timeout_s,
                           sandbox_plan=sandbox_plan)


def _last_line(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1][:200] if lines else ""


async def verify_changes(workspace: Path | str, files_changed: list[str],
                         *, run_tests: bool = True, run_lint: bool = True,
                         run_typecheck: bool = True,
                         timeout_s: float = 300,
                         permissions=None, config=None) -> VerificationResult:
    """Verify the files a turn changed. Always returns a result; never raises.

    ``permissions`` is the run's PermissionEngine: every command is
    checked + audit-logged through it (V3). ``config`` enables the
    sandbox wrap via ``sandbox.shell``. Both default to None for
    direct callers (unit tests), in which case commands still run
    through ``run_shell`` - the environment is secret-stripped
    either way.
    """
    root = Path(workspace).resolve()
    result = VerificationResult()
    from ..sandbox import plan_for

    sandbox_plan = plan_for(config, root) if config is not None else None
    # Inside the sandbox the venv interpreter does not exist (it lives
    # under $HOME, which is not bound); the box's own python3 is the
    # interpreter there. Outside, use this very interpreter.
    python = "python3" if sandbox_plan is not None else sys.executable
    py_files = [root / rel for rel in files_changed
                if rel.endswith(".py") and (root / rel).exists()]
    # Timestamp-based .pyc invalidation uses second-resolution mtimes, so
    # two same-size edits within one second (common in a repair loop) can
    # leave a stale bytecode cache that tests would silently import.
    # Drop the changed files' cached bytecode so verification always
    # exercises the source actually on disk.
    for path in py_files:
        cache_dir = path.parent / "__pycache__"
        if cache_dir.is_dir():
            for cached in cache_dir.glob(path.stem + ".*.pyc"):
                try:
                    cached.unlink()
                except OSError:
                    pass
    if py_files:
        block = tool_module_block(python, "compileall", root)
        if block is not None:
            result.checks.append(VerificationCheck(
                label=f"compileall ({len(py_files)} file(s))",
                passed=False, detail=block))
        else:
            command = shlex.join(
                [python, "-m", "compileall", "-q",
                 *[str(path) for path in py_files]])
            code, out, timed_out = await _run_command(
                command, cwd=root, timeout_s=min(timeout_s, 120),
                tool="run_command", permissions=permissions,
                sandbox_plan=sandbox_plan,
                check_command=shlex.join(
                    ["python", "-m", "compileall", "-q",
                     *[str(path) for path in py_files]]))
            result.checks.append(VerificationCheck(
                label=f"compileall ({len(py_files)} file(s))",
                passed=code == 0 and not timed_out,
                detail="timed out" if timed_out else (_last_line(out) or "ok")))
    if run_lint:
        lint_command = detect_lint_command(root, files_changed)
        if lint_command is not None:
            argv = _command_argv(lint_command)
            if sandbox_plan is not None and argv[:1] == [sys.executable]:
                argv = ["python3", *argv[1:]]
            block = tool_module_block(argv[0], argv[2], root) \
                if argv[1:2] == ["-m"] and len(argv) > 2 else None
            if block is not None:
                result.checks.append(VerificationCheck(
                    label=f"lint: {lint_command}",
                    passed=False, detail=block))
            else:
                code, out, timed_out = await _run_command(
                    shlex.join(argv), cwd=root, timeout_s=timeout_s,
                    tool="run_command", permissions=permissions,
                    sandbox_plan=sandbox_plan,
                    check_command=lint_command)
                result.checks.append(VerificationCheck(
                    label=f"lint: {lint_command}",
                    passed=code == 0 and not timed_out,
                    detail="timed out" if timed_out else (_last_line(out) or f"exit {code}")))
    if run_typecheck:
        typecheck_command = detect_typecheck_command(root, files_changed)
        if typecheck_command is not None:
            argv = _command_argv(typecheck_command)
            if sandbox_plan is not None and argv[:1] == [sys.executable]:
                argv = ["python3", *argv[1:]]
            block = tool_module_block(argv[0], argv[2], root) \
                if argv[1:2] == ["-m"] and len(argv) > 2 else None
            if block is not None:
                result.checks.append(VerificationCheck(
                    label=f"typecheck: {typecheck_command}",
                    passed=False, detail=block))
            else:
                code, out, timed_out = await _run_command(
                    shlex.join(argv), cwd=root, timeout_s=timeout_s,
                    tool="run_command", permissions=permissions,
                    sandbox_plan=sandbox_plan,
                    check_command=typecheck_command)
                result.checks.append(VerificationCheck(
                    label=f"typecheck: {typecheck_command}",
                    passed=code == 0 and not timed_out,
                    detail="timed out" if timed_out else (_last_line(out) or f"exit {code}")))
    if run_tests:
        command = detect_test_command(root, files_changed)
        if command is not None:
            detected = command
            block = None
            if command.startswith("python -m pytest"):
                # Use this interpreter, not a bare `python` from PATH.
                command = shlex.join(
                    [python, "-m", "pytest"] + command.split()[3:])
                block = tool_module_block(python, "pytest", root)
            if block is not None:
                result.checks.append(VerificationCheck(
                    label=command, passed=False, detail=block))
            else:
                code, out, timed_out = await _run_command(
                    command, cwd=root, timeout_s=timeout_s,
                    tool="run_tests", permissions=permissions,
                    sandbox_plan=sandbox_plan, check_command=detected)
                result.checks.append(VerificationCheck(
                    label=command,
                    passed=code == 0 and not timed_out,
                    detail="timed out" if timed_out else (_last_line(out) or f"exit {code}")))
    return result
