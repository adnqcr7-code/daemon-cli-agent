"""Filesystem tools: read/write/edit/search/glob/tree + containment."""

import asyncio

from daemon.checkpoints import CheckpointManager
from daemon.tools import ToolContext, ToolRegistry
from daemon.tools.fs import register_filesystem_tools


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_filesystem_tools(registry)
    return registry


def _ctx(workspace) -> ToolContext:
    return ToolContext(workspace, checkpoints=CheckpointManager(workspace))


def test_read_write_edit_roundtrip(workspace):
    async def go():
        registry, ctx = _registry(), _ctx(workspace)
        await registry.execute("write_file", {
            "path": "src/deep/mod.py", "content": "x = 1\n"}, ctx)
        out = await registry.execute("read_file", {"path": "src/deep/mod.py"}, ctx)
        assert "x = 1" in out.content
        out = await registry.execute("edit_file", {
            "path": "src/deep/mod.py", "old_text": "x = 1",
            "new_text": "x = 2"}, ctx)
        assert not out.is_error
        assert (workspace / "src/deep/mod.py").read_text() == "x = 2\n"

    asyncio.run(go())


def test_edit_requires_unique_match(workspace):
    async def go():
        registry, ctx = _registry(), _ctx(workspace)
        (workspace / "dup.txt").write_text("same\nsame\n")
        out = await registry.execute("edit_file", {
            "path": "dup.txt", "old_text": "same", "new_text": "other"}, ctx)
        assert out.is_error
        assert "2 times" in out.content or "occurs 2" in out.content
        out = await registry.execute("edit_file", {
            "path": "dup.txt", "old_text": "same", "new_text": "other",
            "replace_all": True}, ctx)
        assert not out.is_error
        assert (workspace / "dup.txt").read_text() == "other\nother\n"

    asyncio.run(go())


def test_edit_failure_gives_helpful_hint(workspace):
    async def go():
        registry, ctx = _registry(), _ctx(workspace)
        (workspace / "code.py").write_text("    return a + b\n")
        out = await registry.execute("edit_file", {
            "path": "code.py", "old_text": "retunr a + b",  # typo, no match
            "new_text": "x"}, ctx)
        assert out.is_error
        assert "whitespace" in out.content or "similar line" in out.content

    asyncio.run(go())


def test_search_and_glob(workspace):
    async def go():
        registry, ctx = _registry(), _ctx(workspace)
        out = await registry.execute("search_files", {"pattern": "return"}, ctx)
        assert "bug.py:2" in out.content
        out = await registry.execute("glob", {"pattern": "**/*.toml"}, ctx)
        assert "pyproject.toml" in out.content

    asyncio.run(go())


def test_tree_listing_skips_noise(workspace):
    async def go():
        registry, ctx = _registry(), _ctx(workspace)
        (workspace / "node_modules").mkdir()
        (workspace / "node_modules" / "junk.js").write_text("x")
        out = await registry.execute("list_dir", {"depth": 3}, ctx)
        assert "node_modules" not in out.content
        assert "bug.py" in out.content

    asyncio.run(go())


def test_binary_files_rejected(workspace):
    async def go():
        registry, ctx = _registry(), _ctx(workspace)
        (workspace / "blob.bin").write_bytes(b"\x00\x01\x02binary")
        out = await registry.execute("read_file", {"path": "blob.bin"}, ctx)
        assert out.is_error
        assert "binary" in out.content.lower()

    asyncio.run(go())


def test_traversal_blocked(workspace):
    async def go():
        registry, ctx = _registry(), _ctx(workspace)
        for tool, args in (
                ("read_file", {"path": "../../etc/passwd"}),
                ("write_file", {"path": "/etc/passwd", "content": "x"}),
                ("delete_path", {"path": "../outside"}),
                ("glob", {"pattern": "../../**"}),
        ):
            out = await registry.execute(tool, args, ctx)
            assert out.is_error, tool

    asyncio.run(go())


def test_delete_creates_checkpoint(workspace):
    async def go():
        registry, ctx = _registry(), _ctx(workspace)
        (workspace / "killme.txt").write_text("important")
        out = await registry.execute("delete_path", {"path": "killme.txt"}, ctx)
        assert not out.is_error
        assert not (workspace / "killme.txt").exists()
        latest = ctx.checkpoints.latest()
        assert latest and "killme.txt" in latest.files

    asyncio.run(go())


def test_apply_patch_tool(workspace):
    async def go():
        registry, ctx = _registry(), _ctx(workspace)
        original = (workspace / "bug.py").read_text()
        from daemon.tools.patches import unified_diff_text

        new = original.replace("a - b", "a + b")
        diff = unified_diff_text(original, new, "bug.py")
        out = await registry.execute("apply_patch", {
            "path": "bug.py", "diff": diff}, ctx)
        assert not out.is_error
        assert "a + b" in (workspace / "bug.py").read_text()

    asyncio.run(go())


def test_make_diff_is_readonly(workspace):
    async def go():
        registry, ctx = _registry(), _ctx(workspace)
        out = await registry.execute("make_diff", {
            "path": "bug.py", "new_content": "totally new"}, ctx)
        assert "-    return a - b" in out.content
        assert (workspace / "bug.py").read_text().count("a - b") == 1

    asyncio.run(go())


def test_file_info(workspace):
    async def go():
        registry, ctx = _registry(), _ctx(workspace)
        out = await registry.execute("file_info", {"path": "bug.py"}, ctx)
        assert "lines: 2" in out.content
        assert "python file" not in out.content  # sanity

    asyncio.run(go())


def test_read_offset_limit(workspace):
    async def go():
        registry, ctx = _registry(), _ctx(workspace)
        (workspace / "big.txt").write_text("\n".join(f"line{i}" for i in range(100)))
        out = await registry.execute("read_file", {
            "path": "big.txt", "offset": 10, "limit": 5}, ctx)
        assert "line10" in out.content and "line14" in out.content
        assert "line15" not in out.content
        assert "of 100" in out.content

    asyncio.run(go())


def test_validation_rejects_missing_required(workspace):
    async def go():
        registry, ctx = _registry(), _ctx(workspace)
        out = await registry.execute("read_file", {}, ctx)
        assert out.is_error
        assert "missing required parameter" in out.content

    asyncio.run(go())
