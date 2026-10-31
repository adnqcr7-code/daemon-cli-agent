"""Authentication flows: detection, one-question ambiguity, verification,
storage and model discovery - all against local mocks."""

import pytest

from daemon.auth.manager import AuthManager
from daemon.errors import AuthError


def _manager(config):
    return AuthManager(config)


def test_add_custom_endpoint_verifies_and_discovers_models(mock_openai, config,
                                                           scripted_io):
    manager = _manager(config)
    profile = manager.add(
        scripted_io, key="test-key-1234567890",
        base_url=mock_openai.url + "/v1", label="mocklab")
    assert profile.provider == "custom"
    assert profile.endpoint == mock_openai.url + "/v1"
    stored = manager.store.get("custom#mocklab")
    assert stored is not None
    # the secret round-trips through the vault, never through auth.json
    assert manager.store.resolve_key(stored) == "test-key-1234567890"
    assert "test-key-1234567890" not in (manager.store.path).read_text()
    messages = " ".join(scripted_io.messages)
    assert "Found 2 accessible model" in messages
    assert "Authentication successful" in messages
    manager.close()


def test_add_ambiguous_key_asks_exactly_one_question(config, home, scripted_io):
    manager = _manager(config)
    scripted_io.secret = "sk-" + "ab" * 16  # deepseek vs dashscope
    scripted_io.choices = ["deepseek"]
    profile = manager.add(scripted_io, verify=False)
    assert profile.provider == "deepseek"
    questions = [m for m in scripted_io.messages if m.startswith("choice:")]
    assert len(questions) == 1
    assert "Which provider issued this key?" in questions[0]
    manager.close()


def test_add_unique_key_skips_the_question(config, home, scripted_io,
                                            monkeypatch):
    manager = _manager(config)
    scripted_io.secret = "sk-ant-api03-" + "A" * 40
    # keep the test offline: pretend verification failed benignly
    def fake_verify(io, profile, key):
        io.warn("verification unavailable in this test")
        return None

    monkeypatch.setattr(manager, "_verify", fake_verify)
    profile = manager.add(scripted_io, verify=True)
    assert profile.provider == "anthropic"
    assert not [m for m in scripted_io.messages if m.startswith("choice:")]
    assert "Provider identified: Anthropic" in " ".join(scripted_io.messages)
    manager.close()


def test_add_local_provider_needs_no_key(config, home, scripted_io, monkeypatch):
    manager = _manager(config)
    monkeypatch.setattr(manager, "_verify_or_none", lambda *a, **k: None)
    scripted_io.confirms = True  # save even though server unreachable
    profile = manager.add(scripted_io, provider="ollama")
    assert profile.provider == "ollama"
    assert manager.store.resolve_key(profile) is None
    manager.close()


def test_add_from_environment_variable(config, home, scripted_io, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_env_imported_0000000000000")
    manager = _manager(config)
    profile = manager.add(scripted_io, env_var="GROQ_API_KEY", verify=False)
    assert profile.env_var == "GROQ_API_KEY"
    assert manager.store.resolve_key(profile) == "gsk_env_imported_0000000000000"
    assert manager.vault.get(profile.id) is None  # nothing stored
    manager.close()


def test_add_rejects_duplicate_profile(config, home, scripted_io):
    manager = _manager(config)
    manager.add(scripted_io, key="k" * 30, base_url="http://localhost:9/v1",
                label="one", verify=False)
    with pytest.raises(AuthError):
        manager.add(scripted_io, key="k" * 30,
                    base_url="http://localhost:9/v1", label="one",
                    verify=False)
    manager.close()


def test_verification_failure_without_confirm_aborts(mock_openai, config):
    manager = _manager(config)

    class RejectingIO:
        def info(self, message): ...
        def warn(self, message): ...
        def success(self, message): ...
        def prompt_secret(self, prompt): return "wrong-key-0000000000"
        def ask_text(self, prompt, default=None): return ""
        def ask_choice(self, question, options): return options[0][0]
        def confirm(self, question, default=False): return False

    with pytest.raises(AuthError):
        manager.add(RejectingIO(), base_url=mock_openai.url + "/v1")
    assert manager.store.profiles() == []
    manager.close()


def test_models_flow_uses_cache(mock_openai, config, scripted_io):
    manager = _manager(config)
    manager.add(scripted_io, key="test-key-1234567890",
                base_url=mock_openai.url + "/v1", label=None)
    profile, models = manager.models(scripted_io)
    assert profile.id == "custom"
    assert {model.model_id for model in models} == {"mock-coder", "mock-fast"}
    manager.close()


def test_switch_and_remove(config, home, scripted_io):
    manager = _manager(config)
    manager.add(scripted_io, key="k" * 30, base_url="http://localhost:9/v1",
                label="first", verify=False)
    manager.add(scripted_io, key="k" * 31, base_url="http://localhost:9/v1",
                label="second", verify=False)
    active = manager.switch("custom#second")
    assert active.id == "custom#second"
    removed = manager.remove("custom#first")
    assert removed.id == "custom#first"
    assert [p.id for p in manager.store.profiles()] == ["custom#second"]
    manager.close()


def test_secrets_registered_with_global_redactor(mock_openai, config,
                                                  scripted_io):
    from daemon.security import redact, redactor

    manager = _manager(config)
    manager.add(scripted_io, key="test-key-1234567890",
                base_url=mock_openai.url + "/v1")
    assert "test-key-1234567890" not in redact(
        "leak attempt test-key-1234567890")
    manager.close()
    redactor().forget("test-key-1234567890")
