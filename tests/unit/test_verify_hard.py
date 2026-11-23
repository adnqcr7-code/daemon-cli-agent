"""Hardened verification: lint/typecheck detection, skipping, repair prompt."""

import asyncio
import importlib.util
import shutil

import pytest

import daemon.agent.verify as verify_mod
from daemon.agent.verify import (
    VerificationCheck,
    VerificationResult,
    detect_lint_command,
    detect_typecheck_command,
    verify_changes,
)

_RUFF_AVAILABLE = shutil.which("ruff") is not None \
    or importlib.util.find_spec("ruff") is not None


def _fake_tools(monkeypatch, available):
    """Make tool availability hermetic: only ``available`` binaries/modules exist."""
    monkeypatch.setattr(
        verify_mod.shutil, "which",
        lambda name: f"/fake/{name}" if name in available else None)
    monkeypatch.setattr(
        verify_mod.importlib.util, "find_spec",
        lambda name: object() if name in available else None)


# ---------------------------------------------------------------- detection


def test_detect_lint_none_for_bare_python_project(tmp_path, monkeypatch):
    _fake_tools(monkeypatch, {"ruff"})
    (tmp_path / "mod.py").write_text("x = 1\n")
    assert detect_lint_command(tmp_path, ["mod.py"]) is None


def test_detect_lint_none_when_ruff_configured_but_unavailable(tmp_path, monkeypatch):
    _fake_tools(monkeypatch, set())
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n")
    (tmp_path / "mod.py").write_text("x = 1\n")
    assert detect_lint_command(tmp_path, ["mod.py"]) is None


def test_detect_lint_ruff_when_pyproject_mentions_ruff(tmp_path, monkeypatch):
    _fake_tools(monkeypatch, {"ruff"})
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n")
    (tmp_path / "mod.py").write_text("x = 1\n")
    assert detect_lint_command(tmp_path, ["mod.py", "notes.md"]) == "ruff check mod.py"


def test_detect_lint_ruff_via_ruff_toml_and_module_only(tmp_path, monkeypatch):
    # No binary on PATH, but the ruff module is importable: still runnable.
    _fake_tools(monkeypatch, set())
    monkeypatch.setattr(
        verify_mod.importlib.util, "find_spec",
        lambda name: object() if name == "ruff" else None)
    (tmp_path / "ruff.toml").write_text("line-length = 100\n")
    assert detect_lint_command(tmp_path, ["pkg/mod.py"]) == "ruff check pkg/mod.py"


def test_detect_lint_npm_lint_script(tmp_path, monkeypatch):
    _fake_tools(monkeypatch, {"npm"})
    (tmp_path / "package.json").write_text('{"scripts": {"lint": "eslint ."}}')
    assert detect_lint_command(tmp_path, ["app.ts"]) == "npm run lint"
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}')
    assert detect_lint_command(tmp_path, ["app.ts"]) is None


def test_detect_lint_go_vet(tmp_path, monkeypatch):
    _fake_tools(monkeypatch, {"go"})
    (tmp_path / "go.mod").write_text("module example.com/x\n")
    assert detect_lint_command(tmp_path, ["main.go"]) == "go vet ./..."
    _fake_tools(monkeypatch, set())
    assert detect_lint_command(tmp_path, ["main.go"]) is None


def test_detect_typecheck_none_for_bare_python_project(tmp_path, monkeypatch):
    _fake_tools(monkeypatch, {"mypy", "pyright"})
    (tmp_path / "mod.py").write_text("x = 1\n")
    assert detect_typecheck_command(tmp_path, ["mod.py"]) is None


def test_detect_typecheck_mypy_when_configured(tmp_path, monkeypatch):
    _fake_tools(monkeypatch, {"mypy"})
    (tmp_path / "pyproject.toml").write_text("[tool.mypy]\nstrict = true\n")
    (tmp_path / "mod.py").write_text("x = 1\n")
    assert detect_typecheck_command(tmp_path, ["mod.py"]) == "mypy mod.py"


def test_detect_typecheck_mypy_via_mypy_ini(tmp_path, monkeypatch):
    _fake_tools(monkeypatch, {"mypy"})
    (tmp_path / "mypy.ini").write_text("[mypy]\n")
    assert detect_typecheck_command(tmp_path, ["mod.py"]) == "mypy mod.py"


