# Providers

Daemon's engine is provider-agnostic: three protocol adapters cover the
industry, and a data-only catalog describes every provider. Nothing about a
specific vendor is hardcoded in the agent.

## Protocols

| Protocol | Wire format | Covers |
|---|---|---|
| `openai` | `/chat/completions` + `/models` (SSE streaming, tool calls, `stream_options.usage`) | OpenAI, DeepSeek, Groq, Cerebras, Together, Fireworks, DeepInfra, Hugging Face, NVIDIA NIM, SambaNova, xAI, Mistral, Moonshot/Kimi, Z.AI/GLM, DashScope (compatible mode), Cohere (compat endpoint), Perplexity, AI21, OpenRouter, LiteLLM, Vercel AI Gateway, Cloudflare Workers AI, Ollama Cloud, Ollama, LM Studio, llama.cpp, vLLM, LocalAI, Azure OpenAI (deployments + api-version URL scheme) |
| `anthropic` | `POST /v1/messages` (content blocks, tool use, thinking passthrough, SSE) + `/v1/models` | Anthropic |
| `gemini` | `:generateContent` / `:streamGenerateContent?alt=sse` + `/v1beta/models` | Google Gemini / AI Studio |

## Catalogued providers

| Category | Providers |
|---|---|
| Major | OpenAI, Anthropic, Google Gemini, xAI, Mistral, DeepSeek, Moonshot (Kimi), Z.AI (GLM), DashScope (Qwen), Cohere, Perplexity, AI21 |
| Inference | Groq, Cerebras, Together, Fireworks, DeepInfra, Hugging Face, NVIDIA NIM, SambaNova, Cloudflare Workers AI |
| Gateways | OpenRouter, LiteLLM Proxy, Vercel AI Gateway, any custom OpenAI-compatible endpoint |
| Cloud | Azure OpenAI, Ollama Cloud |
| Local | Ollama, LM Studio, llama.cpp server, vLLM, LocalAI |

This is a catalog, not a ceiling: adding a provider is one descriptor entry
(see below) and never touches the agent engine.

## Cloud infrastructure notes

- **Azure OpenAI**: `daemon auth add --provider azure`, enter the resource
  name when asked. Models are *deployments*; set your model with
  `daemon config set models.coding <deployment-name>`. Live discovery lists
  what the endpoint exposes.
- **Ollama Cloud**: set `OLLAMA_API_KEY` or run
  `daemon auth add --provider ollama-cloud`. The endpoint is
  `https://ollama.com/v1`; set `models.coding` to a model available in your
  Ollama Cloud account.
- **AWS Bedrock / Google Vertex AI**: not natively adaptered in 0.1.0. Route
  them through a LiteLLM proxy (`daemon auth add --base-url
  http://your-litellm:4000/v1`) or OpenRouter. This is an honest scope
  decision, not a claim of support.

## Custom OpenAI-compatible endpoints

Any endpoint speaking the Chat Completions dialect works:

```bash
daemon auth add --base-url https://my-gateway.internal/v1
# or fully scripted:
daemon auth add --api-key "$KEY" --base-url https://my-gateway.internal/v1
```

Provider-specific headers can be configured via MCP-style config editing;
unauthenticated local servers need no key at all.

## Multiple accounts, multiple profiles

```bash
daemon auth add --label work      # provider#work
daemon auth add --label personal  # provider#personal
daemon auth list                  # * marks the active profile
daemon auth switch openai#personal
```

Role models (`models.coding`, `models.planning`, …) may point at a model of
any profiled provider; the runtime resolves the right credentials per call.

## Adding a provider (development)

One entry in `daemon/providers/catalog.py`:

```python
_p("example", "Example Corp", "openai", "major", "https://api.example.com/v1",
   env_keys=("EXAMPLE_API_KEY",),
   key_prefixes=(("ex-", 95),),
   docs_url="https://docs.example.com"),
```

That is the whole integration for standard endpoints: key detection,
verification, model discovery, streaming and tool calls all come from the
protocol adapter. Providers with unusual semantics get a thin adapter
subclass (`providers/anthropic.py` is the reference implementation, ~300
lines including streaming).
