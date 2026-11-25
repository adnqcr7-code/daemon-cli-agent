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


# ------------------------------------------- V3: broker + shell path


def test_verify_does_not_leak_secret_env_to_project_tests(
        tmp_path, monkeypatch):
    # Review round 2, V3 (the reviewer's probe): round 1 ran the
    # project's tests via a raw subprocess that inherited daemon's
    # FULL environment. A project test asserting a secret's absence
    # must pass under verify_changes even when the var is set in the
    # daemon process - verification now runs through run_shell,
    # which strips secret env vars.
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "calc.py").write_text("x = 1\n")
    (tmp_path / "test_env.py").write_text(
        "import os\n\n\n"
        "def test_no_secret_leaks():\n"
        "    assert 'OPENAI_API_KEY' not in os.environ\n")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-must-not-reach-tests")
    result = asyncio.run(verify_changes(tmp_path, ["calc.py"]))
    assert result.passed, result.summary()


def test_verify_commands_are_permission_checked_and_audited(
        home, tmp_path):
    # Every verification command goes through permissions.check():
    # the audit log holds a permission-decision record for it.
    from daemon.log import setup_logging
    from daemon.permissions import PermissionEngine

    setup_logging(home)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n"
        "    assert add(2, 3) == 5\n")
    engine = PermissionEngine(tmp_path, assume_yes=True)
    result = asyncio.run(verify_changes(
        tmp_path, ["calc.py"], permissions=engine))
    assert result.passed, result.summary()
    audit_log = (home / "logs" / "audit.log").read_text(encoding="utf-8")
    decisions = [line for line in audit_log.splitlines()
                 if "permission-decision" in line]
    assert any("tool=run_tests" in line for line in decisions)
    assert any("tool=run_command" in line for line in decisions)


def test_verify_denied_command_is_never_executed(home, tmp_path):
    # A broker DENY on the test command means the project code does
    # not run at all: the check fails as "not permitted" and the
    # test's side effect never happens.
    from daemon.permissions import PermissionEngine

    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "calc.py").write_text("x = 1\n")
    (tmp_path / "test_side_effect.py").write_text(
        "from pathlib import Path\n\n\n"
        "def test_side_effect():\n"
        "    Path('pwned.txt').write_text('ran')\n")
    engine = PermissionEngine(
        tmp_path, assume_yes=True,
        rules=[{"tool": "run_tests", "effect": "deny",
                "description": "tests disabled by policy"}])
    result = asyncio.run(verify_changes(
        tmp_path, ["calc.py"], permissions=engine))
    assert not result.passed
    assert "not permitted" in result.summary()
    assert not (tmp_path / "pwned.txt").exists()


def test_verify_passes_under_a_real_engine_noninteractively(home, tmp_path):
    # Review round 3, R2-1 (the reviewer's end-to-end repro): round
    # 2's verify rewrote its commands to the ABSOLUTE sys.executable
    # path, which classify_command rates "unknown" -> ASK -> denied
    # in a non-interactive run, so a plain `daemon demo` ended with
    # "verification FAILED ... not permitted: unrecognized command"
    # on a CORRECT edit, plus a spurious repair loop. The broker now
    # classifies the detected command's logical form while the
    # normalized argv executes. This engine is the real default:
    # no approver, no assume_yes - exactly the shipped demo path.
    from daemon.permissions import PermissionEngine

    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n"
        "    assert add(2, 3) == 5\n")
    engine = PermissionEngine(tmp_path)
    result = asyncio.run(verify_changes(
        tmp_path, ["calc.py"], permissions=engine))
    assert result.passed, result.summary()
    assert "not permitted" not in result.summary()
    assert "unrecognized" not in result.summary()


# ------------------------------------------- R3-1: tool shadowing


def _plant(tmp_path, name):
    """A fake tool module: if it ever executes as __main__, it
    leaves a marker in its cwd and exits 0 (a minted green run)."""
    (tmp_path / name).write_text(
        "from pathlib import Path\n"
        "Path('pwned.txt').write_text('plant ran')\n")


