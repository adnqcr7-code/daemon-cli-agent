"""Session persistence: transcript (JSONL), metadata, usage, resume."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..config import daemon_home
from ..errors import SessionError
from ..log import get_logger
from ..providers.types import Message, Usage
from ..security import redact

_logger = get_logger("session")


@dataclass
class SessionMeta:
    id: str = field(default_factory=lambda: time.strftime("%Y%m%d-%H%M%S") +
                                        "-" + uuid.uuid4().hex[:6])
    workspace: str = ""
    provider: str = ""
    model: str = ""
    role: str = "build"
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    compacted: bool = False
    title: str = ""

    def to_json(self) -> dict:
        return {
            "id": self.id, "workspace": self.workspace, "provider": self.provider,
            "model": self.model, "role": self.role, "created": self.created,
            "updated": self.updated, "compacted": self.compacted,
            "title": self.title,
        }

    @staticmethod
    def from_json(data: dict) -> "SessionMeta":
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

    def save(self) -> None:
        try:
            path = self.directory
            path.mkdir(parents=True, exist_ok=True)
            with open(path / "meta.json", "w", encoding="utf-8") as handle:
                json.dump(self.meta.to_json(), handle, indent=2)
            with open(path / "usage.json", "w", encoding="utf-8") as handle:
                json.dump(self.usage.to_json(), handle)
            if self._dirty:
                with open(path / "transcript.jsonl", "a", encoding="utf-8") as handle:
                    for message in self.messages:
                        handle.write(json.dumps(redact_safe(message)) + "\n")
                self._dirty = False
            elif not (path / "transcript.jsonl").exists():
                (path / "transcript.jsonl").write_text("", encoding="utf-8")
        except OSError as exc:
            raise SessionError(f"Cannot save session: {exc}") from exc

    def rewrite_transcript(self) -> None:
        """Full rewrite (used after compaction prunes old turns)."""
        path = self.directory / "transcript.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            for message in self.messages:
                handle.write(json.dumps(redact_safe(message)) + "\n")
        self._dirty = False

    @staticmethod
    def load(session_id: str) -> "Session":
        path = daemon_home() / "sessions" / session_id
        if not (path / "meta.json").exists():
            raise SessionError(f"No session '{session_id}'")
        try:
            meta = SessionMeta.from_json(
                json.loads((path / "meta.json").read_text("utf-8")))
            messages: list[Message] = []
            transcript = path / "transcript.jsonl"
            if transcript.exists():
                for line in transcript.read_text("utf-8").splitlines():
                    if line.strip():
                        messages.append(Message.from_json(json.loads(line)))
            usage = Usage()
            usage_path = path / "usage.json"
            if usage_path.exists():
                payload = json.loads(usage_path.read_text("utf-8"))
                usage = Usage(
                    input_tokens=int(payload.get("input_tokens") or 0),
                    output_tokens=int(payload.get("output_tokens") or 0))
            session = Session(meta=meta, messages=messages, usage=usage)
            session._dirty = False
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
