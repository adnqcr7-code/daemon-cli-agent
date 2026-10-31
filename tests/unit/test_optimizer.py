"""Tests for the ContextOptimizer."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from daemon.agent.optimizer import ContextOptimizer, ContextOptimizationStats
from daemon.config import Config
from daemon.providers.types import Message, ToolSpec, ToolResult, ToolCall


def run_async(coro):
    """Run a coroutine to completion."""
    return asyncio.run(coro)


def test_optimizer_defaults():
    """Default optimization is enabled with context enabled."""
    config = Config({"optimization": {"enabled": True, "context": {"enabled": True}}},
                    user_path=None, project_path=None)
    optimizer = ContextOptimizer(config)
    assert optimizer.enabled is True


def test_optimizer_can_be_disabled():
    config = Config({"optimization": {"enabled": False}}, user_path=None, project_path=None)
    optimizer = ContextOptimizer(config)
    assert optimizer.enabled is False


def test_optimizer_context_can_be_disabled():
    config = Config({"optimization": {"enabled": True, "context": {"enabled": False}}},
                    user_path=None, project_path=None)
    optimizer = ContextOptimizer(config)
    assert optimizer.enabled is False


def test_deduplicate_consecutive_identical_tool_results():
    """Consecutive identical tool results should be merged."""
    config = Config({
        "optimization": {
            "enabled": True,
            "context": {"enabled": True, "dedupe_tool_results": True}
        }
    }, user_path=None, project_path=None)
    optimizer = ContextOptimizer(config)

    # Create messages with duplicate tool results
    msg1 = Message.user("Read file")
    msg2 = Message.with_tool_results([
        ToolResult(tool_call_id="1", tool_name="read_file", content="file content"),
    ])
    msg3 = Message.with_tool_results([
        ToolResult(tool_call_id="2", tool_name="read_file", content="file content"),  # duplicate
    ])
    msg4 = Message.with_tool_results([
        ToolResult(tool_call_id="3", tool_name="read_file", content="different content"),
    ])
    msg5 = Message.assistant("Done")

    messages = [msg1, msg2, msg3, msg4, msg5]
    tools = [ToolSpec(name="read_file", description="Read a file", parameters={})]

    optimized, stats = run_async(optimizer.optimize(messages, tools, 128_000))

    # Should have removed one duplicate
    assert stats.tool_results_merged == 1
    # Find the tool result messages (user messages with tool_results)
    tool_msgs = [m for m in optimized if m.role == "user" and m.tool_results]
    assert len(tool_msgs) == 2  # msg2 and msg4 (msg3 was duplicate)
    # Check content
    assert tool_msgs[0].tool_results[0].content == "file content"
    assert tool_msgs[1].tool_results[0].content == "different content"


def test_deduplicate_non_consecutive_not_merged():
    """Non-consecutive identical tool results should NOT be merged."""
    config = Config({
        "optimization": {
            "enabled": True,
            "context": {"enabled": True, "dedupe_tool_results": True}
        }
    }, user_path=None, project_path=None)
    optimizer = ContextOptimizer(config)

    msg1 = Message.user("Read file")
    msg2 = Message.with_tool_results([
        ToolResult(tool_call_id="1", tool_name="read_file", content="file content"),
    ])
    msg3 = Message.assistant("Thinking...")
    msg4 = Message.with_tool_results([
        ToolResult(tool_call_id="2", tool_name="read_file", content="file content"),  # not consecutive
    ])
    msg5 = Message.assistant("Done")

    messages = [msg1, msg2, msg3, msg4, msg5]
    tools = [ToolSpec(name="read_file", description="Read a file", parameters={})]

    optimized, stats = run_async(optimizer.optimize(messages, tools, 128_000))

    # Should NOT merge non-consecutive duplicates
    assert stats.tool_results_merged == 0
    tool_msgs = [m for m in optimized if m.role == "user" and m.tool_results]
    assert len(tool_msgs) == 2


def test_deduplicate_different_tools_not_merged():
    """Different tools with same content should NOT be merged."""
    config = Config({
        "optimization": {
            "enabled": True,
            "context": {"enabled": True, "dedupe_tool_results": True}
        }
    }, user_path=None, project_path=None)
    optimizer = ContextOptimizer(config)

    msg1 = Message.user("Read files")
    msg2 = Message.with_tool_results([
        ToolResult(tool_call_id="1", tool_name="read_file", content="content"),
    ])
    msg3 = Message.with_tool_results([
        ToolResult(tool_call_id="2", tool_name="glob", content="content"),  # different tool
    ])

    messages = [msg1, msg2, msg3]
    tools = [
        ToolSpec(name="read_file", description="Read a file", parameters={}),
        ToolSpec(name="glob", description="Glob files", parameters={}),
    ]

    optimized, stats = run_async(optimizer.optimize(messages, tools, 128_000))

    assert stats.tool_results_merged == 0
    tool_msgs = [m for m in optimized if m.role == "user" and m.tool_results]
    assert len(tool_msgs) == 2


def test_preserve_system_messages():
    """System messages should always be preserved."""
    config = Config({
        "optimization": {
            "enabled": True,
            "context": {"enabled": True, "preserve_recent_turns": 1}
        }
    }, user_path=None, project_path=None)
    optimizer = ContextOptimizer(config)

    messages = [
        Message.system("System prompt"),
        Message.user("Old turn 1"),
        Message.user("Old turn 2"),
        Message.user("Recent turn"),
    ]
    tools = [ToolSpec(name="t", description="d", parameters={})]

    optimized, removed = optimizer._truncate_old_turns(messages)

    # System message should be preserved
    system_msgs = [m for m in optimized if m.role == "system"]
    assert len(system_msgs) == 1
    assert system_msgs[0].text == "System prompt"


def test_truncate_old_turns_preserves_recent():
    """Old turns should be truncated, recent turns preserved."""
    config = Config({
        "optimization": {
            "enabled": True,
            "context": {"enabled": True, "preserve_recent_turns": 2}
        }
    }, user_path=None, project_path=None)
    optimizer = ContextOptimizer(config)

    messages = [
        Message.system("System prompt"),
        Message.user("Old turn 1"),
        Message.user("Old turn 2"),
        Message.user("Old turn 3"),
        Message.user("Recent 1"),
        Message.user("Recent 2"),
    ]
    tools = [ToolSpec(name="t", description="d", parameters={})]

    optimized, removed = optimizer._truncate_old_turns(messages)

    assert removed == 3  # 3 old turns removed
    user_msgs = [m for m in optimized if m.role == "user"]
    assert len(user_msgs) == 2
    assert user_msgs[0].text == "Recent 1"
    assert user_msgs[1].text == "Recent 2"


def test_optimize_returns_stats():
    """Optimize should return proper statistics."""
    config = Config({
        "optimization": {
            "enabled": True,
            "context": {"enabled": True, "dedupe_tool_results": True}
        }
    }, user_path=None, project_path=None)
    optimizer = ContextOptimizer(config)

    messages = [
        Message.user("Hello"),
        Message.with_tool_results([
            ToolResult(tool_call_id="1", tool_name="read_file", content="content"),
        ]),
        Message.with_tool_results([
            ToolResult(tool_call_id="2", tool_name="read_file", content="content"),  # duplicate
        ]),
    ]
    tools = [ToolSpec(name="read_file", description="Read a file", parameters={})]

    optimized, stats = run_async(optimizer.optimize(messages, tools, 128_000))

    assert isinstance(stats, ContextOptimizationStats)
    assert stats.original_tokens > 0
    assert stats.optimized_tokens >= 0
    assert stats.tokens_saved >= 0
    assert stats.tool_results_merged == 1


def test_summarize_older_turns_uses_adapter():
    """Summarization should call the adapter with compact prompt."""
    config = Config({
        "optimization": {
            "enabled": True,
            "context": {"enabled": True, "summarize_old_turns": True, "preserve_recent_turns": 2}
        }
    }, user_path=None, project_path=None)
    optimizer = ContextOptimizer(config)

    # Create mock adapter that returns an async iterator
    class MockAsyncIterator:
        def __init__(self, items):
            self.items = items
            self.index = 0
        
        def __aiter__(self):
            return self
        
        async def __anext__(self):
            if self.index >= len(self.items):
                raise StopAsyncIteration
            item = self.items[self.index]
            self.index += 1
            return item
    
    class MockAdapter:
        def __init__(self):
            self.chat_calls = []
        
        def chat(self, request):
            self.chat_calls.append(request)
            mock_event = MagicMock()
            mock_event.kind = "text"
            mock_event.text = "Summary of earlier conversation."
            return MockAsyncIterator([mock_event])
    
    adapter = MockAdapter()

    messages = [
        Message.system("System prompt"),
        Message.user("Old turn 1"),
        Message.user("Old turn 2"),
        Message.user("Old turn 3"),
        Message.user("Recent 1"),
        Message.user("Recent 2"),
    ]
    tools = [ToolSpec(name="t", description="d", parameters={})]

    optimized, summarized = run_async(optimizer._summarize_older_turns(
        messages, adapter, "test-model"
    ))

    assert summarized == 3  # 3 old turns summarized
    assert len(optimized) == 4  # system + summary + 2 recent
    assert optimized[0].role == "system"
    assert optimized[1].role == "user"
    assert "Summary of the earlier conversation" in optimized[1].text
    assert optimized[2].text == "Recent 1"
    assert optimized[3].text == "Recent 2"


def test_optimizer_stats_tokens_saved():
    """Stats should show tokens saved."""
    config = Config({
        "optimization": {
            "enabled": True,
            "context": {"enabled": True, "dedupe_tool_results": True}
        }
    }, user_path=None, project_path=None)
    optimizer = ContextOptimizer(config)

    # Create messages with lots of duplicates
    messages = [Message.user("Start")]
    for i in range(10):
        messages.append(Message.with_tool_results([
            ToolResult(tool_call_id=str(i), tool_name="read_file", content="x" * 1000),
        ]))
    tools = [ToolSpec(name="read_file", description="Read a file", parameters={})]

    optimized, stats = run_async(optimizer.optimize(messages, tools, 128_000))

    assert stats.original_tokens > stats.optimized_tokens
    assert stats.tokens_saved == stats.original_tokens - stats.optimized_tokens
    assert stats.tool_results_merged == 9  # 9 duplicates removed


def test_empty_messages_handled():
    """Empty message list should be handled gracefully."""
    config = Config({
        "optimization": {
            "enabled": True,
            "context": {"enabled": True}
        }
    }, user_path=None, project_path=None)
    optimizer = ContextOptimizer(config)

    optimized, stats = run_async(optimizer.optimize([], [], 128_000))

    assert optimized == []
    assert stats.original_tokens == 0
    assert stats.optimized_tokens == 0


def test_preserve_recent_turns_config():
    """Preserve recent turns should be configurable."""
    config = Config({
        "optimization": {
            "enabled": True,
            "context": {"enabled": True, "preserve_recent_turns": 10}
        }
    }, user_path=None, project_path=None)
    optimizer = ContextOptimizer(config)

    assert optimizer.preserve_recent_turns == 10


def test_dedupe_disabled_by_config():
    """Deduplication can be disabled via config."""
    config = Config({
        "optimization": {
            "enabled": True,
            "context": {"enabled": True, "dedupe_tool_results": False}
        }
    }, user_path=None, project_path=None)
    optimizer = ContextOptimizer(config)

    assert optimizer.dedupe_tool_results is False

    messages = [
        Message.user("Read"),
        Message.with_tool_results([
            ToolResult(tool_call_id="1", tool_name="read_file", content="content"),
        ]),
        Message.with_tool_results([
            ToolResult(tool_call_id="2", tool_name="read_file", content="content"),
        ]),
    ]
    tools = [ToolSpec(name="read_file", description="Read a file", parameters={})]

    optimized, stats = run_async(optimizer.optimize(messages, tools, 128_000))

    assert stats.tool_results_merged == 0