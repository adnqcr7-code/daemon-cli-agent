"""Context optimization: deduplication, summarization, token budgeting.

Reduces context size by removing redundant information while preserving
instructions, constraints, and important source references.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Iterable

from ..config import Config
from ..providers.types import Message, ToolSpec, ToolCall, ToolResult
from .context import (
    conversation_tokens,
    estimate_tokens,
    should_compact,
    compact_conversation,
)


@dataclass
class ContextOptimizationStats:
    """Statistics from a context optimization pass."""

    original_tokens: int = 0
    optimized_tokens: int = 0
    tokens_saved: int = 0
    turns_removed: int = 0
    tool_results_merged: int = 0
    summaries_created: int = 0


@dataclass
class _ToolResultKey:
    """Normalized key for deduplicating tool results."""

    tool_name: str
    content_hash: str

    @classmethod
    def from_result(cls, result: ToolResult) -> "_ToolResultKey":
        content = result.content.strip()
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
        return cls(tool_name=result.tool_name, content_hash=content_hash)


class ContextOptimizer:
    """Optimizes conversation context by removing redundancy while preserving
    critical information (instructions, constraints, source references)."""

    def __init__(self, config: Config | None = None) -> None:
        # Global optimization toggle disables everything
        global_enabled = config.get("optimization.enabled", True) if config else True
        context_enabled = config.get("optimization.context.enabled", True) if config else True
        self.enabled = global_enabled and context_enabled
        self.preserve_system = True
        self.preserve_tool_specs = True
        self.preserve_recent_turns = config.get("optimization.context.preserve_recent_turns", 4) if config else 4
        self.dedupe_tool_results = config.get("optimization.context.dedupe_tool_results", True) if config else True
        self.summarize_old_turns = config.get("optimization.context.summarize_old_turns", True) if config else True
        self.max_summary_length = config.get("optimization.context.max_summary_length", 2000) if config else 2000

    async def optimize(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        context_window: int,
        *,
        adapter=None,
        model_id: str | None = None,
    ) -> tuple[list[Message], ContextOptimizationStats]:
        """Optimize the conversation context.

        Returns (optimized_messages, stats).
        """
        stats = ContextOptimizationStats()
        if not self.enabled:
            stats.original_tokens = conversation_tokens(messages, tools)
            stats.optimized_tokens = stats.original_tokens
            return messages, stats

        stats.original_tokens = conversation_tokens(messages, tools)

        # Step 1: Deduplicate consecutive identical tool results
        if self.dedupe_tool_results:
            messages, deduped = self._deduplicate_tool_results(messages)
            stats.tool_results_merged = deduped

        # Step 2: If still over budget, summarize old turns
        if should_compact(messages, tools, context_window, threshold=0.7):
            if self.summarize_old_turns and adapter and model_id:
                messages, summarized = await self._summarize_older_turns(
                    messages, adapter, model_id
                )
                stats.summaries_created = summarized
                stats.turns_removed += summarized
            else:
                # Fallback: simple truncation of oldest turns (keep system + recent)
                messages, removed = self._truncate_old_turns(messages)
                stats.turns_removed += removed

        stats.optimized_tokens = conversation_tokens(messages, tools)
        stats.tokens_saved = max(0, stats.original_tokens - stats.optimized_tokens)
        return messages, stats

    def _deduplicate_tool_results(self, messages: list[Message]) -> tuple[list[Message], int]:
        """Merge consecutive messages with identical tool results.

        Tool results are stored on user messages (Anthropic convention),
        so we look for user messages with tool_results.
        
        We compare entire messages - if a user message with tool_results
        has the exact same tool results as the previous such message, it's a duplicate.
        """
        if not messages:
            return messages, 0

        result: list[Message] = []
        deduped = 0
        prev_tool_msg: Message | None = None

        for msg in messages:
            # Tool results travel on user messages (role="user")
            if msg.role != "user" or not msg.tool_results:
                # Not a tool result message, keep as-is
                result.append(msg)
                prev_tool_msg = None
                continue

            # Check if this message's tool results match the previous tool message
            if prev_tool_msg is not None and self._tool_results_equal(msg.tool_results, prev_tool_msg.tool_results):
                # This is a duplicate - skip it
                deduped += len(msg.tool_results)
                continue

            # Not a duplicate, keep it and update prev_tool_msg
            result.append(msg)
            prev_tool_msg = msg

        return result, deduped

    def _tool_results_equal(self, results1: list[ToolResult], results2: list[ToolResult]) -> bool:
        """Check if two lists of tool results are equal."""
        if len(results1) != len(results2):
            return False
        for r1, r2 in zip(results1, results2):
            if r1.tool_name != r2.tool_name:
                return False
            if r1.content != r2.content:
                return False
            if r1.is_error != r2.is_error:
                return False
        return True

    async def _summarize_older_turns(
        self,
        messages: list[Message],
        adapter,
        model_id: str,
    ) -> tuple[list[Message], int]:
        """Summarize older conversation turns using the model.

        Returns (new_messages, num_summarized).
        """
        if len(messages) <= self.preserve_recent_turns + 1:  # +1 for system
            return messages, 0

        # Separate system message, old turns, and recent turns
        system_msgs = [m for m in messages if m.role == "system"]
        non_system = [m for m in messages if m.role != "system"]

        if len(non_system) <= self.preserve_recent_turns:
            return messages, 0

        older = non_system[:-self.preserve_recent_turns]
        recent = non_system[-self.preserve_recent_turns:]

        # Build transcript for summarization
        transcript_lines: list[str] = []
        for msg in older:
            if msg.text:
                transcript_lines.append(f"{msg.role}: {msg.text[:2000]}")
            for call in msg.tool_calls:
                transcript_lines.append(f"assistant tool call: {call.name}({call.arguments})")
            for result in msg.tool_results:
                content_preview = result.content[:600]
                transcript_lines.append(f"tool {result.tool_name} -> {content_preview}")

        transcript = "\n".join(transcript_lines)
        if not transcript.strip():
            return messages, 0

        from ..providers.types import ChatRequest
        from .prompts import compact_prompt

        request = ChatRequest(
            model=model_id,
            messages=[Message.user(compact_prompt() + "\n\n--- TRANSCRIPT ---\n" + transcript[-100_000:])],
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
            + summary_text.strip()[: self.max_summary_length]
        )

        new_messages = system_msgs + [summary] + recent
        return new_messages, len(older)

    def _truncate_old_turns(self, messages: list[Message]) -> tuple[list[Message], int]:
        """Simple fallback: remove oldest non-system turns until under budget."""
        if len(messages) <= self.preserve_recent_turns + 1:
            return messages, 0

        system_msgs = [m for m in messages if m.role == "system"]
        non_system = [m for m in messages if m.role != "system"]

        if len(non_system) <= self.preserve_recent_turns:
            return messages, 0

        # Keep only recent turns
        recent = non_system[-self.preserve_recent_turns:]
        removed = len(non_system) - len(recent)
        return system_msgs + recent, removed


def _normalize_tool_result_content(content: str) -> str:
    """Normalize tool result content for comparison (strip timestamps, paths)."""
    # Remove common variable parts
    content = re.sub(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\b", "<timestamp>", content)
    content = re.sub(r"\b(?:[a-zA-Z]:[\\/]|(?:/[^/]+)+)[^\s]*", "<path>", content)
    content = re.sub(r"\b\d+\.\d+\.\d+\.\d+\b", "<ip>", content)
    return content.strip()