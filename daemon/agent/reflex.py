"""Reflex signals: certified project facts, offered at the right moment.

Design v4 (``veil-reflex-signal-design-v4.md``) Phase 1. A coding agent
loses a large share of its tokens re-deriving structural facts it already
established in an earlier session - where a symbol is defined, what calls
what, which file owns a config key. This module keeps those facts in a
dedicated store, but only ones that were *certified* (see below), and
offers them back as one short note appended to the tail of the
conversation when the agent's trajectory touches the same ground again.

Safety properties, which are the design and not decoration:

- **Proposals never fire.** A fact becomes certified only by execution of
  the claim itself (a tool result that *is* the fact), by two independent
  derivations agreeing, or by explicit user approval. Everything else is
  an unverified proposal. Web and tool free-text can propose only.
- **The system prompt is never mutated by a firing.** Signals are
  appended to the conversation tail at request build, once, so provider
  prompt caching survives. (The failure-loop nudge uses the system-prompt
  rail; it fires rarely by construction. Reflex signals are meant to fire
  routinely, so they may not.)
- **Disagreement quarantines.** A certified entry contradicted by a later
  tool result, or bound to a file that changed, stops firing immediately
  pending re-certification or a user decision.
- **No result substitution.** This module never serves a stored result in
  place of a tool call. ``read_file`` and ``run_command`` always execute.
  ``optimization.reflex.hard_signals`` exists as a config key (default
  false) for Phase 2 and is deliberately unimplemented here.
- **Silence on error.** Any internal failure disables the engine for the
  rest of the run; the agent works unassisted, exactly as the optimizer
  and failure detector behave.

The store is a dedicated JSON file under the workspace
(``.daemon/reflex.json``) - NOT MemoryStore, whose schema, prompt
injection and per-call file parsing cannot carry this design. Writes are
read-modify-write under an flock-ed lock file so parallel worktree
sessions cannot lose updates; the match path reads only the in-memory
index loaded at session start.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config, project_dir
from ..log import audit, get_logger
from ..security import redact

try:  # POSIX locking; on platforms without fcntl, writes are unlocked.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None  # type: ignore[assignment]

_logger = get_logger("reflex")

# Tools whose successful output *is* a structural fact about the code
# (deterministic code intelligence). Results from these tools may certify
# a claim by execution. Everything else can only propose.
EXECUTION_TOOLS = frozenset({"find_symbols", "references"})

# Tools that mutate files; a successful mutation quarantines certified
# entries bound to the touched path.
_MUTATING_FILE_TOOLS = frozenset({
    "write_file", "edit_file", "apply_patch", "delete_path", "move_path",
    "format_code",
})

# Tools that count as "the agent went and re-derived it anyway" when they
# touch a fired entry's ground (an override of a pending signal).
_REDERIVE_TOOLS = frozenset({
    "read_file", "find_symbols", "references", "search_files",
})

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# Symbol-line grammar, taken from the producers - not invented.
# FindSymbolsTool emits exactly "{rel}:{line} {kind} {name}{signature}"
# (daemon/tools/codeintel.py, FindSymbolsTool.run). The kind must come
# from the producers' own vocabulary:
_SYMBOL_KINDS = frozenset({
    # Python AST kinds (codeintel.py _python_symbols).
    "function", "async function", "class",
    # Lexical-heuristic kinds (codeintel.py _DECL_PATTERNS).
    "const", "interface", "type", "func", "fn", "struct", "enum",
    "trait", "impl", "method", "def", "fun",
    # LSP document-symbol kinds (daemon/tools/lsp.py _SYMBOL_KINDS,
    # plus its "symbol" fallback for unmapped kinds).
    "file", "module", "namespace", "package", "property", "field",
    "constructor", "variable", "constant", "string", "number",
    "boolean", "array", "object", "key", "null", "enum member",
    "event", "operator", "type parameter", "symbol",
})
# Longest first, so multi-word kinds ("async function", "enum
# member", "type parameter") win over their prefixes in the
# alternation.
_KIND_ALTERNATION = "|".join(
    re.escape(kind) for kind in
    sorted(_SYMBOL_KINDS, key=len, reverse=True))
# The name is a dotted identifier: AST names are ".".join'ed Python
# identifiers (codeintel.py _python_symbols), heuristic names are
# (\w+) captures (_DECL_PATTERNS), LSP names are dot-flattened by
# daemon/tools/lsp.py document_symbols. The signature is empty
# (heuristic and LSP producers) or _py_signature's exact output: a
# parenthesized ", "-joined list of identifiers with optional */**
# prefixes - argument names only, never defaults or annotations.
# The pattern is anchored end-to-end (review 4, N7): the previous
# loose shape ("path:digits" + an unrestricted (.*) tail) let a
# planted newline-bearing filename mint a fake "symbol line" whose
# tail was arbitrary prose, and that prose certified and fired.
_SYMBOL_LINE = re.compile(
    r"^(?P<path>\S+):(?P<line>\d+) (?P<kind>" + _KIND_ALTERNATION +
    r") (?P<name>\w+(?:\.\w+)*)"
    r"(?P<signature>\((?:(?:\*\*|\*)?[A-Za-z_]\w*"
    r"(?:, (?:\*\*|\*)?[A-Za-z_]\w*)*)?\))?$")
# ReferencesTool LSP rows are exactly "{target}:{line}:{col}" with
# nothing after (codeintel.py, ReferencesTool.run).
_LOCATION_ROW = re.compile(r"^(?P<path>\S+):(?P<line>\d+):(?P<col>\d+)$")
# ReferencesTool lexical fallback shape (no LSP): a header line
# "'name': N reference(s) in M file(s)" then one "file: count" line
# per file. Neither line matches _SYMBOL_LINE (space after the colon),
# so without this shape references results certify nothing (review
# finding 2).
_REFERENCE_HEADER = re.compile(
    r"^'([^']+)': (\d+) reference\(s\) in (\d+) file\(s\)$")
_REFERENCE_COUNT = re.compile(r"^(\S+): (\d+)$")

# Scoring for trajectory matches: a structural hit is required to fire.
_SCORE_FILE = 3
_SCORE_SYMBOL_ARG = 3
_SCORE_FINGERPRINT = 4
_SCORE_TERM_CAP = 1
_FIRE_THRESHOLD = 3


# --------------------------------------------------------------------------
# Entries
# --------------------------------------------------------------------------


@dataclass
class ReflexEntry:
    """One stored fact: a proposal, a certified entry, or a quarantine."""

    result: str
    derivation: str = ""
    # Structural match keys: {"files": [...], "symbols": [...],
    # "tools": [...], "fingerprint": str | None, "terms": [...]}
    pattern: dict = field(default_factory=dict)
    provenance: str = ""            # who/what proposed it ("tool:find_symbols")
    kind: str = "structural"        # structural | live (live never certifies)
    status: str = "proposal"        # proposal | certified | quarantined
    certification: dict = field(default_factory=dict)  # method/detail/at/...
    inputs: dict = field(default_factory=dict)         # {"file_hashes": {...}}
    sources: list[str] = field(default_factory=list)   # claim sources seen
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created: float = field(default_factory=time.time)
    certified_at: float | None = None
    expires_at: float | None = None
    quarantine_reason: str = ""
    fires: int = 0
    follows: int = 0
    overrides: int = 0

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "result": self.result,
            "derivation": self.derivation,
            "pattern": self.pattern,
            "provenance": self.provenance,
            "kind": self.kind,
            "status": self.status,
            "certification": self.certification,
            "inputs": self.inputs,
            "sources": list(self.sources),
            "created": self.created,
            "certified_at": self.certified_at,
            "expires_at": self.expires_at,
            "quarantine_reason": self.quarantine_reason,
            "fires": self.fires,
            "follows": self.follows,
            "overrides": self.overrides,
        }

    @staticmethod
    def from_json(data: dict) -> ReflexEntry:
        return ReflexEntry(
            result=str(data.get("result") or ""),
            derivation=str(data.get("derivation") or ""),
            pattern=dict(data.get("pattern") or {}),
            provenance=str(data.get("provenance") or ""),
            # Canonicalize on load (review 2, N1): the live-kind
            # guards compare the exact string, so a stored "Live" /
            # " live" must become "live" here, at the entry point,
            # before any guard downstream sees it.
            kind=(str(data.get("kind") or "").strip().lower()
                  or "structural"),
            status=str(data.get("status") or "proposal"),
            certification=dict(data.get("certification") or {}),
            inputs=dict(data.get("inputs") or {}),
            sources=list(data.get("sources") or []),
            id=str(data.get("id") or uuid.uuid4().hex[:12]),
            created=float(data.get("created") or time.time()),
            certified_at=data.get("certified_at"),
            expires_at=data.get("expires_at"),
            quarantine_reason=str(data.get("quarantine_reason") or ""),
            fires=int(data.get("fires") or 0),
            follows=int(data.get("follows") or 0),
            overrides=int(data.get("overrides") or 0),
        )

    def is_live(self, now: float | None = None) -> bool:
        """Expiry is enforced here: expired == absent."""
        if self.expires_at is None:
            return True
        return (now if now is not None else time.time()) < self.expires_at

    def pattern_key(self) -> str:
        return _pattern_key(self.pattern)


def _normalize_result(text: str) -> str:
    """Collapse a claim's result for equality checks (whitespace only -
    structural facts keep their numbers and case; line numbers matter)."""
    text = _ANSI.sub("", text or "")
    text = _CONTROL.sub("", text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)


def _terms(text: str) -> list[str]:
    return sorted({term for term in re.split(r"\W+", (text or "").lower())
                   if len(term) >= 3})


def _pattern_key(pattern: dict) -> str:
    fingerprint = pattern.get("fingerprint")
    if fingerprint:
        return f"fp:{fingerprint}"
    files = sorted(pattern.get("files") or [])
    symbols = sorted(pattern.get("symbols") or [])
    payload = json.dumps({"files": files, "symbols": symbols},
                         sort_keys=True)
    return "fs:" + hashlib.sha256(payload.encode()).hexdigest()[:16]


def _fingerprint_args(tool_name: str, args: dict) -> str:
    """Same fingerprint discipline as the failure detector, namespaced by
    tool so identical argument dicts on different tools never collide."""
    try:
        payload = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        payload = str(args)
    return hashlib.sha256(f"{tool_name}:{payload}".encode()).hexdigest()[:16]


def _file_hash(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


class ReflexStore:
    """Dedicated reflex store: one JSON file under the workspace.

    The in-memory index is loaded at construction (session start) and is
    the only thing the match path reads. Every mutation is a
    read-modify-write under a lock file, refreshing memory from the
    freshest on-disk state, so concurrent sessions (parallel worktrees)
    cannot lose each other's updates.
    """

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = Path(workspace) if workspace else None
        self.path = (project_dir(self.workspace) / "reflex.json"
                     if self.workspace else None)
        self._entries: dict[str, ReflexEntry] = {}
        self._index: dict[str, set[str]] = {}
        self.load()

    # ------------------------------------------------------------ loading

    def load(self) -> None:
        self._entries = {}
        if self.path is not None and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                for raw in data.get("entries") or []:
                    entry = ReflexEntry.from_json(raw)
                    if entry.result:
                        self._entries[entry.id] = entry
            except (OSError, ValueError) as exc:
                _logger.warning("reflex store unreadable (%s); starting "
                                "empty", exc)
                self._entries = {}
        self._rebuild_index()

    def _rebuild_index(self) -> None:
        self._index = {}
        for entry in self._entries.values():
            for key in self._index_keys(entry):
                self._index.setdefault(key, set()).add(entry.id)

    @staticmethod
    def _index_keys(entry: ReflexEntry) -> list[str]:
        keys = [f"file:{path}" for path in entry.pattern.get("files") or []]
        keys += [f"symbol:{name.lower()}"
                 for name in entry.pattern.get("symbols") or []]
        keys += [f"tool:{name}" for name in entry.pattern.get("tools") or []]
        if entry.pattern.get("fingerprint"):
            keys.append(f"fp:{entry.pattern['fingerprint']}")
        return keys

    # ------------------------------------------------------------- reads

    def entries(self, status: str | None = None) -> list[ReflexEntry]:
        """All entries (optionally one status), newest first."""
        results = [entry for entry in self._entries.values()
                   if status is None or entry.status == status]
        return sorted(results, key=lambda entry: -entry.created)

    def get(self, entry_id: str) -> ReflexEntry | None:
        return self._entries.get(entry_id)

    def candidates(self, keys: list[str]) -> list[ReflexEntry]:
        """Certified, live entries indexed under any of ``keys``.

        The kind filter is defense in depth (review finding 1): a
        ``kind="live"`` entry must never fire even if one somehow
        reached certified status - live facts are proposal-only."""
        ids: set[str] = set()
        for key in keys:
            ids |= self._index.get(key, set())
        now = time.time()
        return [entry for entry in (self._entries.get(i) for i in ids)
                if entry is not None and entry.status == "certified"
                and entry.kind != "live"
                and entry.is_live(now)]

    # ------------------------------------------------------------ writes

    @contextlib.contextmanager
    def _locked(self):
        if self.path is None or fcntl is None:
            yield
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path.with_suffix(".lock"),
                     os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _mutate(self, fn) -> object:
        """Run ``fn(entries_dict)`` against the freshest state, persist,
        refresh memory. Returns fn's result."""
        with self._locked():
            fresh: dict[str, ReflexEntry] = {}
            if self.path is not None and self.path.exists():
                try:
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                    for raw in data.get("entries") or []:
                        entry = ReflexEntry.from_json(raw)
                        if entry.result:
                            fresh[entry.id] = entry
                except (OSError, ValueError):
                    fresh = dict(self._entries)
            else:
                fresh = dict(self._entries)
            result = fn(fresh)
            self._entries = fresh
            self._rebuild_index()
            self._save_locked()
            return result

    def _save_locked(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1,
                   "entries": [entry.to_json()
                               for entry in self._entries.values()]}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def add(self, entry: ReflexEntry) -> ReflexEntry:
        def _add(entries: dict) -> ReflexEntry:
            entries[entry.id] = entry
            return entry

        self._mutate(_add)
        return entry

    def update(self, entry: ReflexEntry) -> None:
        def _update(entries: dict) -> None:
            if entry.id in entries:
                entries[entry.id] = entry

        self._mutate(_update)

    def approve(self, entry_id: str) -> bool:
        """User approval: a proposal becomes certified (v4 section 3.3.3).

        Approvals run through the same pattern-key collision handling
        as ``ReflexEngine.submit_claim`` (review finding 4): approving
        a proposal whose key matches a certified entry with the same
        result is refused (the fact is already certified; approving
        would duplicate it), and on a contradiction the certified
        entry is quarantined and the approved proposal replaces it.
        ``kind="live"`` proposals can never be approved (finding 1)."""
        displaced: list[str] = []

        def _approve(entries: dict) -> bool:
            entry = entries.get(entry_id)
            if entry is None or entry.status != "proposal":
                return False
            if entry.kind == "live":
                return False
            key = entry.pattern_key()
            normalized = _normalize_result(entry.result)
            collisions = [other for other in entries.values()
                          if other.id != entry.id
                          and other.status == "certified"
                          and other.pattern_key() == key]
            # Refusal check first: _mutate persists whatever fn leaves
            # behind, so a duplicate must be detected before anything
            # is changed.
            if any(_normalize_result(other.result) == normalized
                   for other in collisions):
                return False  # already certified; do not duplicate
            for other in collisions:
                # Contradiction: same rule as submit_claim - the
                # certified entry stops firing and the approved claim
                # replaces it.
                other.status = "quarantined"
                other.quarantine_reason = (
                    f"contradicted by approval of {entry.id}")
                displaced.append(other.id)
            entry.status = "certified"
            entry.certified_at = time.time()
            entry.certification = {
                "method": "user_approval",
                "detail": "approved via daemon reflex approve",
                "at": entry.certified_at,
            }
            return True

        approved = bool(self._mutate(_approve))
        if approved:
            for other_id in displaced:
                audit("reflex-quarantined", id=other_id,
                      reason=f"contradicted by approval of {entry_id}")
            audit("reflex-approved", id=entry_id)
        return approved

    def quarantine(self, entry_id: str, reason: str) -> bool:
        """Stop an entry firing immediately (v4 section 3.5)."""
        def _quarantine(entries: dict) -> bool:
            entry = entries.get(entry_id)
            if entry is None or entry.status != "certified":
                return False
            entry.status = "quarantined"
            entry.quarantine_reason = reason
            return True

        done = bool(self._mutate(_quarantine))
        if done:
            audit("reflex-quarantined", id=entry_id, reason=reason)
        return done

    def quarantine_many(self, reasons: dict[str, str]) -> list[str]:
        """Quarantine several entries in ONE read-modify-write.

        Returns the ids actually quarantined. The match path can
        discover several stale entries in a single analyze pass (a
        branch switch rots many bindings at once); one ``_mutate``
        per entry made that O(entries) full store rewrites
        (review 2, N3)."""
        done: list[str] = []

        def _quarantine_all(entries: dict) -> None:
            for entry_id, reason in reasons.items():
                entry = entries.get(entry_id)
                if entry is None or entry.status != "certified":
                    continue
                entry.status = "quarantined"
                entry.quarantine_reason = reason
                done.append(entry_id)

        if reasons:
            self._mutate(_quarantine_all)
        for entry_id in done:
            audit("reflex-quarantined", id=entry_id,
                  reason=reasons[entry_id])
        return done

    def bump(self, entry_id: str, counter: str) -> None:
        """Increment one of fires / follows / overrides, durably."""
        if counter not in ("fires", "follows", "overrides"):
            return

        def _bump(entries: dict) -> None:
            entry = entries.get(entry_id)
            if entry is not None:
                setattr(entry, counter, getattr(entry, counter) + 1)

        self._mutate(_bump)

    def clear(self) -> int:
        """Purge every entry (the user's one-command exit)."""
        def _clear(entries: dict) -> int:
            count = len(entries)
            entries.clear()
            return count

        count = int(self._mutate(_clear) or 0)
        if count:
            audit("reflex-purged", count=count)
        return count


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------


