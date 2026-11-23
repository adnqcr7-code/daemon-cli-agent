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
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path


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


async def _run(argv: list[str], cwd: Path, timeout_s: float) -> tuple[int, str, bool]:
    """Run ``argv`` (no shell) and return (exit code, output tail, timed out)."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except OSError as exc:
        return 127, f"could not start {' '.join(argv)}: {exc}", False
    try:
        out, _ = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
    except TimeoutError:
        process.kill()
        await process.wait()
        return 124, f"timed out after {timeout_s:g}s", True
    text = out.decode("utf-8", errors="replace")
    return process.returncode or 0, text[-4000:], False


def _last_line(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1][:200] if lines else ""


async def verify_changes(workspace: Path | str, files_changed: list[str],
                         *, run_tests: bool = True, run_lint: bool = True,
                         run_typecheck: bool = True,
                         timeout_s: float = 300) -> VerificationResult:
    """Verify the files a turn changed. Always returns a result; never raises."""
    root = Path(workspace).resolve()
    result = VerificationResult()
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
        code, out, timed_out = await _run(
            [sys.executable, "-m", "compileall", "-q",
             *[str(path) for path in py_files]],
            cwd=root, timeout_s=min(timeout_s, 120))
        result.checks.append(VerificationCheck(
            label=f"compileall ({len(py_files)} file(s))",
            passed=code == 0 and not timed_out,
            detail="timed out" if timed_out else (_last_line(out) or "ok")))
    if run_lint:
        lint_command = detect_lint_command(root, files_changed)
        if lint_command is not None:
            code, out, timed_out = await _run(
                _command_argv(lint_command), cwd=root, timeout_s=timeout_s)
            result.checks.append(VerificationCheck(
                label=f"lint: {lint_command}",
                passed=code == 0 and not timed_out,
                detail="timed out" if timed_out else (_last_line(out) or f"exit {code}")))
    if run_typecheck:
        typecheck_command = detect_typecheck_command(root, files_changed)
        if typecheck_command is not None:
            code, out, timed_out = await _run(
                _command_argv(typecheck_command), cwd=root, timeout_s=timeout_s)
            result.checks.append(VerificationCheck(
                label=f"typecheck: {typecheck_command}",
                passed=code == 0 and not timed_out,
                detail="timed out" if timed_out else (_last_line(out) or f"exit {code}")))
    if run_tests:
        command = detect_test_command(root, files_changed)
        if command is not None:
            if command.startswith("python -m pytest"):
                # Use this interpreter, not a bare `python` from PATH.
                argv = [sys.executable, "-m", "pytest"] + command.split()[3:]
            else:
                argv = command.split()
            code, out, timed_out = await _run(
                argv, cwd=root, timeout_s=timeout_s)
            result.checks.append(VerificationCheck(
                label=command,
                passed=code == 0 and not timed_out,
                detail="timed out" if timed_out else (_last_line(out) or f"exit {code}")))
    return result
