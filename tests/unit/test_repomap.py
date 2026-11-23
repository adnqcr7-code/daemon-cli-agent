"""Tests for the ranked repo map (daemon.agent.repomap)."""

from pathlib import Path

from daemon.agent.repomap import RankedFile, RepoMap


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _workspace(tmp_path: Path) -> Path:
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/core.py",
           "def compute_total(a, b):\n    return a + b\n\n\n"
           "class Ledger:\n    def add(self, amount):\n        return amount\n")
    _write(tmp_path, "pkg/user_a.py",
           "from pkg.core import compute_total\n\n\n"
           "def run_a():\n    return compute_total(1, 2)\n")
    _write(tmp_path, "pkg/user_b.py",
           "import pkg.core\n\n\n"
           "def run_b():\n    return pkg.core.compute_total(3, 4)\n")
    _write(tmp_path, "pkg/other.py",
           "def unrelated_thing():\n    return 42\n")
    _write(tmp_path, "main.py",
           "from pkg.core import compute_total\n\n\n"
           "def main():\n    return compute_total(1, 2)\n")
    _write(tmp_path, "test_core.py",
           "from pkg.core import compute_total\n\n\n"
           "def test_compute_total():\n    assert compute_total(1, 2) == 3\n")
    return tmp_path


def _by_path(ranked: list[RankedFile]) -> dict[str, RankedFile]:
    return {item.path: item for item in ranked}


def test_ranking_puts_core_above_other_for_core_symbol_task(tmp_path):
    repo_map = RepoMap(_workspace(tmp_path))
    ranked = repo_map.rank("fix the compute_total calculation")
    by_path = _by_path(ranked)

    assert "pkg/core.py" in by_path
    assert "pkg/other.py" in by_path
    assert by_path["pkg/core.py"].score > by_path["pkg/other.py"].score
    paths = [item.path for item in ranked]
    assert paths.index("pkg/core.py") < paths.index("pkg/other.py")
    # The score is explainable: a symbol reason names the matched symbol.
    assert any("compute_total" in reason for reason in by_path["pkg/core.py"].reasons)
    # Import centrality: core is imported by user_a, user_b, main and the test.
    assert any("imported/referenced by" in reason
               for reason in by_path["pkg/core.py"].reasons)
    # Symbols come from extract_symbols.
    assert any(symbol["name"] == "compute_total"
               for symbol in by_path["pkg/core.py"].symbols)


def test_mentioned_file_ranks_first(tmp_path):
    repo_map = RepoMap(_workspace(tmp_path))
    ranked = repo_map.rank("fix the compute_total calculation",
                           mentioned=["pkg/other.py"])
    assert ranked[0].path == "pkg/other.py"
    assert any("mentioned" in reason for reason in ranked[0].reasons)


def test_entrypoint_gets_a_reason(tmp_path):
    repo_map = RepoMap(_workspace(tmp_path))
    by_path = _by_path(repo_map.rank("anything"))
    assert "main.py" in by_path
    assert any("entrypoint" in reason for reason in by_path["main.py"].reasons)


def test_render_respects_budget_and_includes_symbols(tmp_path):
    repo_map = RepoMap(_workspace(tmp_path))

    full = repo_map.render("fix compute_total", token_budget=1024)
    assert "pkg/core.py" in full
    assert "compute_total" in full

    small_budget_tokens = 20
    rendered = repo_map.render("fix compute_total",
                               token_budget=small_budget_tokens)
    assert rendered  # the first entry is always emitted
    assert "compute_total" in rendered
    # Never exceeds the budget by more than one file's entry: the first
    # entry alone is well under this bound, so the total stays bounded.
    char_budget = small_budget_tokens * 4
    first_entry_len = len(rendered.split("\n\n")[0]) + 1
    assert len(rendered) <= char_budget + first_entry_len


def test_works_when_not_a_git_repo(tmp_path):
    assert not (tmp_path / ".git").exists()
    repo_map = RepoMap(_workspace(tmp_path))
    ranked = repo_map.rank("compute_total")
    assert ranked
    assert all(isinstance(item, RankedFile) for item in ranked)
    assert repo_map.render("compute_total")
