"""Ranked repo map: the most relevant files for a task, in order.

Aider-style, but deterministic and explainable: no model calls, every point
in a file's score comes with a human-readable reason. The map builds on the
existing :class:`~daemon.project.ProjectIndex` for the file list and on
:func:`~daemon.tools.codeintel.extract_symbols` for symbol names, so it
stays consistent with what the rest of Daemon already knows about a repo.

Ranking signals (see ``RepoMap.rank``): task keywords matching path
components or symbol names, entrypoints, import centrality (how many other
files import this one), recent git history, and explicit mentions, with
small penalties for test files and tiny config/data files unless the task
or the caller asks for them.
"""

from __future__ import annotations

import ast
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from ..project import ProjectIndex
from ..tools.codeintel import extract_symbols

# Suffixes extract_symbols can say something about (Python AST + the
# heuristic declaration scanner in codeintel).
_CODE_SUFFIXES = {
    ".py", ".js", ".jsx", ".mjs", ".ts", ".tsx", ".go", ".rs", ".java",
    ".cs", ".c", ".h", ".cpp", ".cc", ".hpp", ".rb", ".php", ".kt", ".swift",
}

_CONFIG_SUFFIXES = {".json", ".toml", ".yaml", ".yml", ".md"}

_WORD = re.compile(r"[a-z0-9_]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

# import ... from "x" / require("x") / import("x") / Go: import "x"
_JS_IMPORT = re.compile(r"""(?:from|require\(|import\(?)\s*["']([^"']+)["']""")
_GO_IMPORT = re.compile(r"""import\s+(?:\w+\s+)?["']([^"']+)["']""")

_MENTIONED_BOOST = 100.0
_ENTRYPOINT_POINTS = 5.0
_PATH_MATCH_POINTS = 4.0
_SYMBOL_MATCH_POINTS = 6.0
_INBOUND_POINTS = 3.0
_GIT_RECENT_POINTS = 2.0
_TEST_PENALTY = 3.0
_CONFIG_PENALTY = 2.0


@dataclass
class RankedFile:
    """One file in a ranked repo map, with the reasons for its score."""

    path: str
    score: float
    reasons: list[str] = field(default_factory=list)
    symbols: list[dict] = field(default_factory=list)


def _terms(text: str) -> set[str]:
    """Keyword terms from free text: words, ``snake_case`` parts, camel parts."""
    terms: set[str] = set()
    for token in _WORD.findall((text or "").lower()):
        if len(token) < 3:
            continue
        terms.add(token)
        terms.update(part for part in token.split("_") if len(part) >= 3)
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_]*", text or ""):
        for part in _CAMEL.split(token):
            lowered = part.lower()
            if len(lowered) >= 3:
                terms.add(lowered)
    return terms


def _name_terms(name: str) -> set[str]:
    """Terms for a path component or symbol name (its last dotted part too)."""
    terms = _terms(name)
    leaf = name.rsplit(".", 1)[-1]
    terms |= _terms(leaf)
    terms.add(leaf.lower())
    return terms


def _is_test_file(rel: str) -> bool:
    base = rel.rsplit("/", 1)[-1].lower()
    if base.startswith("test_") or base.endswith("_test.go"):
        return True
    if base.endswith(("_test.py", "_test.rs")):
        return True
    if any(marker in base for marker in (".test.", ".spec.")):
        return True
    parts = rel.lower().split("/")
    return "tests" in parts[:-1] or "test" in parts[:-1]


def _normalise_mentioned(mentioned: Iterable[str]) -> list[str]:
    out = []
    for item in mentioned or ():
        text = str(item).replace("\\", "/").strip()
        if text.startswith("./"):
            text = text[2:]
        if text:
            out.append(text)
    return out


def _is_mentioned(rel: str, mentioned: list[str]) -> bool:
    return any(rel == item or rel.endswith("/" + item) or item.endswith("/" + rel)
               for item in mentioned)


