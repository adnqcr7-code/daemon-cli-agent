"""Per-task git worktree isolation.

A :class:`WorktreeManager` gives one agent task its own checkout of the
repository: the worktree lives under ``<workspace>/.daemon/worktrees/
<slug>-<shortid>`` on a dedicated branch ``daemon/<slug>-<shortid>``, so
edits made for that task never touch the user's main checkout until the
work is deliberately merged or the worktree is thrown away.

All git interaction goes through ``subprocess`` with an argument list
(never a shell), and every git failure is raised as :class:`WorktreeError`
with the command's stderr in the message - never a raw traceback.

Keeping ``.daemon/`` out of the user's repository: creating a worktree
inside the main checkout would normally show up as an untracked
``.daemon/`` entry in ``git status``. We deliberately do NOT edit the
user's ``.gitignore`` to hide it (that file is theirs, and a committed
ignore rule would also be wrong for other clones). Instead the rules
``.daemon/worktrees/`` and ``.daemon/worktrees.json`` (the registry,
which sits beside the directory) are appended to ``.git/info/exclude`` -
git's local, uncommitted ignore file - when not already present there.
That keeps the main checkout clean on this machine only, which is exactly
the scope of the worktrees themselves.

A small registry at ``<workspace>/.daemon/worktrees.json`` records every
managed worktree on create/remove. ``list_worktrees`` primarily parses
``git worktree list --porcelain`` and only returns worktrees under
``.daemon/worktrees/``, but merges in registry entries that still exist
on disk so listing stays correct even if porcelain output is odd.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import project_dir
from .errors import DaemonError
from .log import audit, get_logger

_logger = get_logger("worktree")

_EXCLUDE_RULES = (".daemon/worktrees/", ".daemon/worktrees.json")
_SLUG_MAX = 32
_SLUG_CLEAN = re.compile(r"[^a-z0-9-]+")
_SLUG_DASHES = re.compile(r"-{2,}")
_SHORTID_SUFFIX = re.compile(r"-[0-9a-f]{6}$")


class WorktreeError(DaemonError):
    """A git worktree operation failed or is not possible here."""


def sanitize_slug(task_slug: str) -> str:
    """Reduce ``task_slug`` to ``[a-z0-9-]``, at most ~32 characters.

    Lowercases, maps every other character to ``-``, collapses repeated
    dashes and trims them from both ends. An empty result becomes
    ``"task"`` so a branch/path name is always constructible.
    """
    slug = _SLUG_CLEAN.sub("-", str(task_slug).lower())
    slug = _SLUG_DASHES.sub("-", slug).strip("-")
    slug = slug[:_SLUG_MAX].rstrip("-")
    return slug or "task"


@dataclass
class WorktreeInfo:
    """One daemon-managed worktree."""

    path: Path
    branch: str
    base_ref: str
    slug: str

    def to_json(self) -> dict:
        return {
            "path": str(self.path),
            "branch": self.branch,
            "base_ref": self.base_ref,
            "slug": self.slug,
        }

    @staticmethod
    def from_json(data: dict) -> WorktreeInfo:
        return WorktreeInfo(
            path=Path(data["path"]),
            branch=str(data.get("branch") or ""),
            base_ref=str(data.get("base_ref") or ""),
            slug=str(data.get("slug") or ""),
        )


class WorktreeManager:
    """Create, list, inspect and remove per-task git worktrees.

    The manager is stateless: every method takes the main checkout as
    ``workspace``. Module-level functions with the same names delegate to
    a shared instance for callers that prefer plain functions.
    """

    # -- git plumbing ------------------------------------------------------

    def _git(self, workspace: Path, *args: str, check: bool = True) -> str:
        """Run ``git -C <workspace> <args...>``; return stdout stripped.

        Raises :class:`WorktreeError` on a non-zero exit when ``check``.
        """
        try:
            result = subprocess.run(
                ["git", "-C", str(workspace), *args],
                capture_output=True, text=True, check=False)
        except FileNotFoundError as exc:
            raise WorktreeError(
                "The 'git' executable was not found on PATH; "
                "worktree isolation requires git.") from exc
        except OSError as exc:
            raise WorktreeError(f"Could not run git: {exc}") from exc
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise WorktreeError(
                f"git {' '.join(args)} failed: {detail or 'unknown error'}")
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    def is_git_repo(self, workspace: Path | str) -> bool:
        """True when ``workspace`` is inside a git working tree."""
        root = Path(workspace)
        if not root.is_dir():
            return False
        try:
            result = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--git-dir"],
                capture_output=True, text=True, check=False)
        except OSError:
            return False
        return result.returncode == 0

    def _has_commits(self, workspace: Path) -> bool:
        try:
            result = subprocess.run(
                ["git", "-C", str(workspace), "rev-parse", "--verify", "HEAD"],
                capture_output=True, text=True, check=False)
        except OSError:
            return False
        return result.returncode == 0

    def _git_common_dir(self, workspace: Path) -> Path:
        raw = self._git(workspace, "rev-parse", "--git-common-dir")
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = workspace / candidate
        return candidate.resolve()

    def _ensure_excluded(self, workspace: Path) -> None:
        """Append the worktree rules to ``.git/info/exclude`` if absent.

        Never touches the user's ``.gitignore`` - see the module docstring.
        """
        exclude = self._git_common_dir(workspace) / "info" / "exclude"
        try:
            existing = exclude.read_text("utf-8") if exclude.exists() else ""
            present = {line.strip() for line in existing.splitlines()}
            missing = [rule for rule in _EXCLUDE_RULES if rule not in present]
            if not missing:
                return
            exclude.parent.mkdir(parents=True, exist_ok=True)
            prefix = "" if not existing or existing.endswith("\n") else "\n"
            with exclude.open("a", encoding="utf-8") as handle:
                handle.write(prefix + "".join(f"{rule}\n" for rule in missing))
        except OSError as exc:
            raise WorktreeError(
                f"Could not update {exclude} to ignore worktrees: {exc}") from exc

    # -- registry ------------------------------------------------------------

    def _registry_path(self, workspace: Path) -> Path:
        return project_dir(workspace) / "worktrees.json"

    def _load_registry(self, workspace: Path) -> list[WorktreeInfo]:
        path = self._registry_path(workspace)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return []
        rows = data.get("worktrees") if isinstance(data, dict) else data
        infos: list[WorktreeInfo] = []
        for row in rows or []:
            if isinstance(row, dict) and row.get("path"):
                infos.append(WorktreeInfo.from_json(row))
        return infos

    def _save_registry(self, workspace: Path, infos: list[WorktreeInfo]) -> None:
        path = self._registry_path(workspace)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps([info.to_json() for info in infos], indent=2) + "\n",
                "utf-8")
        except OSError as exc:
            raise WorktreeError(f"Could not write worktree registry: {exc}") from exc

    def _register(self, workspace: Path, info: WorktreeInfo) -> None:
        infos = [row for row in self._load_registry(workspace)
                 if Path(row.path) != info.path]
        infos.append(info)
        self._save_registry(workspace, infos)

    def _unregister(self, workspace: Path, path: Path) -> None:
        infos = self._load_registry(workspace)
        kept = [row for row in infos if Path(row.path) != path]
        if len(kept) != len(infos):
            self._save_registry(workspace, kept)

    # -- operations ----------------------------------------------------------

    def create(self, workspace: Path | str, task_slug: str, *,
               base_ref: str | None = None) -> WorktreeInfo:
        """Create a worktree for ``task_slug`` and return its info.

        The worktree is ``<workspace>/.daemon/worktrees/<slug>-<shortid>``
        on a new branch ``daemon/<slug>-<shortid>`` starting at
        ``base_ref`` (default: the current ``HEAD`` commit).

        Raises :class:`WorktreeError` when ``workspace`` is not a git
        repository or has no commits yet.
        """
        root = Path(workspace).resolve()
        if not self.is_git_repo(root):
            raise WorktreeError(
                f"Not a git repository: {root}. "
                "Worktree isolation requires a git working tree.")
        if not self._has_commits(root):
            raise WorktreeError(
                f"Repository at {root} has no commits yet. "
                "Make an initial commit before creating a worktree.")
        if base_ref is None:
            base_ref = self._git(root, "rev-parse", "HEAD")
        else:
            self._git(root, "rev-parse", "--verify", f"{base_ref}^{{commit}}")

        slug = sanitize_slug(task_slug)
        shortid = secrets.token_hex(3)
        branch = f"daemon/{slug}-{shortid}"
        path = project_dir(root) / "worktrees" / f"{slug}-{shortid}"

        self._ensure_excluded(root)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WorktreeError(f"Could not create {path.parent}: {exc}") from exc
        self._git(root, "worktree", "add", "-b", branch, str(path), base_ref)

        info = WorktreeInfo(path=path, branch=branch, base_ref=base_ref, slug=slug)
        self._register(root, info)
        audit("worktree-created", branch=branch, path=str(path))
        _logger.debug("worktree %s on %s", path, branch)
        return info

    def list_worktrees(self, workspace: Path | str) -> list[WorktreeInfo]:
        """Daemon-managed worktrees for ``workspace``, sorted by path.

        Parses ``git worktree list --porcelain`` and keeps only entries
        under ``.daemon/worktrees/``; registry entries that still exist
        on disk are merged in (their ``base_ref`` is authoritative).
        """
        root = Path(workspace).resolve()
        managed_root = (project_dir(root) / "worktrees").resolve()
        registry = {Path(info.path): info
                    for info in self._load_registry(root)}
        found: dict[Path, WorktreeInfo] = {}

        if self.is_git_repo(root):
            output = self._git(root, "worktree", "list", "--porcelain",
                               check=False)
            current_path: Path | None = None
            current_head = ""
            current_branch = ""
            blocks: list[tuple[Path, str, str]] = []
            for line in output.splitlines() + [""]:
                if line.startswith("worktree "):
                    if current_path is not None:
                        blocks.append((current_path, current_head, current_branch))
                    current_path = Path(line[len("worktree "):].strip())
                    current_head, current_branch = "", ""
                elif line.startswith("HEAD "):
                    current_head = line[len("HEAD "):].strip()
                elif line.startswith("branch "):
                    ref = line[len("branch "):].strip()
                    current_branch = ref.removeprefix("refs/heads/")
                elif line == "" and current_path is not None:
                    blocks.append((current_path, current_head, current_branch))
                    current_path = None
            for wt_path, head, branch in blocks:
                resolved = wt_path if wt_path.is_absolute() else root / wt_path
                try:
                    inside = resolved.resolve().is_relative_to(managed_root)
                except (OSError, ValueError):
                    inside = False
                if not inside:
                    continue
                known = registry.get(resolved) or registry.get(wt_path)
                if known is not None:
                    found[Path(known.path)] = known
                    continue
                slug = _SHORTID_SUFFIX.sub("", resolved.name)
                found[resolved] = WorktreeInfo(
                    path=resolved, branch=branch,
                    base_ref=head, slug=slug)
        for path, info in registry.items():
            key = path if path.is_absolute() else root / path
            if key not in found and Path(info.path).exists():
                found[key] = info
        return [found[key] for key in sorted(found, key=str)]

    def remove(self, workspace: Path | str,
               info_or_path: WorktreeInfo | Path | str, *,
               force: bool = False) -> bool:
        """Remove one managed worktree; return True when one was removed.

        Without ``force``, a worktree with changes makes git refuse and
        this raises :class:`WorktreeError`, and the branch is kept so the
        work stays reachable. With ``force=True`` the removal is forced
        and the ``daemon/...`` branch is deleted with ``git branch -D``.
        Returns False when the worktree was already gone (its registry
        entry is still cleaned up).
        """
        root = Path(workspace).resolve()
        if isinstance(info_or_path, WorktreeInfo):
            path, branch = Path(info_or_path.path), info_or_path.branch
        else:
            path, branch = Path(info_or_path), ""
        if not path.is_absolute():
            path = root / path
        if not branch:
            for info in self.list_worktrees(root):
                if Path(info.path) == path:
                    branch = info.branch
                    break

        if not path.exists():
            self._unregister(root, path)
            self.prune(root)
            return False

        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(path))
        self._git(root, *args)
        if force and path.exists():
            # git normally removes the directory; never leave a stale one.
            shutil.rmtree(path, ignore_errors=True)
        if force and branch:
            self._git(root, "branch", "-D", branch, check=False)
        self._unregister(root, path)
        audit("worktree-removed", branch=branch, path=str(path), force=force)
        return True

    def prune(self, workspace: Path | str) -> None:
        """Prune stale git worktree metadata and dead registry entries."""
        root = Path(workspace).resolve()
        if self.is_git_repo(root):
            self._git(root, "worktree", "prune", check=False)
        infos = self._load_registry(root)
        kept = [info for info in infos if Path(info.path).exists()]
        if len(kept) != len(infos):
            self._save_registry(root, kept)

    def status(self, workspace: Path | str, info: WorktreeInfo) -> dict:
        """Change summary for the worktree described by ``info``.

        Returns a dict with ``path``, ``branch``, ``changed_files`` (int
        count), ``status_lines`` (the ``git status --porcelain`` lines)
        and ``diff_stat`` (the ``git diff --stat HEAD`` summary text,
        empty when no tracked file changed).
        """
        path = Path(info.path)
        if not path.exists():
            raise WorktreeError(f"Worktree does not exist: {path}")
        porcelain = self._git(path, "status", "--porcelain", check=False)
        lines = [line for line in porcelain.splitlines() if line.strip()]
        diff_stat = self._git(path, "diff", "--stat", "HEAD", check=False)
        return {
            "path": str(path),
            "branch": info.branch,
            "base_ref": info.base_ref,
            "changed_files": len(lines),
            "status_lines": lines,
            "diff_stat": diff_stat,
        }


_default_manager = WorktreeManager()


def is_git_repo(workspace: Path | str) -> bool:
    """Module-level shortcut for ``WorktreeManager().is_git_repo(...)``."""
    return _default_manager.is_git_repo(workspace)


def create(workspace: Path | str, task_slug: str, *,
           base_ref: str | None = None) -> WorktreeInfo:
    """Module-level shortcut for ``WorktreeManager().create(...)``."""
    return _default_manager.create(workspace, task_slug, base_ref=base_ref)


def list_worktrees(workspace: Path | str) -> list[WorktreeInfo]:
    """Module-level shortcut for ``WorktreeManager().list_worktrees(...)``."""
    return _default_manager.list_worktrees(workspace)


def remove(workspace: Path | str, info_or_path: WorktreeInfo | Path | str, *,
           force: bool = False) -> bool:
    """Module-level shortcut for ``WorktreeManager().remove(...)``."""
    return _default_manager.remove(workspace, info_or_path, force=force)


def prune(workspace: Path | str) -> None:
    """Module-level shortcut for ``WorktreeManager().prune(...)``."""
    _default_manager.prune(workspace)


def status(workspace: Path | str, info: WorktreeInfo) -> dict:
    """Module-level shortcut for ``WorktreeManager().status(...)``."""
    return _default_manager.status(workspace, info)
