"""Static catalog of known providers.

A ``ProviderDescriptor`` is pure data: what the provider is called, which
protocol it speaks, where it lives, how its API keys look and which
environment variables may hold them. Adding a provider to Daemon normally
means adding one descriptor here - the agent engine never changes.

Unknown capability values stay ``None``; the system reports "unknown"
instead of guessing.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProviderCapabilities:
    """What a provider's API can do, at the provider (not model) level.

    ``None`` means unknown — never guessed. Model-level facts (context
    window, pricing, whether a specific model reasons) live in the
    model catalog (``daemon/models.py``); the capability manifest
    (``daemon/providers/manifest.py``) merges the two, and reports
    "unknown" for anything neither layer knows.

    ``reasoning_mode``: how the provider surfaces a model's reasoning
    trace, if it does — ``"native"`` (reasoning models, no separate
    trace field), ``"reasoning_content"`` (an OpenAI-compatible
    ``reasoning_content`` field, e.g. DeepSeek), ``"thinking"``
    (Anthropic-style thinking blocks), ``"none"`` (no reasoning
    surface). ``reasoning_trace_field`` names the wire field when
    there is one. Reasoning traces are stored separately from the
    conversation (``daemon/agent/reasoning.py``) and are never
    replayed as normal history.
    """

    tool_calling: bool | None = None
    parallel_tool_calls: bool | None = None
    reasoning_mode: str | None = None
    reasoning_trace_field: str | None = None
    max_output_tokens: int | None = None
    # Prompt-cache TTL in seconds, where the provider documents one.
    cache_ttl_seconds: int | None = None


@dataclass(frozen=True)
class ProviderDescriptor:
    id: str
    name: str
    protocol: str  # "openai" | "anthropic" | "gemini" | "demo"
    category: str  # "major" | "inference" | "gateway" | "cloud" | "local"
    base_url: str
    env_keys: tuple[str, ...] = ()
    key_prefixes: tuple[tuple[str, int], ...] = ()  # (prefix, confidence 0-100)
    docs_url: str = ""
    notes: str = ""
    requires_key: bool = True
    # OpenAI-compat: send stream_options={"include_usage": true}. Some
    # gateways reject unknown fields, so this is opt-in per provider.
    stream_usage: bool = True
    aliases: tuple[str, ...] = ()
    keyless_note: str = ""
    capabilities: ProviderCapabilities = field(
        default_factory=ProviderCapabilities)

    def endpoint(self, path: str) -> str:
        return self.base_url.rstrip("/") + path

    def summary(self) -> str:
        bits = [f"{self.name}", f"protocol={self.protocol}", self.category]
        if self.requires_key:
            bits.append("api-key")
        else:
            bits.append(self.keyless_note or "no key required")
        return " | ".join(bits)


def _p(id: str, name: str, protocol: str, category: str, base_url: str, **kw: object) -> ProviderDescriptor:
    return ProviderDescriptor(id=id, name=name, protocol=protocol, category=category,
                              base_url=base_url, **kw)  # type: ignore[arg-type]


def _c(**kw: object) -> ProviderCapabilities:
    """Shorthand for a provider's capability record (see the class docstring)."""
    return ProviderCapabilities(**kw)  # type: ignore[arg-type]


