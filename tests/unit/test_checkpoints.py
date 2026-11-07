"""Checkpoints: create, restore, diff, prune."""

import asyncio

from daemon.checkpoints import CheckpointManager
from daemon.errors import CheckpointError


def test_create_and_restore(workspace):
    async def go():
        manager = CheckpointManager(workspace)
        (workspace / "f.txt").write_text("before")
        checkpoint = await manager.create([workspace / "f.txt"])
        (workspace / "f.txt").write_text("after")
        restored = await manager.restore(checkpoint.id)
        assert restored == 1
        assert (workspace / "f.txt").read_text() == "before"

    asyncio.run(go())


def test_latest_and_diff(workspace):
    async def go():
        manager = CheckpointManager(workspace)
        (workspace / "a.txt").write_text("one")
        await manager.create([workspace / "a.txt"])
        (workspace / "a.txt").write_text("two")
        diffs = manager.diff("latest")
        assert diffs and diffs[0][0] == "a.txt"
        assert "-one" in diffs[0][1] and "+two" in diffs[0][1]

    asyncio.run(go())


def test_deleted_file_shows_in_diff(workspace):
    async def go():
        manager = CheckpointManager(workspace)
        (workspace / "gone.txt").write_text("content\n")
        await manager.create([workspace / "gone.txt"])
        (workspace / "gone.txt").unlink()
        diffs = manager.diff("latest")
        assert any("-content" in body for _, body in diffs)

    asyncio.run(go())


def test_nested_paths_preserved(workspace):
    async def go():
        manager = CheckpointManager(workspace)
        nested = workspace / "src" / "deep"
        nested.mkdir(parents=True)
        (nested / "mod.py").write_text("x = 1")
        await manager.create([nested / "mod.py"])
        (nested / "mod.py").write_text("x = 2")
        await manager.restore("latest")
        assert (nested / "mod.py").read_text() == "x = 1"

    asyncio.run(go())


def test_missing_checkpoint_raises(workspace):
    async def go():
        manager = CheckpointManager(workspace)
        try:
            await manager.restore("nope")
        except CheckpointError:
            return True
        return False

    assert asyncio.run(go())


def test_prune_keeps_window(workspace):
    async def go():
        manager = CheckpointManager(workspace, keep=2)
        (workspace / "p.txt").write_text("v")
        for index in range(5):
            (workspace / "p.txt").write_text(f"v{index}")
            await manager.create([workspace / "p.txt"])
        checkpoints = manager.list()
        assert len(checkpoints) <= 4  # keep*2 window

    asyncio.run(go())


def test_undo_deletes_files_created_after_checkpoint(workspace):
    """Regression: /undo used to restore edits but leave created files."""
    async def go():
        manager = CheckpointManager(workspace)
        new_file = workspace / "brand-new.txt"
        checkpoint = await manager.create([], created=[new_file])
        new_file.write_text("created by the agent")
        report = await manager.restore_report(checkpoint.id)
        assert report.restored == 0
        assert report.deleted == 1
        assert report.deleted_paths == ["brand-new.txt"]
        assert not new_file.exists()

    asyncio.run(go())


def test_undo_restores_edited_and_deletes_created(workspace):
    async def go():
        manager = CheckpointManager(workspace)
        (workspace / "old.txt").write_text("before")
        new_file = workspace / "new.txt"
        checkpoint = await manager.create(
            [workspace / "old.txt"], created=[new_file])
        (workspace / "old.txt").write_text("after")
        new_file.write_text("created")
        restored = await manager.restore(checkpoint.id)
        assert restored == 1
        assert (workspace / "old.txt").read_text() == "before"
        assert not new_file.exists()

    asyncio.run(go())


def test_created_file_outside_workspace_is_never_tracked(workspace, tmp_path):
    async def go():
        manager = CheckpointManager(workspace)
        (workspace / "inside.txt").write_text("v")
        outside = tmp_path / "outside.txt"
        checkpoint = await manager.create(
            [workspace / "inside.txt"], created=[outside])
        outside.write_text("do not touch")
        report = await manager.restore_report(checkpoint.id)
        assert report.deleted == 0
        assert outside.read_text() == "do not touch"

    asyncio.run(go())


def test_latest_lists_created_files(workspace):
    async def go():
        manager = CheckpointManager(workspace)
        new_file = workspace / "listed.txt"
        await manager.create([], created=[new_file])
        latest = manager.latest()
        assert latest is not None
        assert latest.created_files == ["listed.txt"]
        assert "created.json" not in latest.files

    asyncio.run(go())
