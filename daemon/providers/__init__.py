"""Provider layer: adapters, protocol implementations, key detection."""

from .base import ProviderAdapter
from .catalog import PROVIDERS, ProviderDescriptor, iter_providers, provider_by_id
from .registry import AdapterRegistry
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

__all__ = [
    "ChatRequest", "Message", "ModelCapabilities", "RemoteModel", "StreamEvent",
    "ToolCall", "ToolResult", "ToolSpec", "Usage", "PROVIDERS",
    "ProviderDescriptor", "provider_by_id", "iter_providers", "ProviderAdapter",
    "AdapterRegistry",
]
