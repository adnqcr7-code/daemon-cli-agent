"""Gemini adapter against a real local HTTP server."""

import asyncio

from daemon.providers.catalog import provider_by_id
from daemon.providers.registry import AdapterRegistry
from daemon.providers.types import ChatRequest, Message, ToolCall, ToolResult


def _adapter(mock, key="AIza" + "a" * 35):
    descriptor = provider_by_id("gemini")
    return AdapterRegistry().create(descriptor, api_key=key, endpoint=mock.url)


def test_list_models_filters_non_chat(mock_gemini):
    adapter = _adapter(mock_gemini)
    models = asyncio.run(adapter.list_models())
    asyncio.run(adapter.close())
    assert [model.model_id for model in models] == ["mock-gemini"]


def test_non_streaming_text(mock_gemini):
    mock_gemini.stages = [{"text": "hello from gemini"}]
    adapter = _adapter(mock_gemini)
    request = ChatRequest(model="mock-gemini", messages=[Message.user("hi")],
                          stream=False)
    events = []

    async def go():
        async for event in adapter.chat(request):
            events.append(event)

    asyncio.run(go())
    asyncio.run(adapter.close())
    text = "".join(event.text or "" for event in events if event.kind == "text")
    assert text == "hello from gemini"


def test_streaming_function_calls(mock_gemini):
    mock_gemini.stages = [{
        "tool_calls": [{"name": "web_search",
                        "arguments": {"query": "python 3.13"}}],
    }]
    adapter = _adapter(mock_gemini)
    request = ChatRequest(model="mock-gemini", messages=[Message.user("find")],
                          stream=True)
    calls = []

    async def go():
        async for event in adapter.chat(request):
            if event.kind == "tool_calls":
                calls.extend(event.tool_calls)

    asyncio.run(go())
    asyncio.run(adapter.close())
    assert calls and calls[0].name == "web_search"
    assert calls[0].arguments == {"query": "python 3.13"}


def test_wire_mapping(mock_gemini):
    mock_gemini.stages = [{"text": "ok"}]
    adapter = _adapter(mock_gemini)
    messages = [
        Message.user("question"),
        Message.assistant(None, [ToolCall(id="call_0", name="web_search",
                                          arguments={"query": "x"})]),
        Message.with_tool_results([ToolResult(
            tool_call_id="call_0", tool_name="web_search",
            content="result text")]),
    ]
    request = ChatRequest(model="mock-gemini", messages=messages,
                          system="sys", stream=False)

    async def go():
        async for _ in adapter.chat(request):
            pass

    asyncio.run(go())
    asyncio.run(adapter.close())
    wire = mock_gemini.requests[-1]
    assert wire["systemInstruction"]["parts"][0]["text"] == "sys"
    roles = [content["role"] for content in wire["contents"]]
    assert roles == ["user", "model", "user"]
    call_part = wire["contents"][1]["parts"][0]["functionCall"]
    assert call_part["name"] == "web_search"
    response_part = wire["contents"][2]["parts"][0]["functionResponse"]
    assert response_part["name"] == "web_search"
    assert response_part["response"]["result"] == "result text"


def test_error_result_mapping(mock_gemini):
    mock_gemini.stages = [{"text": "ok"}]
    adapter = _adapter(mock_gemini)
    messages = [Message.with_tool_results([ToolResult(
        tool_call_id="c", tool_name="t", content="boom", is_error=True)])]
    request = ChatRequest(model="mock-gemini", messages=messages, stream=False)

    async def go():
        async for _ in adapter.chat(request):
            pass

    asyncio.run(go())
    asyncio.run(adapter.close())
    wire = mock_gemini.requests[-1]
    response = wire["contents"][-1]["parts"][0]["functionResponse"]["response"]
    assert "error" in response


def test_bad_key_rejected(mock_gemini):
    from daemon.errors import ProviderError

    adapter = _adapter(mock_gemini, key="AIza" + "b" * 35)
    try:
        asyncio.run(adapter.list_models())
        raised = False
    except ProviderError:
        raised = True
    finally:
        asyncio.run(adapter.close())
    assert raised
