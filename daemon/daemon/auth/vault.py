"""Secure credential storage with a backend ladder:

1. **OS keychain** (Windows Credential Manager, macOS Keychain, Linux
   Secret Service via `libsecret`) through the optional ``keyring`` package.
2. **Encrypted local vault** - AES-256-GCM using the ``cryptography``
   package; master key kept in a separate 0600 file so backups that only
   capture the vault file leak nothing.
3. **Restrictive-permissions fallback** - plain JSON at mode 0600 with a
   loud warning (comparable to AWS-style credential files). Used only when
   neither optional extra is installed.

The active backend is reported by ``daemon doctor`` and ``daemon auth list``
so users always know their security posture. Backend choice can be forced
with ``DAEMON_VAULT_BACKEND=keyring|encrypted|plain``.
"""

from __future__ import annotations

import base64
import json
import os
from abc import ABC, abstractmethod
from pathlib import Path

from ..errors import AuthError
from ..log import audit, get_logger

_logger = get_logger("auth.vault")


class VaultBackend(ABC):
    name: str = "abstract"
    security_level: str = "unknown"
    warning: str | None = None

    @abstractmethod
    def get(self, account: str) -> str | None: ...

    @abstractmethod
    def set(self, account: str, secret: str) -> None: ...

    @abstractmethod
    def delete(self, account: str) -> bool: ...

    def close(self) -> None:  # pragma: no cover - default no-op
        return None


# ---------------------------------------------------------------------------
# OS keychain
# ---------------------------------------------------------------------------


class KeyringBackend(VaultBackend):
    name = "os-keychain"
    security_level = "os-keychain"

    def __init__(self) -> None:
        try:
            import keyring  # type: ignore[import-not-found]
        except ImportError as exc:
            raise AuthError(
                "The 'keyring' package is not installed.",
                hint="pip install 'daemon-agent[keyring]'") from exc
        self._keyring = keyring
        self._service = "daemon"
        # Round-trip probe: a present-but-broken keyring must not be used.
        probe_account = "__daemon_probe__"
        try:
            keyring.set_password(self._service, probe_account, "probe")
            if keyring.get_password(self._service, probe_account) != "probe":
                raise RuntimeError("keyring round-trip mismatch")
            keyring.delete_password(self._service, probe_account)
        except Exception as exc:
            raise AuthError(
                f"OS keychain unavailable: {exc}",
                hint="Install libsecret/gnome-keyring on Linux, or set "
                     "DAEMON_VAULT_BACKEND=encrypted") from exc

    def get(self, account: str) -> str | None:
        try:
            return self._keyring.get_password(self._service, account)
        except Exception as exc:
            raise AuthError(f"Keychain read failed: {exc}") from exc

    def set(self, account: str, secret: str) -> None:
        try:
            self._keyring.set_password(self._service, account, secret)
        except Exception as exc:
            raise AuthError(f"Keychain write failed: {exc}") from exc

    def delete(self, account: str) -> bool:
        try:
            if self._keyring.get_password(self._service, account) is None:
                return False
            self._keyring.delete_password(self._service, account)
            return True
        except Exception as exc:
            raise AuthError(f"Keychain delete failed: {exc}") from exc


# ---------------------------------------------------------------------------
# AES-256-GCM encrypted file
# ---------------------------------------------------------------------------


