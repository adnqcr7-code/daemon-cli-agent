"""File-level checkpoints: snapshot before modify, restore on /undo.

Checkpoints are unintrusive - no git objects, no repository mutation, just
compressed copies of the exact files an agent is about to change, stored
under ``.daemon/checkpoints/<timestamp>/`` with the original relative
paths. A rolling window keeps the most recent checkpoints.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import project_dir
from .errors import CheckpointError
from .log import audit, get_logger
from .security import is_within
from .tools.patches import unified_diff_text

_logger = get_logger("checkpoints")
_KEEP = 20


@dataclass
class Checkpoint:
    id: str
    created: float
    files: list[str]      # workspace-relative paths
    reason: str
    created_files: list[str] = field(default_factory=list)  # paths that
    # did not exist when the checkpoint was taken; /undo deletes them

    @property
    def age_s(self) -> float:
        return time.time() - self.created


@dataclass
class RestoreReport:
    """What one restore actually did."""

    restored: int = 0
    deleted: int = 0
    deleted_paths: list[str] = field(default_factory=list)


class CheckpointManager:
    def __init__(self, workspace: Path, *, keep: int = _KEEP) -> None:
        self.workspace = Path(workspace).resolve()
        self.root = project_dir(self.workspace) / "checkpoints"
        self.keep = keep

    def _dir(self, checkpoint_id: str) -> Path:
        return self.root / checkpoint_id

    async def create(self, files: list[Path], *, reason: str = "agent edit",
                   created: list[Path] | None = None) -> Checkpoint:
        """Snapshot the current content of ``files`` (skipping missing ones).

        ``created`` lists paths that did not exist yet when the checkpoint
        was taken (a write tool about to create them). They get no snapshot
        — there is nothing to restore — but their relative paths are
        recorded in ``created.json`` so :meth:`restore` can delete them
        again. Without this, /undo could never remove a file the agent
        created.
        """
        checkpoint_id = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time()*1000)%1000:03d}"
        target = self._dir(checkpoint_id)
        saved: list[str] = []
        created_rels: list[str] = []
        try:
            for path in files:
                if not path.exists() or not path.is_file():
                    continue
                rel = path.relative_to(self.workspace)
                destination = target / rel
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
                saved.append(str(rel))
            for path in created or ():
                try:
                    rel = path.relative_to(self.workspace)
                except ValueError:
                    continue  # outside the workspace: never tracked
                created_rels.append(str(rel))
            if created_rels:
                target.mkdir(parents=True, exist_ok=True)
                (target / "created.json").write_text(
                    json.dumps(sorted(set(created_rels))), "utf-8")
        except OSError as exc:
            raise CheckpointError(f"Checkpoint failed: {exc}") from exc
        if not saved and not created_rels:
            raise CheckpointError("Nothing to checkpoint (no existing files).")
        self._prune()
        audit("checkpoint-created", id=checkpoint_id, files=len(saved))
        _logger.debug("checkpoint %s: %d files", checkpoint_id, len(saved))
        return Checkpoint(id=checkpoint_id, created=time.time(),
                          files=saved, reason=reason,
                          created_files=created_rels)

    def _created_files(self, directory: Path) -> list[str]:
        manifest = directory / "created.json"
        if not manifest.exists():
            return []
        try:
            data = json.loads(manifest.read_text("utf-8"))
        except (OSError, ValueError):
            return []
        return [str(item) for item in data if isinstance(item, str)]

    def list(self) -> list[Checkpoint]:
        if not self.root.exists():
            return []
        checkpoints: list[Checkpoint] = []
        for entry in sorted(self.root.iterdir(), reverse=True):
            if not entry.is_dir():
                continue
            files = [str(path.relative_to(entry))
                     for path in entry.rglob("*")
                     if path.is_file() and path.name != "created.json"]
            checkpoints.append(Checkpoint(
                id=entry.name, created=entry.stat().st_mtime, files=files,
                reason="", created_files=self._created_files(entry)))
        return checkpoints[: self.keep * 2]

    def latest(self) -> Checkpoint | None:
        checkpoints = self.list()
        return checkpoints[0] if checkpoints else None

    def _resolve(self, checkpoint_id: str) -> Path:
        if checkpoint_id == "latest":
            latest = self.latest()
            if latest is None:
                raise CheckpointError("No checkpoints exist yet.")
            checkpoint_id = latest.id
        directory = self._dir(checkpoint_id)
        if not directory.exists():
            raise CheckpointError(f"No checkpoint '{checkpoint_id}'.")
        return directory

    async def restore(self, checkpoint_id: str = "latest") -> int:
        """Copy checkpointed files back into the workspace.

        Files recorded as created after the checkpoint was taken are
        deleted again (workspace-contained only). Returns the number of
        files restored; use :meth:`restore_report` for the deletion count.
        """
        report = await self.restore_report(checkpoint_id)
        return report.restored

    async def restore_report(self, checkpoint_id: str = "latest") -> RestoreReport:
        """Restore snapshots and delete files created after the checkpoint."""
        directory = self._resolve(checkpoint_id)
        restored = 0
        for snapshot in directory.rglob("*"):
            if not snapshot.is_file() or snapshot.name == "created.json":
                continue
            rel = snapshot.relative_to(directory)
            destination = self.workspace / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(snapshot, destination)
            restored += 1
        deleted_paths: list[str] = []
        for rel in self._created_files(directory):
            candidate = (self.workspace / rel).resolve()
            # Never delete outside the workspace (symlink escape guard) and
            # never delete a directory — only the exact file the agent made.
            if not is_within(candidate, self.workspace):
                continue
            if candidate.is_file():
                try:
                    candidate.unlink()
                except OSError:
                    continue
                deleted_paths.append(rel)
                self._remove_empty_parents(candidate.parent)
        audit("checkpoint-restored", id=checkpoint_id, files=restored,
              deleted=len(deleted_paths))
        return RestoreReport(restored=restored, deleted=len(deleted_paths),
                             deleted_paths=deleted_paths)

    def _remove_empty_parents(self, directory: Path) -> None:
        """Remove directories left empty by a deletion, up to the workspace."""
        current = directory
        while current != self.workspace and is_within(current, self.workspace):
            try:
                current.rmdir()  # only succeeds when empty
            except OSError:
                break
            current = current.parent

    def diff(self, checkpoint_id: str = "latest",
             limit: int = 400) -> list[tuple[str, str]]:
        """[(path, unified_diff)] between a checkpoint and the workspace."""
        directory = self._resolve(checkpoint_id)
        diffs: list[tuple[str, str]] = []
        for snapshot in sorted(directory.rglob("*")):
            if not snapshot.is_file() or snapshot.name == "created.json":
                continue
            rel = str(snapshot.relative_to(directory))
            current = self.workspace / rel
            old = snapshot.read_text("utf-8", errors="replace")
            new = (current.read_text("utf-8", errors="replace")
                   if current.exists() else "")
            if old != new:
                diffs.append((rel, unified_diff_text(old, new, rel)))
            if len(diffs) >= limit:
                break
        # Also report workspace files deleted since the checkpoint? Those
        # show up as old != "" with new == "" above.
        if len(diffs) < limit:
            # Files created after the checkpoint show up as pure additions.
            for rel in self._created_files(directory):
                current = self.workspace / rel
                if current.is_file():
                    new = current.read_text("utf-8", errors="replace")
                    diffs.append((rel, unified_diff_text("", new, rel)))
                if len(diffs) >= limit:
                    break
        return diffs

    def _prune(self) -> None:
        if not self.root.exists():
            return
        entries = sorted(
            (entry for entry in self.root.iterdir() if entry.is_dir()),
            key=lambda entry: entry.name, reverse=True)
        for stale in entries[self.keep:]:
            shutil.rmtree(stale, ignore_errors=True)