@dataclass
class ReflexTrajectory:
    """What the agent has just done - the only thing matching may see."""

    task_text: str = ""
    recent_text: str = ""
    tool_calls: list = field(default_factory=list)  # [(name, args), ...]
    files: list[str] = field(default_factory=list)  # paths from call args


@dataclass
class ReflexSignal:
    """One firing: the entry and the note to append to the conversation."""

    entry: ReflexEntry
    note: str


@dataclass
class ReflexStats:
    """Session event counters (v4 section 3.6 - events, not token fiction)."""

    firings: int = 0
    follows: int = 0
    overrides: int = 0
    quarantines: int = 0


class ReflexEngine:
    """Watches the agent's trajectory and offers certified facts back.

    Modeled on FailureLoopDetector: config-gated, record/analyze shape,
    never terminates a run, never touches permissions. Any internal error
    silences the engine for the rest of the run.
    """

    def __init__(self, config: Config | None = None,
                 workspace: Path | None = None,
                 store: ReflexStore | None = None) -> None:
        global_enabled = config.get("optimization.enabled", True) if config else True
        reflex_enabled = (config.get("optimization.reflex.enabled", True)
                          if config else True)
        self.enabled = bool(global_enabled) and bool(reflex_enabled)
        # Phase 2 fence: the key exists and defaults off; serves are NOT
        # implemented in Phase 1 and this flag gates nothing yet.
        self.hard_signals = bool(
            config.get("optimization.reflex.hard_signals", False)
            if config else False)
        try:
            self.max_signals_per_turn = max(1, int(
                config.get("optimization.reflex.max_signals_per_turn", 1)
                if config else 1))
        except (TypeError, ValueError):
            self.max_signals_per_turn = 1
        ttl_days = (config.get("optimization.reflex.entry_ttl_days")
                    if config else None)
        self._ttl_s = (float(ttl_days) * 86400.0
                       if isinstance(ttl_days, (int, float)) and ttl_days
                       else None)
        self.workspace = Path(workspace) if workspace else None
        self._broken = False
        try:
            self.store = store if store is not None else ReflexStore(workspace)
        except Exception as exc:  # noqa: BLE001 - a broken store must not break runs
            _logger.warning("reflex store failed to load: %s", exc)
            self.store = ReflexStore(None)
            self._broken = True
        self._stats = ReflexStats()
        self._seq = 0
        self._turn_fires = 0
        self._pending: ReflexEntry | None = None

    # ------------------------------------------------------------ lifecycle

    @property
    def stats(self) -> ReflexStats:
        return self._stats

    def begin_turn(self) -> None:
        """Reset the per-turn firing budget (called at each user turn)."""
        self._turn_fires = 0

    def reset(self) -> None:
        self._pending = None
        self._turn_fires = 0

    # ------------------------------------------------------------- matching

    def analyze(self, trajectory: ReflexTrajectory) -> ReflexSignal | None:
        """Offer at most one certified fact for this request, or None.

        Also resolves the previous firing: if the agent moved on without
        re-deriving the fired entry's ground, that is a follow.
        """
        if not self.enabled or self._broken:
            return None
        try:
            self._resolve_pending(followed=True)
            if self._turn_fires >= self.max_signals_per_turn:
                return None
            entry = self._best_match(trajectory)
            if entry is None:
                return None
            note = self._render_note(entry)
            self.store.bump(entry.id, "fires")
            self._stats.firings += 1
            self._turn_fires += 1
            self._pending = entry
            audit("reflex-fired", id=entry.id)
            return ReflexSignal(entry=entry, note=note)
        except Exception as exc:  # noqa: BLE001 - silence on error, by design
            _logger.debug("reflex analyze failed: %s", exc)
            self._broken = True
            return None

    def _best_match(self, trajectory: ReflexTrajectory) -> ReflexEntry | None:
        keys: list[str] = []
        trajectory_files = set(trajectory.files)
        for path in trajectory_files:
            keys.append(f"file:{path}")
        arg_symbols: set[str] = set()
        fingerprints: set[str] = set()
        for name, args in trajectory.tool_calls:
            keys.append(f"tool:{name}")
            fingerprints.add(_fingerprint_args(name, args or {}))
            for value in (args or {}).values():
                if isinstance(value, str):
                    arg_symbols.add(value.lower())
                    keys.append(f"symbol:{value.lower()}")
        for fingerprint in fingerprints:
            keys.append(f"fp:{fingerprint}")
        if not keys:
            return None

        text_terms = set(_terms(trajectory.task_text + " " +
                                trajectory.recent_text))
        # Score first, stale-check second (review 2, N3): hashing
        # every candidate's bound files before scoring made one match
        # O(candidates x bound files) even when nothing could fire.
        # Only candidates at/above the fire threshold are stale-checked
        # at all, best first; if the winner turns out stale it is
        # quarantined and the next runner-up is checked, so a stale
        # entry still never fires and a fresh runner-up still can.
        scored: list[tuple[tuple, ReflexEntry]] = []
        for entry in self.store.candidates(keys):
            score = 0
            pattern = entry.pattern
            if trajectory_files & set(pattern.get("files") or []):
                score += _SCORE_FILE
            if pattern.get("fingerprint") in fingerprints:
                score += _SCORE_FINGERPRINT
            entry_symbols = {name.lower()
                             for name in pattern.get("symbols") or []}
            if entry_symbols & arg_symbols:
                score += _SCORE_SYMBOL_ARG
            if text_terms & set(pattern.get("terms") or []):
                score += _SCORE_TERM_CAP
            if score < _FIRE_THRESHOLD:
                continue
            # Deterministic winner (review finding 4): highest score,
            # then most recently certified, then entry id - a total
            # order, so candidate iteration order can never decide
            # which of two entries fires.
            rank = (score, entry.certified_at or entry.created, entry.id)
            scored.append((rank, entry))
        scored.sort(key=lambda item: item[0], reverse=True)

        stale_found: dict[str, str] = {}
        winner: ReflexEntry | None = None
        for _rank, entry in scored:
            stale = self._stale_binding(entry)
            if stale is None:
                winner = entry
                break
            # A bound file changed outside the session's own file
            # tools (IDE edit, git pull, formatter-on-save): the
            # entry stops firing instead of being discovered stale
            # by luck (review finding 3).
            stale_found[entry.id] = f"bound file changed: {stale}"
        if stale_found:
            # One store mutation for the whole pass, however many
            # entries rotted at once (review 2, N3).
            done = self.store.quarantine_many(stale_found)
            self._stats.quarantines += len(done)
            if self._pending is not None and self._pending.id in done:
                self._pending = None
        return winner

    def _stale_binding(self, entry: ReflexEntry) -> str | None:
        """First bound file whose current hash differs from the hash
        recorded at capture time, or None when the binding is intact
        (or unverifiable: no recorded hashes, or no workspace to
        resolve the workspace-relative paths against)."""
        hashes = (entry.inputs or {}).get("file_hashes") or {}
        if not hashes or self.workspace is None:
            return None
        for rel, digest in hashes.items():
            if _file_hash(self.workspace / rel) != digest:
                return rel
        return None

    @staticmethod
    def _render_note(entry: ReflexEntry) -> str:
        cert = entry.certification or {}
        method = cert.get("method", "unknown")
        when = entry.certified_at or entry.created
        stamp = time.strftime("%Y-%m-%d", time.localtime(when))
        lines = entry.result.splitlines()
        shown = lines[:3]
        extra = (f" (+{len(lines) - 3} more lines)"
                 if len(lines) > 3 else "")
        body = "\n".join(shown) + extra
        note = (f"[reflex] Certified fact ({method}, {stamp}): {body}"
                if len(lines) == 1 else
                f"[reflex] Certified fact ({method}, {stamp}):\n{body}")
        if entry.derivation:
            note += f"\nDerivation: {entry.derivation}"
        note += " You may use this or ignore it."
        return note

    # ------------------------------------------------------------ recording

    def record_tool_result(self, tool_name: str, args: dict, content: str,
                           *, is_error: bool = False) -> None:
        """Observe one executed tool call: overrides, rot, capture."""
        if not self.enabled or self._broken:
            return
        try:
            self._seq += 1
            args = args or {}
            self._check_override(tool_name, args)
            if is_error:
                return
            if tool_name in _MUTATING_FILE_TOOLS:
                self._quarantine_bound_to(args)
            if tool_name in EXECUTION_TOOLS:
                self._capture_execution(tool_name, args, content)
        except Exception as exc:  # noqa: BLE001 - silence on error, by design
            _logger.debug("reflex record failed: %s", exc)
            self._broken = True

    def _check_override(self, tool_name: str, args: dict) -> None:
        """The agent re-deriving a fired entry's ground is an override."""
        entry = self._pending
        if entry is None or tool_name not in _REDERIVE_TOOLS:
            return
        pattern = entry.pattern
        entry_files = set(pattern.get("files") or [])
        entry_symbols = {name.lower()
                         for name in pattern.get("symbols") or []}
        touched = {str(value) for value in args.values()
                   if isinstance(value, str)}
        hit = bool(entry_files & touched) or bool(
            entry_symbols & {value.lower() for value in touched})
        if hit:
            self.store.bump(entry.id, "overrides")
            self._stats.overrides += 1
            audit("reflex-overridden", id=entry.id, tool=tool_name)
            self._pending = None

    def _quarantine_bound_to(self, args: dict) -> None:
        """A successful file mutation rots entries bound to that path."""
        paths = {str(args[key]) for key in ("path", "target")
                 if isinstance(args.get(key), str)}
        if not paths:
            return
        for entry in self.store.entries(status="certified"):
            bound = set(entry.pattern.get("files") or [])
            bound |= set((entry.inputs.get("file_hashes") or {}).keys())
            changed = bound & paths
            if changed:
                reason = f"file changed: {sorted(changed)[0]}"
                if self.store.quarantine(entry.id, reason):
                    self._stats.quarantines += 1
                    if self._pending is not None and \
                            self._pending.id == entry.id:
                        self._pending = None

    def _resolve_pending(self, *, followed: bool) -> None:
        if self._pending is None:
            return
        if followed:
            self.store.bump(self._pending.id, "follows")
            self._stats.follows += 1
        self._pending = None

    def flush_pending(self) -> None:
        """Turn over: a pending signal the agent never re-derived counts
        as followed (called when the run's report is written)."""
        if self._broken:
            return
        try:
            self._resolve_pending(followed=True)
        except Exception as exc:  # noqa: BLE001
            _logger.debug("reflex flush failed: %s", exc)
            self._broken = True

    # ----------------------------------------------------------- capturing

    def _capture_execution(self, tool_name: str, args: dict,
                           content: str) -> None:
        """A codeintel result that *is* the fact certifies a claim."""
        parsed = _parse_symbol_output(content, tool_name, self.workspace)
        if parsed is None:
            return
        files, symbols, result = parsed
        if tool_name == "references":
            # Both references output shapes name the queried symbol
            # only in the header (or nowhere); the rows carry files.
            queried = args.get("symbol")
            if isinstance(queried, str) and queried \
                    and queried not in symbols:
                symbols.insert(0, queried)
        pattern: dict = {
            "files": files,
            "symbols": symbols,
            "tools": [tool_name],
            "fingerprint": _fingerprint_args(tool_name, args),
        }
        pattern["terms"] = _terms(result)
        inputs: dict = {}
        hashes = {}
        if self.workspace is not None:
            for rel in files:
                digest = _file_hash(self.workspace / rel)
                if digest:
                    hashes[rel] = digest
        if hashes:
            inputs["file_hashes"] = hashes
        summary = ", ".join(
            f"{key}={value!r}" for key, value in list(args.items())[:3])
        self.submit_claim(
            f"tool:{tool_name}", pattern, result,
            derivation=f"{tool_name}({summary})",
            executed=True, inputs=inputs)

    def submit_claim(self, source: str, pattern: dict, result: str,
                     derivation: str = "", *, executed: bool = False,
                     kind: str = "structural",
                     inputs: dict | None = None,
                     expires_at: float | None = None) -> ReflexEntry | None:
        """The one learning entry point (v4 section 3.3).

        - ``executed=True``: the claim *is* a tool's deterministic output;
          it certifies immediately, and quarantines any certified entry it
          contradicts.
        - ``executed=False``: the claim becomes a proposal. A second claim
          with the same pattern and result from a *different* source
          certifies it by double independent derivation.
        - ``kind="live"`` claims (prices, "latest", time-indexed facts)
          can never certify in Phase 1; they stay proposals forever.
        """
        if not self.enabled or self._broken:
            return None
        try:
            # Canonicalize kind at this entry point (review 2, N1):
            # every live-kind guard below - and in approve(),
            # candidates() and _certify() - compares the exact string,
            # so a caller-supplied "Live" / " live" must not be able
            # to slip past them by case or padding.
            kind = str(kind or "structural").strip().lower() \
                or "structural"
            normalized = _normalize_result(redact(result.strip()))
            if not normalized:
                return None
            pattern = dict(pattern or {})
            pattern.setdefault("terms", _terms(normalized + " " + derivation))
            key = _pattern_key(pattern)
            if expires_at is None and self._ttl_s is not None:
                expires_at = time.time() + self._ttl_s

            for existing in self.store.entries():
                if existing.pattern_key() != key:
                    continue
                same_result = (_normalize_result(existing.result)
                               == normalized)
                if existing.status == "certified":
                    if same_result:
                        return existing  # re-confirmation; nothing to do
                    # Contradiction: the certified entry stops firing at
                    # once. An executed claim replaces it; an unverified
                    # one does not (it stays a proposal below).
                    if self.store.quarantine(
                            existing.id,
                            f"contradicted by {source}"):
                        self._stats.quarantines += 1
                    if not executed:
                        break
                elif existing.status == "proposal" and same_result:
                    # The STORED entry's kind governs, not the incoming
                    # claim's (review finding 1): a live proposal can
                    # never be laundered into certification by a later
                    # structural or executed claim landing on it.
                    if kind == "live" or existing.kind == "live":
                        return existing
                    others = {item for item in existing.sources
                              if item != source}
                    if executed or others:
                        return self._certify(
                            existing, method=(
                                "execution" if executed
                                else "double_derivation"),
                            detail=(f"execution via {source}" if executed
                                    else "agreed by "
                                    + ", ".join(sorted(others | {source}))),
                            source=source)
                    return existing

            entry = ReflexEntry(
                result=normalized,
                derivation=redact(derivation.strip()),
                pattern=pattern,
                provenance=source,
                kind=kind,
                status="proposal",
                inputs=dict(inputs or {}),
                sources=[source],
                expires_at=expires_at,
            )
            self.store.add(entry)
            if executed and kind != "live":
                return self._certify(
                    entry, method="execution",
                    detail=f"execution via {source}", source=source)
            audit("reflex-proposed", id=entry.id, source=source)
            return entry
        except Exception as exc:  # noqa: BLE001 - silence on error, by design
            _logger.debug("reflex claim failed: %s", exc)
            self._broken = True
            return None

    def propose(self, source: str, pattern: dict, result: str,
                derivation: str = "", **kwargs) -> ReflexEntry | None:
        """An unverified claim. Proposals never fire."""
        return self.submit_claim(source, pattern, result, derivation,
                                 executed=False, **kwargs)

    def approve(self, entry_id: str) -> bool:
        """User approval path (CLI / TUI): certify a proposal."""
        if self._broken:
            return False
        try:
            return self.store.approve(entry_id)
        except Exception as exc:  # noqa: BLE001
            _logger.debug("reflex approve failed: %s", exc)
            self._broken = True
            return False

    def _certify(self, entry: ReflexEntry, *, method: str, detail: str,
                 source: str) -> ReflexEntry:
        if entry.kind == "live":
            # Defense in depth (review finding 1): live facts are
            # proposal-only forever, whatever path led here.
            _logger.warning("reflex: refusing to certify live entry %s",
                            entry.id)
            return entry
        entry.status = "certified"
        entry.certified_at = time.time()
        entry.certification = {"method": method, "detail": detail,
                               "at": entry.certified_at}
        if source not in entry.sources:
            entry.sources.append(source)
        self.store.update(entry)
        audit("reflex-certified", id=entry.id, method=method)
        return entry


