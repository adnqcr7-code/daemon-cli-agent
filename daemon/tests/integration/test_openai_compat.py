"""OpenAI-compatible adapter against a real local HTTP server."""

import asyncio

from daemon.config import Config
from daemon.providers.registry import AdapterRegistry, custom_descriptor
from daemon.providers.types import ChatRequest, Message, ToolSpec
from daemon.errors import AuthError


def _adapter(mock, config=None, key="test-key-1234567890", stream_usage=False):
    from daemon.providers.catalog import ProviderDescriptor

    descriptor = ProviderDescriptor(
        id="mock", name="Mock", protocol="openai", category="gateway",
        base_url=mock.url + "/v1", stream_usage=stream_usage)
    return AdapterRegistry().create(descriptor, api_key=key, config=config)


def test_list_models(mock_openai):
    adapter = _adapter(mock_openai)
    models = asyncio.run(adapter.list_models())
    asyncio.run(adapter.close())
    assert [model.model_id for model in models] == ["mock-coder", "mock-fast"]
    assert all(model.discovered for model in models)


def test_list_models_rejects_bad_key(mock_openai):
    adapter = _adapter(mock_openai, key="wrong-key-0000000")
    from daemon.errors import ProviderError

    try:
        asyncio.run(adapter.list_models())
        raised = False
    except (ProviderError, AuthError):
        raised = True
    finally:
        asyncio.run(adapter.close())
    assert raised


def test_streaming_text(mock_openai):
    adapter = _adapter(mock_openai, stream_usage=True)
    request = ChatRequest(model="mock-coder",
                          messages=[Message.user("hi")], stream=True)
    text = ""
    usage = None
    finish = None

    async def go():
        nonlocal text, usage, finish
        async for event in adapter.chat(request):
            if event.kind == "text" and event.text:
                text += event.text
            elif event.kind == "usage":
                usage = event.usage
            elif event.kind == "done":
                finish = event.finish_reason

    asyncio.run(go())
    asyncio.run(adapter.close())
    assert text == "hello"
    assert usage is not None and usage.input_tokens == 10
    assert finish == "stop"


def test_streaming_tool_calls(mock_openai):
    mock_openai.stages = [{
        "text": "let me read that",
        "tool_calls": [{"name": "read_file",
                        "arguments": {"path": "bug.py"}}],
        "usage": {"prompt_tokens": 21, "completion_tokens": 9},
    }]
    adapter = _adapter(mock_openai)
    request = ChatRequest(model="mock-coder",
                          messages=[Message.user("read bug.py")], stream=True)
    calls = None

    async def go():
        nonlocal calls
        async for event in adapter.chat(request):
            if event.kind == "tool_calls":
                calls = event.tool_calls

    asyncio.run(go())
    asyncio.run(adapter.close())
    assert calls and calls[0].name == "read_file"
    assert calls[0].arguments == {"path": "bug.py"}


def test_non_streaming_round(mock_openai):
    mock_openai.stages = [{"text": "plain answer",
                           "tool_calls": [], "usage": {"prompt_tokens": 3,
                                                       "completion_tokens": 4}}]
    adapter = _adapter(mock_openai)
    request = ChatRequest(model="mock-coder",
                          messages=[Message.user("q")], stream=False)
    events = []

    async def go():
        async for event in adapter.chat(request):
            events.append(event)

    asyncio.run(go())
    asyncio.run(adapter.close())
    kinds = [event.kind for event in events]
    assert "text" in kinds and "done" in kinds
    assert events[-1].finish_reason == "stop"


def test_full_message_cycle_mapping(mock_openai):
    """system/user/assistant/tool_result round-trip maps to wire format."""
    mock_openai.stages = [
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "x"}}]},
        {"text": "done"},
    ]
    adapter = _adapter(mock_openai)
    messages = [
        Message.user("please read x"),
        Message.assistant(None, [__import__("daemon.providers.types",
                                            fromlist=["ToolCall"]).ToolCall(
            id="call_0", name="read_file", arguments={"path": "x"})]),
        Message.with_tool_results([__import__(
            "daemon.providers.types", fromlist=["ToolResult"]).ToolResult(
            tool_call_id="call_0", tool_name="read_file", content="data")]),
    ]
    request = ChatRequest(model="mock-coder", messages=messages,
                          system="be helpful", stream=False)

    async def go():
        async for _ in adapter.chat(request):
            pass

    asyncio.run(go())
    asyncio.run(adapter.close())
    wire = mock_openai.requests[-1]
    roles = [message["role"] for message in wire["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    assert wire["messages"][2]["tool_calls"][0]["function"]["name"] == "read_file"
    assert wire["messages"][3]["tool_call_id"] == "call_0"


def test_tools_are_advertised(mock_openai):
    mock_openai.stages = [{"text": "ok"}]
    adapter = _adapter(mock_openai)
    request = ChatRequest(
        model="mock-coder", messages=[Message.user("q")], stream=False,
        tools=[ToolSpec(name="read_file", description="Read a file",
                        parameters={"type": "object",
                                    "properties": {"path": {"type": "string"}}})])

    async def go():
        async for _ in adapter.chat(request):
            pass

    asyncio.run(go())
    asyncio.run(adapter.close())
    wire = mock_openai.requests[-1]
    assert wire["tools"][0]["function"]["name"] == "read_file"
    assert wire["tool_choice"] == "auto"


def test_retry_on_transient_server_error(mock_openai):
    mock_openai.stages = [{"fail_status": 500}, {"text": "recovered"}]
    adapter = _adapter(mock_openai, config=Config.load())
    request = ChatRequest(model="mock-coder", messages=[Message.user("q")],
                          stream=False)
    text = ""

    async def go():
        nonlocal text
        async for event in adapter.chat(request):
            if event.kind == "text" and event.text:
                text += event.text

    asyncio.run(go())
    asyncio.run(adapter.close())
    assert text == "recovered"
    assert mock_openai.failed_once


def test_reasoning_models_get_max_completion_tokens(mock_openai):
    mock_openai.stages = [{"text": "think"}]
    adapter = _adapter(mock_openai)
    request = ChatRequest(model="o3-mini", messages=[Message.user("q")],
                          stream=False, max_tokens=512, temperature=0.5)

    async def go():
        async for _ in adapter.chat(request):
            pass

    asyncio.run(go())
    asyncio.run(adapter.close())
    wire = mock_openai.requests[-1]
    assert wire.get("max_completion_tokens") == 512
    assert "max_tokens" not in wire
    assert "temperature" not in wire  # reasoning models reject temperature
