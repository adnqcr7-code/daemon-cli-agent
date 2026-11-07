"""Enforced verification: after an agent turn changes files, prove the
change works before the agent claims it is done.

Two checks, both reported — never silently skipped:

* ``compileall`` on every changed ``.py`` file (fast, no dependencies).
* The project's own test runner, scoped to the touched package
  (``pytest`` for Python, ``npm test`` / ``go test`` / ``cargo test``
  for other stacks), when one is detectable.

A verification failure does not roll anything back — the edits stay, the
user sees exactly what failed, and the agent's final message says so.
"""

from __future__ import annotations

import asyncio
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
                         *, run_tests: bool = True,
                         timeout_s: float = 300) -> VerificationResult:
    """Verify the files a turn changed. Always returns a result; never raises."""
    root = Path(workspace).resolve()
    result = VerificationResult()
    py_files = [root / rel for rel in files_changed
                if rel.endswith(".py") and (root / rel).exists()]
    if py_files:
        code, out, timed_out = await _run(
            [sys.executable, "-m", "compileall", "-q",
             *[str(path) for path in py_files]],
            cwd=root, timeout_s=min(timeout_s, 120))
        result.checks.append(VerificationCheck(
            label=f"compileall ({len(py_files)} file(s))",
            passed=code == 0 and not timed_out,
            detail="timed out" if timed_out else (_last_line(out) or "ok")))
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
