"""Model registry: live discovery + curated capability knowledge.

Discovery comes from each provider's model endpoint. Capability and price
knowledge comes from a small curated catalog of widely used models. Anything
we do not know stays ``None`` and is reported as "unknown" - Daemon never
invents capabilities. Catalog figures are labelled approximate in the UI;
providers change pricing frequently.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .errors import ModelError
from .log import get_logger
from .providers.types import ModelCapabilities, RemoteModel, Usage

_logger = get_logger("models")
_CACHE_TTL_SECONDS = 24 * 3600

# ---------------------------------------------------------------------------
# Curated capability catalog (best-known public figures, approximate)
# ---------------------------------------------------------------------------

# (match pattern, capabilities)
_CATALOG: list[tuple[str, dict]] = [
    # OpenAI
    (r"^gpt-5\.1$|^(openai/)?gpt-5$", dict(context_window=400_000, tool_calling=True,
        vision=True, reasoning=True, structured_output=True,
        input_price_per_mtok=1.25, output_price_per_mtok=10.0)),
    (r"gpt-5-mini", dict(context_window=400_000, tool_calling=True, vision=True,
        input_price_per_mtok=0.25, output_price_per_mtok=2.0)),
    (r"gpt-5-nano", dict(context_window=400_000, tool_calling=True,
        input_price_per_mtok=0.05, output_price_per_mtok=0.40)),
    (r"gpt-4\.1-mini", dict(context_window=1_000_000, tool_calling=True, vision=True,
        structured_output=True, input_price_per_mtok=0.40, output_price_per_mtok=1.60)),
    (r"gpt-4\.1$", dict(context_window=1_000_000, tool_calling=True, vision=True,
        structured_output=True, input_price_per_mtok=2.0, output_price_per_mtok=8.0)),
    (r"gpt-4o-mini", dict(context_window=128_000, tool_calling=True, vision=True,
        structured_output=True, input_price_per_mtok=0.15, output_price_per_mtok=0.60)),
    (r"gpt-4o$", dict(context_window=128_000, tool_calling=True, vision=True,
        structured_output=True, input_price_per_mtok=2.50, output_price_per_mtok=10.0)),
    (r"\bo3$", dict(context_window=200_000, tool_calling=True, reasoning=True,
        input_price_per_mtok=2.0, output_price_per_mtok=8.0)),
    (r"o4-mini", dict(context_window=200_000, tool_calling=True, reasoning=True,
        input_price_per_mtok=1.10, output_price_per_mtok=4.40)),
    (r"o3-mini|\bo1\b", dict(context_window=200_000, tool_calling=True, reasoning=True)),
    # Anthropic
    (r"claude.*opus-4", dict(context_window=200_000, tool_calling=True, vision=True,
        reasoning=True, input_price_per_mtok=15.0, output_price_per_mtok=75.0)),
    (r"claude.*sonnet-4", dict(context_window=200_000, tool_calling=True, vision=True,
        reasoning=True, input_price_per_mtok=3.0, output_price_per_mtok=15.0)),
    (r"claude.*haiku", dict(context_window=200_000, tool_calling=True, vision=True,
        input_price_per_mtok=1.0, output_price_per_mtok=5.0)),
    (r"claude-3.*sonnet", dict(context_window=200_000, tool_calling=True, vision=True,
        input_price_per_mtok=3.0, output_price_per_mtok=15.0)),
    (r"claude-3.*haiku", dict(context_window=200_000, tool_calling=True, vision=True,
        input_price_per_mtok=0.25, output_price_per_mtok=1.25)),
    # Google
    (r"gemini-2\.5-pro", dict(context_window=1_000_000, tool_calling=True, vision=True,
        reasoning=True, input_price_per_mtok=1.25, output_price_per_mtok=10.0)),
    (r"gemini-2\.5-flash", dict(context_window=1_000_000, tool_calling=True, vision=True,
        reasoning=True, input_price_per_mtok=0.30, output_price_per_mtok=2.50)),
    (r"gemini-2\.0-flash", dict(context_window=1_000_000, tool_calling=True, vision=True,
        input_price_per_mtok=0.10, output_price_per_mtok=0.40)),
    # DeepSeek
    (r"deepseek-chat$", dict(context_window=64_000, tool_calling=True,
        input_price_per_mtok=0.27, output_price_per_mtok=1.10)),
    (r"deepseek-reasoner", dict(context_window=64_000, tool_calling=False,
        reasoning=True, input_price_per_mtok=0.55, output_price_per_mtok=2.19)),
    # Z.AI
    (r"glm-4\.6", dict(context_window=200_000, tool_calling=True, reasoning=True)),
    (r"glm-4\.5-air", dict(context_window=128_000, tool_calling=True)),
    (r"glm-4\.5", dict(context_window=128_000, tool_calling=True, reasoning=True)),
    (r"glm-4$", dict(context_window=128_000, tool_calling=True)),
    # Moonshot
    (r"kimi-k2", dict(context_window=128_000, tool_calling=True,
        input_price_per_mtok=0.55, output_price_per_mtok=2.20)),
    (r"kimi-latest", dict(context_window=128_000, tool_calling=True)),
    # Mistral
    (r"mistral-large", dict(context_window=128_000, tool_calling=True,
        input_price_per_mtok=2.0, output_price_per_mtok=6.0)),
    (r"mistral-medium", dict(context_window=128_000, tool_calling=True)),
    (r"magistral", dict(context_window=128_000, tool_calling=True, reasoning=True)),
    (r"codestral", dict(context_window=256_000, tool_calling=True)),
    # Qwen / DashScope
    (r"qwen3-coder", dict(context_window=256_000, tool_calling=True)),
    (r"qwen-max", dict(context_window=32_768, tool_calling=True)),
    (r"qwen-plus", dict(context_window=131_072, tool_calling=True)),
    (r"qwen-turbo", dict(context_window=1_000_000, tool_calling=True)),
    # Fast inference tiers
    (r"llama-3\.3-70b.*versatile", dict(context_window=128_000, tool_calling=True,
        input_price_per_mtok=0.59, output_price_per_mtok=0.79)),
    (r"llama-3\.1-8b.*instant", dict(context_window=131_072, tool_calling=True,
        input_price_per_mtok=0.05, output_price_per_mtok=0.08)),
    (r"gpt-oss-120b", dict(context_window=131_072, tool_calling=True, reasoning=True)),
    (r"gpt-oss-20b", dict(context_window=131_072, tool_calling=True, reasoning=True)),
]

_COMPILED = [(re.compile(pattern), caps) for pattern, caps in _CATALOG]

# Providers whose model ids embed the upstream vendor ("openai/gpt-4o").
_VENDOR_PREFIXED = {"openrouter", "together", "deepinfra", "fireworks", "groq",
                    "cerebras", "nvidia", "sambanova", "huggingface"}


def _normalise_ids(model_id: str) -> list[str]:
    """Candidate lookup keys for a model id (handles 'vendor/model')."""
    ids = [model_id]
    if "/" in model_id:
        ids.append(model_id.split("/")[-1])
    if ":" in model_id:  # some gateways use 'model:version'
        ids.append(model_id.split(":")[0])
    return ids


def enrich(model: RemoteModel) -> RemoteModel:
    """Fill UNKNOWN capabilities from the curated catalog (in place)."""
    for candidate_id in _normalise_ids(model.model_id):
        lowered = candidate_id.lower()
        for pattern, caps in _COMPILED:
            if pattern.search(lowered):
                changed = []
                for key, value in caps.items():
                    current = getattr(model.capabilities, key)
                    if current is None:
                        setattr(model.capabilities, key, value)
                        changed.append(key)
                if changed:
                    _logger.debug("enriched %s from catalog: %s",
                                  model.model_id, ",".join(changed))
                return model
    # Heuristic fallbacks, clearly weaker than catalog knowledge.
    lowered = model.model_id.lower()
    if model.capabilities.reasoning is None and re.search(
            r"(o[134]\b|-reasoner|-r1|-thinking|magistral|thought)", lowered):
        model.capabilities.reasoning = True
    return model


def estimate_cost_usd(model: RemoteModel, usage: Usage) -> float | None:
    caps = model.capabilities
    if caps.input_price_per_mtok is None or caps.output_price_per_mtok is None:
        return None
    return round(
        usage.input_tokens / 1_000_000 * caps.input_price_per_mtok +
        usage.output_tokens / 1_000_000 * caps.output_price_per_mtok, 6)


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------

_TASK_PROFILES: dict[str, dict] = {
    "coding": dict(needs_tools=True, want_context=128_000, want_reasoning=True,
                   cost_weight=0.3),
    "planning": dict(needs_tools=False, want_context=128_000, want_reasoning=True,
                     cost_weight=0.2),
    "research": dict(needs_tools=True, want_context=200_000, want_reasoning=False,
                     cost_weight=0.8),
    "review": dict(needs_tools=False, want_context=100_000, want_reasoning=True,
                   cost_weight=0.5),
    "cheap": dict(needs_tools=False, want_context=32_000, want_reasoning=False,
                  cost_weight=1.0),
}


@dataclass
class Recommendation:
    model: RemoteModel
    score: float
    reasons: list[str]

    @property
    def headline(self) -> str:
        price = self.model.capabilities.input_price_per_mtok
        budget = f"~${price:.2f}/Mtok in" if price is not None else "price unknown"
        return f"{self.model.name} ({budget})"


def recommend(models: Iterable[RemoteModel], task: str = "coding",
              *, exclude: set[str] | None = None) -> list[Recommendation]:
    """Rank models for a task. Explanations accompany every score."""
    profile = _TASK_PROFILES.get(task, _TASK_PROFILES["coding"])
    exclude = exclude or set()
    recommendations: list[Recommendation] = []
    for model in models:
        if model.model_id in exclude:
            continue
        caps = model.capabilities
        score = 50.0
        reasons: list[str] = []
        if profile["needs_tools"]:
            if caps.tool_calling is False:
                reasons.append("no tool calling - unsuitable")
                score -= 100
            elif caps.tool_calling is None:
                reasons.append("tool support unverified")
                score -= 10
            else:
                reasons.append("tool calling supported")
                score += 20
        if caps.context_window:
            if caps.context_window >= profile["want_context"]:
                score += 15
                reasons.append(f"{caps.context_window // 1000}k context")
            else:
                score -= min(20, profile["want_context"] // max(
                    caps.context_window, 1) // 1000)
                reasons.append(f"small context ({caps.context_window // 1000}k)")
        else:
            reasons.append("context window unknown")
            score -= 5
        if profile["want_reasoning"]:
            if caps.reasoning:
                score += 10
                reasons.append("reasoning model")
            elif caps.reasoning is None:
                reasons.append("reasoning unknown")
        price = caps.output_price_per_mtok
        if price is not None:
            weight = profile["cost_weight"]
            score += max(-25.0, min(15.0, (5.0 - price) * 5 * weight))
            if price > 20:
                reasons.append(f"expensive (~${price:.2f}/Mtok out)")
        else:
            reasons.append("pricing unknown")
        recommendations.append(Recommendation(model, round(score, 1), reasons))
    recommendations.sort(key=lambda rec: -rec.score)
    return recommendations


# ---------------------------------------------------------------------------
# Registry with cache
# ---------------------------------------------------------------------------


class ModelRegistry:
    """Discovers models through adapters and caches them per profile."""

    def __init__(self, home: Path) -> None:
        self.home = Path(home)
        self.cache_dir = self.home / "cache" / "models"

    def _cache_path(self, profile_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.#-]", "_", profile_id)
        return self.cache_dir / f"{safe}.json"

    def cached(self, profile_id: str, *, max_age: int = _CACHE_TTL_SECONDS
               ) -> list[RemoteModel] | None:
        path = self._cache_path(profile_id)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            age = time.time() - float(payload.get("ts", 0))
            if age > max_age:
                return None
            return [RemoteModel.from_json(entry)
                    for entry in payload.get("models", [])]
        except (OSError, ValueError, KeyError):
            return None

    def _write_cache(self, profile_id: str, models: list[RemoteModel]) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self._cache_path(profile_id)
        path.write_text(json.dumps({
            "ts": time.time(),
            "models": [model.to_json() for model in models],
        }), encoding="utf-8")

    def invalidate(self, profile_id: str) -> None:
        path = self._cache_path(profile_id)
        if path.exists():
            path.unlink()

    async def load_async(self, adapter, profile_id: str, *,
                         refresh: bool = False) -> list[RemoteModel]:
        """Discover (or reuse cached) models, enriched by the catalog."""
        if not refresh:
            cached = self.cached(profile_id)
            if cached is not None:
                return cached
        raw = await adapter.list_models()
        models = [enrich(model) for model in raw]
        if models:
            self._write_cache(profile_id, models)
        return models

    def load_sync(self, adapter, profile_id: str, *,
                  refresh: bool = False) -> list[RemoteModel]:
        import asyncio

        return asyncio.run(self.load_async(adapter, profile_id, refresh=refresh))


def pick_role_model(config, role: str, models: list[RemoteModel]) -> RemoteModel:
    """Resolve the configured model for a role, with a recommended fallback.

    Never switches silently: when the configured model is missing we raise a
    ModelError explaining the options.
    """
    configured = config.get(f"models.{role}") or config.get("default_model")
    if configured:
        for model in models:
            if model.model_id == configured or configured in _normalise_ids(
                    model.model_id):
                return model
        raise ModelError(
            f"Configured {role} model '{configured}' is not available on the "
            f"active profile. Run 'daemon models list' and "
            f"'daemon config set models.{role} <id>'.")
    if models:
        top = recommend(models, role if role in _TASK_PROFILES else "coding")
        return top[0].model
    raise ModelError("No models available for this profile.")