def test_detect_typecheck_none_when_mypy_configured_but_unavailable(
        tmp_path, monkeypatch):
    _fake_tools(monkeypatch, set())
    (tmp_path / "pyproject.toml").write_text("[tool.mypy]\nstrict = true\n")
    assert detect_typecheck_command(tmp_path, ["mod.py"]) is None


def test_detect_typecheck_pyright_fallback(tmp_path, monkeypatch):
    _fake_tools(monkeypatch, {"pyright"})
    (tmp_path / "pyrightconfig.json").write_text("{}")
    assert detect_typecheck_command(tmp_path, ["mod.py"]) == "pyright mod.py"


def test_detect_typecheck_tsc_for_typescript(tmp_path, monkeypatch):
    _fake_tools(monkeypatch, {"npx"})
    (tmp_path / "tsconfig.json").write_text("{}")
    assert detect_typecheck_command(tmp_path, ["app.ts"]) == "npx tsc --noEmit"
    _fake_tools(monkeypatch, set())
    assert detect_typecheck_command(tmp_path, ["app.ts"]) is None


# ------------------------------------------------------------------- results


def test_failed_labels_and_repair_prompt_on_failure():
    result = VerificationResult(checks=[
        VerificationCheck(label="compileall (1 file(s))", passed=True, detail="ok"),
        VerificationCheck(label="lint: ruff check mod.py", passed=False,
                          detail="mod.py:1:1: F401 unused import"),
    ])
    assert result.failed_labels() == ["lint: ruff check mod.py"]
    prompt = result.repair_prompt()
    assert "lint: ruff check mod.py" in prompt
    assert "F401" in prompt
    assert "Do not claim the task is complete while verification fails." in prompt


def test_repair_prompt_empty_on_pass():
    result = VerificationResult(checks=[
        VerificationCheck(label="compileall (1 file(s))", passed=True, detail="ok"),
    ])
    assert result.failed_labels() == []
    assert result.repair_prompt() == ""
    assert VerificationResult().repair_prompt() == ""


def test_repair_prompt_names_failed_check_from_real_run(tmp_path):
    (tmp_path / "broken.py").write_text("def broken(:\n")
    result = asyncio.run(verify_changes(tmp_path, ["broken.py"], run_tests=False))
    assert not result.passed
    assert result.failed_labels() == [result.checks[0].label]
    assert "compileall" in result.repair_prompt()


# -------------------------------------------------------------- verify_changes


def test_verify_good_file_still_passes_with_lint_typecheck_defaults(tmp_path):
    (tmp_path / "ok.py").write_text("x = 1\n")
    result = asyncio.run(verify_changes(tmp_path, ["ok.py"]))
    assert result.passed, result.summary()
    assert "compileall" in result.checks[0].label


def test_verify_skips_unavailable_lint_silently(tmp_path, monkeypatch):
    _fake_tools(monkeypatch, set())
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n[tool.mypy]\n")
    (tmp_path / "ok.py").write_text("x = 1\n")
    result = asyncio.run(verify_changes(tmp_path, ["ok.py"], run_tests=False))
    assert result.passed, result.summary()
    labels = [check.label for check in result.checks]
    assert not any(label.startswith(("lint:", "typecheck:")) for label in labels)


def test_verify_can_disable_lint_and_typecheck(tmp_path, monkeypatch):
    _fake_tools(monkeypatch, {"ruff", "mypy"})
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n[tool.mypy]\n")
    (tmp_path / "ok.py").write_text("x = 1\n")
    result = asyncio.run(verify_changes(
        tmp_path, ["ok.py"], run_tests=False, run_lint=False, run_typecheck=False))
    assert result.passed, result.summary()
    assert [check.label for check in result.checks] == ["compileall (1 file(s))"]


@pytest.mark.skipif(not _RUFF_AVAILABLE, reason="ruff is not available in this env")
def test_verify_fails_on_ruff_f401(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'x'\n\n[tool.ruff]\nline-length = 100\n")
    (tmp_path / "mod.py").write_text("import os\n\n\nx = 1\n")
    result = asyncio.run(verify_changes(
        tmp_path, ["mod.py"], run_tests=False, run_typecheck=False))
    assert not result.passed, result.summary()
    lint_checks = [c for c in result.checks if c.label.startswith("lint: ruff check")]
    assert lint_checks and not lint_checks[0].passed
    assert "lint: ruff check mod.py" in result.repair_prompt()