def _canonical_rel_in_workspace(workspace: Path | None,
                                rel: str) -> str | None:
    """The canonical workspace-relative spelling of *rel*, or None
    when it does not name a real file inside *workspace*.

    Tool output lines embed raw repository filenames, and a filename
    containing newlines can split into extra shaped lines naming
    files that do not exist (review 3, N6; review 4, N7). Genuine
    tool output only ever names files the tool actually scanned, so
    a line whose file is absent - or resolves outside the workspace
    (symlinks included: the resolved target decides) - cannot be
    genuine. Without a workspace there is nothing to verify against,
    so nothing is trusted (fail closed).

    Kept lines are stored under the canonical spelling (review 4,
    A8): "./shop.py", "sub/../shop.py" and the absolute in-workspace
    spelling all name the same file, and only the resolved relative
    form string-matches the paths trajectories carry.
    """
    if workspace is None:
        return None
    try:
        root = workspace.resolve()
        candidate = (root / rel).resolve()
        if not candidate.is_file() or not candidate.is_relative_to(root):
            return None
        return candidate.relative_to(root).as_posix()
    except (OSError, RuntimeError):
        return None


def _file_exists_in_workspace(workspace: Path | None, rel: str) -> bool:
    """True only when *rel* names a real file inside *workspace*
    (see _canonical_rel_in_workspace)."""
    return _canonical_rel_in_workspace(workspace, rel) is not None


