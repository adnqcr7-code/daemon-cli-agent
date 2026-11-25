"""Session persistence: transcript (JSONL), metadata, usage, resume."""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..config import daemon_home
from ..errors import SessionError
from ..log import get_logger
from ..providers.types import Message, Usage
from ..security import redact

try:  # POSIX file locking (same pattern as the ReflexStore lock).
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None  # type: ignore[assignment]

try:  # Windows locking fallback (msvcrt is stdlib on Windows only).
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows platforms
    msvcrt = None  # type: ignore[assignment]

_logger = get_logger("session")


def pid_alive(pid: int) -> bool | None:
    """Whether *pid* is a live process ON THIS HOST.

    True = alive, False = definitely gone, None = cannot be
    determined (no pid recorded, or the platform gives no reliable
    answer). Callers must not claim "interrupted" on None - a live
    session and a dead one are indistinguishable then (review
    round 2, V5).
    """
    if pid <= 0:
        return None
    if pid == os.getpid():
        return True
    if sys.platform.startswith("win"):
        return None  # os.kill(pid, 0) is not a safe probe on Windows
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return None
    return True


# A "running" meta whose heartbeat is older than this is no evidence
# of a live owner (round 3, R2-5). The heartbeat refreshes on every
# meta write - i.e. every journaled turn - so a live owner stays
# fresh; the threshold is generous because one long model call
# journals nothing, and the lock check runs first anyway: a
# lock-holding owner is never judged by heartbeat age.
_HEARTBEAT_STALE_S = 600.0


def _lock_held(session_id: str) -> bool | None:
    """Whether some process currently holds the session's resume lock.

    True = held, False = determinately free, None = the platform
    gives no answer. "Free" is only claimed when this platform HAS
    a lock primitive: where neither fcntl nor msvcrt exists, locks
    cannot be taken at all (acquire_lock() no-ops there), so an
    absent or unlocked file proves nothing about the owner - the
    state is indeterminate, never "free" (round 4, R3-3). The probe
    opens the EXISTING lock file (never creates one - a listing
    must not manufacture lock files in every session directory) and
    tries a non-blocking lock: if the probe can take it, nobody
    holds it.
    """
    if fcntl is None and msvcrt is None:
        return None
    try:
        fd = os.open(daemon_home() / "sessions" / session_id
                     / "session.lock", os.O_RDWR)
    except FileNotFoundError:
        return False
    except OSError:
        return None
    try:
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        if msvcrt is not None:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            return False
        return None
    finally:
        os.close(fd)