def test_verify_planted_pytest_module_fails_closed(tmp_path):
    # Review round 4, R3-1 (the reviewer's repro): a workspace with
    # a pyproject.toml and NO tests at all, plus a planted pytest.py.
    # `python -m pytest` resolves the module from the workspace
    # (cwd is first on sys.path), so the plant used to execute under
    # an ALLOW naming the real pytest and verification reported
    # success having run zero tests. Now the check fails closed,
    # the shadowing file is named, and the plant never runs.
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "calc.py").write_text("x = 1\n")
    _plant(tmp_path, "pytest.py")
    result = asyncio.run(verify_changes(tmp_path, ["calc.py"]))
    assert not result.passed
    assert "verification tool shadowed by workspace file" \
        in result.summary()
    assert "pytest.py" in result.summary()
    assert not (tmp_path / "pwned.txt").exists()


def test_verify_planted_compileall_module_fails_closed(tmp_path):
    # compileall is stdlib, but `python -m compileall` resolves it
    # from the workspace exactly the same way - a planted
    # compileall.py shadows it (R3-1).
    (tmp_path / "calc.py").write_text("x = 1\n")
    _plant(tmp_path, "compileall.py")
    result = asyncio.run(verify_changes(
        tmp_path, ["calc.py"], run_tests=False))
    assert not result.passed
    assert "verification tool shadowed by workspace file" \
        in result.summary()
    assert "compileall.py" in result.summary()
    assert not (tmp_path / "pwned.txt").exists()


def test_verify_planted_ruff_module_fails_closed(tmp_path, monkeypatch):
    # The module-only substitution path (R3-1's second repro): ruff
    # importable but no binary on PATH, so the classified
    # `ruff check calc.py` executes as `python -m ruff ...` - and
    # the planted ruff.py used to run and report lint "passed".
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='x'\n[tool.ruff]\nline-length = 88\n")
    (tmp_path / "calc.py").write_text("x = 1\n")
    _plant(tmp_path, "ruff.py")
    result = asyncio.run(verify_changes(
        tmp_path, ["calc.py"], run_tests=False, run_typecheck=False))
    assert not result.passed
    assert "verification tool shadowed by workspace file" \
        in result.summary()
    assert "ruff.py" in result.summary()
    assert not (tmp_path / "pwned.txt").exists()


def test_verify_root_package_import_is_not_a_shadow(tmp_path):
    # No false positive (R3-1 acceptance): the project's OWN package
    # lives at the workspace root and its tests import it. pytest
    # itself still resolves from the environment - only the tool
    # module's origin is guarded - so verification runs green.
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    pkg = tmp_path / "mypkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "def add(a, b):\n    return a + b\n")
    (tmp_path / "calc.py").write_text("x = 1\n")
    (tmp_path / "test_mypkg.py").write_text(
        "from mypkg import add\n\n\ndef test_add():\n"
        "    assert add(2, 3) == 5\n")
    result = asyncio.run(verify_changes(tmp_path, ["calc.py"]))
    assert result.passed, result.summary()


def test_run_tests_tool_refuses_planted_pytest(tmp_path):
    # R3-1, second surface: the run_tests tool also executes
    # `python -m pytest` with the workspace as cwd, so the same
    # guard applies there - the plant never runs and the refusal
    # names the shadowing file.
    from daemon.tools.base import ToolContext
    from daemon.tools.codeintel import RunTestsTool

    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    _plant(tmp_path, "pytest.py")
    out = asyncio.run(RunTestsTool().run({}, ToolContext(tmp_path)))
    assert out.is_error
    assert "verification tool shadowed by workspace file" in out.content
    assert "pytest.py" in out.content
    assert not (tmp_path / "pwned.txt").exists()


# ------------------------------------------- R4-1: probe blinding


