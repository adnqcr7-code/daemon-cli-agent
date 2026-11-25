"""Tests for the per-provider capability manifest and the separate
reasoning-trace store (v0.9.0 Phase 3)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from click.testing import CliRunner

from daemon.agent.reasoning import ReasoningStore
from daemon.agent.runtime import AgentRuntime
from daemon.agent.session import Session, SessionMeta
from daemon.checkpoints import CheckpointManager
from daemon.cli import main
from daemon.config import Config, daemon_home
from daemon.memory import MemoryStore
from daemon.permissions import PermissionEngine
from daemon.project import ProjectIndex
from daemon.providers.manifest import manifest_for
from daemon.providers.types import (
    ModelCapabilities,
    RemoteModel,
    StreamEvent,
    Usage,
)
from daemon.security import redactor
from daemon.tools import default_registry


def test_deepseek_reasoning_traces_are_named() -> None:
    manifest = manifest_for("deepseek")
    assert manifest is not None
    assert manifest.reasoning_mode == "reasoning_content"
    assert manifest.reasoning_trace_field == "reasoning_content"
    assert manifest.tool_calling is True
    assert manifest.parallel_tool_calls is True


def test_model_layer_overrides_provider_on_tool_calling() -> None:
    """DeepSeek's API offers tools, but deepseek-reasoner does not —
    the model record wins over the provider record."""
    manifest = manifest_for("deepseek", "deepseek-reasoner")
    assert manifest is not None
    assert manifest.tool_calling is False
    assert manifest.reasoning is True
    assert manifest.context_window == 64_000
    assert manifest.sources["tool_calling"] == "model"


def test_anthropic_thinking_and_cache_ttl() -> None:
    manifest = manifest_for("anthropic")
    assert manifest is not None
    assert manifest.reasoning_mode == "thinking"
    assert manifest.reasoning_trace_field == "thinking"
    assert manifest.cache_ttl_seconds == 300
    assert manifest.parallel_tool_calls is True


def test_demo_provider_has_no_reasoning_surface() -> None:
    manifest = manifest_for("demo")
    assert manifest is not None
    assert manifest.reasoning_mode == "none"
    assert manifest.parallel_tool_calls is False
    assert manifest.tool_calling is True


def test_openai_model_record_merges() -> None:
    manifest = manifest_for("openai", "gpt-4o")
    assert manifest is not None
    assert manifest.context_window == 128_000
    assert manifest.input_price_per_mtok == 2.50
    assert manifest.tool_calling is True
    assert manifest.parallel_tool_calls is True
    assert manifest.sources["context_window"] == "model"
    assert manifest.sources["parallel_tool_calls"] == "provider"


def test_unknown_stays_unknown() -> None:
    """Nothing is invented: a provider with no documented reasoning
    surface reports None, and max output is unknown everywhere the
    catalog does not assert it."""
    assert manifest_for("mistral").reasoning_mode is None
    assert manifest_for("deepseek").max_output_tokens is None
    assert manifest_for("openai").context_window is None  # model-level
    assert manifest_for("no-such-provider") is None


def test_providers_show_cli_json() -> None:
    result = CliRunner().invoke(
        main, ["providers", "show", "deepseek", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["reasoning_trace_field"] == "reasoning_content"
    assert payload["provider_id"] == "deepseek"


def test_providers_list_cli() -> None:
    result = CliRunner().invoke(main, ["providers", "list"])
    assert result.exit_code == 0, result.output
    assert "deepseek" in result.output
    assert "demo" in result.output


def test_reasoning_store_round_trip_and_redaction(tmp_path) -> None:
    store = ReasoningStore(home=tmp_path)
    secret = "live-secret-value-123"
    redactor().register(secret)
    store.append("run-1", f"thinking about {secret} here",
                 provider="deepseek", model="deepseek-reasoner")
    store.append("run-1", "second chunk", provider="deepseek",
                 model="deepseek-reasoner")
    records = store.read("run-1")
    assert len(records) == 2
    assert records[0]["provider"] == "deepseek"
    assert secret not in records[0]["reasoning"]
    assert "***REDACTED***" in records[0]["reasoning"]
    assert store.read("no-such-run") == []
    assert store.path_for("run-1").name == "reasoning.jsonl"


class _ThinkingAdapter:
    """Yields a reasoning trace, then a plain answer, no tool calls."""

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, request):
        self.calls += 1
        yield StreamEvent(kind="text",
                          thinking="trace-secret-plan: check bug.py first")
        yield StreamEvent(kind="text", text="Done.")
        yield StreamEvent(kind="usage", usage=Usage())
        yield StreamEvent(kind="done", finish_reason="stop")

    async def close(self) -> None:
        pass


def test_reasoning_trace_is_stored_separately_never_replayed(
        workspace) -> None:
    """A full run through the real runtime: the trace lands in
    reasoning.jsonl and NOWHERE in the conversation — not in the
    session messages, not in the transcript journal."""
    adapter = _ThinkingAdapter()
    model = RemoteModel(
        model_id="deepseek-reasoner", provider="deepseek",
        capabilities=ModelCapabilities(context_window=64_000,
                                       tool_calling=False))
    session = Session(meta=SessionMeta(workspace=str(workspace),
                                       provider="deepseek",
                                       model="deepseek-reasoner"))
    index = ProjectIndex(workspace)
    index.build()
    runtime = AgentRuntime(
        adapter=adapter, model=model, registry=default_registry(),
        permissions=PermissionEngine(workspace, assume_yes=True),
        session=session, config=Config.load(workspace),
        workspace=workspace, memory=MemoryStore(workspace),
        checkpoints=CheckpointManager(workspace), index=index)
    result = asyncio.run(runtime.run("Say hello."))
    assert result.text == "Done."
    run_id = session.meta.id
    records = ReasoningStore().read(run_id)
    assert len(records) == 1
    assert "trace-secret-plan" in records[0]["reasoning"]
    assert records[0]["provider"] == "deepseek"
    transcript = (daemon_home() / "sessions" / run_id
                  / "transcript.jsonl").read_text(encoding="utf-8")
    assert "trace-secret-plan" not in transcript
    for message in session.messages:
        assert "trace-secret-plan" not in (message.text or "")
        for call in message.tool_calls:
            assert "trace-secret-plan" not in json.dumps(call.arguments)
    assert not (Path(workspace) / "bug.py").read_text().endswith("Done.")