@dataclass
class SessionMeta:
    id: str = field(default_factory=lambda: time.strftime("%Y%m%d-%H%M%S") +
                                        "-" + uuid.uuid4().hex[:6])
    workspace: str = ""
    provider: str = ""
    model: str = ""
    role: str = "build"
    # Active environment (§2.4), "" when none: the run's env is
    # recorded in the event log (run.started) and here, so a session
    # listing can say which env a run belonged to.
    env: str = ""
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    compacted: bool = False
    title: str = ""
    # Whether the audit log was recording when this session was built
    # (v0.5.0 §3): a run whose broker decisions could not be recorded
    # says so in its own meta instead of failing silently. Defaults
    # to False (review round 2, V9): construction sites that know the
    # real status (build_context, the orchestrator) set it from
    # audit_status(); a meta that never consulted it must not claim
    # the audit trail was fine.
    audit_ok: bool = False
    # Run lifecycle (v0.5.0 §4): "" for sessions that never ran (or
    # predate the field), "running" while a run is in flight,
    # "complete" / "interrupted" once it ends. A meta still saying
    # "running" when listed means the process died mid-run - the
    # session is reported as interrupted and resumes from its journal.
    status: str = ""
    # Owning process (review round 2, V5): the pid of the process
    # running this session, and a heartbeat timestamp refreshed on
    # every meta write (i.e. every journaled turn). Liveness is
    # decided by liveness() below - the resume lock first, the pid
    # second, the heartbeat as the recycled-pid discriminator
    # (round 3, R2-5).
    pid: int = 0
    heartbeat: float = 0.0

    @property
    def display_status(self) -> str:
        """Status for listings, liveness-checked: see liveness()."""
        return self.liveness()

    def liveness(self) -> str:
        """Live status of a meta that says "running" (V5, R2-5).

        The pid alone is never trusted (R2-5): pids are recycled,
        so a dead owner's pid can belong to a live, unrelated
        process and round 2 reported such a session "running"
        forever. The signals, in order:

        * the resume LOCK is the primary signal - the owner holds
          it for the run's whole lifetime, so held means running;
        * pid verifiably dead means interrupted (with the lock free
          or not - a dead pid is positive evidence on its own);
        * lock determinately free + pid alive + STALE heartbeat
          means the pid was recycled: a live owner on the standard
          path would hold the lock, so the lock-holding owner is
          gone - interrupted, never "running";
        * lock INDETERMINATE + pid alive + stale heartbeat means
          "unknown", never "interrupted" (round 4, R3-3): the owner
          may be lockless by design - lock acquisition is
          best-effort at run start and no-ops entirely on a
          platform with no lock primitive - and one model call
          longer than the heartbeat threshold journals nothing, so
          a live owner's heartbeat goes stale while it is still
          writing. "interrupted" requires positive evidence;
        * pid alive + fresh heartbeat means a live owner that never
          took the lock (a library caller, a subagent) - running;
        * nothing determinable means "unknown", never a claim.
        """
        if self.status != "running":
            return self.status
        held = _lock_held(self.id)
        if held is True:
            return "running"
        alive = pid_alive(self.pid)
        if alive is False:
            return "interrupted"
        if alive is True:
            if self._heartbeat_stale():
                if held is False:
                    return "interrupted"
                if held is None:
                    return "unknown"
            return "running"
        return "unknown"

    def _heartbeat_stale(self) -> bool:
        return self.heartbeat <= 0 \
            or time.time() - self.heartbeat > _HEARTBEAT_STALE_S

    def to_json(self) -> dict:
        return {
            "id": self.id, "workspace": self.workspace, "provider": self.provider,
            "model": self.model, "role": self.role, "env": self.env,
            "created": self.created,
            "updated": self.updated, "compacted": self.compacted,
            "title": self.title, "audit_ok": self.audit_ok,
            "status": self.status, "pid": self.pid,
            "heartbeat": self.heartbeat,
        }

    @staticmethod
    def from_json(data: dict) -> SessionMeta:
        return SessionMeta(**{key: data.get(key, SessionMeta.__dataclass_fields__[key].default)
                              for key in SessionMeta.__dataclass_fields__})


