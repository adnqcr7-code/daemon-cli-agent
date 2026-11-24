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
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import daemon_home, project_dir
from .log import audit, get_logger
from .security import redact

_logger = get_logger("memory")


@dataclass
class MemoryEntry:
    content: str
    # note | fact | decision | research | preference | verified_fact
    kind: str = "note"
    tags: list[str] = field(default_factory=list)
    source: str = "user"      # user | agent | research
    scope: str = "project"    # user | project | session
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created: float = field(default_factory=time.time)
    confidence: float = 1.0   # 0.0 - 1.0, clamped in __post_init__
    source_location: str = ""  # e.g. "file.py:42", a commit hash, or a URL
    verified: bool = False    # explicitly confirmed by user or test
    supersedes: list[str] = field(default_factory=list)
    invalidated: bool = False
    invalidated_reason: str = ""

    def __post_init__(self) -> None:
        try:
            confidence = float(self.confidence)
        except (TypeError, ValueError):
            confidence = 1.0
        self.confidence = max(0.0, min(1.0, confidence))
        self.supersedes = list(self.supersedes or [])

    def to_json(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_json(data: dict) -> MemoryEntry:
        try:
            confidence = float(data.get("confidence", 1.0))
        except (TypeError, ValueError):
            confidence = 1.0
        return MemoryEntry(
            content=data["content"], kind=data.get("kind", "note"),
            tags=list(data.get("tags") or []), source=data.get("source", "user"),
            scope=data.get("scope", "project"), id=data.get("id") or uuid.uuid4().hex[:12],
            created=float(data.get("created") or time.time()),
            confidence=confidence,
            source_location=str(data.get("source_location") or ""),
            verified=bool(data.get("verified", False)),
            supersedes=list(data.get("supersedes") or []),
            invalidated=bool(data.get("invalidated", False)),
            invalidated_reason=str(data.get("invalidated_reason") or ""))


def _normalize_content(content: str) -> str:
    return " ".join(re.split(r"\W+", content.lower())).strip()


def _word_set(content: str) -> set[str]:
    return {term for term in re.split(r"\W+", content.lower()) if term}


def _jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


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

    def add_verified(self, content: str, *, source: str, source_location: str,
                     confidence: float = 0.9, tags: Iterable[str] = (),
                     supersedes: Iterable[str] = ()) -> MemoryEntry:
        """Add a verified fact with source tracking (scope="project")."""
        content = redact(content.strip())
        if not content:
            raise ValueError("Memory content is empty.")
        entry = MemoryEntry(content=content, kind="verified_fact", scope="project",
                            tags=list(tags), source=source, confidence=confidence,
                            source_location=source_location, verified=True,
                            supersedes=list(supersedes))
        path = self._path_for("project")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry.to_json()) + "\n")
        audit("memory-added", kind="verified_fact", scope="project", id=entry.id)
        return entry

    # ------------------------------------------------------------- reading

    def entries(self, scope: str | None = None,
                include_invalidated: bool = False) -> list[MemoryEntry]:
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
        if not include_invalidated:
            results = [entry for entry in results if not entry.invalidated]
        # Newest first, as a total order (v0.4.1): entries are read in
        # append order, so the read index breaks exact `created` ties
        # toward the later-appended entry. Sorting on -created alone
        # left ties in file order (oldest first) - and two adds inside
        # one clock tick tie easily where time.time() granularity is
        # coarse (notably Windows), which made the order - and every
        # consumer built on it - nondeterministic.
        ordered = sorted(enumerate(results),
                         key=lambda pair: (-pair[1].created, -pair[0]))
        return [entry for _index, entry in ordered]

    def get_verified_facts(self, limit: int = 20) -> list[MemoryEntry]:
        """Verified, non-invalidated facts, confidence desc then newest."""
        facts = [entry for entry in self.entries()
                 if entry.verified or entry.kind == "verified_fact"]
        # entries() is already newest-first as a total order, so a
        # stable sort on confidence alone yields confidence desc,
        # then newest - deterministically, including on exact
        # timestamp ties (the old (-confidence, -created) key fell
        # back to oldest-first file order there).
        facts.sort(key=lambda entry: -entry.confidence)
        return facts[:limit]

    def find_contradictions(self, content: str, *,
                            source_location: str = "") -> list[MemoryEntry]:
        """Heuristic contradiction candidates (heuristic, not proof).

        - With a non-empty ``source_location``: non-invalidated verified
          facts sharing that exact source_location but with different
          normalized content are candidates.
        - Without one: non-invalidated verified facts whose word set has
          Jaccard overlap >= 0.6 with ``content`` but whose normalized
          content differs are candidates.
        Deterministic: results follow ``entries()`` order (newest first).
        """
        normalized = _normalize_content(content)
        candidates: list[MemoryEntry] = []
        for entry in self.entries():
            if not (entry.verified or entry.kind == "verified_fact"):
                continue
            if _normalize_content(entry.content) == normalized:
                continue
            if source_location:
                if entry.source_location and entry.source_location == source_location:
                    candidates.append(entry)
            else:
                overlap = _jaccard(_word_set(content), _word_set(entry.content))
                if overlap >= 0.6:
                    candidates.append(entry)
        return candidates

    def invalidate(self, entry_id: str, reason: str) -> bool:
        """Mark an entry invalidated in its JSONL file (never deletes it)."""
        for layer in ("user", "project"):
            path = self._path_for(layer)
            if path is None or not path.exists():
                continue
            lines = path.read_text("utf-8").splitlines()
            updated: list[str] = []
            found = False
            for line in lines:
                if not line.strip() or found:
                    updated.append(line)
                    continue
                try:
                    data = json.loads(line)
                except ValueError:
                    updated.append(line)
                    continue
                if data.get("id") == entry_id:
                    data["invalidated"] = True
                    data["invalidated_reason"] = reason
                    updated.append(json.dumps(data))
                    found = True
                else:
                    updated.append(line)
            if found:
                path.write_text("\n".join(updated) + ("\n" if updated else ""),
                                encoding="utf-8")
                audit("memory-invalidated", id=entry_id, reason=reason)
                return True
        return False

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
        verified = [entry for entry in relevant
                    if entry.verified or entry.kind == "verified_fact"]
        verified.sort(key=lambda entry: (-entry.confidence, -entry.created))
        verified = verified[:8]
        decisions = [entry for entry in relevant if entry.kind == "decision"][:5]
        facts = [entry for entry in relevant
                 if entry.kind != "decision" and entry.kind != "verified_fact"
                 and not entry.verified][:limit]
        blocks: list[str] = []
        if verified:
            blocks.append("Verified facts:\n" + "\n".join(
                f"- {entry.content} (source: {entry.source_location or entry.source}, "
                f"confidence {entry.confidence:.2f})" for entry in verified))
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
