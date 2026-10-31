"""Patch engine: parse, apply, fuzzy match, failure reporting."""

import pytest

from daemon.errors import ToolError
from daemon.tools.patches import (
    apply_hunks,
    parse_unified_diff,
    unified_diff_text,
)

SAMPLE = (
    "import os\n"
    "\n"
    "\n"
    "def add(a, b):\n"
    "    return a - b\n"
    "\n"
    "\n"
    "def mul(a, b):\n"
    "    return a * b\n"
)


def _diff(old: str, new: str, path: str = "file.py") -> str:
    return unified_diff_text(old, new, path)


def test_roundtrip_single_hunk():
    new = SAMPLE.replace("a - b", "a + b")
    diff = _diff(SAMPLE, new)
    _, hunks = parse_unified_diff(diff)
    assert len(hunks) == 1
    assert apply_hunks(SAMPLE, hunks) == new


def test_roundtrip_multi_hunk():
    spaced = SAMPLE.replace("a * b", "a * b\n" + "\n" * 10 + "def tail():\n    return None\n")
    new = spaced.replace("a - b", "a + b").replace("return None", "return 42")
    diff = _diff(spaced, new)
    _, hunks = parse_unified_diff(diff)
    assert len(hunks) >= 2
    assert apply_hunks(spaced, hunks) == new


def test_path_hint_parsed():
    diff = _diff(SAMPLE, SAMPLE.replace("a - b", "a + b"))
    path, _ = parse_unified_diff(diff)
    assert path == "file.py"


def test_fuzzy_matching_when_line_numbers_drift():
    new = SAMPLE.replace("a - b", "a + b")
    diff = _diff(SAMPLE, new)
    shifted = diff.replace("@@ -1,9 +1,9 @@", "@@ -3,9 +3,9 @@")
    _, hunks = parse_unified_diff(shifted)
    assert apply_hunks(SAMPLE, hunks) == new


def test_rejects_nonmatching_hunk():
    new = SAMPLE.replace("a - b", "a + b")
    diff = _diff(SAMPLE, new)
    _, hunks = parse_unified_diff(diff)
    tampered = SAMPLE.replace("a - b", "a / b")
    with pytest.raises(ToolError):
        apply_hunks(tampered, hunks, fuzzy=False)
    with pytest.raises(ToolError):  # even fuzzy cannot find it
        apply_hunks(tampered, hunks)


def test_addition_at_end_of_file():
    old = "line1\nline2\n"
    new = "line1\nline2\nline3\n"
    _, hunks = parse_unified_diff(_diff(old, new))
    assert apply_hunks(old, hunks) == new


def test_deletion_of_file_content():
    old = "keep\nremove me\nkeep2\n"
    new = "keep\nkeep2\n"
    _, hunks = parse_unified_diff(_diff(old, new))
    assert apply_hunks(old, hunks) == new


def test_empty_diff_has_no_hunks():
    assert parse_unified_diff("")[1] == []


def test_git_style_headers_are_tolerated():
    diff = ("diff --git a/x.py b/x.py\n"
            "index 123..456 100644\n"
            "--- a/x.py\n"
            "+++ b/x.py\n"
            "@@ -1,3 +1,3 @@\n"
            " import os\n"
            "-x = 1\n"
            "+x = 2\n")
    path, hunks = parse_unified_diff(diff)
    assert path == "x.py"
    assert len(hunks) == 1
    assert apply_hunks("import os\nx = 1\n", hunks) == "import os\nx = 2\n"