class RepoMap:
    """Ranks the files of a workspace for a task and renders a compact map."""

    def __init__(self, workspace: Path | str, *,
                 index: ProjectIndex | None = None) -> None:
        self.workspace = Path(workspace).resolve()
        self.index = index if index is not None else ProjectIndex(self.workspace)
        self.index.build()
        self._symbol_cache: dict[str, tuple[tuple[float, int], list[dict]]] = {}
        self._inbound_cache: dict[tuple, dict[str, int]] | None = None
        self._inbound_key: tuple | None = None
        self._git_recent_cache: set[str] | None = None

    # -------------------------------------------------------------- symbols

    def _symbols_for(self, rel: str) -> list[dict]:
        """Symbols of one file, cached by the index's (mtime, size)."""
        entry = (self.index.data.get("files") or {}).get(rel) or [0, 0]
        key = (entry[0], entry[1])
        cached = self._symbol_cache.get(rel)
        if cached is not None and cached[0] == key:
            return cached[1]
        symbols: list[dict] = []
        path = self.workspace / rel
        if path.suffix.lower() in _CODE_SUFFIXES and path.is_file():
            try:
                symbols, _method = extract_symbols(path)
            except Exception:  # noqa: BLE001 - a broken file has no symbols
                symbols = []
        self._symbol_cache[rel] = (key, symbols)
        return symbols

    # -------------------------------------------------------------- imports

    def _files_key(self) -> tuple:
        files = self.index.data.get("files") or {}
        return tuple(sorted((rel, entry[0], entry[1]) for rel, entry in files.items()))

    def _module_names(self, rel: str) -> set[str]:
        """Names other files might use to reference ``rel``."""
        stem_path = rel.rsplit(".", 1)[0]  # pkg/core, main, web/app
        names = {stem_path.replace("/", "."), stem_path.rsplit("/", 1)[-1]}
        if stem_path.endswith("/__init__"):
            package = stem_path[: -len("/__init__")]
            names.add(package.replace("/", "."))
            names.add(package.rsplit("/", 1)[-1])
        return {name for name in names if name}

    def _references_of(self, rel: str) -> set[str]:
        """Normalised import/reference strings found in one file."""
        path = self.workspace / rel
        suffix = path.suffix.lower()
        try:
            text = path.read_text("utf-8", errors="replace")
        except OSError:
            return set()
        refs: set[str] = set()
        if suffix == ".py":
            refs |= self._python_references(rel, text)
        elif suffix in {".js", ".jsx", ".mjs", ".ts", ".tsx"}:
            refs = {match.group(1) for match in _JS_IMPORT.finditer(text)}
        elif suffix == ".go":
            refs = {match.group(1) for match in _GO_IMPORT.finditer(text)}
        normalised: set[str] = set()
        for ref in refs:
            cleaned = ref.strip().lstrip("./")
            if cleaned:
                normalised.add(cleaned)
                normalised.add(cleaned.replace("/", "."))
                normalised.add(cleaned.rsplit("/", 1)[-1].rsplit(".", 1)[0])
                normalised.add(cleaned.rsplit(".", 1)[-1])
        return normalised

    @staticmethod
    def _python_references(rel: str, text: str) -> set[str]:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return set()
        package_parts = rel.split("/")[:-1]
        refs: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    refs.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:
                    base = package_parts[: len(package_parts) - node.level + 1] \
                        if node.level <= len(package_parts) else []
                    module = ".".join([*base, module] if module else base)
                if module:
                    refs.add(module)
                for alias in node.names:
                    refs.add(f"{module}.{alias.name}" if module else alias.name)
        return refs

    def _inbound_counts(self) -> dict[str, int]:
        """For each file, how many *other* files import/reference it."""
        key = self._files_key()
        if self._inbound_cache is not None and self._inbound_key == key:
            return self._inbound_cache
        files = sorted((self.index.data.get("files") or {}).keys())
        references = {rel: self._references_of(rel) for rel in files}
        counts: dict[str, int] = {}
        for rel in files:
            names = self._module_names(rel)
            counts[rel] = sum(
                1 for other in files
                if other != rel and names & references[other])
        self._inbound_cache = counts
        self._inbound_key = key
        return counts

    # ------------------------------------------------------------------ git

    def _git_recent(self) -> set[str]:
        """Files touched by the last 20 commits; empty outside a git repo."""
        if self._git_recent_cache is not None:
            return self._git_recent_cache
        recent: set[str] = set()
        try:
            result = subprocess.run(
                ["git", "log", "--name-only", "-20", "--pretty=format:"],
                cwd=self.workspace, capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                recent = {line.strip().replace("\\", "/")
                          for line in result.stdout.splitlines() if line.strip()}
        except Exception:  # noqa: BLE001 - git is optional here
            recent = set()
        self._git_recent_cache = recent
        return recent

    # --------------------------------------------------------------- ranking

    def rank(self, task: str = "", *, mentioned: Iterable[str] = (),
             limit: int = 40) -> list[RankedFile]:
        """Rank indexed files for ``task``, most relevant first.

        Deterministic: ties break on the relative path. Every point added
        or subtracted appends a human-readable string to ``reasons``.
        """
        files = self.index.data.get("files") or {}
        entrypoints = set(self.index.data.get("entrypoints") or [])
        task_terms = _terms(task)
        task_mentions_tests = "test" in (task or "").lower() or \
            "tests" in (task or "").lower()
        mentioned_list = _normalise_mentioned(mentioned)
        inbound = self._inbound_counts()
        recent = self._git_recent()

        ranked: list[RankedFile] = []
        for rel in sorted(files):
            score = 0.0
            reasons: list[str] = []
            symbols = self._symbols_for(rel)
            suffix = Path(rel).suffix.lower()
            mentioned_file = _is_mentioned(rel, mentioned_list)

            if mentioned_file:
                score += _MENTIONED_BOOST
                reasons.append("mentioned explicitly (+100)")

            path_terms: set[str] = set()
            for component in rel.rsplit(".", 1)[0].split("/"):
                path_terms |= _name_terms(component)
            for term in sorted(task_terms & path_terms):
                score += _PATH_MATCH_POINTS
                reasons.append(f"path matches task keyword '{term}' (+4)")

            matched_symbols: list[str] = []
            for symbol in symbols:
                name = str(symbol.get("name") or "")
                if name and task_terms & _name_terms(name):
                    matched_symbols.append(name)
            for name in sorted(matched_symbols):
                score += _SYMBOL_MATCH_POINTS
                reasons.append(f"defines symbol matching the task: {name} (+6)")

            if rel in entrypoints:
                score += _ENTRYPOINT_POINTS
                reasons.append("entrypoint (+5)")

            inbound_count = inbound.get(rel, 0)
            if inbound_count:
                points = inbound_count * _INBOUND_POINTS
                score += points
                reasons.append(
                    f"imported/referenced by {inbound_count} file(s) (+{points:g})")

            if rel in recent:
                score += _GIT_RECENT_POINTS
                reasons.append("recently changed in git history (+2)")

            if _is_test_file(rel) and not task_mentions_tests:
                score -= _TEST_PENALTY
                reasons.append("test file, task does not mention tests (-3)")
            if suffix in _CONFIG_SUFFIXES and not mentioned_file:
                score -= _CONFIG_PENALTY
                reasons.append("config/data file (deprioritised) (-2)")

            ranked.append(RankedFile(path=rel, score=score, reasons=reasons,
                                     symbols=symbols))
        ranked.sort(key=lambda item: (-item.score, item.path))
        return ranked[:limit] if limit is not None else ranked

    # -------------------------------------------------------------- rendering

    def render(self, task: str = "", *, token_budget: int = 1024,
               mentioned: Iterable[str] = ()) -> str:
        """Compact text map within roughly ``token_budget`` tokens.

        Budget accounting is ~4 chars/token. Entries are added in rank
        order until the budget is exhausted; the result never exceeds the
        budget by more than one file's entry (the first entry is always
        emitted, even if it alone is larger than the budget).
        """
        char_budget = max(0, int(token_budget) * 4)
        lines: list[str] = []
        used = 0
        for ranked_file in self.rank(task, mentioned=mentioned):
            entry_lines = [ranked_file.path]
            if Path(ranked_file.path).suffix.lower() in _CODE_SUFFIXES:
                for symbol in ranked_file.symbols:
                    name = str(symbol.get("name") or "")
                    if not name:
                        continue
                    signature = str(symbol.get("signature") or "")
                    if signature and not signature.startswith("("):
                        signature = f"({signature})"
                    entry_lines.append(f"  {name}{signature or '()'}")
            entry = "\n".join(entry_lines) + "\n"
            if lines and used + len(entry) > char_budget:
                break
            lines.append(entry)
            used += len(entry)
        return "".join(lines)
