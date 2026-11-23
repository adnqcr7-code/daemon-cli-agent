"""Worktrees: per-task git worktree isolation with real git.

These tests use real repositories (the ``workspace`` fixture is a git
repo with one commit; the others are built in ``tmp_path``) - git itself
is the oracle, no mocks.
"""

import json
import subprocess
from pathlib import Path

import pytest

from daemon.worktree import (
    WorktreeError,
    WorktreeInfo,
    WorktreeManager,
    sanitize_slug,
)


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=check)


def make_repo(path: Path, *, commit: bool = True) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")
    (path / "bug.py").write_text("def add(a, b):\n    return a - b\n",
                                 encoding="utf-8")
    if commit:
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", "initial")
    return path


def branch_exists(repo: Path, branch: str) -> bool:
    result = git(repo, "rev-parse", "--verify", f"refs/heads/{branch}",
                 check=False)
    return result.returncode == 0


def test_create_returns_existing_path_on_daemon_branch(workspace):
    manager = WorktreeManager()
    info = manager.create(workspace, "fix-bug")

    assert info.path.exists() and info.path.is_dir()
    assert info.path.parent == workspace / ".daemon" / "worktrees"
    assert info.slug == "fix-bug"
    assert info.branch.startswith("daemon/fix-bug-")
    assert info.path.name.startswith("fix-bug-")
    assert len(info.path.name.rsplit("-", 1)[-1]) == 6
    assert info.base_ref  # resolved HEAD commit

    current = git(info.path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert current == info.branch
    assert (info.path / "bug.py").exists()


def test_edit_inside_worktree_leaves_main_checkout_untouched(workspace):
    manager = WorktreeManager()
    info = manager.create(workspace, "isolate-me")
    before = (workspace / "bug.py").read_text(encoding="utf-8")

    (info.path / "bug.py").write_text("def add(a, b):\n    return a + b\n",
                                      encoding="utf-8")
    (info.path / "new-file.txt").write_text("agent work", encoding="utf-8")

    assert (workspace / "bug.py").read_text(encoding="utf-8") == before
    assert not (workspace / "new-file.txt").exists()
    assert "return a + b" in (info.path / "bug.py").read_text(encoding="utf-8")


def test_create_keeps_daemon_out_of_main_status(workspace):
    manager = WorktreeManager()
    manager.create(workspace, "clean-status")

    exclude = (workspace / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert ".daemon/worktrees/" in exclude
    assert not (workspace / ".gitignore").exists()  # never edited/created

    status = git(workspace, "status", "--porcelain").stdout
    assert ".daemon" not in status


def test_list_finds_created_worktree(workspace):
    manager = WorktreeManager()
    assert manager.list_worktrees(workspace) == []

    info = manager.create(workspace, "list-me")
    found = manager.list_worktrees(workspace)

    assert len(found) == 1
    assert found[0].path == info.path
    assert found[0].branch == info.branch
    assert found[0].slug == "list-me"


def test_registry_is_persisted(workspace):
    manager = WorktreeManager()
    info = manager.create(workspace, "registry")

    registry = workspace / ".daemon" / "worktrees.json"
    rows = json.loads(registry.read_text(encoding="utf-8"))
    assert len(rows) == 1
    assert rows[0]["branch"] == info.branch
    assert rows[0]["path"] == str(info.path)


def test_status_reports_changes(workspace):
    manager = WorktreeManager()
    info = manager.create(workspace, "status-check")

    clean = manager.status(workspace, info)
    assert clean["changed_files"] == 0
    assert clean["status_lines"] == []

    (info.path / "bug.py").write_text("def add(a, b):\n    return a + b\n",
                                      encoding="utf-8")
    (info.path / "extra.txt").write_text("new", encoding="utf-8")

    report = manager.status(workspace, info)
    assert report["branch"] == info.branch
    assert report["changed_files"] == 2
    assert any("bug.py" in line for line in report["status_lines"])
    assert any("extra.txt" in line for line in report["status_lines"])
    assert "bug.py" in report["diff_stat"]


def test_remove_cleans_up_path_and_registry_and_keeps_branch(workspace):
    manager = WorktreeManager()
    info = manager.create(workspace, "remove-me")

    assert manager.remove(workspace, info) is True

    assert not info.path.exists()
    assert manager.list_worktrees(workspace) == []
    rows = json.loads(
        (workspace / ".daemon" / "worktrees.json").read_text(encoding="utf-8"))
    assert rows == []
    # Without force the branch is kept so the work stays reachable.
    assert branch_exists(workspace, info.branch)
    # Removing again reports there was nothing to remove.
    assert manager.remove(workspace, info) is False


def test_remove_by_path(workspace):
    manager = WorktreeManager()
    info = manager.create(workspace, "remove-by-path")
    assert manager.remove(workspace, info.path) is True
    assert not info.path.exists()


def test_remove_force_deletes_branch_and_dirty_worktree(workspace):
    manager = WorktreeManager()
    info = manager.create(workspace, "force-remove")
    (info.path / "bug.py").write_text("dirty change", encoding="utf-8")

    assert manager.remove(workspace, info, force=True) is True
    assert not info.path.exists()
    assert not branch_exists(workspace, info.branch)


def test_remove_dirty_worktree_without_force_raises(workspace):
    manager = WorktreeManager()
    info = manager.create(workspace, "dirty")
    (info.path / "bug.py").write_text("dirty change", encoding="utf-8")

    with pytest.raises(WorktreeError):
        manager.remove(workspace, info)
    assert info.path.exists()  # still there for the user to decide on


def test_create_in_non_git_dir_raises(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    manager = WorktreeManager()

    assert manager.is_git_repo(plain) is False
    with pytest.raises(WorktreeError, match="Not a git repository"):
        manager.create(plain, "anything")


def test_is_git_repo_true_for_repo(workspace):
    assert WorktreeManager().is_git_repo(workspace) is True


def test_create_in_repo_without_commits_raises(tmp_path):
    repo = make_repo(tmp_path / "empty", commit=False)
    with pytest.raises(WorktreeError, match="no commits"):
        WorktreeManager().create(repo, "too-early")


def test_create_with_explicit_base_ref(workspace):
    head = git(workspace, "rev-parse", "HEAD").stdout.strip()
    info = WorktreeManager().create(workspace, "from-head", base_ref="HEAD")
    assert info.base_ref == "HEAD"
    assert git(info.path, "rev-parse", "HEAD").stdout.strip() == head


def test_create_with_unknown_base_ref_raises(workspace):
    with pytest.raises(WorktreeError):
        WorktreeManager().create(workspace, "bad-ref", base_ref="no-such-ref")


def test_slug_sanitization():
    assert sanitize_slug("Fix Bug!! Now") == "fix-bug-now"
    assert sanitize_slug("  Fix: BIG Bug (urgent)  ") == "fix-big-bug-urgent"
    assert sanitize_slug("already-good-123") == "already-good-123"
    assert sanitize_slug("___") == "task"
    assert sanitize_slug("") == "task"
    long_slug = sanitize_slug("a" * 100)
    assert len(long_slug) <= 32


def test_create_sanitizes_slug(workspace):
    info = WorktreeManager().create(workspace, "  Fix: BIG Bug!! (urgent) ")
    assert info.slug == "fix-big-bug-urgent"
    assert info.branch == f"daemon/fix-big-bug-urgent-{info.path.name.rsplit('-', 1)[-1]}"
    assert info.path.exists()


def test_worktree_info_json_round_trip():
    info = WorktreeInfo(path=Path("/tmp/wt"), branch="daemon/x-abc123",
                        base_ref="HEAD", slug="x")
    assert WorktreeInfo.from_json(info.to_json()) == info


def test_prune_drops_dead_registry_entries(workspace):
    manager = WorktreeManager()
    info = manager.create(workspace, "prune-me")
    assert manager.remove(workspace, info, force=True) is True
    manager.prune(workspace)
    assert manager.list_worktrees(workspace) == []
