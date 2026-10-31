"""Adapter registry: protocol -> implementation, plus custom endpoints."""

from __future__ import annotations

from ..config import Config
from ..errors import AuthError, ProviderError
from .anthropic import AnthropicAdapter
from .base import ProviderAdapter
from .catalog import PROVIDERS, ProviderDescriptor, provider_by_id
from .gemini import GeminiAdapter
from .openai_compat import OpenAICompatAdapter
from .types import RemoteModel

_PROTOCOLS: dict[str, type[ProviderAdapter]] = {
    "openai": OpenAICompatAdapter,
    "anthropic": AnthropicAdapter,
    "gemini": GeminiAdapter,
}


def custom_descriptor(name: str, base_url: str) -> ProviderDescriptor:
    """Descriptor for a user-supplied OpenAI-compatible endpoint."""
    return ProviderDescriptor(
        id="custom",
        name=name or "Custom OpenAI-compatible",
        protocol="openai",
        category="gateway",
        base_url=base_url,
        notes="User-defined OpenAI-compatible endpoint.",
        stream_usage=False,
    )


class AdapterRegistry:
    """Creates configured adapter instances for provider descriptors."""

    def create(
        self,
        descriptor: ProviderDescriptor,
        *,
        api_key: str | None = None,
        endpoint: str | None = None,
        config: Config | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> ProviderAdapter:
        protocol = _PROTOCOLS.get(descriptor.protocol)
        if protocol is None:
            raise ProviderError(
                f"No adapter implemented for protocol '{descriptor.protocol}' "
                f"(provider {descriptor.id}).")
        return protocol(descriptor, api_key=api_key, endpoint=endpoint,
                        config=config, extra_headers=extra_headers)

    def create_by_id(
        self,
        provider_id: str,
        *,
        api_key: str | None = None,
        endpoint: str | None = None,
        config: Config | None = None,
    ) -> ProviderAdapter:
        descriptor = provider_by_id(provider_id)
        if descriptor is None:
            if endpoint:
                descriptor = custom_descriptor(provider_id, endpoint)
            else:
                raise AuthError(
                    f"Unknown provider '{provider_id}'. Use 'daemon auth add' or "
                    f"pass --base-url for a custom OpenAI-compatible endpoint.")
        return self.create(descriptor, api_key=api_key, endpoint=endpoint,
                           config=config)

    @staticmethod
    def protocols() -> list[str]:
        return sorted(_PROTOCOLS)

    @staticmethod
    def protocol_for(provider_id: str) -> str | None:
        descriptor = provider_by_id(provider_id)
        return descriptor.protocol if descriptor else None


async def discover_models(adapter: ProviderAdapter) -> list[RemoteModel]:
    """List models through an adapter, normalising failures to AuthError."""
    try:
        return await adapter.list_models()
    except ProviderError as exc:
        raise AuthError(
            f"Could not list models at {adapter.display_name}: {exc}",
            hint=exc.hint) from exc
