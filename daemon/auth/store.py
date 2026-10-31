"""Credential profiles: multiple accounts, multiple providers, one active.

The profile database (``~/.daemon/auth.json``) stores NO secret material -
only metadata. Secrets live in the vault (keychain / encrypted file) or, if
the profile was imported from an environment variable, in the environment
itself.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..errors import AuthError
from ..log import audit, get_logger
from ..security import redact
from .vault import VaultBackend

_logger = get_logger("auth.store")


@dataclass
class Profile:
    provider: str
    label: str | None = None
    endpoint: str | None = None  # custom endpoint (incl. resolved placeholders)
    account_id: str | None = None  # e.g. Cloudflare account id
    env_var: str | None = None  # resolve key from environment instead of vault
    default_model: str | None = None
    created: float = field(default_factory=time.time)
    last_used: float | None = None

    @property
    def id(self) -> str:
        base = self.provider
        return f"{base}#{self.label}" if self.label else base

    @property
    def display(self) -> str:
        return self.label or self.provider

    def to_json(self) -> dict:
        return asdict(self) | {"id": self.id}

    @staticmethod
    def from_json(data: dict) -> "Profile":
        return Profile(
            provider=data["provider"],
            label=data.get("label"),
            endpoint=data.get("endpoint"),
            account_id=data.get("account_id"),
            env_var=data.get("env_var"),
            default_model=data.get("default_model"),
            created=float(data.get("created") or time.time()),
            last_used=data.get("last_used"),
        )


class CredentialStore:
    """Profile registry on disk + secret resolution through the vault."""

    def __init__(self, home: Path, vault: VaultBackend) -> None:
        self.home = Path(home)
        self.vault = vault
        self.path = self.home / "auth.json"
        self._data = self._load()

    # -- persistence ------------------------------------------------------

    def _load(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "profiles": [], "active_profile": None}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AuthError(f"Cannot read {self.path}: {exc}") from exc
        data.setdefault("profiles", [])
        data.setdefault("active_profile", None)
        return data

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        try:
            import os

            os.chmod(tmp, 0o600)
        except OSError:  # pragma: no cover
            pass
        os.replace(tmp, self.path)

    # -- profiles ----------------------------------------------------------

    def profiles(self) -> list[Profile]:
        return [Profile.from_json(entry) for entry in self._data["profiles"]]

    def get(self, profile_id: str) -> Profile | None:
        for profile in self.profiles():
            if profile.id == profile_id:
                return profile
        return None

    def find(self, provider: str, label: str | None = None) -> Profile | None:
        for profile in self.profiles():
            if profile.provider == provider and profile.label == label:
                return profile
        return None

    def active(self) -> Profile | None:
        active_id = self._data.get("active_profile")
        if active_id:
            return self.get(active_id)
        profiles = self.profiles()
        return profiles[0] if profiles else None

    def set_active(self, profile_id: str) -> Profile:
        profile = self.get(profile_id)
        if profile is None:
            raise AuthError(f"No profile '{profile_id}'. See: daemon auth list")
        self._data["active_profile"] = profile_id
        self._save()
        audit("auth-switched", profile=redact(profile_id))
        return profile

    def add(self, profile: Profile, secret: str | None = None,
            *, allow_keyless: bool = False) -> Profile:
        existing = self.find(profile.provider, profile.label)
        if existing is not None:
            raise AuthError(
                f"A profile for {profile.id} already exists; remove it first "
                f"or use a different --label.")
        if profile.env_var is None:
            if secret is None and not allow_keyless:
                raise AuthError("Refusing to store an empty credential.")
            if secret is not None:
                self.vault.set(profile.id, secret)
        self._data["profiles"].append(profile.to_json())
        if self._data.get("active_profile") is None:
            self._data["active_profile"] = profile.id
        self._save()
        audit("auth-added", provider=profile.provider, profile=profile.id,
              storage="env" if profile.env_var else self.vault.name)
        return profile

    def remove(self, profile_id: str) -> Profile:
        profile = self.get(profile_id)
        if profile is None:
            raise AuthError(f"No profile '{profile_id}'")
        if profile.env_var is None:
            self.vault.delete(profile.id)
        self._data["profiles"] = [
            entry for entry in self._data["profiles"]
            if entry.get("id") != profile_id
        ]
        if self._data.get("active_profile") == profile_id:
            remaining = self._data["profiles"]
            self._data["active_profile"] = (
                remaining[0].get("id") if remaining else None)
        self._save()
        audit("auth-removed", profile=redact(profile_id))
        return profile

    def set_default_model(self, profile_id: str, model: str) -> None:
        for entry in self._data["profiles"]:
            if entry.get("id") == profile_id:
                entry["default_model"] = model
                break
        else:
            raise AuthError(f"No profile '{profile_id}'")
        self._save()

    def touch(self, profile: Profile) -> None:
        profile.last_used = time.time()
        for entry in self._data["profiles"]:
            if entry.get("id") == profile.id:
                entry["last_used"] = profile.last_used
                break
        self._save()

    # -- secret resolution ---------------------------------------------------

    def resolve_key(self, profile: Profile) -> str | None:
        """Fetch the secret for a profile without ever printing it."""
        if profile.env_var:
            import os

            return os.environ.get(profile.env_var)
        return self.vault.get(profile.id)
