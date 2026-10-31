"""The Research agent: restricted by construction, citation-oriented."""

import asyncio

import pytest

from daemon.config import Config
from daemon.memory import MemoryStore
from daemon.models import RemoteModel
from daemon.permissions import Effect
from daemon.providers.catalog import ProviderDescriptor
from daemon.providers.registry import AdapterRegistry
from daemon.providers.types import ModelCapabilities
from daemon.research import ResearchAgent, research_registry


def _model() -> RemoteModel:
    return RemoteModel(model_id="mock-research", provider="mock",
                       capabilities=ModelCapabilities(
                           context_window=200_000, tool_calling=True))


def _agent(mock, workspace, *, config=None):
    descriptor = ProviderDescriptor(
        id="mock", name="Mock", protocol="openai", category="gateway",
        base_url=(mock.url + "/v1") if mock else "http://localhost:9/v1",
        stream_usage=False)
    adapter = AdapterRegistry().create(descriptor, api_key="test-key-1234567890")
    agent = ResearchAgent(adapter=adapter, model=_model(), workspace=workspace,
                          config=config or Config.load(workspace),
                          memory=MemoryStore(workspace))
    return agent, adapter


def test_research_registry_has_no_mutating_tools():
    names = research_registry().names()
    assert "web_search" in names and "web_fetch" in names
    for forbidden in ("write_file", "edit_file", "apply_patch", "delete_path",
                      "run_command", "git_commit", "format_code", "move_path"):
        assert forbidden not in names, forbidden


def test_research_policy_denies_writes_and_allows_web(workspace):
    agent, adapter = _agent(None, workspace)
    engine = agent.permissions
    write = engine.check("write_file", {"path": "x", "content": "y"},
                         mutating=True)
    assert write.effect is Effect.DENY
    search = engine.check("web_search", {"query": "x"}, network=True)
    assert search.effect is Effect.ALLOW
    command = engine.check("run_command", {"command": "pytest"}, mutating=True)
    assert command.effect is Effect.DENY
    asyncio.run(adapter.close())


def test_research_run_with_mocked_web(mock_openai, workspace, monkeypatch):
    async def fake_web_search(query, *, backend, limit, searxng_url=None):
        return [
            {"title": "Daemon docs", "url": "https://example.com/daemon",
             "snippet": "An agent"},
            {"title": "MCP spec", "url": "https://example.com/mcp",
             "snippet": "The protocol"},
        ][:limit]

    monkeypatch.setattr("daemon.tools.web.web_search", fake_web_search)
    mock_openai.stages = [
        {"tool_calls": [{"name": "web_search",
                         "arguments": {"query": "daemon agent mcp"}}]},
        {"tool_calls": [{"name": "web_fetch",
                         "arguments": {"url": mock_openai.url + "/v1/models"}}]},
        {"text": "Findings: Daemon is an agent [1]; the models endpoint "
                 "listed mock-coder and mock-fast [2]."},
    ]
    agent, adapter = _agent(mock_openai, workspace)

    async def go():
        return await agent.ask("What is Daemon and which models exist?")

    result = asyncio.run(go())
    asyncio.run(adapter.close())
    assert "mock-coder" in result.text
    assert "[1]" in result.text and "[2]" in result.text
    # the fetch actually went through the real HTTP stack to the mock server
    assert result.tool_calls == 2
    # nothing was written to the workspace
    assert (workspace / "bug.py").read_text().count("a - b") == 1


def test_research_agent_cannot_invoke_write_tools(mock_openai, workspace,
                                                  monkeypatch):
    async def fake_web_search(query, *, backend, limit, searxng_url=None):
        return []

    monkeypatch.setattr("daemon.tools.web.web_search", fake_web_search)
    mock_openai.stages = [
        {"tool_calls": [{"name": "write_file",
                         "arguments": {"path": "pwned.txt",
                                       "content": "hacked"}}]},
        {"text": "I cannot write files; I am read-only."},
    ]
    agent, adapter = _agent(mock_openai, workspace)

    async def go():
        return await agent.ask("write pwned.txt")

    result = asyncio.run(go())
    asyncio.run(adapter.close())
    assert not (workspace / "pwned.txt").exists()
    session = agent.session
    tool_message = next(m for m in session.messages if m.tool_results)
    assert tool_message.tool_results[0].is_error
    assert "Unknown tool" in tool_message.tool_results[0].content


def test_research_system_prompt_is_distinct(workspace):
    agent, adapter = _agent(None, workspace)
    runtime = agent.runtime()
    prompt = runtime.system_prompt()
    assert "Daemon Research" in prompt
    assert "citations" in prompt.lower()
    build = agent.runtime()
    assert "read-only" in prompt.lower() or "read-only" in build.system_prompt()
    asyncio.run(adapter.close())


def test_research_notes_are_allowed(mock_openai, workspace):
    mock_openai.stages = [
        {"tool_calls": [{"name": "memory_note", "arguments": {
            "content": "MCP uses JSON-RPC 2.0",
            "kind": "research"}}]},
        {"text": "Noted."},
    ]
    agent, adapter = _agent(mock_openai, workspace)

    async def go():
        return await agent.ask("save that note")

    asyncio.run(go())
    asyncio.run(adapter.close())
    assert any("JSON-RPC" in entry.content
               for entry in MemoryStore(workspace).entries())


def test_research_denies_shell_even_via_policy_layers(workspace):
    """Even if someone later adds run_command to the registry, the
    research policy rules deny it (defence in depth)."""
    agent, adapter = _agent(None, workspace)
    decision = agent.permissions.check(
        "run_command", {"command": "ls"}, mutating=True)
    assert decision.effect is Effect.DENY
    asyncio.run(adapter.close())
