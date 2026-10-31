"""Context management: token estimation, compaction triggers, truncation."""

import asyncio

from daemon.agent.context import (
    context_budget,
    estimate_tokens,
    should_compact,
    truncate_tool_output,
)
from daemon.providers.types import Message, ToolSpec, Usage


def test_estimate_tokens_ascii():
    assert estimate_tokens("x" * 400) == 100
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0


def test_estimate_tokens_cjk_weighted():
    # CJK characters are counted closer to one token each.
    cjk = "中" * 100
    assert estimate_tokens(cjk) == 100


def test_message_tokens_counts_tool_results():
    from daemon.agent.context import message_tokens
    from daemon.providers.types import ToolResult

    message = Message.with_tool_results([ToolResult(
        tool_call_id="1", tool_name="read_file", content="x" * 800)])
    assert message_tokens(message) >= 200


def test_conversation_tokens_includes_tool_descriptions():
    from daemon.agent.context import conversation_tokens

    tools = [ToolSpec(name="t", description="d" * 400,
                      parameters={"type": "object"})]
    total = conversation_tokens([Message.user("hi")], tools)
    assert total >= 100 + 60


def test_context_budget_reserves_output_space():
    assert context_budget(128_000) == 128_000 - 32_000
    assert context_budget(None) > 0  # sane default


def test_should_compact_threshold():
    tools = [ToolSpec(name="t", description="x", parameters={})]
    big = [Message.user("y" * 12_000)] * 30   # ~90k tokens
    small = [Message.user("hi")]
    assert should_compact(big, tools, 128_000)
    assert not should_compact(small, tools, 128_000)


def test_truncate_tool_output_keeps_head_and_tail():
    content = "HEAD" + "x" * 100_000 + "TAIL"
    truncated = truncate_tool_output(content, max_chars=1000)
    assert len(truncated) < len(content)
    assert truncated.startswith("HEAD")
    assert truncated.endswith("TAIL")
    assert "truncated" in truncated


def test_usage_merge():
    usage = Usage(input_tokens=10, output_tokens=5)
    usage.add(Usage(input_tokens=1, output_tokens=2))
    assert usage.input_tokens == 11
    assert usage.output_tokens == 7
    assert usage.total_tokens == 18


def test_compact_conversation_summarizes(mock_openai):
    from daemon.agent.context import compact_conversation
    from daemon.providers.registry import AdapterRegistry, custom_descriptor

    adapter = AdapterRegistry().create(
        custom_descriptor("mock", mock_openai.url + "/v1"),
        api_key="test-key-1234567890", config=None)
    messages = [Message.user(f"turn {index}") for index in range(10)]

    async def go():
        summary, kept = await compact_conversation(
            adapter, "mock-coder", messages, keep_recent=4)
        return summary, kept

    summary, kept = asyncio.run(go())
    assert summary.role == "user"
    assert "Summary of the earlier conversation" in summary.text
    assert len(kept) == 5  # summary + 4 recent
    asyncio.run(adapter.close())
