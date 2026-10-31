"""Logging and audit trail with mandatory secret redaction.

Daemon keeps two streams under ``~/.daemon/logs``:

* ``daemon.log``  - operational debug log (rotating).
* ``audit.log``   - security-relevant events: permission decisions, tool
  invocations, credential additions, model switches. Never contains secrets
  because every record passes through the global :class:`SecretRedactor`.

Console output is intentionally NOT handled here; the UI layer owns the
terminal.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path

from .security import redact

_LOGGER_NAME = "daemon"
_AUDIT_NAME = "daemon.audit"
_configured = False
_configured_home: Path | None = None


class RedactingFilter(logging.Filter):
    """Redacts every message and traceback fragment passing through."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed args
            message = str(record.msg)
        record.msg = redact(message)
        record.args = ()
        if record.exc_info:
            import traceback

            exc_type, exc_value, exc_tb = record.exc_info
            try:
                formatted = "".join(traceback.format_exception(
                    exc_type, exc_value, exc_tb))
            except Exception:  # pragma: no cover - defensive
                formatted = f"{exc_type.__name__}: {exc_value}"
            # Pre-render and redact so the formatter never emits raw secrets
            # from exception messages or source lines.
            record.exc_text = redact(formatted)
            record.exc_info = None
        return True


def setup_logging(home: Path, *, level: int | None = None, audit: bool = True) -> None:
    """Initialise the rotating debug log and the audit log. Idempotent."""
    global _configured, _configured_home
    home = Path(home)
    if _configured and _configured_home == home:
        return
    if _configured:  # DAEMON_HOME changed (tests, multi-home setups)
        logging.getLogger(_LOGGER_NAME).handlers.clear()
        logging.getLogger(_AUDIT_NAME).handlers.clear()
    if level is None:
        level_name = os.environ.get("DAEMON_LOG_LEVEL", "INFO").upper()
        level = getattr(logging, level_name, logging.INFO)

    log_dir = Path(home) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger(_LOGGER_NAME)
    root.setLevel(level)

    handler = logging.handlers.RotatingFileHandler(
        log_dir / "daemon.log", maxBytes=5 * 1024 * 1024, backupCount=2,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    ))
    handler.addFilter(RedactingFilter())
    root.addHandler(handler)
    root.propagate = False

    if audit:
        audit_logger = logging.getLogger(_AUDIT_NAME)
        audit_logger.setLevel(logging.INFO)
        audit_handler = logging.handlers.RotatingFileHandler(
            log_dir / "audit.log", maxBytes=2 * 1024 * 1024, backupCount=3,
            encoding="utf-8",
        )
        audit_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        audit_handler.addFilter(RedactingFilter())
        audit_logger.addHandler(audit_handler)
        audit_logger.propagate = False

    # Silence noisy third-party loggers unless explicitly debugging.
    for noisy in ("httpx", "httpcore", "keyring", "asyncio"):
        logging.getLogger(noisy).setLevel(
            logging.DEBUG if os.environ.get("DAEMON_LOG_LEVEL") == "DEBUG" else logging.WARNING
        )
    _configured = True
    _configured_home = home


def get_logger(name: str = _LOGGER_NAME) -> logging.Logger:
    """Return a namespaced child logger."""
    if not name.startswith(_LOGGER_NAME):
        name = f"{_LOGGER_NAME}.{name}"
    return logging.getLogger(name)


def audit(event: str, **details: object) -> None:
    """Append a security-relevant event to the audit log (best effort)."""
    logger = logging.getLogger(_AUDIT_NAME)
    if not logger.handlers:  # audit before setup_logging (tests, library use)
        return
    payload = " ".join(f"{key}={redact(str(value))}" for key, value in details.items())
    logger.info("%s %s", event, payload)


def describe_exception(exc: BaseException) -> str:
    """User-safe exception text: type + message, redacted, no traceback."""
    return redact(f"{type(exc).__name__}: {exc}")


def safe_argv() -> str:
    """The command line used to launch Daemon, redacted."""
    return redact(" ".join(sys.argv))


_ = time  # available for future timing helpers
