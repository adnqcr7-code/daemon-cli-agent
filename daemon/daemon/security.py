"""Security primitives shared by the whole application.

This module is deliberately dependency-free so it can be used from every
layer (tools, providers, logging, UI) without import cycles. It provides:

* ``SecretRedactor``     - redacts known secret values and credential-shaped
                           strings from *any* text before it is shown, logged
                           or persisted.
* ``classify_command``   - static analysis of shell command lines used by the
                           permission engine to decide allow / ask / deny.
* ``scan_injection``     - heuristic scanner for prompt-injection payloads in
                           untrusted tool output (web pages, repo files, ...).
* ``resolve_in_root``    - workspace containment guard for filesystem tools
                           (rejects ``..`` traversal and symlink escapes).

Design rule: nothing in this module ever *executes* anything. It only
classifies and sanitizes.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Iterable

from .errors import PathEscapeError

# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------

# Credential-shaped patterns. Each is (compiled regex, replacement).
# Long, high-entropy shapes only - we must not shred ordinary text.
_REDACTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bsk-ant-(?:api\d+-)?[A-Za-z0-9_\-]{16,}"), "sk-ant-***"),
    (re.compile(r"\bsk-proj-[A-Za-z0-9_\-]{16,}"), "sk-proj-***"),
    (re.compile(r"\bsk-svcacct-[A-Za-z0-9_\-]{16,}"), "sk-svcacct-***"),
    (re.compile(r"\bsk-admin-[A-Za-z0-9_\-]{16,}"), "sk-admin-***"),
    (re.compile(r"\bsk-or-(?:v1-)?[A-Za-z0-9_\-]{20,}"), "sk-or-***"),
    (re.compile(r"\bgsk_[A-Za-z0-9]{20,}"), "gsk_***"),
    (re.compile(r"\bxai-[A-Za-z0-9_\-]{16,}"), "xai-***"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"), "AIza***"),
    (re.compile(r"\btgp_v1_[A-Za-z0-9_\-]{20,}"), "tgp_v1_***"),
    (re.compile(r"\bfw_[A-Za-z0-9_\-]{20,}"), "fw_***"),
    (re.compile(r"\bpplx-[A-Za-z0-9_\-]{16,}"), "pplx-***"),
    (re.compile(r"\bnvapi-[A-Za-z0-9_\-]{20,}"), "nvapi-***"),
    (re.compile(r"\bhf_[A-Za-z0-9]{20,}"), "hf_***"),
    (re.compile(r"\bcsk-[A-Za-z0-9_\-]{16,}"), "csk-***"),
    (re.compile(r"\beyJhb[A-Za-z0-9_\-\.]{40,}"), "eyJhb***"),
    (re.compile(r"\b[0-9a-f]{32}\.[0-9a-f]{16}\b"), "***.***"),
    (re.compile(
        r"(?i)(?:api[_-]?key|secret|token|password|passwd|pwd)\s*['\"]?\s*[:=]\s*['\"]?"
        r"[A-Za-z0-9_\-\.]{16,}"), "credential=***"),
    (re.compile(r"(?i)authorization\s*:\s*bearer\s+[A-Za-z0-9_\-\.]{16,}"),
     "authorization: Bearer ***"),
    (re.compile(r"(?i)(?:x-api-key|api-key)\s*:\s*[A-Za-z0-9_\-\.]{16,}"),
     "api-key: ***"),
    (re.compile(r"(?i)[?&](?:key|api_key|apikey|access_token)=([A-Za-z0-9_\-\.%]{16,})"),
     "\\1=***"),
]

_REDACTION_PATTERNS[-1] = (  # URL query variant keeps the parameter name
    re.compile(r"(?i)([?&](?:key|api_key|apikey|access_token)=)[A-Za-z0-9_\-\.%]{16,}"),
    "\\1***",
)

# Environment variables known to carry credentials. Stripped from child
# processes by default and redacted from environment dumps.
KNOWN_SECRET_ENV_VARS: tuple[str, ...] = (
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
    "GROQ_API_KEY", "DEEPSEEK_API_KEY", "MISTRAL_API_KEY", "TOGETHER_API_KEY",
    "FIREWORKS_API_KEY", "PERPLEXITY_API_KEY", "COHERE_API_KEY", "XAI_API_KEY",
    "MOONSHOT_API_KEY", "MOONSHOTAI_API_KEY", "ZAI_API_KEY", "ZHIPUAI_API_KEY",
    "DASHSCOPE_API_KEY", "CEREBRAS_API_KEY", "SAMBANOVA_API_KEY", "DEEPINFRA_API_KEY",
    "NVIDIA_API_KEY", "NVIDIA_NIM_API_KEY", "HF_TOKEN", "OPENROUTER_API_KEY",
    "TAVILY_API_KEY", "SERPER_API_KEY", "BRAVE_API_KEY", "LITELLM_API_KEY",
    "VERCEL_AI_GATEWAY_KEY", "CLOUDFLARE_API_TOKEN", "DAEMON_MASTER_KEY",
)


class SecretRedactor:
    """Redacts registered secret values plus credential-shaped strings.

    The instance is process-global (see :func:`redactor`) so the logging
    filter, the UI renderer and tool outputs all pass through the same
    registry of live secrets.
    """

    def __init__(self) -> None:
        self._known: set[str] = set()

    def register(self, *values: str | None) -> None:
        """Register live secret values that must never appear anywhere."""
        for value in values:
            if value and len(value) >= 8:
                self._known.add(value)

    def forget(self, *values: str | None) -> None:
        for value in values:
            self._known.discard(value)  # type: ignore[arg-type]

    def redact(self, text: str | None) -> str:
        """Return ``text`` with every known secret and credential pattern removed."""
        if not text:
            return text or ""
        for value in self._known:
            if value in text:
                text = text.replace(value, "***REDACTED***")
        for pattern, replacement in _REDACTION_PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    def redact_mapping(self, mapping: dict[str, str]) -> dict[str, str]:
        return {key: self.redact(value) for key, value in mapping.items()}


_GLOBAL_REDACTOR = SecretRedactor()


def redactor() -> SecretRedactor:
    """Process-wide redactor instance."""
    return _GLOBAL_REDACTOR


def redact(text: str | None) -> str:
    """Convenience wrapper around the global redactor."""
    return _GLOBAL_REDACTOR.redact(text)


# ---------------------------------------------------------------------------
# Shell command classification
# ---------------------------------------------------------------------------

# Commands that read credentials or otherwise must never run through the
# agent without an explicit, non-default user decision.
_DENY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(^|[;&|\s])sudo\b"), "runs commands as root"),
    (re.compile(r"(?i)(^|[;&|\s])su\b"), "switches user"),
    (re.compile(r"(?i)\.ssh\b|id_rsa|id_ed25519|\.pem\b|\.aws/credentials"),
     "reads credential material"),
    (re.compile(r"(?i)(^|[;&|\s])(shutdown|reboot|halt|poweroff)\b"),
     "powers the machine off"),
    (re.compile(r":\(\)\s*\{\s*:\|:&\s*\}\s*;:"), "fork bomb"),
    (re.compile(r"(?i)dd\s+[^\n]*of=/dev/(sd|nvme|disk)"), "raw device write"),
    (re.compile(r"(?i)>\s*/dev/(sd|nvme|disk)"), "raw device write"),
    (re.compile(r"(?i)mkfs(\.\w+)?\b"), "formats a filesystem"),
    (re.compile(r"(?i)\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+(/|~|/\*|\$HOME)(\s|$)"),
     "deletes the entire filesystem or home directory"),
]

# Commands that are consequential but not catastrophic: the user should get
# an approval prompt (never auto-approved in a fresh configuration).
_ASK_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+"), "recursive/forced delete"),
    (re.compile(r"(?i)\bgit\s+push\b[^\n]*(--force(?!-with-lease)|-f)\b"),
     "force-pushes history"),
    (re.compile(r"(?i)\bgit\s+reset\s+--hard\b"), "discards uncommitted work"),
    (re.compile(r"(?i)\bgit\s+clean\s+-[a-zA-Z]*f"), "deletes untracked files"),
    (re.compile(r"(?i)\bgit\s+branch\s+-D\b"), "force-deletes a branch"),
    (re.compile(r"(?i)\bgit\s+checkout\s+--\s|\bgit\s+restore\b"),
     "discards file changes"),
    (re.compile(r"(?i)(curl|wget)[^\n|;&]*\|\s*(ba|z|fi)?sh\b"),
     "pipes a download into a shell"),
    (re.compile(r"(?i)\bnpx\b|\bdeno\s+run\b|\bbunx\b"),
     "downloads and executes packages from a registry"),
    (re.compile(r"(?i)\b(chmod|chown)\s+-R\b[^\n]*(/|~)"), "recursive ownership/permission change on a system path"),
    (re.compile(r"(?i)\bnpm\s+(i|install|ci|update)\b|\bpip\s+install\b|\bpip3\s+install\b|"
                r"\bpipx\s+install\b|\bcargo\s+add\b|\bgo\s+get\b|\bnpm\s+uninstall\b"),
     "installs or removes dependencies"),
    (re.compile(r"(?i)\bnpm\s+publish\b|\bcargo\s+publish\b|\btwine\s+upload\b"),
     "publishes a package"),
    (re.compile(r"(?i)\bdocker\s+(system\s+prune|rmi\s+-f|rm\s+-f|volume\s+rm)\b|"
                r"\bkubectl\s+delete\b|\bhelm\s+(uninstall|delete)\b"),
     "destroys containers / cluster resources"),
    (re.compile(r"(?i)\b(kill|pkill)\s+-9\b"), "force-kills processes"),
    (re.compile(r"(?i)(^|[;&|\s])(env|printenv)\b(?![^\n]*=\S+\s+[^\s]*\.\/)"),
     "prints the environment (may contain secrets)"),
    (re.compile(r"(?i)\bhistory\b|/etc/shadow|\.netrc|\.git-credentials|keychain"),
     "reads sensitive history/credential stores"),
    (re.compile(r"(?i)\bmv\b[^\n]*\s/(etc|usr|var|boot)\b|\bcp\b[^\n]*\s/(etc|usr|var|boot)\b"),
     "writes into system directories"),
    (re.compile(r"(?i)\bscp\b|\brsync\b[^\n]*--delete|\bssh\b"), "network copy / remote shell"),
]

# Ordinary, safe development commands. Anything not matched anywhere is
# treated as "ask" (fail closed for unknown commands).
_SAFE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)^(pytest|python|python3|pip list|pip show|pip freeze|uv run|uv sync)\b"),
    re.compile(r"(?i)^(ls|cat|head|tail|wc|file|stat|tree|du|df|find|grep|rg|which|where|whoami)\b"),
    re.compile(r"(?i)^(git status|git diff|git log|git show|git branch|git add|git stash list)\b"),
    re.compile(r"(?i)^(npm (run|test|exec|ls|outdated)|yarn (test|build|lint)|pnpm (test|build|lint))\b"),
    re.compile(r"(?i)^(make|cargo (build|test|check|clippy|fmt --check)|go (build|test|vet|fmt))\b"),
    re.compile(r"(?i)^(node|deno|bun|ruby|gem list|bundle (check|exec)|php|composer (validate|install))\b"),
    re.compile(r"(?i)^(javac|java -version|mvn -q|gradle)\b"),
    re.compile(r"(?i)^(ruff|black --check|isort --check|mypy|pylint|flake8|eslint|tsc|prettier --check)\b"),
    re.compile(r"(?i)^(echo|pwd|date|true|false)\b"),
]


def classify_command(command: str) -> tuple[str, list[str]]:
    """Classify a shell command line.

    Returns ``(level, reasons)`` where level is one of:

    * ``"deny"``  - credential/system access; refused by default policy and
      only runnable with an explicit user rule.
    * ``"ask"``   - consequential; requires approval.
    * ``"allow"`` - ordinary development command.
    * ``"unknown"``- not recognized; the permission engine treats unknown as
      *ask* (fail closed).
    """
    reasons: list[str] = []
    for pattern, reason in _DENY_PATTERNS:
        if pattern.search(command):
            reasons.append(reason)
    if reasons:
        return "deny", reasons
    for pattern, reason in _ASK_PATTERNS:
        if pattern.search(command):
            reasons.append(reason)
    if reasons:
        return "ask", reasons
    first = command.strip().split(" ", 1)[0] if command.strip() else ""
    for pattern in _SAFE_PATTERNS:
        if pattern.match(command.strip()):
            return "allow", []
    _ = first
    return "unknown", ["unrecognized command"]


# ---------------------------------------------------------------------------
# Prompt-injection scanning for untrusted tool output
# ---------------------------------------------------------------------------

_INJECTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)ignore\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier|preceding)\s+"
                r"(instructions|prompts?|rules?|directives?)"), "asks to ignore prior instructions"),
    (re.compile(r"(?i)disregard\s+(all|any|the|your)\s+(previous|prior|above|earlier)?\s*"
                r"(instructions|prompts?|rules?|guidance)?"), "asks to disregard instructions"),
    (re.compile(r"(?i)you\s+are\s+now\s+(a|an|in)\b|act\s+as\s+(if\s+)?(a|an)\s+(different|new)"),
     "attempts identity override"),
    (re.compile(r"(?i)(reveal|show|print|repeat|output)\s+(your|the|all)\s+"
                r"(system\s+)?(prompt|instructions|api\s*keys?|credentials|secrets?)"),
     "attempts to extract prompts or credentials"),
    (re.compile(r"(?i)(admin|developer|god|dan)\s+mode\b|jailbreak\b"), "jailbreak attempt"),
    (re.compile(r"(?i)new\s+(instructions|rules|directives)\s*:"), "injects new instructions"),
    (re.compile(r"(?i)(send|post|upload|exfiltrate|transmit|curl|wget)[^\n]{0,80}"
                r"(api\s*keys?|credentials|secrets?|\.env|ssh|tokens?)"),
     "attempts to exfiltrate secrets"),
    (re.compile(r"(?i)(curl|wget)[^\n|;&]{0,200}\|\s*(ba|z|fi)?sh\b"),
     "suggests piping a download into a shell"),
    (re.compile(r"(?i)\brm\s+-[a-zA-Z]*[rf]"), "suggests destructive deletion"),
]


def scan_injection(text: str) -> list[tuple[str, str]]:
    """Return [(description, snippet)] for suspicious content in untrusted text."""
    findings: list[tuple[str, str]] = []
    seen: set[str] = set()
    for pattern, description in _INJECTION_PATTERNS:
        for match in pattern.finditer(text):
            snippet = text[max(0, match.start() - 20):match.end() + 20].replace("\n", " ")
            key = description + snippet
            if key not in seen:
                seen.add(key)
                findings.append((description, redact(snippet.strip())))
    return findings


# ---------------------------------------------------------------------------
# Workspace path containment
# ---------------------------------------------------------------------------


def resolve_in_root(root: Path, user_path: str, *, must_exist: bool = False) -> Path:
    """Resolve ``user_path`` inside ``root`` and refuse any escape.

    Guards against ``..`` traversal, absolute paths outside the workspace and
    symlinks that point outside (resolution happens *before* the containment
    check, so a symlink inside the tree pointing out is rejected too).
    """
    root = Path(root).resolve()
    if not user_path or not isinstance(user_path, str):
        raise PathEscapeError("Empty path.")
    candidate = Path(user_path)
    if candidate.is_absolute():
        proposed = candidate.resolve()
        if not _contained(root, proposed):
            raise PathEscapeError(
                f"Absolute path '{redact(user_path)}' is outside the workspace "
                f"({root}). Use a relative path, or add an explicit permission rule."
            )
    else:
        # PurePosixPath normalises 'a/../../b' style traversal safely.
        normalised = PurePosixPath(candidate.as_posix())
        if normalised.is_absolute() or ".." in normalised.parts:
            raise PathEscapeError(
                f"Path '{redact(user_path)}' escapes the workspace via '..'."
            )
        proposed = (root / Path(*normalised.parts)).resolve()
        if not _contained(root, proposed):
            raise PathEscapeError(
                f"Path '{redact(user_path)}' resolves outside the workspace "
                f"({root}); likely a symlink escape."
            )
    if must_exist and not proposed.exists():
        raise PathEscapeError(f"'{redact(user_path)}' does not exist.")
    return proposed


def _contained(root: Path, proposed: Path) -> bool:
    try:
        proposed.relative_to(root)
        return True
    except ValueError:
        return False


def is_within(path: Path, root: Path) -> bool:
    """True when ``path`` (already resolved) lives inside ``root``."""
    return _contained(Path(root).resolve(), Path(path).resolve())


def strip_secret_env(env: dict[str, str] | None) -> dict[str, str]:
    """Drop credential-carrying variables from a child environment."""
    if env is None:
        import os

        env = dict(os.environ)
    return {k: v for k, v in env.items() if k not in KNOWN_SECRET_ENV_VARS}


def first(iterable: Iterable[str]) -> str | None:
    for item in iterable:
        return item
    return None