class Session:
    """One conversation with the agent, persistable as JSONL."""

    def __init__(self, meta: SessionMeta | None = None,
                 messages: list[Message] | None = None,
                 usage: Usage | None = None) -> None:
        self.meta = meta or SessionMeta()
        self.messages: list[Message] = messages or []
        self.usage = usage or Usage()
        self._dirty = False
        # Messages already durably written to transcript.jsonl (the
        # journal). save()/journal() append only messages past this
        # mark, so a transcript never duplicates a turn (§4).
        self._journaled = 0
        # Set by load() when the meta still said "running": the
        # previous process died mid-run and this is a crash resume.
        self.resumed_interrupted = False
        # OS file descriptor holding the resume lock (load(lock=True)
        # / acquire_lock()), or None. The lock is held for the life
        # of this object - in CLI use, until the process exits.
        self._lock_fd: int | None = None

    # ------------------------------------------------------------ transcript

    def append(self, message: Message) -> Message:
        self.messages.append(message)
        self.meta.updated = time.time()
        self._dirty = True
        return message

    def add_user(self, text: str) -> Message:
        if not self.meta.title:
            self.meta.title = text.splitlines()[0][:70]
        return self.append(Message.user(text))

    def add_assistant(self, text: str | None,
                      tool_calls: list | None = None) -> Message:
        return self.append(Message.assistant(text, tool_calls))

    def add_tool_results(self, results: list) -> Message:
        return self.append(Message.with_tool_results(results))

    def last_user_text(self) -> str | None:
        for message in reversed(self.messages):
            if message.role == "user" and message.text:
                return message.text
        return None

    # ---------------------------------------------------------------- usage

    def add_usage(self, usage: Usage) -> None:
        self.usage.add(usage)
        self._dirty = True

    # ------------------------------------------------------------ persistence

    @property
    def directory(self) -> Path:
        return daemon_home() / "sessions" / self.meta.id

    def save_meta(self) -> None:
        """Write meta + usage only (no transcript append).

        Used when a run starts: the meta must say "running" on disk
        from the outset, while the transcript is journaled per
        completed turn only.
        """
        try:
            self._write_meta_usage()
        except OSError as exc:
            raise SessionError(f"Cannot save session meta: {exc}") from exc

    def save(self) -> None:
        try:
            self._write_meta_usage()
            self._append_transcript(fsync=False)
        except OSError as exc:
            raise SessionError(f"Cannot save session: {exc}") from exc

    def journal(self) -> None:
        """Durably append every not-yet-written message, with fsync.

        Called by the runtime as each turn completes (v0.5.0 §4): a
        hard kill - SIGKILL, power loss, host swap - loses at most
        the in-flight turn, whose messages were never journaled and
        therefore cannot duplicate on resume.
        """
        try:
            self._write_meta_usage()
            self._append_transcript(fsync=True)
        except OSError as exc:
            raise SessionError(f"Cannot journal session: {exc}") from exc

    def _write_meta_usage(self) -> None:
        # Every meta write is a heartbeat (V5): journal() runs per
        # completed turn, so a live run's heartbeat stays fresh.
        self.meta.heartbeat = time.time()
        path = self.directory
        path.mkdir(parents=True, exist_ok=True)
        with open(path / "meta.json", "w", encoding="utf-8") as handle:
            json.dump(self.meta.to_json(), handle, indent=2)
        with open(path / "usage.json", "w", encoding="utf-8") as handle:
            json.dump(self.usage.to_json(), handle)

    def _append_transcript(self, *, fsync: bool) -> None:
        path = self.directory
        path.mkdir(parents=True, exist_ok=True)
        if self._journaled > len(self.messages):
            # The in-memory list was replaced by a shorter one
            # (optimizer/compaction): the journal no longer aligns,
            # so rewrite it wholesale from memory - the only
            # consistent state left.
            self.rewrite_transcript()
            return
        pending = self.messages[self._journaled:]
        if pending:
            # Turn-atomic append (review round 2, V2): the whole
            # pending turn is serialized FIRST and written with ONE
            # write call, then flushed (+fsync for journal()). The
            # round-1 code wrote one line at a time, so a kill between
            # lines left complete valid lines for a PARTIAL turn,
            # which load() then accepted. A single write can still be
            # torn at its tail by a kill mid-write - load() drops a
            # torn final line AND validates turn completeness, so a
            # partial turn is never replayed as valid.
            payload = "".join(
                json.dumps(redact_safe(message)) + "\n"
                for message in pending)
            with open(path / "transcript.jsonl", "a",
                      encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                if fsync:
                    os.fsync(handle.fileno())
            self._journaled = len(self.messages)
        elif not (path / "transcript.jsonl").exists():
            (path / "transcript.jsonl").write_text("", encoding="utf-8")
        self._dirty = False

    def rewrite_transcript(self) -> None:
        """Full rewrite (used after compaction prunes old turns)."""
        path = self.directory / "transcript.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = "".join(
            json.dumps(redact_safe(message)) + "\n"
            for message in self.messages)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(payload)
        self._journaled = len(self.messages)
        self._dirty = False

    # ------------------------------------------------------------ resume lock

    def acquire_lock(self) -> None:
        """Take the session's exclusive resume lock (V5).

        Uses the ReflexStore cross-platform lock pattern (flock on
        POSIX, msvcrt byte lock on Windows) on ``session.lock`` in
        the session directory, held - not scoped - for as long as
        this object lives. Two concurrent resumes of one session
        would interleave transcript appends and duplicate turns
        (round 1 had no lock at all); the second acquirer fails here
        with a clear message instead.
        """
        if self._lock_fd is not None:
            return
        if fcntl is None and msvcrt is None:
            return  # no locking primitive on this platform
        self.directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.directory / "session.lock",
                     os.O_CREAT | os.O_RDWR, 0o644)
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            os.close(fd)
            owner = f" (owner pid {self.meta.pid})" \
                if self.meta.pid else ""
            raise SessionError(
                f"Session '{self.meta.id}' is already in use by "
                f"another process{owner}; refusing a second resume "
                f"that would corrupt the transcript") from exc
        self._lock_fd = fd

    def release_lock(self) -> None:
        """Release the resume lock, if held (process exit also does)."""
        if self._lock_fd is None:
            return
        fd, self._lock_fd = self._lock_fd, None
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            elif msvcrt is not None:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        os.close(fd)

    @staticmethod
    def load(session_id: str, *, lock: bool = False) -> Session:
        path = daemon_home() / "sessions" / session_id
        if not (path / "meta.json").exists():
            raise SessionError(f"No session '{session_id}'")
        try:
            meta = SessionMeta.from_json(
                json.loads((path / "meta.json").read_text("utf-8")))
            messages: list[Message] = []
            transcript = path / "transcript.jsonl"
            if transcript.exists():
                lines = [line for line in
                         transcript.read_text("utf-8").splitlines()
                         if line.strip()]
                for index, line in enumerate(lines):
                    try:
                        messages.append(Message.from_json(json.loads(line)))
                    except ValueError:
                        if index == len(lines) - 1:
                            # Torn tail: a kill landed mid-append.
                            # The partial turn was never completed,
                            # so dropping it loses nothing whole.
                            _logger.warning(
                                "session %s: dropping torn final "
                                "journal line", session_id)
                            break
                        raise
            # Turn completeness (review round 2, V2): dropping a torn
            # LINE is not enough. A kill during the journal write can
            # leave a turn's early lines fully durable while its
            # tool-results line never landed, ending the transcript
            # with an assistant message whose tool_calls have no
            # results. That partial turn must never be replayed as
            # valid - providers reject unpaired calls, and re-driving
            # the call would execute it twice - so the dangling
            # assistant message is dropped. The user message that
            # opened the turn stays: the ask is preserved and will
            # simply be answered again on resume.
            while messages and messages[-1].role == "assistant" \
                    and messages[-1].tool_calls:
                _logger.warning(
                    "session %s: dropping partial final turn "
                    "(tool calls without tool results)", session_id)
                messages.pop()
            usage = Usage()
            usage_path = path / "usage.json"
            if usage_path.exists():
                payload = json.loads(usage_path.read_text("utf-8"))
                usage = Usage(
                    input_tokens=int(payload.get("input_tokens") or 0),
                    output_tokens=int(payload.get("output_tokens") or 0))
            session = Session(meta=meta, messages=messages, usage=usage)
            session._dirty = False
            session._journaled = len(messages)
            if meta.status == "running" \
                    and meta.liveness() == "interrupted":
                # The owner is gone (lock free, and the pid dead -
                # or recycled, which the stale heartbeat betrays):
                # it died mid-run and this is a crash resume. A live
                # owner's session is not interrupted (V5), and an
                # undeterminable state proves nothing either way -
                # neither is claimed as interrupted. Checked BEFORE
                # acquire_lock() below takes the lock for us.
                session.resumed_interrupted = True
            if lock:
                session.acquire_lock()
            return session
        except (OSError, ValueError, KeyError) as exc:
            raise SessionError(f"Session '{session_id}' is corrupt: {exc}") from exc

    @staticmethod
    def list_sessions(limit: int = 30) -> list[SessionMeta]:
        root = daemon_home() / "sessions"
        if not root.exists():
            return []
        metas: list[SessionMeta] = []
        for entry in root.iterdir():
            meta_path = entry / "meta.json"
            if not meta_path.exists():
                continue
            try:
                metas.append(SessionMeta.from_json(
                    json.loads(meta_path.read_text("utf-8"))))
            except (OSError, ValueError):
                continue
        metas.sort(key=lambda meta: -meta.updated)
        return metas[:limit]

    @staticmethod
    def delete(session_id: str) -> None:
        import shutil

        path = daemon_home() / "sessions" / session_id
        if not path.exists():
            raise SessionError(f"No session '{session_id}'")
        shutil.rmtree(path)


def redact_safe(message: Message) -> dict:
    """Serialize a message with its content passed through the redactor."""
    data = message.to_json()
    if data.get("text"):
        data["text"] = redact(data["text"])
    for result in data.get("tool_results") or []:
        result["content"] = redact(result.get("content", ""))
    return data