class EncryptedFileBackend(VaultBackend):
    name = "encrypted"
    security_level = "encrypted (AES-256-GCM, separate master key)"

    def __init__(self, vault_path: Path, key_path: Path) -> None:
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
        except ImportError as exc:
            raise AuthError(
                "The 'cryptography' package is not installed.",
                hint="pip install 'daemon-agent[crypto]'") from exc
        self._vault_path = vault_path
        self._key_path = key_path
        self._load_or_create_key()
        self._entries = self._load()

    def _load_or_create_key(self) -> None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401

        if self._key_path.exists():
            key = base64.b64decode(self._key_path.read_bytes())
            if len(key) != 32:
                raise AuthError("Vault master key is corrupt (wrong length).")
            self._key = key
            return
        self._key_path.parent.mkdir(parents=True, exist_ok=True)
        key = os.urandom(32)
        self._key_path.write_bytes(base64.b64encode(key))
        self._restrict(self._key_path)
        self._key = key

    def _load(self) -> dict[str, str]:
        if not self._vault_path.exists():
            return {}
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        try:
            blob = json.loads(self._vault_path.read_text(encoding="utf-8"))
            nonce = base64.b64decode(blob["nonce"])
            ciphertext = base64.b64decode(blob["data"])
            plaintext = AESGCM(self._key).decrypt(nonce, ciphertext, None)
            entries = json.loads(plaintext.decode("utf-8"))
            if not isinstance(entries, dict):
                raise ValueError("vault payload is not an object")
            return entries
        except FileNotFoundError:
            return {}
        except Exception as exc:
            raise AuthError(
                f"Cannot open the encrypted vault: {exc}",
                hint="If the master key was lost, remove "
                     f"'{self._vault_path}' and re-add credentials.") from exc

    def _save(self) -> None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = os.urandom(12)
        ciphertext = AESGCM(self._key).encrypt(
            nonce, json.dumps(self._entries).encode("utf-8"), None)
        self._vault_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._vault_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "version": 1,
            "nonce": base64.b64encode(nonce).decode(),
            "data": base64.b64encode(ciphertext).decode(),
        }), encoding="utf-8")
        self._restrict(tmp)
        os.replace(tmp, self._vault_path)

    @staticmethod
    def _restrict(path: Path) -> None:
        try:
            os.chmod(path, 0o600)
        except OSError:  # pragma: no cover - platform specific
            pass

    def get(self, account: str) -> str | None:
        return self._entries.get(account)

    def set(self, account: str, secret: str) -> None:
        self._entries[account] = secret
        self._save()

    def delete(self, account: str) -> bool:
        if account not in self._entries:
            return False
        del self._entries[account]
        self._save()
        return True


# ---------------------------------------------------------------------------
# Restrictive-permissions fallback
# ---------------------------------------------------------------------------


class PlainFileBackend(VaultBackend):
    name = "plain"
    security_level = "restrictive file permissions only"
    warning = (
        "Credentials are stored as plain JSON with 0600 permissions because "
        "neither the OS keychain nor the 'cryptography' package is available. "
        "Install 'daemon-agent[keyring]' or 'daemon-agent[crypto]' for "
        "encrypted storage."
    )

    def __init__(self, path: Path) -> None:
        self._path = path
        if path.exists():
            try:
                self._entries: dict[str, str] = json.loads(path.read_text("utf-8"))
            except Exception as exc:
                raise AuthError(f"Cannot read credential file: {exc}") from exc
        else:
            self._entries = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._entries), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:  # pragma: no cover
            pass
        os.replace(tmp, self._path)

    def get(self, account: str) -> str | None:
        return self._entries.get(account)

    def set(self, account: str, secret: str) -> None:
        self._entries[account] = secret
        self._save()

    def delete(self, account: str) -> bool:
        if account not in self._entries:
            return False
        del self._entries[account]
        self._save()
        return True


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def open_vault(home: Path) -> VaultBackend:
    """Pick the best available backend (env override honoured)."""
    forced = os.environ.get("DAEMON_VAULT_BACKEND", "").strip().lower()
    attempts: list[str]
    if forced in ("keyring", "encrypted", "plain"):
        attempts = [forced]
    else:
        attempts = ["keyring", "encrypted", "plain"]
    errors: list[str] = []
    for choice in attempts:
        try:
            if choice == "keyring":
                backend = KeyringBackend()
            elif choice == "encrypted":
                backend = EncryptedFileBackend(home / "vault.bin", home / "vault.key")
            else:
                backend = PlainFileBackend(home / "vault.json")
            audit("vault-backend-selected", backend=backend.name)
            _logger.debug("credential backend: %s", backend.name)
            return backend
        except AuthError as exc:
            errors.append(f"{choice}: {exc}")
    raise AuthError(
        "No usable credential storage backend is available:\n  " +
        "\n  ".join(errors))
