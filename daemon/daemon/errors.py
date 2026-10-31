"""Error hierarchy and process exit codes for Daemon.

Every user-facing failure should surface as a DaemonError subclass so the CLI
can print a clean message (never a raw traceback, never a secret) and exit
with a meaningful code.
"""

from __future__ import annotations


class DaemonError(Exception):
    """Base class for all expected Daemon failures."""

    exit_code = 1

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


class ConfigError(DaemonError):
    """Configuration is missing, unreadable or invalid."""

    exit_code = 2


class AuthError(DaemonError):
    """Credential problems: missing, invalid, revoked or ambiguous."""

    exit_code = 3


class ProviderError(DaemonError):
    """A provider API request failed after retries (or was rejected)."""

    exit_code = 4

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        status: int | None = None,
        retryable: bool = False,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        self.provider = provider
        self.status = status
        self.retryable = retryable


class ModelError(DaemonError):
    """Model selection / capability problems."""

    exit_code = 5


class ToolError(DaemonError):
    """A tool rejected its input or failed while executing."""

    exit_code = 6


class PathEscapeError(ToolError):
    """A tool was asked to operate outside the trusted workspace."""


class PermissionDenied(DaemonError):
    """The permission engine (or the user) refused an action."""

    exit_code = 7


class SessionError(DaemonError):
    """Session persistence problems."""

    exit_code = 8


class McpError(DaemonError):
    """MCP server connectivity / protocol problems."""

    exit_code = 9


class CheckpointError(DaemonError):
    """Checkpoint creation or restore failed."""


class UsageLimitExceeded(DaemonError):
    """A configured token / cost ceiling was hit. Never silent."""

    exit_code = 10


class Interrupted(DaemonError):
    """The user or the system cancelled the current operation."""

    exit_code = 130
