"""Daemon Research: a separate, lightweight, restricted research agent.

Distinct from the Build agent by construction, not just by prompt:

* its tool registry physically contains no write/shell/git tools,
* its permission policy allows web tools (that is its purpose) while the
  Build agent asks before any network egress,
* its system prompt is smaller and citation-driven,
* the UI presents it with the Archive theme.

If a future refactor accidentally registers a mutating tool into the
research registry, the permission engine still denies it (defence in depth
via research_policy()).
"""

from __future__ import annotations

from pathlib import Path

from .agent.runtime import AgentRuntime, AgentUI, RunResult
from .agent.session import Session, SessionMeta
from .config import Config
from .permissions import Effect, PermissionEngine, Rule
from .providers.types import RemoteModel
from .tools import research_registry


def research_policy_rules() -> list[Rule]:
    """Role-scoped rules layered on the engine defaults."""
    return [
        Rule(tool="web_search", effect=Effect.ALLOW, origin="user",
             description="Research agent may search the web"),
        Rule(tool="web_fetch", effect=Effect.ALLOW, origin="user",
             description="Research agent may fetch pages"),
        Rule(tool="memory_note", effect=Effect.ALLOW, origin="user",
             description="Research agent stores notes"),
        Rule(tool="run_command", effect=Effect.DENY, origin="user",
             description="Research agent never runs shell commands"),
        Rule(tool="write_file", effect=Effect.DENY, origin="user",
             description="Research agent never writes files"),
        Rule(tool="edit_file", effect=Effect.DENY, origin="user",
             description="Research agent never edits files"),
        Rule(tool="apply_patch", effect=Effect.DENY, origin="user",
             description="Research agent never applies patches"),
        Rule(tool="delete_path", effect=Effect.DENY, origin="user",
             description="Research agent never deletes anything"),
        Rule(tool="git_commit", effect=Effect.DENY, origin="user",
             description="Research agent never touches git history"),
    ]


class ResearchAgent:
    """Factory for restricted research runtimes."""

    def __init__(
        self,
        *,
        adapter,
        model: RemoteModel,
        workspace: Path,
        config: Config | None = None,
        memory=None,
        ui: AgentUI | None = None,
        session: Session | None = None,
    ) -> None:
        self.adapter = adapter
        self.model = model
        self.workspace = Path(workspace).resolve()
        self.config = config
        self.memory = memory
        self.ui = ui
        self.session = session or Session(meta=SessionMeta(
            workspace=str(self.workspace), provider=model.provider,
            model=model.model_id, role="research"))
        self.registry = research_registry()
        self.permissions = PermissionEngine(
            self.workspace,
            rules=research_policy_rules() + (
                (config.get("permissions.rules") or []) if config else []),
        )

    def runtime(self, *, extra_prompt: str = "") -> AgentRuntime:
        from .agent.prompts import research_system_prompt

        return AgentRuntime(
            adapter=self.adapter,
            model=self.model,
            registry=self.registry,
            permissions=self.permissions,
            session=self.session,
            ui=self.ui,
            config=self.config,
            workspace=self.workspace,
            memory=self.memory,
            role="research",
            system_prompt=research_system_prompt(
                workspace=str(self.workspace), extra=extra_prompt),
        )

    async def ask(self, question: str) -> RunResult:
        """One-shot research turn."""
        runtime = self.runtime()
        return await runtime.run(question)

    def tool_names(self) -> list[str]:
        return self.registry.names()
