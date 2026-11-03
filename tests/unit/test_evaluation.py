import json

import pytest

from daemon.evaluation import (
    _check_files,
    _load_tasks,
    _parse_target,
    _verify_command,
)


def test_target_accepts_profile_and_active_profile():
    assert _parse_target("anthropic#work=claude-sonnet") == (
        "anthropic#work", "claude-sonnet")
    assert _parse_target("=gemma4:e4b") == (None, "gemma4:e4b")


def test_target_requires_model_separator():
    with pytest.raises(ValueError, match="PROFILE=MODEL_ID"):
        _parse_target("claude-sonnet")


def test_task_file_must_have_tasks(tmp_path):
    task_file = tmp_path / "tasks.json"
    task_file.write_text(json.dumps({"tasks": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="non-empty"):
        _load_tasks(task_file)


def test_file_check_rejects_paths_outside_workspace(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("should not be read", encoding="utf-8")
    checks = _check_files(tmp_path, {"../outside.txt": "should not be read"})
    assert checks == [{
        "path": "../outside.txt",
        "passed": False,
        "expected": "should not be read",
    }]


def test_verification_uses_the_current_python(tmp_path):
    result = _verify_command(tmp_path, ["{python}", "-c", "print('verified')"])
    assert result is not None
    assert result["passed"]
    assert "verified" in result["output"]
