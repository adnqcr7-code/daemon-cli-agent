"""Credential vault backends and the profile store."""

import json

import pytest

from daemon.auth.store import CredentialStore, Profile
from daemon.auth.vault import EncryptedFileBackend, PlainFileBackend, open_vault
from daemon.errors import AuthError


def test_plain_backend_roundtrip(home):
    backend = PlainFileBackend(home / "vault.json")
    backend.set("openai", "sk-key-1234567890")
    assert backend.get("openai") == "sk-key-1234567890"
    assert backend.delete("openai")
    assert backend.get("openai") is None
    assert not backend.delete("openai")
    # warning is surfaced for doctor/UI
    assert backend.warning


def test_encrypted_backend_roundtrip(home, monkeypatch):
    monkeypatch.delenv("DAEMON_VAULT_BACKEND")
    backend = EncryptedFileBackend(home / "vault.bin", home / "vault.key")
    backend.set("anthropic", "sk-ant-key-abcdefghij")
    assert backend.get("anthropic") == "sk-ant-key-abcdefghij"
    # ciphertext on disk must not contain the plaintext
    blob = (home / "vault.bin").read_text()
    assert "sk-ant-key-abcdefghij" not in blob
    assert backend.delete("anthropic")


def test_encrypted_backend_requires_master_key(home, monkeypatch):
    monkeypatch.delenv("DAEMON_VAULT_BACKEND")
    backend = EncryptedFileBackend(home / "vault.bin", home / "vault.key")
    backend.set("x", "value-12345678")
    # losing the key -> clear error, not silent data loss
    (home / "vault.key").unlink()
    with pytest.raises(AuthError):
        EncryptedFileBackend(home / "vault.bin", home / "vault.key")


def test_open_vault_forced_backend(home, monkeypatch):
    monkeypatch.setenv("DAEMON_VAULT_BACKEND", "plain")
    assert isinstance(open_vault(home), PlainFileBackend)


def test_open_vault_prefers_encrypted_when_available(home, monkeypatch):
    monkeypatch.delenv("DAEMON_VAULT_BACKEND")
    backend = open_vault(home)
    assert isinstance(backend, EncryptedFileBackend)
    backend.close()


def test_credential_store_profiles(home):
    store = CredentialStore(home, PlainFileBackend(home / "vault.json"))
    store.add(Profile(provider="openai", label="work"),
              secret="sk-work-key-0000000")
    store.add(Profile(provider="openai", label="personal"),
              secret="sk-personal-key-000")
    store.add(Profile(provider="groq"), secret="gsk_key_00000000000000000")
    assert len(store.profiles()) == 3
    assert store.active().id == "openai#work"
    store.set_active("groq")
    assert store.active().provider == "groq"
    assert store.resolve_key(store.get("openai#personal")) == "sk-personal-key-000"
    removed = store.remove("openai#work")
    assert removed.id == "openai#work"
    assert store.get("openai#work") is None


def test_credential_store_never_writes_secrets_to_auth_json(home):
    backend = PlainFileBackend(home / "vault.json")
    store = CredentialStore(home, backend)
    store.add(Profile(provider="openai"), secret="sk-super-secret-value")
    payload = json.loads((home / "auth.json").read_text())
    dumped = json.dumps(payload)
    assert "sk-super-secret-value" not in dumped


def test_duplicate_profile_rejected(home):
    store = CredentialStore(home, PlainFileBackend(home / "vault.json"))
    store.add(Profile(provider="openai"), secret="sk-one-key-000000000")
    with pytest.raises(AuthError):
        store.add(Profile(provider="openai"), secret="sk-two-key-000000000")


def test_env_profile_resolves_from_environment(home, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_from_environment_000000")
    store = CredentialStore(home, PlainFileBackend(home / "vault.json"))
    store.add(Profile(provider="groq", env_var="GROQ_API_KEY"))
    assert store.resolve_key(store.active()) == "gsk_from_environment_000000"
    # nothing stored in the vault
    assert store.vault.get("groq") is None
