"""Provider layer: adapters, protocol implementations, key detection."""

from .types import (
    ChatRequest,
    Message,
    ModelCapabilities,
    RemoteModel,
    StreamEvent,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
)
from .catalog import PROVIDERS, ProviderDescriptor, provider_by_id, iter_providers
from .base import ProviderAdapter
from .registry import AdapterRegistry

__all__ = [
    "ChatRequest", "Message", "ModelCapabilities", "RemoteModel", "StreamEvent",
    "ToolCall", "ToolResult", "ToolSpec", "Usage", "PROVIDERS",
    "ProviderDescriptor", "provider_by_id", "iter_providers", "ProviderAdapter",
    "AdapterRegistry",
]
