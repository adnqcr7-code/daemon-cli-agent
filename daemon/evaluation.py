"""Repeatable repository-task evaluations for configured providers and models."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


_PACKAGE_ROOT = Path(__file__).resolve().parent
_REPO_ROOT = _PACKAGE_ROOT.parent
_DEFAULT_TASKS = _PACKAGE_ROOT / "evals" / "tasks.json"


def run_evaluations(
    targets: tuple[str, ...],
    *,
    task_file: Path = _DEFAULT_TASKS,
) -> dict[str, Any]:
    """Run each benchmark task against each ``profile=model`` target."""
    if not targets:
        raise ValueError("Provide at least one --target PROFILE=MODEL_ID.")
    tasks = _load_tasks(task_file)
    parsed_targets = [_parse_target(target) for target in targets]
    runs: list[dict[str, Any]] = []

    for profile, model in parsed_targets:
        for task in tasks:
            runs.append(_run_task(profile, model, task, task_file.parent))

    passed = sum(1 for run in runs if run["passed"])
    return {
        "runs": runs,
        "summary": {
            "passed": passed,
            "total": len(runs),
            "pass_rate": passed / len(runs) if runs else 0.0,
        },
    }


def _load_tasks(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot load evaluation tasks from {path}: {exc}") from exc
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, list) or not tasks:
        raise ValueError(f"{path} must contain a non-empty 'tasks' array.")
    for task in tasks:
        if not isinstance(task, dict) or not all(
                isinstance(task.get(key), str) and task[key]
                for key in ("id", "prompt", "fixture")):
            raise ValueError("Each evaluation task needs id, prompt, and fixture strings.")
        if not isinstance(task.get("checks"), dict):
            raise ValueError(f"Task '{task['id']}' needs a checks object.")
        expected = task["checks"].get("files_contain", {})
        if not isinstance(expected, dict) or not all(
                isinstance(path, str) and isinstance(content, str)
                for path, content in expected.items()):
            raise ValueError(
                f"Task '{task['id']}' files_contain must map paths to strings.")
    return tasks


def _parse_target(value: str) -> tuple[str | None, str]:
    if "=" not in value:
        raise ValueError(
            f"Invalid target {value!r}; use CREDENTIAL_PROFILE=MODEL_ID.")
    profile, model = value.split("=", 1)
    if not model.strip():
        raise ValueError(f"Invalid target {value!r}; model id cannot be empty.")
    return profile.strip() or None, model.strip()


def _run_task(
    profile: str | None,
    model: str,
    task: dict[str, Any],
    task_root: Path,
) -> dict[str, Any]:
    fixture = (task_root / task["fixture"]).resolve()
    if not fixture.is_dir() or not fixture.is_relative_to(task_root.resolve()):
        raise ValueError(f"Task fixture must be a directory under {task_root}: {task['fixture']}")

    with tempfile.TemporaryDirectory(prefix=f"daemon-eval-{task['id']}-") as temp:
        workspace = Path(temp) / "workspace"
        shutil.copytree(fixture, workspace)
        command = [
            sys.executable, "-m", "daemon.cli", "run", task["prompt"],
            "--workspace", str(workspace), "--model", model, "--yes", "--json",
        ]
        if profile:
            command.extend(["--profile", profile])
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                command, cwd=_REPO_ROOT, capture_output=True, text=True,
                timeout=float(task.get("timeout_seconds", 1800)), check=False)
            elapsed = time.perf_counter() - started
            payload = _parse_run_output(completed.stdout)
            error = completed.stderr.strip() or payload.get("error")
        except (OSError, subprocess.TimeoutExpired) as exc:
            elapsed = time.perf_counter() - started
            payload = {}
            error = (f"Agent run timed out after {exc.timeout} seconds."
                     if isinstance(exc, subprocess.TimeoutExpired) else str(exc))
            completed = None

        checks = task["checks"]
        file_checks = _check_files(workspace, checks.get("files_contain", {}))
        verification = _verify_command(workspace, task.get("verify"))
        passed = (
            completed is not None
            and completed.returncode == 0
            and bool(payload.get("ok"))
            and all(item["passed"] for item in file_checks)
            and (verification is None or verification["passed"])
        )
        return {
            "task": task["id"],
            "provider": payload.get("provider"),
            "model": payload.get("model", model),
            "passed": passed,
            "tests_passed": verification["passed"] if verification else None,
            "file_checks": file_checks,
            "tool_calls": payload.get("tool_calls"),
            "permission_prompts": payload.get("permission_prompts"),
            "usage": payload.get("usage"),
            "cost_usd": payload.get("cost_usd"),
            "agent_duration_s": payload.get("duration_s"),
            "elapsed_s": round(elapsed, 3),
            "error": error if not passed else None,
            "text": payload.get("text", ""),
        }


def _parse_run_output(stdout: str) -> dict[str, Any]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return {"error": stdout.strip() or "daemon run returned no JSON output"}
    return payload if isinstance(payload, dict) else {}


def _check_files(workspace: Path, expected: dict[str, str]) -> list[dict[str, Any]]:
    checks = []
    for relative, expected_text in expected.items():
        path = (workspace / relative).resolve()
        safe = path.is_relative_to(workspace.resolve())
        actual = path.read_text(encoding="utf-8", errors="replace") if safe and path.is_file() else ""
        checks.append({
            "path": relative,
            "passed": safe and expected_text in actual,
            "expected": expected_text,
        })
    return checks


def _verify_command(workspace: Path, command: list[str] | None) -> dict[str, Any] | None:
    if command is None:
        return None
    if not isinstance(command, list) or not command or not all(
            isinstance(part, str) for part in command):
        raise ValueError("Task verify must be a non-empty command argument array.")
    command = [sys.executable if part == "{python}" else part for part in command]
    try:
        result = subprocess.run(
            command, cwd=workspace, capture_output=True, text=True,
            timeout=600, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "passed": False,
            "command": command,
            "returncode": None,
            "output": str(exc),
        }
    return {
        "passed": result.returncode == 0,
        "command": command,
        "returncode": result.returncode,
        "output": (result.stdout + result.stderr)[-8000:],
    }
