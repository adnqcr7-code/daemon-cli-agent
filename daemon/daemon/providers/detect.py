"""API-key fingerprinting: identify the provider that issued a credential.

Principles (they matter):

1. Detection is LOCAL. A key is never sent anywhere before a likely provider
   has been identified (or the user supplied an endpoint).
2. Ambiguity is honest. Many providers share key formats; when we cannot
   tell, we say so and ask exactly ONE short question instead of guessing.
3. No probing. We never try a key against random endpoints to "discover"
   where it works.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from .catalog import PROVIDERS, provider_by_id, providers_by_prefix


@dataclass
class Candidate:
    provider_id: str
    confidence: int  # 0-100
    reason: str


@dataclass
class DetectionResult:
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def unique(self) -> bool:
        return len(self.candidates) == 1 and self.candidates[0].confidence >= 85

    @property
    def needs_question(self) -> bool:
        return not self.unique

    @property
    def best(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None

    def provider_ids(self) -> list[str]:
        return [candidate.provider_id for candidate in self.candidates]


# Format heuristics for providers whose keys carry no distinctive prefix.
_FORMAT_RULES: list[tuple[re.Pattern[str], list[tuple[str, int, str]]]] = [
    # Google AI Studio keys: 'AIza' + 35 chars (39 total).
    (re.compile(r"^AIza[0-9A-Za-z_\-]{35}$"), [
        ("gemini", 90, "matches Google AI Studio key format (39 chars)"),
    ]),
    # sk- + 32 hex characters: DeepSeek and DashScope both use this shape.
    (re.compile(r"^sk-[0-9a-f]{32}$"), [
        ("deepseek", 70, "matches DeepSeek key format (sk- + 32 hex)"),
        ("dashscope", 55, "matches DashScope compatible-mode key format"),
    ]),
    # Z.AI / Zhipu: '{32 hex}.{16 hex}'
    (re.compile(r"^[0-9a-f]{32}\.[0-9a-f]{16}$"), [
        ("zai", 92, "matches Z.AI key format (id.secret)"),
    ]),
    # Moonshot: sk- + ~48-52 mixed-case base62
    (re.compile(r"^sk-[A-Za-z0-9]{44,56}$"), [
        ("moonshot", 60, "matches Moonshot key length"),
        ("openai", 35, "could be a legacy OpenAI key"),
    ]),
    # Generic long sk- key (legacy OpenAI and many OpenAI-compatible clones)
    (re.compile(r"^sk-[A-Za-z0-9_\-]{20,}$"), [
        ("openai", 40, "generic 'sk-' key - many providers use this format"),
    ]),
    # 32 plain alphanumeric: Mistral La Plateforme
    (re.compile(r"^[A-Za-z0-9]{32}$"), [
        ("mistral", 55, "matches Mistral key format (32 alphanumerics)"),
    ]),
    # 40 alphanumeric: Cohere and Cloudflare-ish tokens
    (re.compile(r"^[A-Za-z0-9]{40}$"), [
        ("cohere", 45, "matches Cohere key format"),
        ("cloudflare", 35, "could be a Cloudflare API token"),
    ]),
    # 37-char alphanumeric: Cerebras / SambaNova style
    (re.compile(r"^[A-Za-z0-9]{37}$"), [
        ("sambanova", 30, "possible SambaNova key"),
    ]),
    # JWT-shaped: DeepInfra and AI21
    (re.compile(r"^eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*$"), [
        ("deepinfra", 45, "JWT-shaped key (DeepInfra style)"),
        ("ai21", 35, "JWT-shaped key (AI21 style)"),
    ]),
]


def fingerprint(key: str) -> DetectionResult:
    """Analyse a key LOCALLY. Returns ranked candidates, never sends anything."""
    key = (key or "").strip()
    if not key:
        return DetectionResult()

    candidates: list[Candidate] = []

    # 1. Distinctive prefix match.
    for descriptor, confidence in providers_by_prefix(key):
        candidates.append(Candidate(
            descriptor.id, confidence,
            f"starts with the registered '{descriptor.name}' key prefix"))

    # 2. Format heuristics - skipped entirely when a distinctive prefix
    #    already identified the provider with high confidence.
    distinctive = any(candidate.confidence >= 85 for candidate in candidates)
    if not distinctive:
        for pattern, rules in _FORMAT_RULES:
            if pattern.match(key):
                for provider_id, confidence, reason in rules:
                    candidates.append(Candidate(provider_id, confidence, reason))
                break  # first matching format family wins

    # 3. Environment confirmation: if the key equals a provider's env var we
    #    can be far more confident - treat it as decisive evidence.
    env_confirmed: list[Candidate] = []
    for provider_id, descriptor in PROVIDERS.items():
        for env_name in descriptor.env_keys:
            if os.environ.get(env_name) == key:
                env_confirmed.append(Candidate(
                    provider_id, 95,
                    f"matches the {env_name} environment variable"))
    if env_confirmed:
        candidates = env_confirmed

    # Deduplicate by provider id keeping the best confidence/reason.
    merged: dict[str, Candidate] = {}
    for candidate in candidates:
        existing = merged.get(candidate.provider_id)
        if existing is None or candidate.confidence > existing.confidence:
            if existing is not None:
                candidate.reason = f"{candidate.reason}; {existing.reason}"
            merged[candidate.provider_id] = candidate
    ranked = sorted(merged.values(), key=lambda c: -c.confidence)
    return DetectionResult(candidates=ranked[:6])


def question_options(result: DetectionResult) -> list[tuple[str, str]]:
    """Short, ordered (value, label) choices for the ONE ambiguity question."""
    options: list[tuple[str, str]] = []
    for candidate in result.candidates[:4]:
        descriptor = PROVIDERS.get(candidate.provider_id)
        name = descriptor.name if descriptor else candidate.provider_id
        options.append((candidate.provider_id, name))
    options.append(("custom", "Other / custom OpenAI-compatible endpoint"))
    return options
