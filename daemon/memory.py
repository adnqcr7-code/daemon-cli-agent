"""Memory layers: session, project, user, research notes and decisions.

Every layer is plain JSONL/JSON on disk (no opaque database), searchable,
inspectable and removable through `daemon memory` commands. Repository and
web content is NEVER treated as trusted instructions; memory entries are
data records with a source field.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from .config import daemon_home, project_dir
from .log import audit, get_logger
from .security import redact

_logger = get_logger("memory")


@dataclass
class MemoryEntry:
    content: str
    kind: str = "note"        # note | fact | decision | research | preference
    tags: list[str] = field(default_factory=list)
    source: str = "user"      # user | agent | research
    scope: str = "project"    # user | project | session
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created: float = field(default_factory=time.time)

    def to_json(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_json(data: dict) -> "MemoryEntry":
        return MemoryEntry(
            content=data["content"], kind=data.get("kind", "note"),
            tags=list(data.get("tags") or []), source=data.get("source", "user"),
            scope=data.get("scope", "project"), id=data.get("id") or uuid.uuid4().hex[:12],
            created=float(data.get("created") or time.time()))


class MemoryStore:
    """Two persistent layers: user-level and project-level memory."""

    def __init__(self, workspace: Path | None = None,
                 home: Path | None = None) -> None:
        self.home = Path(home) if home else daemon_home()
        self.workspace = Path(workspace) if workspace else None
        self.user_path = self.home / "memory.jsonl"
        self.project_path = (project_dir(self.workspace) / "memory.jsonl"
                             if self.workspace else None)

    # ------------------------------------------------------------- writing

    def add(self, content: str, *, kind: str = "note", scope: str = "project",
            tags: Iterable[str] = (), source: str = "user") -> MemoryEntry:
        content = redact(content.strip())
        if not content:
            raise ValueError("Memory content is empty.")
        entry = MemoryEntry(content=content, kind=kind, scope=scope,
                            tags=list(tags), source=source)
        path = self._path_for(scope)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry.to_json()) + "\n")
        audit("memory-added", kind=kind, scope=scope, id=entry.id)
        return entry

    # ------------------------------------------------------------- reading

    def entries(self, scope: str | None = None) -> list[MemoryEntry]:
        results: list[MemoryEntry] = []
        for layer in ("user", "project"):
            if scope is not None and scope != layer:
                continue
            path = self._path_for(layer)
            if not path or not path.exists():
                continue
            try:
                for line in path.read_text("utf-8").splitlines():
                    if line.strip():
                        results.append(MemoryEntry.from_json(json.loads(line)))
            except (OSError, ValueError) as exc:
                _logger.warning("corrupt memory line in %s: %s", path, exc)
        return sorted(results, key=lambda entry: -entry.created)

    def search(self, query: str, *, kind: str | None = None,
               limit: int = 20) -> list[MemoryEntry]:
        terms = [term for term in re.split(r"\W+", query.lower()) if term]
        scored: list[tuple[float, MemoryEntry]] = []
        for entry in self.entries():
            if kind and entry.kind != kind:
                continue
            haystack = (entry.content + " " + " ".join(entry.tags)).lower()
            score = sum(1.0 for term in terms if term in haystack)
            if score:
                scored.append((score / max(len(terms), 1), entry))
        scored.sort(key=lambda pair: -pair[0])
        return [entry for _, entry in scored[:limit]]

    def project_context(self, limit: int = 12) -> str:
        """Curated memory block for the agent system prompt."""
        relevant = [entry for entry in self.entries() if entry.scope == "project"]
        decisions = [entry for entry in relevant if entry.kind == "decision"][:5]
        facts = [entry for entry in relevant if entry.kind != "decision"][:limit]
        blocks: list[str] = []
        if decisions:
            blocks.append("Engineering decisions:\n" + "\n".join(
                f"- {entry.content}" for entry in decisions))
        if facts:
            blocks.append("Project memory:\n" + "\n".join(
                f"- {entry.content}" for entry in facts))
        if not blocks:
            return ""
        return "\n\n".join(blocks)

    # ------------------------------------------------------------- removal

    def remove(self, entry_id: str) -> bool:
        for layer in ("user", "project"):
            path = self._path_for(layer)
            if path is None or not path.exists():
                continue
            lines = path.read_text("utf-8").splitlines()
            kept = []
            removed = False
            for line in lines:
                try:
                    entry = json.loads(line)
                except ValueError:
                    kept.append(line)
                    continue
                if entry.get("id") == entry_id:
                    removed = True
                else:
                    kept.append(line)
            if removed:
                path.write_text("\n".join(kept) + ("\n" if kept else ""),
                                encoding="utf-8")
                audit("memory-removed", id=entry_id)
                return True
        return False

    def clear(self, scope: str) -> int:
        path = self._path_for(scope)
        if path is None or not path.exists():
            return 0
        count = sum(1 for line in path.read_text("utf-8").splitlines()
                    if line.strip())
        path.unlink()
        audit("memory-cleared", scope=scope, count=count)
        return count

    # ------------------------------------------------------------- internals

    def _path_for(self, scope: str) -> Path:
        if scope == "user":
            return self.user_path
        if self.project_path is None:
            return self.user_path  # degenerate: no workspace, store as user
        return self.project_path
