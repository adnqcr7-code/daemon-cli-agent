
from pathlib import Path

from daemon.diffs import preview_edit_diff, truncate_diff


def main():
    ws = Path.cwd()
    big = ws / "big.py"
    before = big.read_bytes()
    new = "".join(f"new_value_{i} = {i}\n" for i in range(300))
    preview = preview_edit_diff(ws, "write_file",
                                {"path": "big.py", "content": new})
    assert preview is not None, "no preview computed"
    truncated = truncate_diff(preview)
    assert "more lines" in truncated, truncated[:200]
    assert "new_value_299" not in truncated, "tail leaked past the budget"
    assert big.read_bytes() == before, "preview mutated the file"
    print("diff preview truncated to budget with marker; file untouched")


main()
