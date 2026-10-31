"""Rich-based terminal renderer implementing the AgentUI callbacks.

Also provides the building blocks the CLI and REPL use: banners, model
lines, diffs, approval prompts, task lists and usage/cost lines. Every
piece of output passes through the global secret redactor.
"""

from __future__ import annotations

import os
import sys
from typing import IO

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..agent.runtime import AgentUI
from ..models import estimate_cost_usd
from ..permissions import Decision
from ..providers.types import RemoteModel, Usage
from ..security import redact
from ..tools.base import ToolOutput
from .themes import Theme, supports_unicode

_GLYPHS = {
    "unicode": {"bullet": "•", "arrow": "→", "done": "✔", "fail": "✖",
                "spark": "◆", "tool": "▸", "prompt": "❯", "branch": "└"},
    "ascii": {"bullet": "-", "arrow": "->", "done": "[ok]", "fail": "[!]",
              "spark": "*", "tool": ">", "prompt": ">", "branch": "|-"},
}


class Renderer(AgentUI):
    def __init__(self, theme: Theme, *, stream: IO | None = None,
                 plain: bool = False, show_tool_output: bool = True) -> None:
        self.theme = theme
        self.plain = plain
        self.show_tool_output = show_tool_output
        self.console = Console(
            file=stream or sys.stdout,
            no_color=plain or bool(os.environ.get("NO_COLOR")),
            highlight=False,
            soft_wrap=False,
        )
        self.glyphs = _GLYPHS["unicode" if supports_unicode() else "ascii"]
        self._cumulative_cost = 0.0

    # ------------------------------------------------------------- helpers

    def _g(self, key: str) -> str:
        return self.glyphs[key]

    def styled(self, role: str, text: str) -> Text:
        return Text(redact(text), style=self.theme.style(role))

    def line(self, role: str, text: str, *, end: str = "\n") -> None:
        self.console.print(self.styled(role, text), end=end,
                           markup=False, highlight=False)

    def print(self, text: str) -> None:
        self.console.print(redact(text), markup=False, highlight=False)

    def rule(self, title: str = "") -> None:
        if self.plain:
            self.print(f"--- {title} " + "-" * max(0, 40 - len(title)) if title
                       else "-" * 44)
            return
        self.console.rule(self.styled("accent", title) if title else "",
                          style=self.theme.style("border"))

    # --------------------------------------------------------------- banner

    def banner(self, model: RemoteModel | None = None,
               mode: str = "build") -> None:
        if self.plain:
            self.print(f"daemon ({mode} mode)"
                       + (f" - {model.model_id}" if model else ""))
            return
        title = Text()
        title.append("DAEMON", style=f"bold {self.theme.style('accent')}")
        subtitle = "the engineering agent"
        body = [f"{subtitle}"]
        if model:
            body.append(f"model   {model.model_id}")
            body.append(f"provider {model.provider}")
        if mode:
            body.append(f"mode    {mode}")
        self.console.print(Panel(
            "\n".join(body), title=title, border_style=self.theme.style("border"),
            subtitle=Text(f"daemon v{_version()}", style=self.theme.style("muted")),
            subtitle_align="right"))

    # ------------------------------------------------------------ agent UI

    async def text_delta(self, delta: str, *, thinking: bool = False) -> None:
        style = self.theme.style("thinking") if thinking else None
        self.console.print(redact(delta), end="", markup=False,
                           highlight=False, style=style)

    async def text_done(self) -> None:
        self.console.print()

    async def tool_start(self, name: str, summary: str) -> None:
        glyph = self._g("tool")
        self.line("tool", f"{glyph} {name}" + (f"  {summary}" if summary else ""))

    async def tool_end(self, name: str, output: ToolOutput) -> None:
        if output.is_error:
            self.line("tool_err",
                      f"{self._g('fail')} {name} failed: "
                      f"{output.content.splitlines()[0] if output.content else ''}")
        else:
            self.line("tool_ok", f"{self._g('done')} {name} ok")

    async def tool_line(self, tool: str, line: str) -> None:
        if self.show_tool_output and line.strip():
            self.line("dim", f"  {self._g('branch')} {line}")

    async def usage(self, usage: Usage, cost_usd: float | None,
                    cumulative_cost: float | None) -> None:
        parts = [f"{usage.input_tokens:,} in / {usage.output_tokens:,} out"]
        if cost_usd is not None:
            parts.append(f"${cost_usd:.4f}")
        if cumulative_cost:
            parts.append(f"session ${cumulative_cost:.2f}")
        self.line("usage", f"{self._g('spark')} " + " · ".join(parts))

    async def notice(self, message: str, level: str = "info") -> None:
        role = {"info": "muted", "warn": "warn", "error": "error",
                "success": "success"}.get(level, "muted")
        self.line(role, f"{self._g('bullet')} {message}")

    async def approval(self, decision: Decision) -> tuple[bool, bool]:
        return self.approve(decision)

    # ------------------------------------------------------------- approval

    def approve(self, decision: Decision) -> tuple[bool, bool]:
        """Interactive approval prompt. Returns (approved, remember)."""
        if self.plain or not sys.stdin.isatty():
            return False, False
        header = Text("APPROVAL REQUIRED", style=f"bold {self.theme.style('warn')}")
        body_lines = [f"action      {decision.summary}"]
        if decision.files:
            body_lines.append("files       " + ", ".join(decision.files[:8]))
        if decision.reasons:
            body_lines.append("why         " + "; ".join(decision.reasons[:4]))
        body_lines.append(
            "reversible  " + ("yes - checkpointed" if decision.reversible else
                              ("NO" if decision.dangerous else "maybe")))
        if decision.dangerous:
            body_lines.append("DANGER      this action is destructive")
        self.console.print(Panel("\n".join(body_lines), title=header,
                                 border_style=self.theme.style("warn")))
        try:
            answer = self.console.input(
                f"[{self.theme.style('prompt')}]allow? "
                f"[y]es / [n]o / [a]lways this session > [/]").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False, False
        if answer in ("y", "yes"):
            return True, False
        if answer in ("a", "always"):
            return True, True
        return False, False

    # ---------------------------------------------------------------- diffs

    def render_diff(self, diff_text: str) -> None:
        for line in redact(diff_text).splitlines():
            if line.startswith("+"):
                self.line("add", line)
            elif line.startswith("-"):
                self.line("del", line)
            elif line.startswith("@@"):
                self.line("hunk", line)
            else:
                self.line("text", line)

    # --------------------------------------------------------------- tables

    def models_table(self, models: list[RemoteModel]) -> None:
        table = Table(title="Models", border_style=self.theme.style("border"),
                      title_style=self.theme.style("header"))
        for column in ("model", "provider", "context", "tools", "reasoning",
                       "$/Mtok in", "$/Mtok out"):
            table.add_column(column)
        for model in models:
            caps = model.capabilities
            table.add_row(
                escape(model.model_id), escape(model.provider),
                _fmt_ctx(caps.context_window),
                _fmt_bool(caps.tool_calling), _fmt_bool(caps.reasoning),
                _fmt_price(caps.input_price_per_mtok),
                _fmt_price(caps.output_price_per_mtok),
            )
        self.console.print(table)

    def tools_table(self, tools) -> None:
        table = Table(title="Tools", border_style=self.theme.style("border"),
                      title_style=self.theme.style("header"))
        table.add_column("name")
        table.add_column("category")
        table.add_column("access")
        table.add_column("description", overflow="fold")
        for tool in tools:
            access = ("read" if tool.read_only else
                      "network" if tool.network else "write/execute")
            table.add_row(escape(tool.name), escape(tool.category), access,
                          escape(tool.description[:100]))
        self.console.print(table)

    def task_list(self, tracker) -> None:
        self.print(tracker.render())

    def session_line(self, session) -> None:
        self.line("muted", f"session {session.meta.id} · "
                           f"{len(session.messages)} messages · "
                           f"{session.usage.input_tokens:,}/"
                           f"{session.usage.output_tokens:,} tokens")


def _fmt_ctx(value: int | None) -> str:
    if value is None:
        return "?"
    if value >= 1000:
        return f"{value // 1000}k"
    return str(value)


def _fmt_bool(value: bool | None) -> str:
    return {True: "yes", False: "no", None: "?"}[value]


def _fmt_price(value: float | None) -> str:
    return "~" + f"{value:g}" if value is not None else "?"


def _version() -> str:
    from .. import __version__

    return __version__
