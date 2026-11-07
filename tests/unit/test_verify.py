"""Enforced verification: compileall + scoped test runner after edits."""

import asyncio

from daemon.agent.verify import detect_test_command, verify_changes


def test_detect_test_command_scopes_pytest_to_touched_package(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert detect_test_command(tmp_path, ["daemon/foo.py"]) == \
        "python -m pytest -q daemon"
    assert detect_test_command(tmp_path, ["bug.py"]) == \
        "python -m pytest -q ."
    assert detect_test_command(tmp_path, ["daemon/a.py", "daemon/b.py"]) == \
        "python -m pytest -q daemon"


def test_detect_test_command_returns_none_without_markers(tmp_path):
    assert detect_test_command(tmp_path, ["daemon/foo.py"]) is None
    (tmp_path / "package.json").write_text('{"name": "x"}')
    assert detect_test_command(tmp_path, ["app.js"]) == "npm test"
    assert detect_test_command(tmp_path, ["daemon/foo.py"]) is None


def test_verify_passes_for_good_python(tmp_path):
    (tmp_path / "ok.py").write_text("x = 1\n")
    result = asyncio.run(verify_changes(tmp_path, ["ok.py"], run_tests=False))
    assert result.passed
    assert "compileall" in result.checks[0].label


def test_verify_fails_for_syntax_error(tmp_path):
    (tmp_path / "broken.py").write_text("def broken(:\n")
    result = asyncio.run(verify_changes(tmp_path, ["broken.py"], run_tests=False))
    assert not result.passed
    assert "FAILED" in result.summary()


def test_verify_runs_scoped_pytest(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    result = asyncio.run(verify_changes(tmp_path, ["calc.py"]))
    assert result.passed, result.summary()
    labels = " ".join(check.label for check in result.checks)
    assert "pytest" in labels


def test_verify_reports_failing_pytest(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (tmp_path / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    result = asyncio.run(verify_changes(tmp_path, ["calc.py"]))
    assert not result.passed
    assert "FAILED" in result.summary()


def test_verify_with_no_applicable_checks(tmp_path):
    result = asyncio.run(verify_changes(tmp_path, ["notes.md"]))
    assert result.passed  # nothing to check is not a failure
    assert not result.ran_anything
    assert "no checks applied" in result.summary()