PROVIDERS: dict[str, ProviderDescriptor] = {
    # ---------------- major providers ----------------
    p.id: p
    for p in [
        _p("openai", "OpenAI", "openai", "major", "https://api.openai.com/v1",
           capabilities=_c(tool_calling=True, parallel_tool_calls=True,
                           reasoning_mode="native"),
           env_keys=("OPENAI_API_KEY",),
           key_prefixes=(("sk-proj-", 95), ("sk-svcacct-", 95), ("sk-admin-", 95)),
           docs_url="https://platform.openai.com/docs/api-reference",
           notes="Legacy 48+ char 'sk-' keys are ambiguous with OpenAI-compatible providers."),
        _p("anthropic", "Anthropic", "anthropic", "major", "https://api.anthropic.com",
           capabilities=_c(tool_calling=True, parallel_tool_calls=True,
                           reasoning_mode="thinking", reasoning_trace_field="thinking",
                           # Anthropic documents a 5-minute default prompt-cache TTL
                           # (1 hour available per request).
                           cache_ttl_seconds=300),
           env_keys=("ANTHROPIC_API_KEY",),
           key_prefixes=(("sk-ant-api03-", 98), ("sk-ant-", 90)),
           docs_url="https://docs.anthropic.com/en/api/getting-started"),
        _p("gemini", "Google Gemini", "gemini", "major",
           "https://generativelanguage.googleapis.com",
           capabilities=_c(tool_calling=True, parallel_tool_calls=True,
                           reasoning_mode="thinking"),
           env_keys=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
           key_prefixes=(("AIza", 80),),
           docs_url="https://ai.google.dev/gemini-api/docs"),
        _p("xai", "xAI (Grok)", "openai", "major", "https://api.x.ai/v1",
           capabilities=_c(tool_calling=True, parallel_tool_calls=True),
           env_keys=("XAI_API_KEY",), key_prefixes=(("xai-", 95),),
           docs_url="https://docs.x.ai/"),
        _p("mistral", "Mistral AI", "openai", "major", "https://api.mistral.ai/v1",
           capabilities=_c(tool_calling=True, parallel_tool_calls=True),
           env_keys=("MISTRAL_API_KEY",),
           notes="La Plateforme keys are 32 alphanumeric characters (no prefix)."),
        _p("deepseek", "DeepSeek", "openai", "major", "https://api.deepseek.com/v1",
           capabilities=_c(tool_calling=True, parallel_tool_calls=True,
                           reasoning_mode="reasoning_content",
                           reasoning_trace_field="reasoning_content"),
           env_keys=("DEEPSEEK_API_KEY",),
           notes="Keys look like 'sk-' + 32 hex characters."),
        _p("moonshot", "Moonshot AI (Kimi)", "openai", "major",
           "https://api.moonshot.cn/v1",
           capabilities=_c(tool_calling=True), env_keys=("MOONSHOT_API_KEY", "MOONSHOTAI_API_KEY"),
           notes="International endpoint: https://api.moonshot.ai/v1"),
        _p("zai", "Z.AI (GLM)", "openai", "major", "https://api.z.ai/api/paas/v4",
           capabilities=_c(tool_calling=True),
           env_keys=("ZAI_API_KEY", "ZHIPUAI_API_KEY"),
           notes="China endpoint: https://open.bigmodel.cn/api/paas/v4. "
                 "Keys look like '{32 hex}.{16 hex}'."),
        _p("dashscope", "Alibaba DashScope (Qwen)", "openai", "major",
           "https://dashscope.aliyuncs.com/compatible-mode/v1",
           capabilities=_c(tool_calling=True),
           env_keys=("DASHSCOPE_API_KEY",),
           notes="OpenAI-compatible mode. Native DashScope API is not used."),
        _p("cohere", "Cohere", "openai", "major",
           "https://api.cohere.ai/compatibility/v1",
           capabilities=_c(tool_calling=True), env_keys=("COHERE_API_KEY",),
           notes="Uses Cohere's OpenAI-compatible endpoint."),
        _p("perplexity", "Perplexity", "openai", "major", "https://api.perplexity.ai",
           capabilities=_c(tool_calling=True),
           env_keys=("PERPLEXITY_API_KEY",), key_prefixes=(("pplx-", 95),),
           notes="Primarily search-grounded chat models."),
        _p("ai21", "AI21 Labs", "openai", "major", "https://api.ai21.com/studio/v1",
           capabilities=_c(tool_calling=True),
           env_keys=("AI21_API_KEY",)),
        # ---------------- inference providers ----------------
        _p("groq", "Groq", "openai", "inference", "https://api.groq.com/openai/v1",
           capabilities=_c(tool_calling=True),
           env_keys=("GROQ_API_KEY",), key_prefixes=(("gsk_", 97),),
           docs_url="https://console.groq.com/docs/overview"),
        _p("cerebras", "Cerebras", "openai", "inference", "https://api.cerebras.ai/v1",
           capabilities=_c(tool_calling=True),
           env_keys=("CEREBRAS_API_KEY",), key_prefixes=(("csk-", 92),)),
        _p("together", "Together AI", "openai", "inference", "https://api.together.xyz/v1",
           capabilities=_c(tool_calling=True),
           env_keys=("TOGETHER_API_KEY",), key_prefixes=(("tgp_v1_", 97),)),
        _p("fireworks", "Fireworks AI", "openai", "inference",
           "https://api.fireworks.ai/inference/v1",
           capabilities=_c(tool_calling=True), env_keys=("FIREWORKS_API_KEY",),
           key_prefixes=(("fw_", 95),)),
        _p("deepinfra", "DeepInfra", "openai", "inference",
           "https://api.deepinfra.com/v1/openai",
           capabilities=_c(tool_calling=True), env_keys=("DEEPINFRA_API_KEY",),
           notes="Keys are long JWT-shaped strings starting 'eyJ'."),
        _p("huggingface", "Hugging Face Inference", "openai", "inference",
           "https://router.huggingface.co/v1",
           capabilities=_c(tool_calling=True), env_keys=("HF_TOKEN",),
           key_prefixes=(("hf_", 95),)),
        _p("nvidia", "NVIDIA NIM", "openai", "inference",
           "https://integrate.api.nvidia.com/v1",
           capabilities=_c(tool_calling=True), env_keys=("NVIDIA_API_KEY", "NVIDIA_NIM_API_KEY"),
           key_prefixes=(("nvapi-", 96),)),
        _p("sambanova", "SambaNova", "openai", "inference", "https://api.sambanova.ai/v1",
           capabilities=_c(tool_calling=True),
           env_keys=("SAMBANOVA_API_KEY",)),
        _p("cloudflare", "Cloudflare Workers AI", "openai", "inference",
           "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
           capabilities=_c(tool_calling=True),
           env_keys=("CLOUDFLARE_API_TOKEN",),
           notes="Requires your Cloudflare account id (set at auth time)."),
        # ---------------- gateways ----------------
        _p("openrouter", "OpenRouter", "openai", "gateway", "https://openrouter.ai/api/v1",
           capabilities=_c(tool_calling=True),
           env_keys=("OPENROUTER_API_KEY",),
           key_prefixes=(("sk-or-v1-", 97), ("sk-or-", 85)),
           docs_url="https://openrouter.ai/docs"),
        _p("litellm", "LiteLLM Proxy", "openai", "gateway", "http://localhost:4000/v1",
           capabilities=_c(tool_calling=True),
           requires_key=False, keyless_note="local proxy (key optional)",
           env_keys=("LITELLM_API_KEY",),
           notes="Point --base-url at your own proxy deployment."),
        _p("vercel", "Vercel AI Gateway", "openai", "gateway",
           "https://ai-gateway.vercel.sh/v1",
           capabilities=_c(tool_calling=True), env_keys=("VERCEL_AI_GATEWAY_KEY",)),
        # ---------------- cloud infrastructure ----------------
        _p("azure", "Azure OpenAI", "openai", "cloud",
           "https://{resource}.openai.azure.com/openai",
           capabilities=_c(tool_calling=True, parallel_tool_calls=True),
           env_keys=("AZURE_OPENAI_API_KEY",),
           notes="Requires resource name, deployment names and api-version; "
                 "models are configured as deployments, so live discovery "
                 "lists deployments only when the endpoint allows it."),
        _p("ollama-cloud", "Ollama Cloud", "openai", "cloud",
           "https://ollama.com/v1",
           capabilities=_c(tool_calling=True), env_keys=("OLLAMA_API_KEY",),
           stream_usage=False,
           docs_url="https://docs.ollama.com/openai",
           notes="Ollama's hosted OpenAI-compatible API. Use an Ollama API key "
                 "and a cloud model name."),
        # ---------------- local inference ----------------
        _p("ollama", "Ollama", "openai", "local", "http://localhost:11434/v1",
           capabilities=_c(tool_calling=True),
           requires_key=False, keyless_note="local server",
           env_keys=("OLLAMA_API_KEY",),
           docs_url="https://github.com/ollama/ollama/blob/main/docs/api.md"),
        _p("lmstudio", "LM Studio", "openai", "local", "http://localhost:1234/v1",
           capabilities=_c(tool_calling=True),
           requires_key=False, keyless_note="local server"),
        _p("llamacpp", "llama.cpp server", "openai", "local", "http://localhost:8080/v1",
           capabilities=_c(tool_calling=True),
           requires_key=False, keyless_note="local server"),
        _p("vllm", "vLLM", "openai", "local", "http://localhost:8000/v1",
           capabilities=_c(tool_calling=True),
           requires_key=False, keyless_note="local server"),
        _p("localai", "LocalAI", "openai", "local", "http://localhost:8080/v1",
           capabilities=_c(tool_calling=True),
           requires_key=False, keyless_note="local server"),
        # ---------------- built-in scripted demo ----------------
        _p("demo", "Demo (scripted)", "demo", "local", "",
           capabilities=_c(tool_calling=True, parallel_tool_calls=False,
                           reasoning_mode="none"),
           requires_key=False,
           keyless_note="scripted demo - not a real model",
           notes="Built-in deterministic demo provider: no network, no "
                 "model. Exercises the real tool/permission/checkpoint "
                 "path offline (smoke tests, CI, first run)."),
    ]
}

