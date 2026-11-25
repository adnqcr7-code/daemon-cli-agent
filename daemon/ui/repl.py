"""The interactive Daemon REPL.

Loop design: one persistent asyncio event loop owns the provider adapter and
MCP subprocesses across turns; input is read synchronously (prompt_toolkit
when available, plain input() otherwise); Ctrl+C cancels the running agent
turn cleanly (checkpoints and session state survive), Ctrl+D exits.
"""

from __future__ import annotations

import asyncio

from ..agent.orchestration import Orchestrator, SubagentSpec
from ..agent.planner import TaskTracker
from ..agent.prompts import subagent_roles
from ..agent.session import Session, SessionMeta
from ..context import DaemonContext, attach_mcp_tools, skill_loader
from ..errors import DaemonError
from ..log import describe_exception, get_logger
from ..research import ResearchAgent
from ..tools.git import git as git_run
from .render import Renderer
from .themes import ARCHIVE, get_theme
from .themes import themes as all_themes

_logger = get_logger("repl")

SLASH_COMMANDS = {
    "/help": "show this help",
    "/exit": "leave the session",
    "/model": "list or switch models: /model [id]",
    "/models": "table of available models",
    "/agents": "list specialized subagents",
    "/tools": "list registered tools",
    "/permissions": "show effective permission rules",
    "/plan": "create an implementation plan: /plan <goal>",
    "/edit-plan": "edit the active plan in $EDITOR and record the edit: /edit-plan [id]",
    "/build": "execute plan tasks (or run a message)",
    "/design": "build from a BITON design reference: /design <slug or url>",
    "/review": "spawn the reviewer subagent on the current diff",
    "/debug": "spawn the debugger subagent: /debug <symptom>",
    "/research": "spawn the restricted research subagent: /research <query>",
    "/diff": "show this session's changes",
    "/undo": "restore the latest checkpoint",
    "/compact": "force context compaction",
    "/tasks": "show the active task plan",
    "/memory": "search memory: /memory [query]",
    "/remember": "store a decision: /remember <text>",
    "/stats": "usage and cost for this session",
    "/tokens": "show context window token usage",
    "/theme": "switch theme: /theme [name]",
    "/session": "session info",
    "/save": "save the session now",
    "/resume": "resume a session: /resume <id>",
    "/clear": "start a fresh session",
    "/mcp": "MCP server status",
    "/skills": "list skills; attach with /skills use <name>",
}


