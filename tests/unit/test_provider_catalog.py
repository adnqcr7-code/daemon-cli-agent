from daemon.providers.catalog import provider_by_id


def test_ollama_cloud_is_a_keyed_openai_compatible_provider():
    provider = provider_by_id("ollama-cloud")

    assert provider is not None
    assert provider.name == "Ollama Cloud"
    assert provider.protocol == "openai"
    assert provider.category == "cloud"
    assert provider.base_url == "https://ollama.com/v1"
    assert provider.requires_key is True
    assert provider.env_keys == ("OLLAMA_API_KEY",)
    assert provider.stream_usage is False
