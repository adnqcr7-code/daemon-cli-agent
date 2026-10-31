"""Anthropic adapter against a real local HTTP server."""

import asyncio

from daemon.providers.catalog import provider_by_id
from daemon.providers.registry import AdapterRegistry
from daemon.providers.types import ChatRequest, Message, ToolCall, ToolResult


def _adapter(mock, key="sk-ant-api03-mock-key-000000000000"):
    descriptor = provider_by_id("anthropic")
    return AdapterRegistry().create(descriptor, api_key=key,
                                    endpoint=mock.url)


def test_list_models(mock_anthropic):
    adapter = _adapter(mock_anthropic)
    models = asyncio.run(adapter.list_models())
    asyncio.run(adapter.close())
    assert models and models[0].model_id == "mock-claude"
    assert models[0].capabilities.tool_calling is True


def test_non_streaming_text_and_usage(mock_anthropic):
    mock_anthropic.stages = [{"text": "hello there",
                              "usage": {"input_tokens": 12,
                                        "output_tokens": 7}}]
    adapter = _adapter(mock_anthropic)
    request = ChatRequest(model="mock-claude", messages=[Message.user("hi")],
                          stream=False)
    events = []

    async def go():
        async for event in adapter.chat(request):
            events.append(event)

    asyncio.run(go())
    asyncio.run(adapter.close())
    text = "".join(event.text or "" for event in events if event.kind == "text")
    assert text == "hello there"
    usage = next(event for event in events if event.kind == "usage")
    assert usage.usage.input_tokens == 12


def test_streaming_tool_use(mock_anthropic):
    mock_anthropic.stages = [{
        "text": "checking",
        "tool_calls": [{"name": "read_file", "arguments": {"path": "x.py"}}],
    }]
    adapter = _adapter(mock_anthropic)
    request = ChatRequest(model="mock-claude", messages=[Message.user("read")],
                          stream=True)
    text = ""
    calls = []

    async def go():
        nonlocal text
        async for event in adapter.chat(request):
            if event.kind == "text" and event.text:
                text += event.text
            elif event.kind == "tool_calls":
                calls.extend(event.tool_calls)

    asyncio.run(go())
    asyncio.run(adapter.close())
    assert text == "checking"
    assert calls and calls[0].name == "read_file"
    assert calls[0].arguments == {"path": "x.py"}


def test_wire_mapping_and_merging(mock_anthropic):
    mock_anthropic.stages = [{"text": "ok"}]
    adapter = _adapter(mock_anthropic)
    messages = [
        Message.user("first"),
        Message.user("second"),  # consecutive user turns must merge
        Message.assistant("mid"),
        Message.with_tool_results([ToolResult(
            tool_call_id="call_0", tool_name="read_file", content="data")]),
    ]
    request = ChatRequest(model="mock-claude", messages=messages,
                          system="sys prompt", stream=False)

    async def go():
        async for _ in adapter.chat(request):
            pass

    asyncio.run(go())
    asyncio.run(adapter.close())
    wire = mock_anthropic.requests[-1]
    assert wire["system"] == "sys prompt"
    roles = [message["role"] for message in wire["messages"]]
    assert roles == ["user", "assistant", "user"]  # merged!
    last_blocks = wire["messages"][-1]["content"]
    assert last_blocks[0]["type"] == "tool_result"
    assert last_blocks[0]["tool_use_id"] == "call_0"


def test_assistant_tool_use_maps_to_blocks(mock_anthropic):
    mock_anthropic.stages = [{"text": "done"}]
    adapter = _adapter(mock_anthropic)
    messages = [
        Message.user("q"),
        Message.assistant(None, [ToolCall(id="call_1", name="run_tests",
                                           arguments={"path": "tests"})]),
    ]
    request = ChatRequest(model="mock-claude", messages=messages, stream=False)

    async def go():
        async for _ in adapter.chat(request):
            pass

    asyncio.run(go())
    asyncio.run(adapter.close())
    wire = mock_anthropic.requests[-1]
    block = wire["messages"][-1]["content"][-1]
    assert block["type"] == "tool_use"
    assert block["input"] == {"path": "tests"}


def test_bad_key_rejected(mock_anthropic):
    from daemon.errors import ProviderError

    adapter = _adapter(mock_anthropic, key="sk-ant-api03-wrong-key-0000")
    try:
        asyncio.run(adapter.list_models())
        raised = False
    except ProviderError:
        raised = True
    finally:
        asyncio.run(adapter.close())
    assert raised


def test_max_tokens_defaults_present(mock_anthropic):
    mock_anthropic.stages = [{"text": "x"}]
    adapter = _adapter(mock_anthropic)
    request = ChatRequest(model="mock-claude", messages=[Message.user("q")],
                          stream=False)

    async def go():
        async for _ in adapter.chat(request):
            pass

    asyncio.run(go())
    asyncio.run(adapter.close())
    assert mock_anthropic.requests[-1]["max_tokens"] == 8192