def _parse_reference_counts(
        lines: list[str],
        workspace: Path | None = None) -> tuple[list[str], list[str], str] | None:
    """Parse the ReferencesTool lexical-fallback output: a header
    naming the symbol and totals, then one ``file: count`` line per
    file. The lines are the tool's own deterministic output (AST /
    lexical counts), so they certify by execution exactly like
    find_symbols rows. Returns (files, symbols, result) or None.

    Body lines are kept only when the named file really exists under
    the workspace (review 3, N6), and are stored under the canonical
    workspace-relative spelling (review 4, A8): the lines embed raw
    filenames, so a planted newline-bearing filename can mint extra
    "file: count" lines for files that do not exist, and those must
    not reach a certified entry's result.

    The kept lines must also balance the header's books (review 4,
    A7): genuine output satisfies two arithmetic identities by
    construction - the header's file count is the number of body
    lines, and its total is the sum of the body counts
    (codeintel.py, ReferencesTool.run). A fragment naming a real but
    unscanned file ("real.py: 99") passes the existence check, so
    existence alone certifies a fabricated count; when the kept
    lines do not add up to the header, the parse fails closed and
    certifies nothing. (A scanned file whose own name carries
    newlines has its body line destroyed by the split, so its count
    is unrecoverable and the books can never balance - the price of
    failing closed, paid only in repositories that plant such names.
    An attacker who balances the books exactly, replacing a dropped
    fragment's count with a fabricated one for a real file, remains
    parser-indistinguishable from a genuine run over a different
    repo; that residue is inherent to parsing line-shaped output
    and is retired only by structured tool metadata - Phase 2.)
    """
    header: str | None = None
    symbol = ""
    header_total = 0
    header_files = 0
    kept: list[tuple[str, int]] = []
    for line in lines:
        match = _REFERENCE_HEADER.match(line)
        if match:
            # First header wins (review 2, N2): genuine tool output
            # has exactly one header, always first. A later
            # header-shaped line can only come from repository content
            # the tool echoed - ReferencesTool builds its body lines
            # from filenames, and a filename containing newlines can
            # split into a line that matches the header shape exactly.
            # Overwriting here would let a planted filename steer the
            # certified result and symbols, so later matches are
            # ignored entirely.
            if header is None:
                header, symbol = line, match.group(1)
                header_total = int(match.group(2))
                header_files = int(match.group(3))
            continue
        match = _REFERENCE_COUNT.match(line)
        if match and header is not None:
            canonical = _canonical_rel_in_workspace(
                workspace, match.group(1))
            if canonical is not None:
                kept.append((canonical, int(match.group(2))))
    if header is None or not kept:
        return None
    if len(kept) != header_files \
            or sum(count for _path, count in kept) != header_total:
        return None
    # The stored result is capped at 20 lines (header + 19 body);
    # the binding is capped with it (review finding 6). Validation
    # above ran over every kept line, capped or not, so a genuine
    # wide result still certifies its stored subset.
    files: list[str] = []
    body: list[str] = []
    for canonical, count in kept[:19]:
        if canonical not in files:
            files.append(canonical)
        body.append(f"{canonical}: {count}")
    return files, [symbol], "\n".join([header, *body])


