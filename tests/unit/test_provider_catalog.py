from daemon.providers.catalog import PROVIDERS, provider_by_id
from daemon.providers.registry import AdapterRegistry


def test_every_catalog_provider_has_a_real_adapter():
    """The '30+ providers' claim is backed by 3 protocol adapters: every
    descriptor's protocol must resolve to an implemented adapter, and no
    descriptor may name a protocol the registry cannot build."""
    import asyncio

    from daemon.providers.registry import _PROTOCOLS

    registry = AdapterRegistry()
    assert len(PROVIDERS) >= 30
    for descriptor in PROVIDERS.values():
        assert descriptor.protocol in _PROTOCOLS, descriptor.id
        adapter = registry.create(descriptor, api_key="test-key")
        assert adapter is not None
        close = getattr(adapter, "close", None)
        if close is not None:
            asyncio.run(close())



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
