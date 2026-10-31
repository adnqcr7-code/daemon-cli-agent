"""File-level checkpoints: snapshot before modify, restore on /undo.

Checkpoints are unintrusive - no git objects, no repository mutation, just
compressed copies of the exact files an agent is about to change, stored
under ``.daemon/checkpoints/<timestamp>/`` with the original relative
paths. A rolling window keeps the most recent checkpoints.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .config import project_dir
from .errors import CheckpointError
from .log import audit, get_logger
from .tools.patches import unified_diff_text

_logger = get_logger("checkpoints")
_KEEP = 20


@dataclass
class Checkpoint:
    id: str
    created: float
    files: list[str]      # workspace-relative paths
    reason: str

    @property
    def age_s(self) -> float:
        return time.time() - self.created


class CheckpointManager:
    def __init__(self, workspace: Path, *, keep: int = _KEEP) -> None:
        self.workspace = Path(workspace).resolve()
        self.root = project_dir(self.workspace) / "checkpoints"
        self.keep = keep

    def _dir(self, checkpoint_id: str) -> Path:
        return self.root / checkpoint_id

    async def create(self, files: list[Path], *, reason: str = "agent edit") -> Checkpoint:
        """Snapshot the current content of ``files`` (skipping missing ones)."""
        checkpoint_id = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time()*1000)%1000:03d}"
        target = self._dir(checkpoint_id)
        saved: list[str] = []
        try:
            for path in files:
                if not path.exists() or not path.is_file():
                    continue
                rel = path.relative_to(self.workspace)
                destination = target / rel
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
                saved.append(str(rel))
        except OSError as exc:
            raise CheckpointError(f"Checkpoint failed: {exc}") from exc
        if not saved:
            raise CheckpointError("Nothing to checkpoint (no existing files).")
        self._prune()
        audit("checkpoint-created", id=checkpoint_id, files=len(saved))
        _logger.debug("checkpoint %s: %d files", checkpoint_id, len(saved))
        return Checkpoint(id=checkpoint_id, created=time.time(),
                          files=saved, reason=reason)

    def list(self) -> list[Checkpoint]:
        if not self.root.exists():
            return []
        checkpoints: list[Checkpoint] = []
        for entry in sorted(self.root.iterdir(), reverse=True):
            if not entry.is_dir():
                continue
            files = [str(path.relative_to(entry))
                     for path in entry.rglob("*") if path.is_file()]
            checkpoints.append(Checkpoint(
                id=entry.name, created=entry.stat().st_mtime, files=files,
                reason=""))
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
        """Copy checkpointed files back into the workspace."""
        directory = self._resolve(checkpoint_id)
        restored = 0
        for snapshot in directory.rglob("*"):
            if not snapshot.is_file():
                continue
            rel = snapshot.relative_to(directory)
            destination = self.workspace / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(snapshot, destination)
            restored += 1
        audit("checkpoint-restored", id=checkpoint_id, files=restored)
        return restored

    def diff(self, checkpoint_id: str = "latest",
             limit: int = 400) -> list[tuple[str, str]]:
        """[(path, unified_diff)] between a checkpoint and the workspace."""
        directory = self._resolve(checkpoint_id)
        diffs: list[tuple[str, str]] = []
        for snapshot in sorted(directory.rglob("*")):
            if not snapshot.is_file():
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
        return diffs

    def _prune(self) -> None:
        if not self.root.exists():
            return
        entries = sorted(
            (entry for entry in self.root.iterdir() if entry.is_dir()),
            key=lambda entry: entry.name, reverse=True)
        for stale in entries[self.keep:]:
            shutil.rmtree(stale, ignore_errors=True)