def _parse_symbol_output(content: str, tool_name: str,
                         workspace: Path | None = None
                         ) -> tuple[list[str], list[str], str] | None:
    """Extract (files, symbols, result) from find_symbols/references
    output, or None when the output carries no structural fact."""
    clean = [line for line in
             (raw.strip() for raw in (content or "").splitlines())
             if line and not line.startswith("[")]
    if tool_name == "references":
        # The references-fallback shape is attempted ONLY for
        # references output (review 3, N6): find_symbols has no
        # genuine header at all, and its lines embed raw filenames
        # too, so running the fallback parser over its output lets a
        # planted filename's header-shaped fragment certify a fake
        # "fact" - and swallow the genuine symbol rows. find_symbols
        # output is parsed solely by the symbol-line parser below.
        fallback = _parse_reference_counts(clean, workspace)
        if fallback is not None:
            return fallback
    lines = []
    files: list[str] = []
    symbols: list[str] = []
    for line in clean:
        # A kept line must satisfy the producers' full grammar
        # (_SYMBOL_LINE, or _LOCATION_ROW for references-LSP output)
        # AND name a file that really exists under the workspace,
        # stored under its canonical spelling (review 4, N7/A8).
        # The grammar alone stops prose tails - a fragment naming a
        # real file with a prose tail fails the kind slot - and the
        # existence check alone stops ghost paths; a fragment that
        # satisfies both is indistinguishable from a genuine symbol
        # row, which is the accepted residue of parsing text output
        # (structured tool metadata retires it - Phase 2).
        match = _SYMBOL_LINE.match(line)
        if match is not None:
            canonical = _canonical_rel_in_workspace(
                workspace, match.group("path"))
            if canonical is None:
                continue
            name = match.group("name")
            symbol = name.split(".")[0]
            if symbol not in symbols:
                symbols.append(symbol)
            stored = (f"{canonical}:{match.group('line')} "
                      f"{match.group('kind')} {name}"
                      f"{match.group('signature') or ''}")
        else:
            row = _LOCATION_ROW.match(line)
            if row is None:
                continue
            canonical = _canonical_rel_in_workspace(
                workspace, row.group("path"))
            if canonical is None:
                continue
            stored = (f"{canonical}:{row.group('line')}"
                      f":{row.group('col')}")
        if canonical not in files:
            files.append(canonical)
        lines.append(stored)
        if len(lines) >= 20:
            # The stored result is capped at 20 lines; the binding
            # (files/symbols the entry can fire and rot on) is capped
            # with it, so a directory-wide result cannot bind an
            # entry to hundreds of files its stored lines never
            # mention (review finding 6).
            break
    if not lines:
        return None
    return files, symbols, "\n".join(lines[:20])
