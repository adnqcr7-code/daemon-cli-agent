"""Context management: token estimation, budgeting, conversation compaction.

Token counts are a documented heuristic (roughly chars/4, CJK-aware). It is
used for *budgeting* decisions only - never for billing. When the estimate
exceeds a fraction of the model's context window, older turns are replaced
by a model-generated summary while recent turns are preserved verbatim.
"""

from __future__ import annotations

import re

from ..errors import ProviderError
from ..providers.types import ChatRequest, Message, ToolSpec, Usage
from .prompts import compact_prompt

_CJK = re.compile(r"[\u3000-\u9fff\uff00-\uffef]")


def estimate_tokens(text: str | None) -> int:
    """Heuristic token estimate: chars/4, CJK chars counted individually."""
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    other = len(text) - cjk
    return max(1, (other // 4) + cjk)


def message_tokens(message: Message) -> int:
    total = estimate_tokens(message.text)
    for call in message.tool_calls:
        total += estimate_tokens(call.name) + estimate_tokens(str(call.arguments)) + 16
    for result in message.tool_results:
        total += estimate_tokens(result.content) + 16
    return total


def conversation_tokens(messages: list[Message], tools: list[ToolSpec]) -> int:
    total = sum(message_tokens(message) for message in messages)
    for tool in tools:
        total += estimate_tokens(tool.description) + 60
    return total


def context_budget(context_window: int | None) -> int:
    """Usable input budget: window minus reserved output space."""
    if not context_window or context_window <= 0:
        context_window = 128_000
    reserve = min(32_768, context_window // 4)
    return context_window - reserve


def should_compact(messages: list[Message], tools: list[ToolSpec],
                   context_window: int | None,
                   *, threshold: float = 0.7) -> bool:
    budget = context_budget(context_window)
    return conversation_tokens(messages, tools) > budget * threshold


def truncate_tool_output(content: str, *, max_chars: int = 60_000) -> str:
    if len(content) <= max_chars:
        return content
    head = content[: int(max_chars * 0.7)]
    tail = content[-int(max_chars * 0.2):]
    return (head + "\n[... middle truncated to protect the context window ...]\n"
            + tail)


async def compact_conversation(
    adapter,
    model_id: str,
    messages: list[Message],
    *,
    keep_recent: int = 4,
) -> tuple[Message, list[Message]]:
    """Summarize older turns; returns (summary_message, new_messages)."""
    if len(messages) <= keep_recent:
        return Message.user("(nothing to compact)"), messages
    older = messages[:-keep_recent]
    recent = messages[-keep_recent:]
    transcript = []
    for message in older:
        if message.text:
            transcript.append(f"{message.role}: {message.text[:2000]}")
        for call in message.tool_calls:
            transcript.append(f"assistant tool call: {call.name}({call.arguments})")
        for result in message.tool_results:
            transcript.append(f"tool {result.tool_name} -> "
                              f"{result.content[:600]}")
    request = ChatRequest(
        model=model_id,
        messages=[Message.user(
            compact_prompt() + "\n\n--- TRANSCRIPT ---\n" +
            "\n".join(transcript)[-100_000:])],
        stream=False,
        max_tokens=1200,
    )
    summary_text = ""
    async for event in adapter.chat(request):
        if event.kind == "text" and event.text:
            summary_text += event.text
    if not summary_text.strip():
        summary_text = "(compaction produced no summary; earlier context dropped)"
    summary = Message.user(
        "[Context compaction] Summary of the earlier conversation:\n"
        + summary_text.strip())
    return summary, [summary] + recent


_ = Usage  # reserved for cost-aware compaction policies
_ = ProviderError