# Reverse alias map ("glm" -> "zai", "qwen" -> "dashscope", ...)
_ALIASES: dict[str, str] = {
    "gpt": "openai", "openai-compatible": "custom",
    "claude": "anthropic", "google": "gemini", "googlegemini": "gemini",
    "grok": "xai", "kimi": "moonshot", "moonshotai": "moonshot",
    "zhipu": "zai", "zhipuai": "zai", "glm": "zai", "bigmodel": "zai",
    "qwen": "dashscope", "alibaba": "dashscope", "dashscope-compatible": "dashscope",
    "hf": "huggingface", "nim": "nvidia", "workers-ai": "cloudflare",
    "router": "openrouter", "ai-gateway": "vercel", "azure-openai": "azure",
    "bedrock": "litellm", "vertex": "gemini", "vertexai": "gemini",
}


def provider_by_id(identifier: str) -> ProviderDescriptor | None:
    """Resolve a provider by id or alias (case-insensitive)."""
    key = identifier.strip().lower()
    if key in PROVIDERS:
        return PROVIDERS[key]
    if key in _ALIASES:
        target = _ALIASES[key]
        return PROVIDERS.get(target)
    for provider in PROVIDERS.values():
        if identifier.lower() in (provider.name.lower(),) or key in provider.aliases:
            return provider
    return None


def iter_providers(category: str | None = None) -> Iterable[ProviderDescriptor]:
    for provider in PROVIDERS.values():
        if category is None or provider.category == category:
            yield provider


def providers_by_prefix(key: str) -> list[tuple[ProviderDescriptor, int]]:
    """Return providers whose registered key prefix matches ``key``."""
    matches: list[tuple[ProviderDescriptor, int]] = []
    for provider in PROVIDERS.values():
        for prefix, confidence in provider.key_prefixes:
            if key.startswith(prefix) and confidence >= 80:
                matches.append((provider, confidence))
                break
    return matches
