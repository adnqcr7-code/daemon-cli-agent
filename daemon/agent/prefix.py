"""Cache discipline: the stable system prefix (v0.9.0 Phase 3).

Providers cache the prompt prefix (system prompt + early history)
and charge full price — and full latency — when it changes. The
discipline is therefore two rules, enforced here:

1. **Stable system prompt.** The runtime computes the system prompt
   once per run and reuses it for every turn. One-shot additions
   (the failure-loop nudge, the context estimate) are appended as
   tail messages on the request copy only — never into the system
   prompt, never into the session — exactly like the Reflex signal.
2. **Append-only history.** A turn may only append messages; it may
   never rewrite an earlier one in place. Compaction and context
   optimization are the only sanctioned rewrites, and they go
   through an explicit ``reset()`` with a reason, so a rewrite is
   always a deliberate, logged event — never a silent mutation.

``PrefixGuard`` records a canonical hash of (system prompt, message
list) at each request build and raises ``PrefixViolation`` the
moment an earlier segment differs. The runtime lets the violation
fail the run loudly: a silently rewritten prefix would mean the
provider's cache no longer matches what the agent thinks it sent.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from ..errors import DaemonError
from ..log import get_logger
from ..providers.types import Message

_logger = get_logger("prefix")


class PrefixViolation(DaemonError):
    """The cached prefix was rewritten in place instead of appended to."""


@dataclass(frozen=True)
class PrefixSnapshot:
    """One observed prefix: its hashes, for tests and debugging."""

    system_hash: str
    message_hashes: tuple[str, ...] = ()

    @property
    def prefix_hash(self) -> str:
        """Hash of the whole prefix (system + history, in order)."""
        digest = hashlib.sha256()
        digest.update(self.system_hash.encode("utf-8"))
        for message_hash in self.message_hashes:
            digest.update(message_hash.encode("utf-8"))
        return digest.hexdigest()

    @property
    def message_count(self) -> int:
        return len(self.message_hashes)


def _canonical(message: Message) -> str:
    return json.dumps(message.to_json(), sort_keys=True, ensure_ascii=False)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class PrefixGuard:
    """Enforces the stable-prefix / append-only rules for one run."""

    def __init__(self) -> None:
        self._baseline: PrefixSnapshot | None = None
        self.snapshots: list[PrefixSnapshot] = []

    def check(self, system: str, messages: list[Message]) -> PrefixSnapshot:
        """Record and verify the current prefix.

        The first call baselines; later calls require the same system
        hash and require the new message list to extend the previous
        one (same hashes at every earlier index). Raises
        :class:`PrefixViolation` on the first difference.
        """
        snapshot = PrefixSnapshot(
            system_hash=_hash(system),
            message_hashes=tuple(_hash(_canonical(m)) for m in messages))
        if self._baseline is not None:
            previous = self._baseline
            if snapshot.system_hash != previous.system_hash:
                raise PrefixViolation(
                    "system prompt changed mid-run — the cached prefix "
                    "is no longer what the provider holds")
            if (snapshot.message_hashes[:len(previous.message_hashes)]
                    != previous.message_hashes):
                first = next(
                    (index for index, (old, new) in enumerate(zip(
                        previous.message_hashes, snapshot.message_hashes))
                     if old != new),
                    len(previous.message_hashes))
                raise PrefixViolation(
                    "conversation history was rewritten in place at "
                    f"message {first} instead of appended to — the "
                    "cached prefix no longer matches")
        self._baseline = snapshot
        self.snapshots.append(snapshot)
        return snapshot

    def reset(self, reason: str) -> None:
        """Re-baseline after a sanctioned rewrite (compaction,
        context optimization). The rewrite itself is logged."""
        _logger.debug("prefix baseline reset: %s", reason)
        self._baseline = None

    @property
    def checks(self) -> int:
        return len(self.snapshots)
