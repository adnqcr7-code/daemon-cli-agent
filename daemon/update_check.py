"""Check GitHub Releases for a newer Daemon version."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import httpx

from .log import get_logger

_logger = get_logger("updates")
_RELEASE_URL = (
    "https://api.github.com/repos/adnqcr7-code/daemon-cli-agent/releases/latest"
)
_CACHE_TTL_SECONDS = 24 * 60 * 60
_VERSION_RE = re.compile(r"v?(\d+(?:\.\d+)*)(?:[-+][0-9A-Za-z.-]+)?")


def check_for_update(current_version: str, home: Path, *,
                     now: float | None = None) -> str | None:
    """Return a newer release version, using a daily per-user cache."""
    checked_at = time.time() if now is None else now
    cache_path = home / "update-check.json"
    cached = _read_cache(cache_path)
    if (cached is not None
            and 0 <= checked_at - cached[0] < _CACHE_TTL_SECONDS):
        latest = cached[1]
    else:
        latest = _fetch_latest_version()
        if latest is None and cached is not None:
            latest = cached[1]
        else:
            _write_cache(cache_path, checked_at, latest)

    latest_key = _version_key(latest) if latest else None
    current_key = _version_key(current_version)
    if latest_key is not None and current_key is not None:
        if latest_key > current_key:
            return latest
    return None


def _fetch_latest_version() -> str | None:
    try:
        response = httpx.get(
            _RELEASE_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "daemon-cli-agent",
            },
            timeout=httpx.Timeout(2.0, connect=1.0),
            follow_redirects=True,
        )
        if response.status_code == 404:
            return ""
        response.raise_for_status()
        payload: Any = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        _logger.debug("Could not check for updates: %s", exc)
        return None

    if not isinstance(payload, dict):
        return None
    tag = payload.get("tag_name")
    if not isinstance(tag, str) or _version_key(tag) is None:
        return None
    return tag.removeprefix("v")


def _version_key(version: str) -> tuple[int, ...] | None:
    match = _VERSION_RE.fullmatch(version.strip())
    if match is None:
        return None
    try:
        parts = [int(part) for part in match.group(1).split(".")]
        while len(parts) > 1 and parts[-1] == 0:
            parts.pop()
        return tuple(parts)
    except ValueError:
        return None


def _read_cache(path: Path) -> tuple[float, str] | None:
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        checked_at = payload.get("checked_at")
        version = payload.get("latest_version")
        if (not isinstance(checked_at, (int, float))
                or not isinstance(version, str)
                or (version and _version_key(version) is None)):
            return None
        return float(checked_at), version
    except (OSError, ValueError):
        return None


def _write_cache(path: Path, checked_at: float, version: str | None) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "checked_at": checked_at,
            "latest_version": version or "",
        }), encoding="utf-8")
    except OSError as exc:
        _logger.debug("Could not cache update check: %s", exc)