def _plant_json_shadow(tmp_path):
    """A json.py whose dumps() reports 'no candidate paths' - the
    round-5 reviewer's blinding plant for the round-4 probe, whose
    interpreter was started inside the workspace and imported json
    from it."""
    (tmp_path / "json.py").write_text(
        "def dumps(obj, **kwargs):\n    return '[]'\n")


def test_verify_json_shadow_cannot_blind_the_guard(tmp_path):
    # Review round 5, R4-1 (the reviewer's combined repro): the
    # round-3 pytest.py plant PLUS a json.py that made the origin
    # probe print [] - the guard concluded "no resolution inside
    # the workspace" and the plant executed to a green verification.
    # Detection is now plain pathlib in the daemon process and the
    # probe runs isolated (python -I), so the combined plant fails
    # closed with the shadowing named and never runs.
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "calc.py").write_text("x = 1\n")
    _plant(tmp_path, "pytest.py")
    _plant_json_shadow(tmp_path)
    result = asyncio.run(verify_changes(tmp_path, ["calc.py"]))
    assert not result.passed
    assert "verification tool shadowed by workspace file" \
        in result.summary()
    assert "pytest.py" in result.summary()
    assert not (tmp_path / "pwned.txt").exists()


def test_run_tests_tool_json_shadow_cannot_blind_the_guard(tmp_path):
    # R4-1, second surface: same combined plant against run_tests.
    from daemon.tools.base import ToolContext
    from daemon.tools.codeintel import RunTestsTool

    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    _plant(tmp_path, "pytest.py")
    _plant_json_shadow(tmp_path)
    out = asyncio.run(RunTestsTool().run({}, ToolContext(tmp_path)))
    assert out.is_error
    assert "verification tool shadowed by workspace file" in out.content
    assert "pytest.py" in out.content
    assert not (tmp_path / "pwned.txt").exists()


def test_json_shadow_alone_does_not_block_verification(tmp_path):
    # R4-1 acceptance, the other direction: a json.py with NO tool
    # plant shadows the stdlib for the PROJECT's own code - the
    # project's problem, not the guard's. The guard only cares
    # about the tool modules, and its isolated probe imports the
    # real json regardless, so every tool module still confirms a
    # clean origin and legitimate verification proceeds.
    import sys

    from daemon.agent.verify import tool_module_block

    (tmp_path / "calc.py").write_text("x = 1\n")
    _plant_json_shadow(tmp_path)
    for module in ("pytest", "ruff", "mypy", "compileall"):
        assert tool_module_block(sys.executable, module, tmp_path) \
            is None, module
    result = asyncio.run(verify_changes(
        tmp_path, ["calc.py"], run_tests=False, run_lint=False,
        run_typecheck=False))
    assert result.passed, result.summary()


def test_tool_module_block_covers_every_shadow_shape(tmp_path):
    # R4-1: detection must not depend on the probe seeing the
    # plant. Each guarded module, in each shape `python -m` would
    # resolve from the cwd - module file, sourceless bytecode, a
    # regular package dir, a namespace package dir - is blocked
    # and named, checked directly against the guard.
    import sys

    from daemon.agent.verify import tool_module_block

    for module in ("pytest", "ruff", "mypy", "compileall"):
        for shape in ("file", "pyc", "package", "namespace"):
            ws = tmp_path / f"{module}-{shape}"
            ws.mkdir()
            if shape == "file":
                (ws / f"{module}.py").write_text("x = 1\n")
                named = f"{module}.py"
            elif shape == "pyc":
                (ws / f"{module}.pyc").write_bytes(b"\x00\x01")
                named = f"{module}.pyc"
            elif shape == "package":
                pkg = ws / module
                pkg.mkdir()
                (pkg / "__init__.py").write_text("x = 1\n")
                named = "__init__.py"
            else:
                (ws / module).mkdir()
                named = module
            block = tool_module_block(sys.executable, module, ws)
            assert block is not None, (module, shape)
            assert "verification tool shadowed by workspace file" \
                in block
            assert named in block, (module, shape, block)
