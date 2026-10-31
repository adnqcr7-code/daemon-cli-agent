"""Wiring: assemble a fully configured Daemon runtime from config + auth.

Used by the REPL, `daemon run/plan/review/research` and the doctor command
so every entry point shares identical construction semantics.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from .agent.runtime import AgentRuntime
from .agent.session import Session, SessionMeta
from .auth.manager import AuthManager
from .checkpoints import CheckpointManager
from .config import Config, daemon_home
from .errors import AuthError, DaemonError, ModelError
from .log import get_logger, setup_logging
from .memory import MemoryStore
from .models import ModelRegistry, RemoteModel, pick_role_model
from .mcp import McpManager, McpRemoteTool
from .permissions import PermissionEngine
from .project import ProjectIndex
from .providers.catalog import provider_by_id
from .providers.registry import AdapterRegistry, custom_descriptor
from .security import redactor
from .tools import ToolRegistry, default_registry

_logger = get_logger("context")


@dataclass
class DaemonContext:
    config: Config
    workspace: Path
    home: Path
    renderer: object | None = None
    auth: AuthManager | None = None
    models: list[RemoteModel] = field(default_factory=list)
    adapter = None
    model: RemoteModel | None = None
    registry: ToolRegistry = field(default_factory=ToolRegistry)
    permissions: PermissionEngine | None = None
    session: Session | None = None
    memory: MemoryStore | None = None
    checkpoints: CheckpointManager | None = None
    index: ProjectIndex | None = None
    mcp: McpManager | None = None
    interactive: bool = True
    _mcp_clients: list = field(default_factory=list)

    # ------------------------------------------------------------ factories

    def build_runtime(self, *, ui=None, session=None, role="build",
                      extra_context=None) -> AgentRuntime:
        return AgentRuntime(
            adapter=self.adapter,
            model=self.model,
            registry=self.registry,
            permissions=self.permissions,
            session=session or self.session,
            ui=ui,
            config=self.config,
            workspace=self.workspace,
            memory=self.memory,
            checkpoints=self.checkpoints,
            index=self.index,
            role=role,
            extra_context=extra_context,
        )

    def adapter_for_provider(self, provider_id: str):
        """Adapter for a model's provider: reuse the active adapter when the
        provider matches, else try another stored profile for it."""
        if self.model is not None and provider_id == self.model.provider:
            return self.adapter
        if self.auth is None:
            raise AuthError("No credential store available for multi-provider models.")
        profile = self.auth.store.find(provider_id) or \
            self.auth.store.find(provider_id, None)
        if profile is None:
            raise ModelError(
                f"No stored profile for provider '{provider_id}' (needed by a "
                f"role model). Add one with 'daemon auth add'.")
        key = self.auth.store.resolve_key(profile)
        descriptor = provider_by_id(provider_id) or custom_descriptor(
            profile.provider, profile.endpoint or "")
        return AdapterRegistry().create(
            descriptor, api_key=key, endpoint=profile.endpoint,
            config=self.config)

    def close(self) -> None:
        for client in self._mcp_clients:
            try:
                asyncio.run(client.close())
            except RuntimeError:
                pass
        if self.auth is not None:
            self.auth.close()


def build_context(
    *,
    workspace: Path | None = None,
    config: Config | None = None,
    renderer=None,
    interactive: bool = True,
    assume_yes: bool = False,
    session_id: str | None = None,
    model_id: str | None = None,
    role: str = "build",
    require_auth: bool = True,
) -> DaemonContext:
    """Assemble providers, models, tools, permissions and session state."""
    workspace = (Path(workspace) if workspace else Path.cwd()).resolve()
    home = daemon_home()
    home.mkdir(parents=True, exist_ok=True)
    setup_logging(home)
    config = config or Config.load(workspace)

    ctx = DaemonContext(config=config, workspace=workspace, home=home,
                        renderer=renderer, interactive=interactive)
    ctx.index = ProjectIndex(workspace, config=config)
    try:
        ctx.index.build()
    except OSError as exc:
        _logger.warning("project index failed: %s", exc)
    ctx.memory = MemoryStore(workspace, home=home)
    ctx.checkpoints = CheckpointManager(workspace)
    ctx.mcp = McpManager(config, workspace)

    # ---- provider + model --------------------------------------------------
    auth = None
    try:
        auth = AuthManager(config)
    except DaemonError as exc:
        if require_auth:
            raise
        _logger.warning("auth unavailable: %s", exc)
    ctx.auth = auth

    if auth is not None:
        profile = auth.store.active()
        if profile is None and require_auth:
            raise AuthError(
                "No provider credentials configured. Run 'daemon auth add' "
                "and paste an API key - Daemon will detect the provider.",
                hint="Local models: 'daemon auth add --provider ollama' "
                     "needs no key.")
        if profile is not None:
            key = auth.store.resolve_key(profile)
            if key:
                redactor().register(key)
            descriptor = provider_by_id(profile.provider) or custom_descriptor(
                "Custom endpoint", profile.endpoint or "")
            ctx.adapter = AdapterRegistry().create(
                descriptor, api_key=key, endpoint=profile.endpoint,
                config=config)
            model_registry = ModelRegistry(home)
            try:
                ctx.models = asyncio.run(model_registry.load_async(
                    ctx.adapter, profile.id))
            finally:
                asyncio.run(ctx.adapter.close())  # clients bind to loops
            _logger.info("loaded %d models for profile %s",
                         len(ctx.models), profile.id)
            preferred = model_id or profile.default_model
            ctx.model = _select_model(ctx.models, preferred, role, config)
        elif require_auth:
            raise AuthError("The active profile has no usable credential.")

    # ---- tools + permissions -------------------------------------------------
    ctx.registry = default_registry()
    rules = config.get("permissions.rules") or []
    ctx.permissions = PermissionEngine(
        workspace, rules=rules,
        approver=(renderer.approve if renderer is not None and interactive
                  else None),
        assume_yes=assume_yes or bool(config.get("permissions.assume_yes")
                                      and not interactive),
    )

    # ---- session ---------------------------------------------------------------
    if session_id:
        ctx.session = Session.load(session_id)
    else:
        ctx.session = Session(meta=SessionMeta(
            workspace=str(workspace),
            provider=(ctx.model.provider if ctx.model else ""),
            model=(ctx.model.model_id if ctx.model else ""),
            role=role))
    return ctx


def _select_model(models: list[RemoteModel], preferred: str | None,
                  role: str, config: Config) -> RemoteModel:
    if preferred:
        for model in models:
            if model.model_id == preferred:
                return model
        raise ModelError(
            f"Model '{preferred}' is not available on the active profile. "
            f"Run 'daemon models list' to see what is.")
    return pick_role_model(config, role, models)


async def attach_mcp_tools(ctx: DaemonContext, *, renderer=None) -> int:
    """Connect enabled+trusted MCP servers and register their tools.

    Untrusted servers are NOT connected: their tools stay invisible until
    the user reviews and approves them (`daemon mcp enable`).
    """
    registered = 0
    manager = ctx.mcp
    if manager is None:
        return 0
    for server in manager.servers():
        if not server.enabled:
            continue
        if not server.trusted:
            if renderer is not None:
                await renderer.notice(
                    f"MCP server '{server.id}' is enabled but not reviewed; "
                    f"run 'daemon mcp enable {server.id}' after review.",
                    level="warn")
            continue
        client = server.client()
        try:
            await client.initialize()
            tools = await client.list_tools()
        except Exception as exc:  # noqa: BLE001
            if renderer is not None:
                await renderer.notice(
                    f"MCP server '{server.id}' failed: {exc}", level="warn")
            continue
        for spec in tools:
            try:
                tool = McpRemoteTool(server.id, client, spec, trusted=True)
                ctx.registry.register(tool, replace=True)
                registered += 1
            except Exception as exc:  # noqa: BLE001
                _logger.warning("mcp tool registration failed: %s", exc)
        ctx._mcp_clients.append(client)
        if renderer is not None:
            await renderer.notice(
                f"MCP '{server.id}': {len(tools)} tool(s) registered",
                level="success")
    return registered


def skill_loader(ctx: DaemonContext):
    from .skills import SkillLibrary

    return SkillLibrary(user_dir=ctx.home / "skills",
                        project_dir=ctx.workspace / ".daemon" / "skills")
