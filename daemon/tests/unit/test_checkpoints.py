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
