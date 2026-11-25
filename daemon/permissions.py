"""The permission engine: allow / ask / deny for every tool invocation.

Layers, most specific first:

1. **Session overrides** - what the user picked on an approval prompt
   ("always allow this in this session").
2. **User / project rules** - ``[permissions] rules`` entries in config.
   Env-fragment rules arrive with origin ``"env"`` (tagged by
   ``Config.load``) and can NEVER relax the classifier floor — only
   ``"user"`` / ``"project"`` rules may (review round 1, R1-4).
3. **Command classification** - static safety analysis for shell/git tools
   (credential access floors to deny, destructive commands floor to ask).
4. **Built-in defaults** - read tools allowed, workspace writes allowed with
   checkpoints, network and consequential actions ask, credentials denied.

A tool call never executes unless the resolved effect is ALLOW. "Ask" is
resolved by the UI approval prompt in interactive mode; in non-interactive
mode "ask" fails closed unless the user explicitly passed --assume-yes.
"""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import json
import math
import os
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .config import daemon_home
from .errors import PathEscapeError
from .events import emit as emit_event
from .log import audit, get_logger
from .security import classify_command, resolve_in_root

try:  # POSIX locking for the grant-state store (ReflexStore pattern).
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None  # type: ignore[assignment]

try:  # Windows locking fallback (msvcrt is stdlib on Windows only).
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows platforms
    msvcrt = None  # type: ignore[assignment]

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
    # Grant expiry (v0.5.0 §3): an ALLOW rule carrying ttl_minutes is
    # a grant, valid for that many minutes from when the grant was
    # first recorded. The creation time is PERSISTED under
    # DAEMON_HOME (grant_state.json, keyed by rule identity), so the
    # window survives process restarts - a fresh engine is built for
    # every CLI invocation, and a construction-relative window would
    # silently restart on each one (review round 2, V6). Once
    # expired the rule stops matching and resolution falls back (for
    # ask-by-default tools, back to ASK). ttl_minutes <= 0 is expired
    # from the start (fail closed). TTL on non-ALLOW rules is
    # ignored - policy is not a grant.
    ttl_minutes: float | None = None

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
    # Unified-diff preview for file-edit ASKs (§2.5): the diff the
    # edit would produce, computed without mutating anything and
    # truncated to the display budget. Rendered by the approval
    # panel; None for non-edit decisions.
    diff_preview: str | None = None
    # Detail lines for the approval panel (plan.execute): the task
    # titles and descriptions the user is approving, so a tampered
    # plan cannot hide behind a goal line and a file list (review
    # round 1, R1-3). Rendered verbatim by the panel.
    details: list[str] = field(default_factory=list)

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
        session_grant_ttl_minutes: float | None = None,
        run_id: str | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.approver = approver
        self.assume_yes = assume_yes
        # Event-kernel run id (v0.9.0 Phase 1): the session id of the
        # run whose decisions this engine records. None outside a
        # run (library use, verify-only callers) - then decisions are
        # audit-logged only, as before. Set by the runtime at run()
        # start; the engine itself never invents one.
        self.run_id = run_id
        # Session grants: decision key -> (effect, expires_at | None).
        self.session_overrides: dict[str, tuple[Effect, float | None]] = {}
        self.session_grant_ttl_minutes = session_grant_ttl_minutes
        self._started = time.time()
        self.rules: list[Rule] = list(DEFAULT_RULES)
        for raw in rules or []:
            if isinstance(raw, Rule):
                self.rules.append(raw)
            else:
                self.rules.append(_rule_from_config(
                    raw, origin=_origin_of(raw)))
        # Persisted grant creation times (V6), loaded after the rule
        # list exists: rule identity -> first-seen timestamp.
        self._grant_created: dict[str, float] = \
            self._load_grant_state()

    # ------------------------------------------------------------------ API

    def check(self, tool_name: str, args: dict[str, Any], *,
              read_only: bool = False, mutating: bool = False,
              network: bool = False) -> Decision:
        """Evaluate policy for one tool call.

        Every resolution - ALLOW, ASK and DENY alike - is written to
        the audit log (v0.5.0 §3): the broker has no blind spots.
        """
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
            return self._finish(decision, command_text)

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
        # Expired ALLOW grants (ttl_minutes) no longer match - the
        # resolution falls back, and says why when it lands on ASK.
        matched_all = [
            rule for rule in self.rules
            if rule.origin != "session" and rule.matches(
                tool_name, args, command_text)
        ]
        expired_grants = [rule for rule in matched_all
                          if self._grant_expired(rule)]
        expired_ids = {id(rule) for rule in expired_grants}
        matched = [rule for rule in matched_all
                   if id(rule) not in expired_ids]
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
        if expired_grants and effect is Effect.ASK:
            reasons.append(
                "an allow grant expired (ttl_minutes) - asking again")
        decision = Decision(
            tool=tool_name, effect=effect, summary=summary, reasons=reasons,
            files=[str(path) for path in files], reversible=reversible,
            dangerous=dangerous or level == "deny",
            rule=chosen, key=_decision_key(tool_name, args, command_text))
        return self._finish(decision, command_text)

    def resolve(self, decision: Decision) -> Decision:
        """Resolve an ASK decision into ALLOW/DENY via the approver."""
        if decision.effect is not Effect.ASK:
            return decision
        if self.approver is not None:
            approved, remember = self.approver(decision)
            if approved:
                if remember:
                    self.remember(decision.key, Effect.ALLOW)
                decision.effect = Effect.ALLOW
                audit("permission-granted", tool=decision.tool,
                      detail=decision.summary)
                via = "approver"
            else:
                decision.effect = Effect.DENY
                audit("permission-denied", tool=decision.tool,
                      detail=decision.summary)
                via = "approver"
        elif self.assume_yes:
            decision.effect = Effect.ALLOW
            audit("permission-assumed-yes", tool=decision.tool,
                  detail=decision.summary)
            via = "assume-yes"
        else:
            decision.effect = Effect.DENY
            audit("permission-denied-noninteractive", tool=decision.tool,
                  detail=decision.summary)
            via = "noninteractive"
        # The ASK resolution is a second decision point (see
        # _finish): the kernel records the final effect with it.
        emit_event(self.run_id, "permission.decided", {
            "tool": decision.tool, "effect": decision.effect.value,
            "target": (decision.files[0] if decision.files else "-"),
            "reason": decision.reasons[0] if decision.reasons
                      else "default policy",
            "key": decision.key, "via": via})
        return decision

    def approve_plan(self, *, plan_id: str, goal: str, task_count: int,
                     files: list[str], plan_hash: str,
                     tasks: list[Any] | None = None,
                     approver: Callable[[Decision], tuple[bool, bool]]
                     | None = None) -> bool:
        """The plan-execution gate (v0.9.0 §2.3): approve executing a
        stored plan through the broker, exactly like a tool decision
        — audit-logged, kernel-recorded, interactive prompt.

        ``--yes`` NEVER substitutes: resolve()'s assume-yes path is
        neutralised for this decision, so without an interactive
        approver the answer is always no (a non-tty run refuses
        outright rather than auto-approving).

        The approval panel shows the full task list (titles +
        descriptions + files) via ``details`` — the user approves
        the content they see, not a goal line (review round 1,
        R1-3). The panel is NOT the defence against a pre-approval
        forgery, though: a forger who rewrites the goal too makes
        the panel fully consistent with the forgery. The defence is
        the kernel check (``verify_approved``): execution also
        requires the file's hash to equal the hash in the latest
        recorded version (plan.created, or a plan.edited from the
        recorded edit path), which a workspace forger cannot move
        (review round 2, R1-3). The session key is scoped to the
        CONTENT HASH, so an "always this session" grant covers
        exactly the approved content: a tampered plan has a
        different key and prompts again. The kernel check is the
        backstop: even a remembered grant cannot execute content
        whose hash differs from the recorded version.
        """
        details: list[str] = []
        shown = list(tasks or [])[:10]
        for task in shown:
            title = task.get("title") if isinstance(task, dict) \
                else getattr(task, "title", "")
            description = task.get("description") \
                if isinstance(task, dict) \
                else getattr(task, "description", "")
            task_files = task.get("files") if isinstance(task, dict) \
                else list(getattr(task, "files", []) or [])
            line = f"{task.get('id') if isinstance(task, dict) else getattr(task, 'id', '?')}. {title}"
            if description:
                line += f" — {description}"
            details.append(line)
            if task_files:
                details.append(f"   files: {', '.join(task_files)}")
        if tasks is not None and len(tasks) > len(shown):
            details.append(
                f"   … {len(tasks) - len(shown)} more task(s) — "
                f"see the stored plan file")
        decision = Decision(
            tool="plan.execute", effect=Effect.ASK,
            summary=(f"Execute plan {plan_id}: {goal} "
                     f"({task_count} task(s), hash {plan_hash[:12]})"),
            reasons=[f"stored plan {plan_id} — approval executes its "
                     f"{task_count} task(s) in order"],
            files=sorted(set(files)), reversible=True, dangerous=False,
            details=details,
            key=f"plan.execute:{plan_id}:{plan_hash[:16]}")
        decision = self._finish(decision, None)
        previous_approver = self.approver
        previous_assume_yes = self.assume_yes
        try:
            if approver is not None:
                self.approver = approver
            # Neutralise assume-yes for this one decision: plan
            # approval is never a default.
            self.assume_yes = False
            resolved = self.resolve(decision)
            return resolved.effect is Effect.ALLOW
        finally:
            self.approver = previous_approver
            self.assume_yes = previous_assume_yes

    def remember(self, key: str, effect: Effect, *,
                 ttl_minutes: float | None = None) -> None:
        """Record a session grant, optionally expiring.

        The TTL comes from the explicit argument, else the engine's
        ``session_grant_ttl_minutes`` (config
        ``permissions.session_grant_ttl_minutes``), else the grant
        lasts for the whole session as before.
        """
        ttl = ttl_minutes if ttl_minutes is not None \
            else self.session_grant_ttl_minutes
        expires_at = time.time() + ttl * 60 if ttl is not None else None
        self.session_overrides[key] = (effect, expires_at)

    def effective_rules(self) -> list[Rule]:
        return list(self.rules)

    # ------------------------------------------------------------ internals

    def _grant_expired(self, rule: Rule) -> bool:
        """True when an ALLOW rule's ttl_minutes grant window closed.

        Measured from the grant's PERSISTED creation time (V6), not
        from this engine's construction - restarting the CLI must
        not restart the window. ttl<=0 expires immediately (the
        persisted start plus a non-positive window is already past),
        so misconfigured grants fail closed.
        """
        if rule.effect is not Effect.ALLOW or rule.ttl_minutes is None:
            return False
        created = self._grant_created.get(
            _rule_identity(rule), self._started)
        return time.time() >= created + rule.ttl_minutes * 60

    # ------------------------------------------------ grant persistence

    def _load_grant_state(self) -> dict[str, float]:
        """Load persisted grant creation times; record new grants.

        A config ALLOW rule with ttl_minutes is a grant whose window
        opens the first time any engine sees it. That first-seen
        time is written to ``grant_state.json`` under DAEMON_HOME so
        every later process measures the SAME window. Saves
        reload-merge under the store lock (R3-4), so this engine's
        save never drops a grant a concurrent engine recorded.
        Persistence is
        best-effort: a state file that cannot be read or written
        degrades to per-process windows, never to a crash - and the
        degradation is logged, not silent. Entries are validated
        one by one (R2-6): an unusable timestamp fails closed for
        its own rule only, never resetting the other windows.
        """
        stored: dict[str, float] = {}
        path = _grant_state_path()
        now = time.time()
        changed = False
        if path is not None and path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                raw = data.get("grants") if isinstance(data, dict) \
                    else None
                if isinstance(raw, dict):
                    for key, value in raw.items():
                        created = _sane_grant_time(value, now)
                        if created is None:
                            # Fail closed for THIS rule only (round
                            # 3, R2-6): a non-finite, unparseable
                            # or future-dated timestamp otherwise
                            # immortalizes the grant - a 2036 date,
                            # NaN and Infinity (all accepted by
                            # json.loads) each compare as "window
                            # not yet closed" forever. An epoch start
                            # is expired for any sane TTL. The entry
                            # is KEPT at 0.0, not dropped: dropping
                            # it would re-record it below as
                            # first-seen-now and restart the window.
                            # Other rules' entries are untouched.
                            _logger.warning(
                                "grant state: unusable timestamp "
                                "for one grant; treating that "
                                "grant as expired")
                            stored[str(key)] = 0.0
                            changed = True
                        else:
                            stored[str(key)] = created
            except (OSError, ValueError) as exc:
                _logger.warning("grant state unreadable (%s); grant "
                                "windows restart with this process", exc)
                stored = {}
        for rule in self.rules:
            if rule.effect is Effect.ALLOW \
                    and rule.ttl_minutes is not None:
                identity = _rule_identity(rule)
                if identity not in stored:
                    stored[identity] = now
                    changed = True
        if changed:
            self._save_grant_state(stored)
        return stored

    @staticmethod
    def _save_grant_state(grants: dict[str, float]) -> None:
        """Persist grant creation times as reload-merge-write (R3-4).

        The old save wrote the saver's snapshot over the whole
        file, last-writer-wins: a concurrent engine whose snapshot
        predated another engine's grant dropped that grant, and the
        next engine re-recorded it as first-seen-now - a half-spent
        TTL window restarted from zero. Now the save runs under the
        cross-platform store lock (the ReflexStore pattern: flock
        on POSIX, an msvcrt byte lock on Windows), RELOADS the
        freshest state first, and merges: entries present in the
        freshly loaded state but absent from the saver's snapshot
        are preserved, and the saver's own updates win for its keys.
        Still best-effort: any failure degrades to per-process
        windows with a warning, never to a crash.
        """
        path = _grant_state_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with _grant_state_lock(path):
                merged = _read_grant_state(path)
                merged.update(grants)
                tmp = path.with_name(
                    f".grant_state.{os.getpid()}."
                    f"{uuid.uuid4().hex}.tmp")
                try:
                    tmp.write_text(
                        json.dumps({"grants": merged}, indent=1),
                        encoding="utf-8")
                    # Windows os.replace fails while another handle
                    # has the destination open; POSIX replace is
                    # atomic and does not care, so the bounded retry
                    # only ever engages on Windows (ReflexStore
                    # pattern).
                    for attempt in range(10):
                        try:
                            os.replace(tmp, path)
                            break
                        except PermissionError:
                            if attempt == 9:
                                raise
                            time.sleep(0.05)
                except BaseException:
                    with contextlib.suppress(OSError):
                        tmp.unlink()
                    raise
        except OSError as exc:
            _logger.warning("grant state not persisted (%s); grant "
                            "windows will restart with the next "
                            "process", exc)

    def _finish(self, decision: Decision,
                command_text: str | None) -> Decision:
        """Apply session grants, then audit-log the resolution.

        Every check() outcome passes through here exactly once, so
        the audit log holds a record for every tool call's decision -
        ALLOW and DENY included, not just ASK resolutions (§3).
        """
        decision = self._apply_session(decision)
        target = decision.files[0] if decision.files \
            else (command_text or "-")
        audit("permission-decision", tool=decision.tool,
              effect=decision.effect.value, target=target,
              reason=(decision.reasons[0] if decision.reasons
                      else "default policy"),
              key=decision.key)
        # Event kernel (v0.9.0 Phase 1): the same single point
        # records the policy resolution. An ASK decision resolved
        # later (resolve(), or the runtime's approval path) emits a
        # second event with via="resolve"/"approval" and the final
        # effect - two distinct decision points, in seq order.
        emit_event(self.run_id, "permission.decided", {
            "tool": decision.tool, "effect": decision.effect.value,
            "target": target,
            "reason": decision.reasons[0] if decision.reasons
                      else "default policy",
            "key": decision.key, "via": "policy"})
        return decision

    def _apply_session(self, decision: Decision) -> Decision:
        stored = self.session_overrides.get(decision.key)
        if stored is not None:
            override, expires_at = stored
            if expires_at is not None and time.time() >= expires_at:
                # Expired grant: forget it; the decision falls back
                # to whatever the rules resolved (ASK for ask-first
                # tools), and the record says so.
                del self.session_overrides[decision.key]
                decision.reasons.append("session grant expired (ttl)")
                stored = None
            elif decision.effect is Effect.ASK:
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


