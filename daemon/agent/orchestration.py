"""Subagent orchestration: specialized roles, parallel where safe.

Read-only roles (architect, reviewer, security, researcher) may run in
parallel; the Developer/Debugger/Tester roles mutate state and always run
sequentially - two agents never edit the same files concurrently.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from ..errors import DaemonError
from ..log import audit, get_logger
from ..models import RemoteModel
from ..permissions import PermissionEngine
from ..providers.types import Message, Usage
from ..tools import ToolRegistry, read_only_registry, default_registry
from .prompts import subagent_role
from .runtime import AgentRuntime, AgentUI
from .session import Session, SessionMeta

_logger = get_logger("orchestrator")


@dataclass
class SubagentSpec:
    role: str
    brief: str
    model: RemoteModel | None = None      # falls back to role configuration
    read_only: bool | None = None         # default: from the role definition


@dataclass
class SubagentResult:
    role: str
    ok: bool
    text: str = ""
    usage: Usage = field(default_factory=Usage)
    error: str = ""


class Orchestrator:
    """Spawns isolated runtimes for specialized subagents."""

    def __init__(self, *, adapter_factory, models: dict[str, RemoteModel],
                 default_model: RemoteModel, workspace: Path,
                 config: Config | None = None, memory=None, index=None,
                 checkpoints=None) -> None:
        """
        adapter_factory(model: RemoteModel) -> adapter for that model's
        provider (the main profile's adapter when all models share it).
        """
        self.adapter_factory = adapter_factory
        self.models = models
        self.default_model = default_model
        self.workspace = workspace
        self.config = config
        self.memory = memory
        self.index = index
        self.checkpoints = checkpoints

    def _model_for(self, spec: SubagentSpec) -> RemoteModel:
        if spec.model is not None:
            return spec.model
        role = subagent_role(spec.role) or {}
        model_role = role.get("model_role")
        if model_role and model_role in self.models:
            return self.models[model_role]
        return self.default_model

    def _registry_for(self, spec: SubagentSpec, role: dict) -> ToolRegistry:
        read_only = spec.read_only
        if read_only is None:
            read_only = spec.role in ("architect", "reviewer", "security",
                                      "researcher")
        return read_only_registry() if read_only else default_registry()

    async def run_subagent(self, spec: SubagentSpec,
                           ui: AgentUI | None = None) -> SubagentResult:
        role = subagent_role(spec.role)
        if role is None:
            raise DaemonError(
                f"Unknown subagent role '{spec.role}'. Available: "
                f"{', '.join(sorted(subagent_role.__module__ and [])) if False else ''}")
        model = self._model_for(spec)
        adapter = self.adapter_factory(model)
        registry = self._registry_for(spec, role)
        permissions = PermissionEngine(
            self.workspace,
            rules=(self.config.get("permissions.rules") or []) if self.config else None,
            assume_yes=False,
        )
        session = Session(meta=SessionMeta(
            workspace=str(self.workspace), provider=model.provider,
            model=model.model_id, role=spec.role,
            title=f"[{role['name']}] {spec.brief[:60]}"))
        runtime = AgentRuntime(
            adapter=adapter, model=model, registry=registry,
            permissions=permissions, session=session, ui=ui,
            config=self.config, workspace=self.workspace, memory=self.memory,
            index=self.index, role=spec.role,
            system_prompt=role["prompt"],
        )
        try:
            result = await runtime.run(spec.brief)
            return SubagentResult(
                role=spec.role, ok=True, text=result.text,
                usage=result.usage)
        except DaemonError as exc:
            return SubagentResult(role=spec.role, ok=False, error=str(exc))
        finally:
            await adapter.close()

    async def run_parallel(self, specs: list[SubagentSpec],
                           ui: AgentUI | None = None) -> list[SubagentResult]:
        """Run read-only subagents concurrently. Mutating specs are refused
        (run them sequentially through run_subagent instead)."""
        for spec in specs:
            role = subagent_role(spec.role)
            if role is None:
                raise DaemonError(f"Unknown subagent role '{spec.role}'.")
            read_only = spec.read_only
            if read_only is None:
                read_only = spec.role in ("architect", "reviewer", "security",
                                          "researcher")
            if not read_only:
                raise DaemonError(
                    f"Role '{spec.role}' can modify files and must not run "
                    f"in parallel with other agents.")
        audit("subagents-parallel", roles=",".join(s.role for s in specs))
        return await asyncio.gather(
            *(self.run_subagent(spec, ui) for spec in specs))
