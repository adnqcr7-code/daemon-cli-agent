"""The permission engine: allow / ask / deny for every tool invocation.

Layers, most specific first:

1. **Session overrides** - what the user picked on an approval prompt
   ("always allow this in this session").
2. **User / project rules** - ``[permissions] rules`` entries in config.
3. **Command classification** - static safety analysis for shell/git tools
   (credential access floors to deny, destructive commands floor to ask).
4. **Built-in defaults** - read tools allowed, workspace writes allowed with
   checkpoints, network and consequential actions ask, credentials denied.

A tool call never executes unless the resolved effect is ALLOW. "Ask" is
resolved by the UI approval prompt in interactive mode; in non-interactive
mode "ask" fails closed unless the user explicitly passed --assume-yes.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from .errors import PathEscapeError
from .log import audit, get_logger
from .security import classify_command, resolve_in_root

_logger = get_logger("permissions")


class Effect(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass
class Rule:
    tool: str = "*"                 # glob on the tool name
    effect: Effect = Effect.ASK
    path: str | None = None         # glob on a path argument (fs tools)
    command: str | None = None      # regex matched against shell command text
    description: str = ""
    origin: str = "default"         # default | user | project | session

    def matches_tool(self, tool_name: str) -> bool:
        return fnmatch.fnmatchcase(tool_name, self.tool)

    def matches(self, tool_name: str, args: dict[str, Any],
                command_text: str | None) -> bool:
        if not self.matches_tool(tool_name):
            return False
        if self.command is not None:
            if command_text is None or not re.search(self.command, command_text):
                return False
        if self.path is not None:
            target = args.get("path") or args.get("file") or ""
            if not target or not fnmatch.fnmatchcase(str(target), self.path):
                return False
        return True

    def specificity(self) -> int:
        score = 0
        if self.tool != "*":
            score += 2
        if self.command is not None:
            score += 3
        if self.path is not None:
            score += 2
        return score


@dataclass
class Decision:
    tool: str
    effect: Effect
    summary: str
    reasons: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    reversible: bool = True
    dangerous: bool = False
    rule: Rule | None = None
    key: str = ""                   # stable identity for "always allow"

    @property
    def approved(self) -> bool:
        return self.effect is Effect.ALLOW


DEFAULT_RULES: list[Rule] = [
    # Read-side tools are safe inside the workspace.
    Rule(tool="read_file", effect=Effect.ALLOW,
         description="read project files"),
    Rule(tool="list_dir", effect=Effect.ALLOW, description="list directories"),
    Rule(tool="glob", effect=Effect.ALLOW, description="glob files"),
    Rule(tool="search_files", effect=Effect.ALLOW, description="search file contents"),
    Rule(tool="file_info", effect=Effect.ALLOW, description="inspect file metadata"),
    Rule(tool="find_symbols", effect=Effect.ALLOW, description="symbol lookup"),
    Rule(tool="references", effect=Effect.ALLOW, description="find references"),
    Rule(tool="diagnostics", effect=Effect.ALLOW, description="run linters/read-only checks"),
    Rule(tool="project_map", effect=Effect.ALLOW, description="project index summary"),
    Rule(tool="git_status", effect=Effect.ALLOW, description="git status"),
    Rule(tool="git_diff", effect=Effect.ALLOW, description="git diff"),
    Rule(tool="git_log", effect=Effect.ALLOW, description="git log"),
    Rule(tool="git_branches", effect=Effect.ALLOW, description="list git branches"),
    Rule(tool="git_show", effect=Effect.ALLOW, description="show a git object"),
    Rule(tool="make_diff", effect=Effect.ALLOW, description="compute a diff"),
    Rule(tool="run_tests", effect=Effect.ALLOW,
         description="run the project's test suite in the workspace"),
    # Workspace writes are allowed but checkpointed.
    Rule(tool="write_file", effect=Effect.ALLOW,
         description="write files inside the workspace (checkpointed)"),
    Rule(tool="edit_file", effect=Effect.ALLOW,
         description="edit files inside the workspace (checkpointed)"),
    Rule(tool="apply_patch", effect=Effect.ALLOW,
         description="apply patches inside the workspace (checkpointed)"),
    Rule(tool="create_dir", effect=Effect.ALLOW, description="create directories"),
    Rule(tool="format_code", effect=Effect.ALLOW,
         description="run configured formatters (checkpointed)"),
    # Consequential actions ask.
    Rule(tool="delete_path", effect=Effect.ASK, description="delete files/dirs"),
    Rule(tool="move_path", effect=Effect.ASK, description="move/rename paths"),
    # run_command has no blanket rule: the command classifier decides
    # (safe commands allow, unfamiliar ask, dangerous deny).
    Rule(tool="git_commit", effect=Effect.ASK, description="create git commits"),
    Rule(tool="git_create_branch", effect=Effect.ALLOW,
         description="create (not delete) git branches"),
    Rule(tool="git_checkpoint", effect=Effect.ALLOW,
         description="snapshot files before modifications"),
    Rule(tool="git_rollback", effect=Effect.ASK,
         description="restore files from a checkpoint"),
    # Network egress asks by default; the Research role replaces this.
    Rule(tool="web_search", effect=Effect.ASK, description="search the web"),
    Rule(tool="web_fetch", effect=Effect.ASK, description="fetch a web page"),
    # MCP tools are untrusted until reviewed and approved.
    Rule(tool="mcp_*", effect=Effect.ASK,
         description="MCP server tool (untrusted until reviewed)"),
    # Memory of the agent itself is fine.
    Rule(tool="memory_note", effect=Effect.ALLOW, description="store research notes"),
]

# Tools that may read credentials: denied outright, only an explicit user
# rule can change that.
_CREDENTIAL_TOOLS = ("read_credentials",)


class PermissionEngine:
    def __init__(
        self,
        workspace_root: Path,
        *,
        rules: list[Rule] | None = None,
        approver: Callable[[Decision], tuple[bool, bool]] | None = None,
        assume_yes: bool = False,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.approver = approver
        self.assume_yes = assume_yes
        self.session_overrides: dict[str, Effect] = {}
        self.rules: list[Rule] = list(DEFAULT_RULES)
        for raw in rules or []:
            if isinstance(raw, Rule):
                self.rules.append(raw)
            else:
                self.rules.append(_rule_from_config(raw, origin="user"))

    # ------------------------------------------------------------------ API

    def check(self, tool_name: str, args: dict[str, Any], *,
              read_only: bool = False, mutating: bool = False,
              network: bool = False) -> Decision:
        """Evaluate policy for one tool call (no side effects)."""
        command_text = _command_text(tool_name, args)
        reasons: list[str] = []
        files = _extract_files(args)
        reversible = read_only or tool_name in _CHECKPOINTED_TOOLS
        dangerous = False
        # 0. Credential access floor.
        if tool_name in _CREDENTIAL_TOOLS:
            decision = Decision(
                tool_name, Effect.DENY,
                f"{tool_name} would expose stored credentials",
                reasons=["credential access is denied by default"], files=files,
                reversible=False, dangerous=True,
                key=f"{tool_name}")
            return self._apply_session(decision)

        # 1. Command classification floor (shell & git tools).
        level, level_reasons = "allow", []
        if command_text is not None:
            level, level_reasons = classify_command(command_text)
            if level in ("deny", "ask"):
                reasons.extend(level_reasons)

        # 2. Path containment: filesystem access outside the workspace asks.
        if files:
            outside = [
                path for path in files
                if not str(path).startswith("$") and not _inside(
                    self.workspace_root, path, args)
            ]
            if outside:
                reasons.append("touches paths outside the workspace")
                level = "deny" if level == "deny" else "ask"

        # 3. Matching rules; user/project rules outrank defaults.
        matched = [
            rule for rule in self.rules
            if rule.origin != "session" and rule.matches(
                tool_name, args, command_text)
        ]
        def _rank(rule: Rule) -> tuple[int, int, int]:
            # specificity desc, user rules above defaults, deny>ask>allow.
            return (-rule.specificity(),
                    0 if rule.origin in ("user", "project", "session") else 1,
                    -_EFFECT_ORDER.get(rule.effect, 0))

        matched.sort(key=_rank)
        chosen = matched[0] if matched else None
        user_rule = chosen is not None and chosen.origin in ("user", "project")

        # 4. Resolve the effect.
        if chosen is not None and chosen.description:
            reasons.append(f"rule({chosen.origin}): {chosen.description}")
        if level == "deny":
            # Credential/system floor: absolute. Deliberately NOT
            # overridable by user rules or session overrides - reading key
            # material or destroying the machine is never an agent task.
            effect = Effect.DENY
        elif level in ("ask", "unknown"):
            if user_rule:
                effect = chosen.effect  # deliberate user opt-in
            else:
                base = (chosen.effect if chosen is not None else
                        self._default_effect(tool_name, read_only=read_only,
                                             mutating=mutating, network=network,
                                             level=level))
                # Default rules never relax an ask/unknown floor.
                effect = base if _EFFECT_ORDER.get(base, 0) >= _EFFECT_ORDER[Effect.ASK] \
                    else Effect.ASK
        else:
            effect = (chosen.effect if chosen is not None else
                      self._default_effect(tool_name, read_only=read_only,
                                           mutating=mutating, network=network,
                                           level=level))

        summary = _describe(tool_name, args, command_text)
        if level_reasons:
            reasons = level_reasons + [r for r in reasons
                                       if r not in level_reasons]
        decision = Decision(
            tool=tool_name, effect=effect, summary=summary, reasons=reasons,
            files=[str(path) for path in files], reversible=reversible,
            dangerous=dangerous or level == "deny",
            rule=chosen, key=_decision_key(tool_name, args, command_text))
        return self._apply_session(decision)

    def resolve(self, decision: Decision) -> Decision:
        """Resolve an ASK decision into ALLOW/DENY via the approver."""
        if decision.effect is not Effect.ASK:
            return decision
        if self.approver is not None:
            approved, remember = self.approver(decision)
            if approved:
                if remember:
                    self.session_overrides[decision.key] = Effect.ALLOW
                decision.effect = Effect.ALLOW
                audit("permission-granted", tool=decision.tool,
                      detail=decision.summary)
            else:
                decision.effect = Effect.DENY
                audit("permission-denied", tool=decision.tool,
                      detail=decision.summary)
            return decision
        if self.assume_yes:
            decision.effect = Effect.ALLOW
            audit("permission-assumed-yes", tool=decision.tool,
                  detail=decision.summary)
            return decision
        decision.effect = Effect.DENY
        audit("permission-denied-noninteractive", tool=decision.tool,
              detail=decision.summary)
        return decision

    def remember(self, key: str, effect: Effect) -> None:
        self.session_overrides[key] = effect

    def effective_rules(self) -> list[Rule]:
        return list(self.rules)

    # ------------------------------------------------------------ internals

    def _apply_session(self, decision: Decision) -> Decision:
        override = self.session_overrides.get(decision.key)
        if override is not None and decision.effect is Effect.ASK:
            decision.effect = override
            decision.reasons.append("session override")
        return decision

    @staticmethod
    def _default_effect(tool_name: str, *, read_only: bool, mutating: bool,
                        network: bool, level: str) -> Effect:
        if level == "deny":
            return Effect.DENY
        if level == "ask":
            return Effect.ASK
        if read_only:
            return Effect.ALLOW
        if network:
            return Effect.ASK
        if mutating:
            return Effect.ALLOW  # in-workspace writes are checkpointed
        return Effect.ASK


_CHECKPOINTED_TOOLS = {
    "write_file", "edit_file", "apply_patch", "format_code", "delete_path",
    "move_path", "git_rollback",
}
_LEVEL_RANK = {"allow": 0, "unknown": 1, "ask": 2, "deny": 3}
_EFFECT_ORDER = {Effect.DENY: 3, Effect.ASK: 2, Effect.ALLOW: 1}


def _rule_from_config(raw: dict[str, Any], origin: str) -> Rule:
    effect = Effect(str(raw.get("effect", "ask")).lower())
    return Rule(
        tool=str(raw.get("tool", "*")),
        effect=effect,
        path=raw.get("path"),
        command=raw.get("command"),
        description=str(raw.get("description", "")),
        origin=origin,
    )


_COMMAND_TOOLS = frozenset({
    "run_command", "run_tests", "git_commit", "git_create_branch",
    "git_rollback", "format_code",
})


def _command_text(tool_name: str, args: dict[str, Any]) -> str | None:
    if isinstance(args.get("argv"), list):
        return " ".join(str(item) for item in args["argv"])
    if tool_name not in _COMMAND_TOOLS:
        return None
    for key in ("command", "cmd"):
        value = args.get(key)
        if isinstance(value, str):
            return value
    if tool_name == "git_commit":
        return f"git commit {args.get('message', '')}".strip()
    if tool_name == "git_create_branch":
        return f"git branch {args.get('name', '')}".strip()
    if tool_name == "git_rollback":
        return "git restore files from checkpoint"
    if tool_name == "format_code":
        return f"format {args.get('path', '')}".strip()
    return None


def _extract_files(args: dict[str, Any]) -> list[str]:
    files: list[str] = []
    for key in ("path", "file", "target"):
        value = args.get(key)
        if isinstance(value, str) and value:
            files.append(value)
    return files


def _inside(root: Path, path_str: str, args: dict[str, Any]) -> bool:
    try:
        resolve_in_root(root, path_str)
    except PathEscapeError:
        return False
    return True


def _describe(tool_name: str, args: dict[str, Any],
              command_text: str | None) -> str:
    if command_text is not None and tool_name in ("run_command", "run_tests"):
        timeout = args.get("timeout_s", "")
        return f"Run: {command_text}" + (
            f" (timeout {timeout}s)" if timeout else "")
    path = args.get("path") or args.get("file") or args.get("url") or ""
    verb = {
        "write_file": "Write file", "edit_file": "Edit file",
        "delete_path": "Delete", "move_path": "Move",
        "create_dir": "Create directory", "apply_patch": "Apply patch to",
        "format_code": "Format", "git_commit": "Git commit",
        "git_rollback": "Restore files from checkpoint",
        "web_fetch": "Fetch URL", "web_search": "Search the web for",
    }.get(tool_name, f"Run tool {tool_name}")
    return f"{verb} {path}".strip()


def _decision_key(tool_name: str, args: dict[str, Any],
                  command_text: str | None) -> str:
    if command_text:
        # Session-level "always allow" keyed on the command head, so
        # `npm test -- -k x` matches `npm test`.
        head = command_text.strip().split(" ", 3)
        signature = " ".join(head[:3])
        return f"{tool_name}:{signature}"
    if tool_name.startswith("mcp_"):
        return tool_name  # per-MCP-tool decision
    path = args.get("path") or ""
    return f"{tool_name}:{path}"