class Repl:
    def __init__(self, ctx: DaemonContext, renderer: Renderer,
                 initial_prompt: str | None = None, *,
                 demo_mode: bool = False) -> None:
        self.ctx = ctx
        self.renderer = renderer
        self.tracker = TaskTracker()
        self.skills = skill_loader(ctx)
        self._attached_skills: list = []
        self.initial_prompt = initial_prompt
        # demo_mode: no credentials were configured, so the context runs
        # the scripted demo provider. The REPL offers the labeled demo
        # up front, once, as a per-run choice (never persisted).
        self.demo_mode = demo_mode
        # The stored plan /plan created (its id under .daemon/plans/):
        # /build executes exactly this record, re-hashed, after the
        # broker gate. None until /plan runs.
        self._active_plan_id: str | None = None
        self._loop = asyncio.new_event_loop()
        self.runtime = ctx.build_runtime(ui=renderer,
                                         extra_context=self._context_chain)
        self._prompt = self._build_prompt()

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._startup())
            if self.demo_mode:
                self._offer_demo()
            if self.initial_prompt:
                prompt, self.initial_prompt = self.initial_prompt, None
                self._turn(prompt)
            while True:
                try:
                    line = self._read_line()
                except KeyboardInterrupt:
                    self.renderer.line("muted", "Exiting. (Use Ctrl+D or /exit next time.)")
                    break
                except EOFError:
                    break
                if line is None:
                    break
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.lower() in ("exit", "quit"):
                    break
                if stripped.startswith("/"):
                    if self._dispatch(stripped):
                        break
                else:
                    self._turn(stripped)
        finally:
            self._loop.run_until_complete(self._shutdown())
            try:
                asyncio.set_event_loop(None)
            except RuntimeError:  # pragma: no cover
                pass
            self._loop.close()

    async def _startup(self) -> None:
        self.renderer.banner(self.ctx.model, mode="build")
        await attach_mcp_tools(self.ctx, renderer=self.renderer)
        if self.ctx.session is not None:
            self.renderer.line(
                "muted", f"session {self.ctx.session.meta.id} · "
                         f"{self.ctx.workspace}")
        skills = self.skills.skills()
        if skills:
            self.renderer.line(
                "muted", f"{len(skills)} skill(s) available - /skills to list")
        self.renderer.line("muted", "type /help for commands, ctrl-d to exit")

    async def _shutdown(self) -> None:
        try:
            if self.ctx.session is not None:
                self.ctx.session.save()
        except DaemonError as exc:
            self.renderer.line("error", f"session save failed: {exc}")
        for closer in (self._close_mcp, self._close_adapter, self._close_auth):
            try:
                await closer()
            except Exception as exc:  # noqa: BLE001
                _logger.debug("shutdown step failed: %s", exc)

    async def _close_mcp(self) -> None:
        for client in self.ctx._mcp_clients:
            await client.close()

    async def _close_adapter(self) -> None:
        if self.ctx.adapter is not None:
            await self.ctx.adapter.close()

    async def _close_auth(self) -> None:
        if self.ctx.auth is not None:
            self.ctx.auth.close()

    # ----------------------------------------------------------------- input

    def _build_prompt(self):
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
            from prompt_toolkit.completion import WordCompleter
            from prompt_toolkit.history import FileHistory

            return PromptSession(
                history=FileHistory(str(self.ctx.home / "history")),
                completer=WordCompleter(
                    sorted(SLASH_COMMANDS), ignore_case=True,
                    sentence=True),
                auto_suggest=AutoSuggestFromHistory(),
                enable_history_search=True,
            )
        except Exception:  # noqa: BLE001 - graceful degradation
            return None

    def _read_line(self) -> str | None:
        if self._prompt is not None:
            return self._prompt.prompt("› ")
        return input("› ")

    def _offer_demo(self) -> None:
        """The front-door demo offer (demo mode only, once per run).

        Labeled as scripted, per-run, never persisted as a profile:
        accepting runs the scripted demo task through the real
        runtime; declining leaves a working REPL on the demo provider
        (which answers text but makes no model claims).
        """
        self.renderer.line(
            "warn", "demo mode — no credentials configured")
        self.renderer.line(
            "muted", "This is the scripted demo provider, not a real "
                     "model: no API key, no network, nothing leaves "
                     "this machine.")
        self.renderer.line(
            "muted", "Try the demo? It adds a docstring to a.py in "
                     "this workspace through the real permission and "
                     "checkpoint path. (Per-run choice — never saved.)")
        if ask_demo_offer():
            self._turn("add a docstring to a.py")

    # ------------------------------------------------------------- agent turn

    def _turn(self, text: str) -> None:
        task = self._loop.create_task(self._run_turn(text))
        try:
            self._loop.run_until_complete(asyncio.shield(task))
        except KeyboardInterrupt:
            task.cancel()
            self._drain(task)
            self.renderer.line("warn", "^C interrupted (checkpoint state kept)")
        except DaemonError as exc:
            self._render_error(exc)
        except Exception as exc:  # noqa: BLE001
            self.renderer.line("error", describe_exception(exc))

    async def _run_turn(self, text: str) -> None:
        try:
            result = await self.runtime.run(text)
            if result.interrupted:
                self.renderer.line("warn", "run interrupted")
            elif result.files_changed:
                self.renderer.line(
                    "muted", "changed: " + ", ".join(result.files_changed[:8]) +
                             (f" (+{len(result.files_changed) - 8})" if
                              len(result.files_changed) > 8 else ""))
            # Show token counter after each turn
            self._show_token_counter()
        except DaemonError:
            raise

    def _drain(self, *tasks) -> None:
        async def _gather():
            await asyncio.gather(*tasks, return_exceptions=True)

        try:
            self._loop.run_until_complete(_gather())
        except KeyboardInterrupt:
            pass  # second ctrl-c during cleanup: drop everything

    def _render_error(self, exc: DaemonError) -> None:
        self.renderer.line("error", str(exc))
        if getattr(exc, "hint", None):
            self.renderer.line("muted", f"hint: {exc.hint}")

    def _show_token_counter(self) -> None:
        """Display current context token usage."""
        tokens = self.runtime.context_tokens()
        window = self.ctx.model.capabilities.context_window
        from daemon.agent.context import context_budget
        budget = context_budget(window) if window else None
        self.renderer.token_counter(tokens, budget, window)

    # ----------------------------------------------------------------- slash

    def _handler_for(self, command: str):
        """Resolve a slash command to its handler method.

        Command names are hyphenated (``/edit-plan``) while the
        methods are underscored (``_cmd_edit_plan``); the hyphen
        must be normalized here, in the one place that does the
        lookup, or a hyphenated command is silently unreachable
        (review round 3, R1-3d).
        """
        name = command.lstrip("/").replace("-", "_")
        return getattr(self, f"_cmd_{name}", None)

    def _dispatch(self, line: str) -> bool:
        parts = line.split(None, 1)
        command = parts[0].lower()
        rest = (parts[1] if len(parts) > 1 else "").strip()
        handler = self._handler_for(command)
        if handler is None:
            self.renderer.line("error", f"unknown command {command} (/help)")
            return False
        try:
            return bool(handler(rest))
        except DaemonError as exc:
            self._render_error(exc)
            return False
        except Exception as exc:  # noqa: BLE001
            self.renderer.line("error", describe_exception(exc))
            return False

    def _cmd_help(self, rest: str) -> bool:
        for name, description in sorted(SLASH_COMMANDS.items()):
            self.renderer.line("accent", f"{name:<14}")
            self.renderer.line("muted", f"               {description}")
        return False

    def _cmd_exit(self, rest: str) -> bool:
        return True

    def _cmd_model(self, rest: str) -> bool:
        if not rest:
            self.renderer.line(
                "muted", f"current: {self.ctx.model.model_id} "
                         f"({self.ctx.model.provider})")
            self.renderer.models_table(self.ctx.models)
            self.renderer.line("muted", "switch with: /model <id>")
            return False
        return self._switch_model(rest)

    def _cmd_models(self, rest: str) -> bool:
        self.renderer.models_table(self.ctx.models)
        return False

    def _switch_model(self, model_id: str) -> bool:
        match = next((m for m in self.ctx.models if m.model_id == model_id),
                     None)
        if match is None:
            self.renderer.line("error", f"no such model: {model_id}")
            return False
        if match.provider != self.ctx.model.provider:
            adapter = self.ctx.adapter_for_provider(match.provider)
            if self.ctx.adapter is not None:
                self._loop.run_until_complete(self.ctx.adapter.close())
            self.ctx.adapter = adapter
        self.ctx.model = match
        self.ctx.session.meta.model = match.model_id
        self.ctx.session.meta.provider = match.provider
        self.runtime = self.ctx.build_runtime(
            ui=self.renderer, extra_context=self._context_chain)
        self.renderer.line("success", f"model: {match.model_id}")
        return False

    def _cmd_agents(self, rest: str) -> bool:
        for role in subagent_roles():
            self.renderer.line("accent", role)
        self.renderer.line("muted", "use /review, /debug, /research or ask "
                                    "the agent to delegate")
        return False

    def _cmd_tools(self, rest: str) -> bool:
        self.renderer.tools_table(self.ctx.registry.tools())
        return False

    def _cmd_design(self, rest: str) -> bool:
        if not rest:
            self.renderer.line(
                "muted", "usage: /design <slug|biton design url> "
                         "(find one with design_search)")
            return False
        from ..designs import BitonClient

        design = self._loop.run_until_complete(BitonClient().get(rest))
        self.renderer.line("accent", f"reference: {design.title}")
        self.renderer.line("muted", design.summary_line())
        self._turn(design.build_task())
        return False

    def _cmd_permissions(self, rest: str) -> bool:
        for rule in self.ctx.permissions.effective_rules():
            effect = {"allow": "tool_ok", "ask": "warn",
                      "deny": "error"}[rule.effect.value]
            self.renderer.line(
                effect,
                f"{rule.effect.value.upper():<6} {rule.tool:<16} "
                + (f"cmd~{rule.command} " if rule.command else "")
                + (f"path~{rule.path} " if rule.path else "")
                + (f"[{rule.origin}]" if rule.origin != "default" else ""))
        self.renderer.line("muted", "add rules via config permissions.rules")
        return False

    # -------------------------------------------------------------- planning

    def _cmd_plan(self, rest: str) -> bool:
        if not rest:
            self.renderer.line("muted", "usage: /plan <goal>")
            return False
        self._loop.run_until_complete(self._do_plan(rest))
        return False

    async def _do_plan(self, goal: str) -> None:
        """Create a plan, persist it under .daemon/plans/, and STOP.

        Nothing executes here: /build is the separate, gated step.
        """
        from ..plans import draft_plan

        record = await draft_plan(self.ctx, goal, created_by="repl")
        self.tracker.load(record.plan)
        self._active_plan_id = record.id
        self.renderer.rule("plan")
        self.renderer.task_list(self.tracker)
        self.renderer.line(
            "muted",
            f"plan {record.id} saved — edit with /edit-plan, execute "
            f"with /build (approval required, tasks run in order)")
        if self.ctx.session is not None:
            self.ctx.session.add_user(f"/plan {goal}")
            self.ctx.session.add_assistant(
                f"Plan {record.id} created with {len(record.tasks)} tasks:\n" +
                "\n".join(f"- {task.id}. {task.title}"
                          for task in record.tasks))

    def _cmd_edit_plan(self, rest: str) -> bool:
        """The recorded edit path in the REPL (review round 2, R1-3).

        Opens the active plan (or the id given) in $EDITOR and
        records the result as a plan.edited kernel event, so the
        edited content can execute. A file rewritten any other way
        keeps the old kernel record and /build refuses it. The
        in-memory tracker is rebuilt from the edited record, so
        /tasks shows the edited tasks.
        """
        from ..plans import PlanError, PlanStore, edit_plan_record

        plan_id = rest.strip() or self._active_plan_id
        if plan_id is None:
            self.renderer.line(
                "muted", "no active plan; /plan <goal> first, or "
                         "/edit-plan <id>")
            return False
        run_id = (self.ctx.session.meta.id
                  if self.ctx.session is not None else None)
        try:
            record, changed = edit_plan_record(
                PlanStore(self.ctx.workspace), plan_id, run_id=run_id)
        except PlanError as exc:
            self.renderer.line("error", str(exc))
            return False
        self.tracker.load(record.plan)
        self._active_plan_id = record.id
        if changed:
            self.renderer.line(
                "muted",
                f"plan {record.id} edited — new version recorded in "
                f"the event kernel (hash {record.hash[:12]}); "
                f"execute with /build")
        else:
            self.renderer.line(
                "muted",
                f"plan {record.id} unchanged — it already matches the "
                f"recorded version")
        return False

    def _cmd_build(self, rest: str) -> bool:
        if self.tracker.plan is not None:
            self._loop.run_until_complete(self._execute_plan(rest))
            return False
        if rest:
            self._turn(rest)
            return False
        self.renderer.line("muted", "no active plan; /plan <goal> first, or "
                                    "just type your request")
        return False

    async def _execute_plan(self, note: str) -> None:
        """Execute the stored plan behind the broker gate (§2.3).

        The stored record is re-loaded (schema + content hash) so a
        plan rewritten outside the recorded edit path between /plan
        and /build cannot run — only an edit recorded with
        /edit-plan moves the kernel anchor the check binds to; the
        broker asks for approval (the REPL's interactive approver);
        per-task verification stops the loop on the first failure.
        """
        assert self.tracker.plan is not None
        from ..agent.planner import TaskTracker
        from ..events import emit_plan_approved
        from ..plans import PlanError, PlanStore, run_plan_tasks, verify_approved

        plan_id = self._active_plan_id
        if plan_id is None:
            self.renderer.line(
                "error", "no stored plan to execute — run /plan <goal> first")
            return
        try:
            record = PlanStore(self.ctx.workspace).load(plan_id)
        except PlanError as exc:
            self.renderer.line("error", str(exc))
            return
        approved = self.ctx.permissions.approve_plan(
            plan_id=record.id, goal=record.goal,
            task_count=len(record.tasks),
            files=[file for task in record.tasks for file in task.files],
            plan_hash=record.hash, tasks=list(record.tasks))
        if not approved:
            self.renderer.line(
                "warn", "plan execution not approved — nothing ran")
            return
        # Kernel-anchored approval (review round 1, R1-3a): the
        # hash is recorded outside the workspace, and the file is
        # re-read and checked against it before anything executes.
        seq = None
        if self.ctx.session is not None:
            seq = emit_plan_approved(
                self.ctx.session.meta.id, record.plan, via="/build",
                plan_id=record.id, plan_hash=record.hash)
        if seq is None:
            self.renderer.line(
                "error", "plan approval could not be recorded in the "
                         "event kernel — refusing to execute")
            return
        try:
            record = PlanStore(self.ctx.workspace).load(plan_id)
            verify_approved(record)
        except PlanError as exc:
            self.renderer.line("error", str(exc))
            return
        # Fresh tracker from the VERIFIED record (review round 1,
        # R1-3b): the tracker loaded at /plan time is stale — an
        # edit between /plan and /build was validated above and
        # then silently ignored when the old tracker ran.
        self.tracker = TaskTracker()
        self.tracker.load(record.plan)
        done = await run_plan_tasks(self.runtime, self.tracker,
                                    renderer=self.renderer, note=note)
        self.renderer.line(
            "muted",
            "plan execution finished - /tasks for state, /diff for changes"
            if done else
            "plan stopped early — a task failed; /tasks for state, "
            "/diff for changes")

    def _cmd_tasks(self, rest: str) -> bool:
        self.renderer.task_list(self.tracker)
        return False

    # -------------------------------------------------------------- subagents

    def _orchestrator(self) -> Orchestrator:
        return Orchestrator(
            adapter_factory=lambda model: self.ctx.adapter_for_provider(
                model.provider),
            models={},
            default_model=self.ctx.model,
            workspace=self.ctx.workspace,
            config=self.ctx.config,
            memory=self.ctx.memory,
            index=self.ctx.index,
            checkpoints=self.ctx.checkpoints,
        )

    def _cmd_review(self, rest: str) -> bool:
        diff = self._collect_diff()
        if not diff.strip():
            self.renderer.line("muted", "no changes to review yet")
            return False
        brief = ("Review the following changes against the current project "
                 "state. Read any files you need.\n\n" + diff[:60_000])
        self._run_subagent("reviewer", brief)
        return False

    def _cmd_debug(self, rest: str) -> bool:
        if not rest:
            self.renderer.line("muted", "usage: /debug <symptom or error>")
            return False
        self._run_subagent(
            "debugger",
            f"Debug this problem in the current workspace:\n\n{rest}")
        return False

    def _cmd_research(self, rest: str) -> bool:
        if not rest:
            self.renderer.line("muted", "usage: /research <query>")
            return False
        self._run_subagent("researcher", rest)
        return False

    def _run_subagent(self, role: str, brief: str) -> None:
        orchestrator = self._orchestrator()

        async def _go() -> None:
            previous_theme = self.renderer.theme
            if role == "researcher":
                self.renderer.theme = ARCHIVE
            try:
                self.renderer.rule(f"{role} subagent")
                result = await orchestrator.run_subagent(
                    SubagentSpec(role=role, brief=brief), ui=self.renderer)
                if result.ok:
                    self.renderer.line("success", f"{role} finished")
                else:
                    self.renderer.line("error",
                                       f"{role} failed: {result.error}")
            finally:
                self.renderer.theme = previous_theme

        task = self._loop.create_task(_go())
        try:
            self._loop.run_until_complete(asyncio.shield(task))
        except KeyboardInterrupt:
            task.cancel()
            self._drain(task)
            self.renderer.line("warn", "^C subagent interrupted")
        except DaemonError as exc:
            self._render_error(exc)

    # ----------------------------------------------------------------- diffs

    def _collect_diff(self) -> str:
        diffs = []
        if self.ctx.checkpoints is not None:
            for path, diff in self.ctx.checkpoints.diff("latest"):
                diffs.append(diff)
        if not diffs and (self.ctx.workspace / ".git").exists():
            code, out = self._loop.run_until_complete(
                git_run("diff", "--no-color", "HEAD",
                        cwd=self.ctx.workspace))
            if code == 0 and out.strip():
                diffs.append(out)
        return "\n".join(diffs)

    def _cmd_diff(self, rest: str) -> bool:
        diff = self._collect_diff()
        if not diff.strip():
            self.renderer.line("muted", "no changes detected")
            return False
        self.renderer.rule("session diff")
        self.renderer.render_diff(diff[:40_000])
        return False

    def _cmd_undo(self, rest: str) -> bool:
        latest = self.ctx.checkpoints.latest() if self.ctx.checkpoints else None
        if latest is None:
            self.renderer.line("muted", "no checkpoints to undo")
            return False
        created = len(latest.created_files)
        detail = f"restore {len(latest.files)} file(s)"
        if created:
            detail += f" and delete {created} file(s) created after it"
        answer = input(f"{detail} from checkpoint "
                       f"{latest.id}? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            self.renderer.line("muted", "aborted")
            return False
        report = self._loop.run_until_complete(
            self.ctx.checkpoints.restore_report(latest.id))
        message = f"restored {report.restored} file(s)"
        if report.deleted:
            message += f", deleted {report.deleted} created file(s)"
        self.renderer.line("success", message)
        return False

    def _cmd_compact(self, rest: str) -> bool:
        ok = self._loop.run_until_complete(self.runtime.compact_now())
        self.renderer.line("success" if ok else "warn",
                           "context compacted" if ok else "compaction failed")
        return False

    # ---------------------------------------------------------------- memory

    def _cmd_memory(self, rest: str) -> bool:
        entries = (self.ctx.memory.search(rest) if rest
                   else self.ctx.memory.entries()[:20])
        if not entries:
            self.renderer.line("muted", "memory is empty"
                               + (f" for '{rest}'" if rest else ""))
            return False
        for entry in entries[:20]:
            self.renderer.line("accent", f"[{entry.kind}] {entry.id}")
            self.renderer.line("text", f"        {entry.content[:120]}")
        return False

    def _cmd_remember(self, rest: str) -> bool:
        if not rest:
            self.renderer.line("muted", "usage: /remember <decision>")
            return False
        entry = self.ctx.memory.add(rest, kind="decision", source="user")
        self.renderer.line("success", f"stored {entry.id}")
        return False

    def _cmd_stats(self, rest: str) -> bool:
        session = self.ctx.session
        cost = self.runtime.cost_estimate()
        self.renderer.line(
            "muted",
            f"tokens {session.usage.input_tokens:,} in / "
            f"{session.usage.output_tokens:,} out · "
            + (f"cost ~${cost:.4f}" if cost is not None else "cost unknown "
              "(no pricing data for this model)"))
        return False

    def _cmd_tokens(self, rest: str) -> bool:
        self._show_token_counter()
        return False

    # ------------------------------------------------------------------ misc

    def _cmd_theme(self, rest: str) -> bool:
        if not rest:
            for theme in all_themes():
                marker = "*" if theme.name == self.renderer.theme.name else " "
                self.renderer.line(
                    "accent", f"{marker} {theme.name:<10}")
                self.renderer.line("muted", f"    {theme.description}")
            return False
        self.renderer.theme = get_theme(rest)
        self.ctx.config.set_user("ui.theme", self.renderer.theme.name)
        self.renderer.line("success", f"theme: {self.renderer.theme.name}")
        return False

    def _cmd_session(self, rest: str) -> bool:
        self.renderer.session_line(self.ctx.session)
        return False

    def _cmd_save(self, rest: str) -> bool:
        self.ctx.session.save()
        self.renderer.line("success",
                           f"saved {self.ctx.session.meta.id}")
        return False

    def _cmd_resume(self, rest: str) -> bool:
        if not rest:
            for meta in Session.list_sessions(10):
                self.renderer.line(
                    "accent", f"{meta.id}")
                self.renderer.line(
                    "muted", f"    {meta.title[:70]} · "
                             f"{len(_safe_load(meta.id))} messages")
            return False
        session = Session.load(rest, lock=True)
        self.ctx.session = session
        self.runtime = self.ctx.build_runtime(
            ui=self.renderer, session=session,
            extra_context=self._context_chain)
        self.renderer.line("success", f"resumed {session.meta.id}")
        self.renderer.session_line(session)
        return False

    def _cmd_clear(self, rest: str) -> bool:
        self.ctx.session = Session(meta=SessionMeta(
            workspace=str(self.ctx.workspace),
            provider=self.ctx.model.provider,
            model=self.ctx.model.model_id))
        self.runtime = self.ctx.build_runtime(
            ui=self.renderer, extra_context=self._context_chain)
        self.renderer.line("success", "fresh session started")
        return False

    def _cmd_mcp(self, rest: str) -> bool:
        servers = self.ctx.mcp.servers() if self.ctx.mcp else []
        if not servers:
            self.renderer.line("muted", "no MCP servers configured "
                                        "(daemon mcp add)")
            return False
        for server in servers:
            state = ("trusted+enabled" if server.trusted and server.enabled
                     else "enabled, unreviewed" if server.enabled
                     else "disabled")
            self.renderer.line("accent", server.id)
            self.renderer.line(
                "muted",
                f"    {server.transport} · {state} · "
                + (server.command or server.url or ""))
        mcp_tools = [name for name in self.ctx.registry.names()
                     if name.startswith("mcp_")]
        self.renderer.line(
            "muted", f"{len(mcp_tools)} MCP tool(s) currently registered")
        return False

    def _cmd_skills(self, rest: str) -> bool:
        parts = rest.split(None, 1)
        if parts and parts[0] == "use" and len(parts) > 1:
            skill = self.skills.get(parts[1].strip())
            if skill is None:
                self.renderer.line("error", f"no skill '{parts[1]}'")
                return False
            self._attached_skills.append(skill)
            self.renderer.line("success", f"attached skill {skill.name}")
            return False
        skills = self.skills.skills()
        if not skills:
            self.renderer.line("muted", "no skills found "
                                        "(~/.daemon/skills/*.md)")
            return False
        for skill in skills:
            self.renderer.line("accent", skill.name)
            self.renderer.line("muted", f"    {skill.description[:90]}")
        self.renderer.line("muted", "attach: /skills use <name>")
        return False

    # ------------------------------------------------------------ shared ctx

    def _context_chain(self) -> str:
        parts: list[str] = []
        if self.tracker.plan is not None:
            parts.append(self.tracker.context_block())
        for skill in self._attached_skills:
            parts.append(f"Applied {skill.header()}:\n{skill.body}")
        return "\n\n".join(parts)


def ask_demo_offer(input_fn=input) -> bool:
    """One yes/no for the front-door demo offer; empty input = yes.

    Factored out of :meth:`Repl._offer_demo` so the choice itself is
    unit-testable without driving the REPL loop.
    """
    try:
        answer = input_fn("Try the demo? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("", "y", "yes")


def _safe_load(session_id: str):
    try:
        return Session.load(session_id).messages
    except DaemonError:
        return []


def run_research_repl(ctx: DaemonContext, renderer: Renderer) -> None:
    """A dedicated REPL for the Research agent (Archive theme)."""
    renderer.theme = ARCHIVE
    research = ResearchAgent(
        adapter=ctx.adapter, model=ctx.model, workspace=ctx.workspace,
        config=ctx.config, memory=ctx.memory, ui=renderer)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_research_startup(ctx, renderer, research))

        async def _ask(question: str):
            return await research.ask(question)

        while True:
            try:
                line = input("archive › ")
            except EOFError:
                break
            except KeyboardInterrupt:
                print()
                continue
            if not line.strip():
                continue
            if line.strip().lower() in ("/exit", "/quit", "exit", "quit"):
                break
            if line.strip().lower() == "/help":
                renderer.line("muted", "type a research question; "
                                       "/exit to leave; the agent is "
                                       "read-only and cannot modify files")
                continue
            task = loop.create_task(_ask(line.strip()))
            try:
                loop.run_until_complete(asyncio.shield(task))
            except KeyboardInterrupt:
                task.cancel()
                loop.run_until_complete(
                    asyncio.gather(task, return_exceptions=True))
                renderer.line("warn", "^C interrupted")
            except DaemonError as exc:
                renderer.line("error", str(exc))
                if exc.hint:
                    renderer.line("muted", f"hint: {exc.hint}")
    finally:
        loop.run_until_complete(ctx.adapter.close())
        loop.close()


async def _research_startup(ctx: DaemonContext, renderer: Renderer,
                            research: ResearchAgent) -> None:
    renderer.banner(ctx.model, mode="research")
    renderer.line(
        "muted",
        f"tools: {', '.join(research.tool_names())}")
    renderer.line("muted", "read-only agent · citations expected · "
                           "/exit to leave")