def _rule_identity(rule: Rule) -> str:
    """Stable identity of a rule across processes (V6): the fields
    that define what the rule matches and grants. The description is
    cosmetic and deliberately excluded - rewording a grant does not
    restart its window; changing what it matches (or its TTL) does.
    """
    payload = json.dumps(
        [rule.origin, rule.tool, rule.effect.value, rule.path,
         rule.command, rule.ttl_minutes], sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _grant_state_path() -> Path | None:
    try:
        return daemon_home() / "grant_state.json"
    except Exception:  # noqa: BLE001 - persistence must never break checks
        return None


def _read_grant_state(path: Path) -> dict[str, float]:
    """The persisted grants, sanitized per entry (R2-6 rules).

    Used by the save path's reload-merge (R3-4): entries another
    engine recorded are preserved exactly as a fresh load would see
    them - an unusable timestamp becomes 0.0 (expired), never a
    revived window. An unreadable file degrades to {} (the load
    path's documented degradation).
    """
    stored: dict[str, float] = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return stored
    raw = data.get("grants") if isinstance(data, dict) else None
    if isinstance(raw, dict):
        now = time.time()
        for key, value in raw.items():
            created = _sane_grant_time(value, now)
            stored[str(key)] = created if created is not None else 0.0
    return stored


# Bounded wait for the grant-state lock (round 5, R4-3): the lock
# is normally held only for one reload-merge-write, so a couple of
# seconds is generous. Past the budget the save degrades to the
# module's best-effort contract instead of hanging the caller.
_GRANT_LOCK_TIMEOUT_S = 2.0
_GRANT_LOCK_RETRY_S = 0.05


@contextlib.contextmanager
def _grant_state_lock(path: Path):
    """Exclusive cross-process lock for one grant-state save.

    The ReflexStore lock pattern (round 4, R3-4): flock on POSIX,
    an msvcrt byte lock on Windows, both on a sibling ``.lock``
    file held only for the save's reload-merge-write. On a platform
    with neither primitive the save proceeds unlocked - the merge
    still narrows the race, and persistence stays best-effort.

    Acquisition is BOUNDED (round 5, R4-3): the round-4 lock was a
    blocking flock with no timeout, so a live-but-wedged holder
    hung PermissionEngine construction indefinitely - worse than
    the lost-update degradation the lock replaced, and against
    this module's best-effort contract ("failures warn and
    degrade, never crash" - a hang is neither). After
    _GRANT_LOCK_TIMEOUT_S of retries the save proceeds WITHOUT
    the lock (load-merge-write best-effort) with a warning logged.
    """
    if fcntl is None and msvcrt is None:
        yield
        return
    fd = os.open(path.with_name(path.name + ".lock"),
                 os.O_CREAT | os.O_RDWR, 0o644)
    acquired = False
    try:
        if msvcrt is not None and fcntl is None \
                and os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        deadline = time.monotonic() + _GRANT_LOCK_TIMEOUT_S
        while True:
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(_GRANT_LOCK_RETRY_S)
        if not acquired:
            _logger.warning(
                "grant state lock busy for over %.1fs; saving "
                "without the lock (best-effort merge-write)",
                _GRANT_LOCK_TIMEOUT_S)
        try:
            yield
        finally:
            if acquired:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                else:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(fd)


# A persisted grant timestamp may run at most this far ahead of the
# local clock (ordinary skew between the processes that wrote it).
_GRANT_CLOCK_SKEW_S = 60.0


def _sane_grant_time(value: object, now: float) -> float | None:
    """A persisted grant creation time, or None when unusable (R2-6).

    Usable means a real number (bools excluded), finite, non-
    negative, and not future-dated beyond clock skew. Python's
    json.loads accepts the NaN / Infinity literals, so finiteness
    is checked explicitly: both would otherwise compare as "the
    window never closes" in _grant_expired.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    created = float(value)
    if not math.isfinite(created) or created < 0 \
            or created > now + _GRANT_CLOCK_SKEW_S:
        return None
    return created
_LEVEL_RANK = {"allow": 0, "unknown": 1, "ask": 2, "deny": 3}
_EFFECT_ORDER = {Effect.DENY: 3, Effect.ASK: 2, Effect.ALLOW: 1}


def _origin_of(raw: Any) -> str:
    """The origin of a config rule dict.

    ``Config.load`` tags env-fragment rules with origin ``"env"``;
    user and project rules carry no tag and default to ``"user"``.
    Only the known origins pass through — anything else defaults to
    ``"user"``, the legacy behavior for untagged rules. (Origin
    ``"env"`` is what stops an env rule relaxing the classifier
    floor in ``check()``; the tag is written by the loader, never
    trusted from the fragment itself.)
    """
    if isinstance(raw, dict):
        origin = raw.get("origin")
        if origin in ("user", "project", "env"):
            return str(origin)
    return "user"


def _rule_from_config(raw: dict[str, Any], origin: str) -> Rule:
    effect = Effect(str(raw.get("effect", "ask")).lower())
    ttl_raw = raw.get("ttl_minutes")
    try:
        ttl_minutes = float(ttl_raw) if ttl_raw is not None else None
    except (TypeError, ValueError):
        ttl_minutes = None
    return Rule(
        tool=str(raw.get("tool", "*")),
        effect=effect,
        path=raw.get("path"),
        command=raw.get("command"),
        description=str(raw.get("description", "")),
        origin=origin,
        ttl_minutes=ttl_minutes,
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
