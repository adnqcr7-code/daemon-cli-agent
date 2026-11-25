"""Per-provider capability manifest (v0.9.0 Phase 3).

One merged record per (provider, model): the provider layer (from
``daemon/providers/catalog.py`` — what the provider's API can do)
plus the model layer (from ``daemon/models.py`` — context window,
pricing, whether that specific model reasons). Anything neither
layer knows stays ``None`` and is reported as "unknown"; the
manifest never guesses.

``sources`` records, per field, which layer supplied the value
(``"provider"``, ``"model"``, or ``"unknown"``), so a reader can
tell a documented fact from a gap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..models import enrich
from ..providers.types import ModelCapabilities, RemoteModel
from .catalog import provider_by_id


@dataclass(frozen=True)
class CapabilityManifest:
    provider_id: str
    provider_name: str
    protocol: str
    model_id: str | None = None
    # Model layer (None when no model was named, or unknown).
    context_window: int | None = None
    reasoning: bool | None = None
    input_price_per_mtok: float | None = None
    output_price_per_mtok: float | None = None
    # Provider layer.
    tool_calling: bool | None = None
    parallel_tool_calls: bool | None = None
    reasoning_mode: str | None = None
    reasoning_trace_field: str | None = None
    max_output_tokens: int | None = None
    cache_ttl_seconds: int | None = None
    sources: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "provider_name": self.provider_name,
            "protocol": self.protocol,
            "model_id": self.model_id,
            "context_window": self.context_window,
            "reasoning": self.reasoning,
            "input_price_per_mtok": self.input_price_per_mtok,
            "output_price_per_mtok": self.output_price_per_mtok,
            "tool_calling": self.tool_calling,
            "parallel_tool_calls": self.parallel_tool_calls,
            "reasoning_mode": self.reasoning_mode,
            "reasoning_trace_field": self.reasoning_trace_field,
            "max_output_tokens": self.max_output_tokens,
            "cache_ttl_seconds": self.cache_ttl_seconds,
            "sources": dict(self.sources),
        }


def manifest_for(provider_id: str,
                 model_id: str | None = None) -> CapabilityManifest | None:
    """The merged capability record for a provider, optionally for one
    of its models. ``None`` when the provider id is unknown."""
    descriptor = provider_by_id(provider_id)
    if descriptor is None:
        return None
    caps = descriptor.capabilities
    sources: dict[str, str] = {}
    model_caps: ModelCapabilities | None = None
    if model_id is not None:
        model = enrich(RemoteModel(model_id=model_id, provider=descriptor.id))
        model_caps = model.capabilities

    def _model_value(name: str) -> Any:
        return getattr(model_caps, name) if model_caps is not None else None

    # Tool calling: the model layer wins when it knows (a provider can
    # offer tools while one of its models cannot — DeepSeek reasoner),
    # otherwise the provider layer.
    tool_calling = _model_value("tool_calling")
    if tool_calling is None:
        tool_calling = caps.tool_calling
        sources["tool_calling"] = "provider" if tool_calling is not None \
            else "unknown"
    else:
        sources["tool_calling"] = "model"
    for name in ("context_window", "reasoning", "input_price_per_mtok",
                 "output_price_per_mtok"):
        value = _model_value(name)
        sources[name] = "model" if value is not None else "unknown"
    for name in ("parallel_tool_calls", "reasoning_mode",
                 "reasoning_trace_field", "max_output_tokens",
                 "cache_ttl_seconds"):
        value = getattr(caps, name)
        sources[name] = "provider" if value is not None else "unknown"
    return CapabilityManifest(
        provider_id=descriptor.id,
        provider_name=descriptor.name,
        protocol=descriptor.protocol,
        model_id=model_id,
        context_window=_model_value("context_window"),
        reasoning=_model_value("reasoning"),
        input_price_per_mtok=_model_value("input_price_per_mtok"),
        output_price_per_mtok=_model_value("output_price_per_mtok"),
        tool_calling=tool_calling,
        parallel_tool_calls=caps.parallel_tool_calls,
        reasoning_mode=caps.reasoning_mode,
        reasoning_trace_field=caps.reasoning_trace_field,
        max_output_tokens=caps.max_output_tokens,
        cache_ttl_seconds=caps.cache_ttl_seconds,
        sources=sources,
    )
