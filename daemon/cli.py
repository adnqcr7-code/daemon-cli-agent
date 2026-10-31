"""Daemon command-line interface.

Commands:
  daemon                     interactive engineering agent
  daemon init                initialize a project
  daemon auth ...            credentials (add/list/remove/test/switch/models)
  daemon models ...          model inspection and selection
  daemon run / plan / review / research
  daemon tools / agents / mcp / sessions / memory / config
  daemon doctor              environment diagnostics
  daemon version
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import click

from . import __version__
from .config import Config, daemon_home
from .errors import AuthError, DaemonError
from .log import get_logger, setup_logging
from .security import redact

_logger = get_logger("cli")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class ClickIO:
    """PromptIO implementation over click for auth flows."""

    def info(self, message: str) -> None:
        click.secho(message, fg="cyan")

    def warn(self, message: str) -> None:
        click.secho(message, fg="yellow")

    def success(self, message: str) -> None:
        click.secho(message, fg="green")

    def prompt_secret(self, prompt: str) -> str:
        return click.prompt(prompt, hide_input=True).strip()

    def ask_text(self, prompt: str, default: str | None = None) -> str:
        return click.prompt(prompt, default=default, show_default=True)

    def ask_choice(self, question: str, options: list[tuple[str, str]]) -> str:
        click.echo(question)
        for index, (value, label) in enumerate(options, start=1):
            click.echo(f"  {index}. {label}")
        while True:
            raw = click.prompt("Choose", type=str).strip()
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return options[int(raw) - 1][0]
            for value, _ in options:
                if raw.lower() == value:
                    return value
            click.echo(f"Enter 1-{len(options)} or a name.")

    def confirm(self, question: str, default: bool = False) -> bool:
        return click.confirm(question, default=default)


def _workspace_option(func):
    return click.option("--workspace", "-w", type=click.Path(
        exists=True, file_okay=False, path_type=Path),
        default=None, help="Project directory (default: cwd)")(func)


def _fail(exc: DaemonError) -> None:
    click.secho(f"error: {exc}", fg="red", err=True)
    if getattr(exc, "hint", None):
        click.secho(f"hint: {exc.hint}", fg="yellow", err=True)
    sys.exit(exc.exit_code)


def _make_renderer(config: Config, *, plain: bool = False, theme: str | None = None):
    from .ui.render import Renderer
    from .ui.themes import resolve_theme

    return Renderer(resolve_theme(theme or config.theme_name()), plain=plain)


def _echo(message: str, *, color: str | None = None) -> None:
    click.secho(redact(message), fg=color)


# ---------------------------------------------------------------------------
# Root: interactive agent
# ---------------------------------------------------------------------------


@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="daemon")
@click.option("--workspace", "-w", type=click.Path(exists=True, file_okay=False,
                                                   path_type=Path), default=None)
@click.option("--model", "-m", default=None, help="Model id for this session")
@click.option("--theme", default=None, help="Terminal theme")
@click.option("--session", "-s", default=None, help="Resume a session id")
@click.option("--plain", is_flag=True, help="Plain (uncoloured) output")
@click.pass_context
def main(ctx: click.Context, workspace: Path | None, model: str | None,
         theme: str | None, session: str | None, plain: bool) -> None:
    """Daemon - the open-source AI engineering agent for your terminal."""
    if ctx.invoked_subcommand is not None:
        return
    _launch_repl(workspace=workspace, model=model, theme=theme,
                 session_id=session, plain=plain)


def _launch_repl(*, workspace: Path | None, model: str | None, theme: str | None,
                 session_id: str | None, plain: bool,
                 research: bool = False) -> None:
    from .context import build_context
    from .ui.repl import Repl, run_research_repl

    try:
        config = Config.load(workspace or Path.cwd())
        renderer = _make_renderer(config, plain=plain, theme=theme)
        ctx = build_context(workspace=workspace, config=config,
                            renderer=renderer, interactive=True,
                            session_id=session_id, model_id=model,
                            role="research" if research else "build")
    except DaemonError as exc:
        _fail(exc)
    try:
        if research:
            run_research_repl(ctx, renderer)
        else:
            Repl(ctx, renderer).start()
    except DaemonError as exc:
        _fail(exc)
    finally:
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@main.command()
@click.argument("path", type=click.Path(file_okay=False), default=".")
def init(path: str) -> None:
    """Initialize .daemon/ project state (config, memory, skills)."""
    workspace = Path(path).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    daemon_dir = workspace / ".daemon"
    for sub in ("memory", "skills", "checkpoints"):
        (daemon_dir / sub).mkdir(parents=True, exist_ok=True)
    config_path = daemon_dir / "config.toml"
    if not config_path.exists():
        config_path.write_text(
            "# Daemon project configuration.\n"
            "# Docs: https://github.com/daemon-agent/daemon docs/configuration.md\n",
            encoding="utf-8")
    gitignore = workspace / ".gitignore"
    entry = ".daemon/checkpoints/\n"
    if gitignore.exists():
        content = gitignore.read_text("utf-8")
        if ".daemon/checkpoints" not in content:
            gitignore.write_text(content.rstrip("\n") + "\n" + entry, "utf-8")
    else:
        gitignore.write_text(entry, encoding="utf-8")
    from .project import ProjectIndex

    index = ProjectIndex(workspace)
    index.build()
    _echo(f"Initialized {daemon_dir}", color="green")
    _echo(index.summary())


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


@main.group()
def auth() -> None:
    """Manage provider credentials."""


@auth.command("add")
@click.option("--provider", default=None, help="Skip detection: provider id")
@click.option("--base-url", default=None, help="Custom OpenAI-compatible endpoint")
@click.option("--label", default=None, help="Account label (multiple accounts)")
@click.option("--env", "env_var", default=None,
              help="Import credential from an environment variable")
@click.option("--api-key", default=None,
              help="Non-interactive key (scripting; prefer the prompt)")
@click.option("--no-verify", is_flag=True, help="Store without verification")
def auth_add(provider: str | None, base_url: str | None, label: str | None,
             env_var: str | None, api_key: str | None, no_verify: bool) -> None:
    """Add a credential; Daemon detects the provider automatically."""
    from .auth.manager import AuthManager

    try:
        manager = AuthManager()
        manager.add(ClickIO(), key=api_key, provider=provider,
                    base_url=base_url, label=label, env_var=env_var,
                    verify=not no_verify)
        manager.close()
    except DaemonError as exc:
        _fail(exc)


@auth.command("list")
def auth_list() -> None:
    """List stored profiles (never the secrets)."""
    from .auth.manager import AuthManager

    try:
        manager = AuthManager()
    except DaemonError as exc:
        _fail(exc)
    profiles = manager.store.profiles()
    active = manager.store.active()
    if not profiles:
        _echo("No profiles. Run: daemon auth add", color="yellow")
        return
    for profile in profiles:
        marker = "*" if active and profile.id == active.id else " "
        endpoint = f"  endpoint {profile.endpoint}" if profile.endpoint else ""
        source = f"  env ${profile.env_var}" if profile.env_var else "  vault"
        default = f"  default model {profile.default_model}" \
            if profile.default_model else ""
        _echo(f"{marker} {profile.id}{source}{endpoint}{default}")
    _echo(f"credential storage: {manager.vault.security_level}")
    if manager.vault.warning:
        _echo(f"warning: {manager.vault.warning}", color="yellow")
    manager.close()


@auth.command("remove")
@click.argument("profile_id")
def auth_remove(profile_id: str) -> None:
    from .auth.manager import AuthManager

    try:
        manager = AuthManager()
        removed = manager.store.remove(profile_id)
        manager.close()
    except DaemonError as exc:
        _fail(exc)
    _echo(f"Removed {removed.id}" if removed else f"No profile {profile_id}",
          color="green" if removed else "yellow")


@auth.command("test")
@click.argument("profile_id", required=False)
def auth_test(profile_id: str | None) -> None:
    """Verify credentials by listing models."""
    from .auth.manager import AuthManager

    try:
        manager = AuthManager()
        ok = manager.test(ClickIO(), profile_id)
        manager.close()
    except DaemonError as exc:
        _fail(exc)
    sys.exit(0 if ok else 1)


@auth.command("switch")
@click.argument("profile_id")
def auth_switch(profile_id: str) -> None:
    from .auth.manager import AuthManager

    try:
        manager = AuthManager()
        profile = manager.switch(profile_id)
        manager.close()
    except DaemonError as exc:
        _fail(exc)
    _echo(f"Active profile: {profile.id}", color="green")


@auth.command("models")
@click.argument("profile_id", required=False)
def auth_models(profile_id: str | None) -> None:
    """List models discovered for a profile."""
    from .auth.manager import AuthManager

    try:
        manager = AuthManager()
        profile, models = manager.models(ClickIO(), profile_id)
        manager.close()
    except DaemonError as exc:
        _fail(exc)
    renderer = _make_renderer(Config.load())
    renderer.models_table(models)


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------


@main.group()
def models() -> None:
    """Inspect and select models."""


@models.command("list")
@click.option("--refresh", is_flag=True, help="Bypass the cache")
@_workspace_option
def models_list(refresh: bool, workspace: Path | None) -> None:
    config = Config.load(workspace)
    from .context import build_context

    try:
        ctx = build_context(workspace=workspace, config=config,
                            interactive=False, require_auth=True,
                            select_model=False, refresh_models=refresh)
    except DaemonError as exc:
        _fail(exc)
    renderer = _make_renderer(config)
    try:
        renderer.models_table(ctx.models)
    finally:
        ctx.close()


@models.command("recommend")
@click.option("--task", default="coding",
              type=click.Choice(["coding", "planning", "research",
                                 "review", "cheap"]))
@_workspace_option
def models_recommend(task: str, workspace: Path | None) -> None:
    """Rank available models for a task, with reasons."""
    config = Config.load(workspace)
    from .context import build_context
    from .models import recommend

    try:
        ctx = build_context(workspace=workspace, config=config,
                            interactive=False)
    except DaemonError as exc:
        _fail(exc)
    try:
        for rec in recommend(ctx.models, task)[:5]:
            click.secho(f"{rec.headline}", fg="cyan")
            for reason in rec.reasons:
                click.echo(f"    - {reason}")
            click.echo(f"    score {rec.score}")
    finally:
        ctx.close()


@models.command("set")
@click.argument("role", type=click.Choice(["default", "coding", "planning",
                                           "research", "review"]))
@click.argument("model_id")
def models_set(role: str, model_id: str) -> None:
    """Configure the model used for a role."""
    config = Config.load()
    dotted = "default_model" if role == "default" else f"models.{role}"
    config.set_user(dotted, model_id)
    _echo(f"{dotted} = {model_id}", color="green")


# ---------------------------------------------------------------------------
# run / plan / review / research
# ---------------------------------------------------------------------------


@main.command()
@click.argument("task")
@click.option("--model", "-m", default=None)
@click.option("--yes", "assume_yes", is_flag=True,
              help="Assume-yes for permission prompts (CI/scripting)")
@click.option("--json", "as_json", is_flag=True, help="Structured output")
@_workspace_option
def run(task: str, model: str | None, assume_yes: bool, as_json: bool,
        workspace: Path | None) -> None:
    """Run one task non-interactively, then exit."""
    from .context import attach_mcp_tools, build_context

    config = Config.load(workspace)
    try:
        ctx = build_context(workspace=workspace, config=config,
                            interactive=False, assume_yes=assume_yes,
                            model_id=model)
        from daemon.agent.runtime import AgentUI

        if as_json:
            ui = AgentUI()  # silent: --json emits machine-readable output only
        else:
            ui = renderer = _make_renderer(config,
                                           plain=not sys.stdout.isatty())
        ctx.renderer = ui
        runtime = ctx.build_runtime(ui=ui)
        asyncio.run(attach_mcp_tools(ctx, renderer=ui))
        result = asyncio.run(runtime.run(task))
    except DaemonError as exc:
        if as_json:
            click.echo(json.dumps({"ok": False, "error": str(exc)}))
            sys.exit(exc.exit_code)
        _fail(exc)
    if as_json:
        click.echo(json.dumps({
            "ok": not result.interrupted,
            "text": result.text,
            "steps": result.steps,
            "tool_calls": result.tool_calls,
            "usage": ctx.session.usage.to_json(),
            "cost_usd": result.cost_usd,
            "files_changed": result.files_changed,
        }, indent=2))
    else:
        # the streamed text was already printed live by the renderer;
        # only files/verification summary is added here
        if result.files_changed:
            _echo("changed: " + ", ".join(result.files_changed), color="green")
        if result.interrupted:
            sys.exit(130)
    ctx.close()


@main.command()
@click.argument("goal")
@click.option("--execute", is_flag=True, help="Execute the plan after creating it")
@click.option("--yes", "assume_yes", is_flag=True)
@_workspace_option
def plan(goal: str, execute: bool, assume_yes: bool, workspace: Path | None) -> None:
    """Create an implementation plan (and optionally execute it)."""
    from .context import build_context

    config = Config.load(workspace)
    try:
        ctx = build_context(workspace=workspace, config=config,
                            interactive=False, assume_yes=assume_yes)
    except DaemonError as exc:
        _fail(exc)
    renderer = _make_renderer(config, plain=not sys.stdout.isatty())
    from .agent.planner import TaskTracker, parse_plan
    from .agent.prompts import planner_prompt
    from .providers.types import ChatRequest, Message

    tracker = TaskTracker()

    async def _go() -> None:
        context = ctx.index.summary() if ctx.index else ""
        request = ChatRequest(
            model=ctx.model.model_id,
            messages=[Message.user(planner_prompt(goal, context))],
            stream=True, max_tokens=2000)
        text = ""
        async for event in ctx.adapter.chat(request):
            if event.kind == "text" and event.text:
                text += event.text
                await renderer.text_delta(event.text)
        await renderer.text_done()
        parsed = parse_plan(goal, text)
        tracker.load(parsed)
        renderer.task_list(tracker)
        if execute:
            runtime = ctx.build_runtime(ui=renderer,
                                        extra_context=tracker.context_block)
            while True:
                task_obj = tracker.plan.next_task()
                if task_obj is None:
                    break
                tracker.start(task_obj)
                renderer.rule(f"task {task_obj.id}")
                result = await runtime.run(
                    f"Execute task {task_obj.id}: {task_obj.title}\n"
                    f"{task_obj.description}\n"
                    + (f"Verification: {task_obj.verification}"
                       if task_obj.verification else ""))
                tracker.finish(task_obj, success=True,
                               result=result.text or "")
            renderer.task_list(tracker)

    try:
        asyncio.run(_go())
    except DaemonError as exc:
        _fail(exc)
    finally:
        ctx.close()


@main.command()
@click.option("--ref", default=None, help="Git ref to review (default: session diff)")
@_workspace_option
def review(ref: str | None, workspace: Path | None) -> None:
    """Run the reviewer subagent over the current changes."""
    from .context import build_context

    config = Config.load(workspace)
    try:
        ctx = build_context(workspace=workspace, config=config,
                            interactive=False)
    except DaemonError as exc:
        _fail(exc)
    renderer = _make_renderer(config, plain=not sys.stdout.isatty())

    async def _go() -> None:
        from .agent.orchestration import Orchestrator, SubagentSpec
        from .tools.git import git as git_run

        diff = ""
        if ref is not None:
            code, diff = await git_run("diff", "--no-color", ref,
                                       cwd=ctx.workspace)
        else:
            if ctx.checkpoints is not None:
                diff = "\n".join(
                    body for _, body in ctx.checkpoints.diff("latest"))
            if not diff.strip() and (ctx.workspace / ".git").exists():
                code, diff = await git_run("diff", "--no-color", "HEAD",
                                           cwd=ctx.workspace)
        if not diff.strip():
            await renderer.notice("no changes to review", level="warn")
            return
        orchestrator = Orchestrator(
            adapter_factory=lambda model: ctx.adapter_for_provider(
                model.provider), models={}, default_model=ctx.model,
            workspace=ctx.workspace, config=ctx.config)
        result = await orchestrator.run_subagent(SubagentSpec(
            role="reviewer",
            brief="Review these changes; read files as needed:\n\n"
                  + diff[:60_000]), ui=renderer)
        if not result.ok:
            await renderer.notice(f"review failed: {result.error}",
                                  level="error")

    try:
        asyncio.run(_go())
    except DaemonError as exc:
        _fail(exc)
    finally:
        ctx.close()


@main.command()
@click.argument("query", required=False)
@click.option("--theme", default=None)
@_workspace_option
def research(query: str | None, theme: str | None, workspace: Path | None) -> None:
    """Start the restricted Research agent (REPL or one-shot)."""
    if query:
        _run_research_one_shot(query, workspace)
    else:
        _launch_repl(workspace=workspace, model=None, theme=theme,
                     session_id=None, plain=False, research=True)


def _run_research_one_shot(query: str, workspace: Path | None) -> None:
    from .context import build_context
    from .research import ResearchAgent

    config = Config.load(workspace)
    try:
        ctx = build_context(workspace=workspace, config=config,
                            interactive=False, role="research")
    except DaemonError as exc:
        _fail(exc)
    renderer = _make_renderer(config, plain=not sys.stdout.isatty())
    agent = ResearchAgent(adapter=ctx.adapter, model=ctx.model,
                          workspace=ctx.workspace, config=ctx.config,
                          memory=ctx.memory, ui=renderer)
    try:
        result = asyncio.run(agent.ask(query))
    except DaemonError as exc:
        _fail(exc)
    finally:
        ctx.close()
    _echo(result.text or "(no findings)")


# ---------------------------------------------------------------------------
# tools / agents / mcp / sessions / memory / config
# ---------------------------------------------------------------------------


@main.command("tools")
def tools_cmd() -> None:
    """List all built-in tools."""
    from .tools import default_registry

    renderer = _make_renderer(Config.load())
    renderer.tools_table(default_registry().tools())
    _echo("MCP server tools appear here after 'daemon mcp enable'.")


@main.command("agents")
def agents_cmd() -> None:
    """List specialized subagents."""
    from .agent.prompts import subagent_role, subagent_roles

    for role in subagent_roles():
        spec = subagent_role(role) or {}
        click.secho(f"{role}", fg="cyan")
        click.echo(f"    {spec.get('prompt', '')[:160]}...")


@main.group()
def mcp() -> None:
    """Manage MCP servers."""


@mcp.command("list")
def mcp_list() -> None:
    from .mcp import McpManager

    manager = McpManager(Config.load())
    servers = manager.servers()
    if not servers:
        _echo("No MCP servers configured.", color="yellow")
        _echo("Add one: daemon mcp add <id> --command 'uvx some-server'")
        return
    for server in servers:
        state = ("trusted" if server.trusted else
                 "unreviewed" if server.enabled else "disabled")
        target = server.command or server.url or ""
        _echo(f"{server.id:<20} {server.transport:<7} {state:<11} {target}")


@mcp.command("add")
@click.argument("server_id")
@click.option("--command", default=None, help="stdio: executable to launch")
@click.option("--arg", "args", multiple=True, help="stdio: argument (repeatable)")
@click.option("--url", default=None, help="http: streamable HTTP endpoint")
@click.option("--env", "envs", multiple=True, help="K=V env entry (repeatable)")
@click.option("--header", "headers", multiple=True, help="K=V header (http)")
@click.option("--project", is_flag=True, help="Store in project config")
@click.option("--trust", is_flag=True, help="Mark trusted immediately")
def mcp_add(server_id: str, command: str | None, args: tuple[str, ...],
            url: str | None, envs: tuple[str, ...], headers: tuple[str, ...],
            project: bool, trust: bool) -> None:
    """Register an MCP server (stdio command or HTTP URL)."""
    from .mcp import McpManager, McpServerConfig

    if not command and not url:
        raise click.UsageError("Provide --command (stdio) or --url (http).")
    transport = "http" if url else "stdio"
    server = McpServerConfig(
        id=server_id, transport=transport, command=command,
        args=list(args), url=url,
        env=_parse_kv(envs), headers=_parse_kv(headers),
        trusted=trust, scope="project" if project else "user")
    config = Config.load()
    McpManager(config).add(server)
    _echo(f"Added MCP server '{server_id}' ({transport}).", color="green")
    if not trust:
        _echo("It is UNTRUSTED: review it, then 'daemon mcp enable "
              f"{server_id}' to expose its tools.", color="yellow")


@mcp.command("remove")
@click.argument("server_id")
def mcp_remove(server_id: str) -> None:
    from .mcp import McpManager

    manager = McpManager(Config.load())
    if manager.remove(server_id):
        _echo(f"Removed {server_id}", color="green")
    else:
        _echo(f"No server {server_id}", color="yellow")


@mcp.command("test")
@click.argument("server_id")
def mcp_test(server_id: str) -> None:
    from .mcp import McpManager

    async def _go() -> dict:
        return await McpManager(Config.load()).test(server_id)

    try:
        report = asyncio.run(_go())
    except DaemonError as exc:
        _fail(exc)
    _echo(f"server    {report['server']} v{report['version']}", color="green")
    _echo(f"protocol  {report['protocol']}")
    _echo(f"tools     {', '.join(report['tools']) or '(none)'}")
    _echo(f"resources {report['resources']} · prompts "
          f"{', '.join(report['prompts']) or '(none)'}")


@mcp.command("enable")
@click.argument("server_id")
@click.option("--yes", is_flag=True, help="Skip the review confirmation")
def mcp_enable(server_id: str, yes: bool) -> None:
    """Approve a reviewed server and expose its tools."""
    from .mcp import McpManager

    if not yes:
        yes = click.confirm(
            f"I reviewed MCP server '{server_id}' and trust its tools",
            default=False)
    if not yes:
        _echo("Not enabled.", color="yellow")
        return
    manager = McpManager(Config.load())
    if manager.set_trusted(server_id, True) and \
            manager.set_enabled(server_id, True):
        _echo(f"Enabled {server_id}; its tools register at startup.",
              color="green")
    else:
        _echo(f"No server {server_id}", color="yellow")


@mcp.command("disable")
@click.argument("server_id")
def mcp_disable(server_id: str) -> None:
    from .mcp import McpManager

    manager = McpManager(Config.load())
    manager.set_trusted(server_id, False)
    manager.set_enabled(server_id, False)
    _echo(f"Disabled {server_id}")


@main.group()
def sessions() -> None:
    """Manage saved sessions."""


@sessions.command("list")
def sessions_list() -> None:
    from .agent.session import Session

    metas = Session.list_sessions()
    if not metas:
        _echo("No saved sessions.")
        return
    for meta in metas:
        _echo(f"{meta.id:<24} {meta.role:<9} {meta.title[:60]}")


@sessions.command("show")
@click.argument("session_id")
def sessions_show(session_id: str) -> None:
    from .agent.session import Session

    try:
        session = Session.load(session_id)
    except DaemonError as exc:
        _fail(exc)
    _echo(f"session {session.meta.id} · {session.meta.role} · "
          f"{session.usage.input_tokens}/{session.usage.output_tokens} tokens")
    for message in session.messages[-20:]:
        prefix = {"user": "> ", "assistant": "< ", "system": "# "}.get(
            message.role, "  ")
        text = message.text or ", ".join(
            call.name for call in message.tool_calls) or \
            f"{len(message.tool_results)} tool result(s)"
        _echo(prefix + text.splitlines()[0][:100])


@sessions.command("delete")
@click.argument("session_id")
def sessions_delete(session_id: str) -> None:
    from .agent.session import Session

    try:
        Session.delete(session_id)
    except DaemonError as exc:
        _fail(exc)
    _echo(f"Deleted {session_id}", color="green")


@sessions.command("resume")
@click.argument("session_id")
def sessions_resume(session_id: str) -> None:
    """Continue a session in the interactive agent."""
    _launch_repl(workspace=None, model=None, theme=None,
                 session_id=session_id, plain=False)


@main.group()
def memory() -> None:
    """Manage persistent memory."""


@memory.command("list")
@click.option("--workspace", "-w", type=click.Path(file_okay=False,
                                                   path_type=Path), default=None)
def memory_list(workspace: Path | None) -> None:
    from .memory import MemoryStore

    store = MemoryStore(workspace)
    entries = store.entries()
    if not entries:
        _echo("Memory is empty.")
        return
    for entry in entries[:40]:
        _echo(f"[{entry.kind:<8}] {entry.scope:<7} {entry.content[:90]}")


@memory.command("search")
@click.argument("query")
@click.option("--workspace", "-w", type=click.Path(file_okay=False,
                                                   path_type=Path), default=None)
def memory_search(query: str, workspace: Path | None) -> None:
    from .memory import MemoryStore

    store = MemoryStore(workspace)
    for entry in store.search(query):
        _echo(f"[{entry.kind:<8}] {entry.content[:120]}")


@memory.command("clear")
@click.option("--scope", type=click.Choice(["user", "project"]), default="project")
@click.option("--workspace", "-w", type=click.Path(file_okay=False,
                                                   path_type=Path), default=None)
def memory_clear(scope: str, workspace: Path | None) -> None:
    from .memory import MemoryStore

    count = MemoryStore(workspace).clear(scope)
    _echo(f"Cleared {count} {scope} memory entries", color="green")


@main.group()
def config() -> None:
    """Manage configuration."""


@config.command("path")
def config_path() -> None:
    _echo(str(daemon_home() / "config.toml"))


@config.command("get")
@click.argument("dotted")
def config_get(dotted: str) -> None:
    value = Config.load().get(dotted)
    if value is None:
        _echo("(not set)")
    else:
        _echo(json.dumps(value, indent=2, default=str))


@config.command("set")
@click.argument("dotted")
@click.argument("value")
@click.option("--project", "as_project", is_flag=True)
@click.option("--workspace", "-w", type=click.Path(file_okay=False,
                                                   path_type=Path), default=None)
def config_set(dotted: str, value: str, as_project: bool,
               workspace: Path | None) -> None:
    if dotted == "active_profile" and not as_project:
        from .auth.manager import AuthManager

        try:
            manager = AuthManager(Config.load(workspace))
            profile = manager.switch(value)
            manager.close()
        except DaemonError as exc:
            _fail(exc)
        _echo(f"Active profile: {profile.id}", color="green")
        return

    cfg = Config.load(workspace)
    parsed: object
    if value.lower() in ("true", "false"):
        parsed = value.lower() == "true"
    else:
        try:
            parsed = int(value)
        except ValueError:
            try:
                parsed = float(value)
            except ValueError:
                parsed = value
    if as_project:
        cfg.set_project(workspace or Path.cwd(), dotted, parsed)
    else:
        cfg.set_user(dotted, parsed)
    _echo(f"{dotted} = {parsed!r}", color="green")


@config.command("list")
def config_list() -> None:
    cfg = Config.load()
    _echo(json.dumps(cfg.raw, indent=2, default=str))


# ---------------------------------------------------------------------------
# doctor / version
# ---------------------------------------------------------------------------


@main.command()
@click.option("--net", is_flag=True, help="Probe provider endpoints")
@click.option("--fix", is_flag=True, help="Attempt safe repairs")
@_workspace_option
def doctor(net: bool, fix: bool, workspace: Path | None) -> None:
    """Diagnose the installation and environment."""
    from .doctor import run_doctor

    setup_logging(daemon_home())
    config = Config.load(workspace)
    report = run_doctor(config, workspace or Path.cwd(), network=net, fix=fix)
    colours = {"ok": "green", "warn": "yellow", "fail": "red", "info": "cyan"}
    icons = {"ok": "[ok]", "warn": "[!!]", "fail": "[XX]", "info": "[--]"}
    for status, check, detail in report.rows:
        click.secho(f"{icons[status]:<6} {check:<22}", fg=colours[status], nl=False)
        click.echo(f" {detail}" if detail else "")
    summary = (f"{len(report.rows)} checks: "
               f"{report.rows and sum(1 for s, _, _ in report.rows if s == 'ok')} ok, "
               f"{report.warnings} warnings, {report.failures} failures")
    click.echo(summary)
    sys.exit(1 if report.failures else 0)


@main.command()
@click.option("--verbose", is_flag=True)
def version(verbose: bool) -> None:
    """Show version information."""
    from .providers.registry import AdapterRegistry

    _echo(f"daemon {__version__} (Python {sys.version.split()[0]})")
    if verbose:
        from .providers.catalog import PROVIDERS

        _echo(f"protocols: {', '.join(AdapterRegistry.protocols())}")
        _echo(f"providers catalogued: {len(PROVIDERS)}")
        _echo(f"home: {daemon_home()}")


def _parse_kv(pairs: tuple[str, ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    for pair in pairs:
        if "=" in pair:
            key, value = pair.split("=", 1)
            result[key] = value
    return result


def cli_main() -> None:
    """Entry point with global error handling."""
    try:
        main(prog_name="daemon")
    except DaemonError as exc:
        _fail(exc)
    except KeyboardInterrupt:
        click.secho("\ninterrupted", fg="yellow", err=True)
        sys.exit(130)


if __name__ == "__main__":
    cli_main()
