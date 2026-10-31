"""Provider detection: prefixes, format families, ambiguity honesty."""

from daemon.providers.detect import fingerprint


def test_anthropic_prefix_is_unique():
    result = fingerprint("sk-ant-api03-" + "A" * 40)
    assert result.unique
    assert result.best.provider_id == "anthropic"


def test_openrouter_prefix_is_unique():
    result = fingerprint("sk-or-v1-" + "x" * 40)
    assert result.unique
    assert result.best.provider_id == "openrouter"


def test_groq_prefix_is_unique():
    assert fingerprint("gsk_" + "a" * 40).best.provider_id == "groq"


def test_gemini_format():
    result = fingerprint("AIza" + "A" * 35)
    assert result.unique
    assert result.best.provider_id == "gemini"


def test_zai_format():
    result = fingerprint("a" * 32 + "." + "b" * 16)
    assert result.unique
    assert result.best.provider_id == "zai"


def test_deepseek_hex32_is_ambiguous_with_dashscope():
    result = fingerprint("sk-" + "ab" * 16)
    assert not result.unique
    ids = result.provider_ids()
    assert "deepseek" in ids and "dashscope" in ids


def test_generic_sk_is_ambiguous_and_honest():
    result = fingerprint("sk-" + "Z" * 44)
    assert result.needs_question


def test_plain_alnum32_is_ambiguous():
    result = fingerprint("A" * 32)
    assert not result.unique
    assert "mistral" in result.provider_ids()


def test_empty_key():
    assert fingerprint("").candidates == []


def test_env_confirmation_boosts_confidence(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-" + "ab" * 16)
    result = fingerprint("sk-" + "ab" * 16)
    assert result.unique
    assert result.best.provider_id == "deepseek"
    assert result.best.confidence >= 95


def test_unknown_format_has_no_candidates():
    result = fingerprint("totally-unknown-key-format")
    assert result.candidates == []
    assert result.needs_question
