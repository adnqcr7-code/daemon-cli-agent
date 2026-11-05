"""Optimization engine wiring: tool pruning and failure-loop intervention
reach the provider through the real agent loop."""

import asyncio
import json

from tests.integration.test_agent_runtime import _runtime

FAILING_CALL = {"tool_calls": [{
    "name": "read_file",
    "arguments": {"path": "no-such-file.py"}}]}


def test_tool_pruning_reaches_the_provider(mock_openai, workspace):
    mock_openai.stages = [{"text": "Nothing to change."}]
    runtime, adapter, session = _runtime(mock_openai, workspace,
                                         assume_yes=True)

    asyncio.run(runtime.run("fix the off-by-one bug in add()"))
    asyncio.run(adapter.close())

    advertised = [tool["function"]["name"]
                  for tool in mock_openai.requests[0].get("tools", [])]
    assert 0 < len(advertised) <= 20          # 33 builtin tools, pruned
    assert "read_file" in advertised          # core loop always available
    assert "run_tests" in advertised
    assert "design_get" not in advertised     # irrelevant to this task
    assert runtime.tool_selector.last_stats.tokens_saved > 0


def test_failure_loop_nudge_reaches_the_model(mock_openai, workspace):
    mock_openai.stages = [FAILING_CALL, FAILING_CALL, FAILING_CALL,
                          FAILING_CALL, {"text": "Changed approach."}]
    runtime, adapter, session = _runtime(mock_openai, workspace,
                                         assume_yes=True)

    result = asyncio.run(runtime.run("fix the flaky check"))
    asyncio.run(adapter.close())

    assert result.tool_calls == 4
    assert result.text == "Changed approach."
    bodies = [json.dumps(request) for request in mock_openai.requests]
    assert not any("Failure-loop intervention" in body for body in bodies[:3])
    assert any("Failure-loop intervention" in body for body in bodies[3:])
