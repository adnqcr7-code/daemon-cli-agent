"""Built-in scripted demo provider: a deterministic, offline stand-in.

This is NOT a model and never pretends to be one. It exists so a full
``daemon run`` can be exercised end-to-end with zero credentials: smoke
tests, CI, the first-run experience, and offline eval targets. Every
surface labels it as scripted (catalog row, doctor, run banner, session
meta via provider id ``demo``, and its own reply text).

Behavior is a small deterministic scenario engine over the conversation:

- If the task names a file, it drives the REAL tool loop: ``read_file``
  the file, then propose one minimal real edit (for ``.py``: a docstring
  on the first top-level function lacking one), then finish with a
  summary. All edits go through the normal permission + checkpoint path.
- If no file is named, or the file cannot be read, it answers with a
  canned explanation and makes no tool calls.
- Requests that offer no tools (compaction/summarization calls) get a
  plain canned text reply and never any tool calls.

No network, no randomness, no clock dependence: the same conversation
always produces the same events.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from pathlib import Path

from .base import ProviderAdapter
from .types import (
    ChatRequest,
    Message,
    ModelCapabilities,
    RemoteModel,
    StreamEvent,
    ToolCall,
    Usage,
)

DEMO_MODEL_ID = "demo-scripted"
LABEL = "demo provider (scripted - not a real model)"

_FILE_TOKEN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-/]*\.[A-Za-z0-9]{1,6}")
_READ_HEADER = re.compile(r"^\[.*\| lines \d+-\d+ of \d+\]\n?")
_DEF_START = re.compile(r"^(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(")
# The planner prompt's first line (daemon/agent/prompts.py): the demo
# answers planner requests with one scripted task so `daemon plan
# --provider demo` and REPL /plan work offline. The plan JSON is
# labeled scripted; the demo does not plan, it carries the goal
# verbatim into a single task so the plan gate can be exercised.
_PLANNER_MARKER = "Design an implementation plan for this goal:"


class DemoAdapter(ProviderAdapter):
    """Scripted provider speaking no wire protocol at all."""

    # The workspace the current run operates on. Adapters are
    # otherwise stateless protocol objects; the agent runtime sets
    # this before a run because the scenario engine may only treat
    # a task token as a file when it resolves to an existing file
    # inside the workspace (review round 2, V11). When unset
    # (direct adapter use), the process working directory is the
    # root.
    workspace: Path | None = None

    async def list_models(self) -> list[RemoteModel]:
        return [RemoteModel(
            model_id=DEMO_MODEL_ID,
            provider="demo",
            display_name="Demo (scripted, no model)",
            capabilities=ModelCapabilities(
                context_window=128_000, tool_calling=True, streaming=True,
                input_price_per_mtok=0.0, output_price_per_mtok=0.0),
            discovered=True,
        )]

    async def chat(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        text, calls = _decide(request, self.workspace)
        if text:
            yield StreamEvent(kind="text", text=text)
        if calls:
            yield StreamEvent(kind="tool_calls", tool_calls=calls)
        yield StreamEvent(kind="usage", usage=Usage())
        yield StreamEvent(kind="done",
                          finish_reason="tool_calls" if calls else "stop")


# ---------------------------------------------------------------------------
# Scenario engine (pure functions over the conversation - unit-testable)
# ---------------------------------------------------------------------------


def _decide(request: ChatRequest,
            workspace: Path | None = None
            ) -> tuple[str | None, list[ToolCall]]:
    """Return (final_text, tool_calls); exactly one of them is non-empty."""
    messages = request.messages
    task = _task_text(messages)
    if task.startswith(_PLANNER_MARKER):
        # Planner requests carry no tools: answer with a scripted plan
        # JSON (labeled) so the plan gate works offline. Checked before
        # the compaction branch, which would otherwise swallow it.
        root = Path(workspace).resolve() if workspace is not None \
            else Path.cwd().resolve()
        return _scripted_plan(task, root), []
    if not request.tools:
        # Compaction / one-shot text calls: never drive tools.
        return (f"[{LABEL}] Scripted summary unavailable; earlier turns "
                "were dropped by compaction."), []

    results = [result for message in messages
               for result in message.tool_results]
    if not results:
        root = Path(workspace).resolve() if workspace is not None \
            else Path.cwd().resolve()
        target = _file_named_in(task, root)
        if target is None:
            return (f"[{LABEL}] This is a scripted demonstration, not a "
                    "model. Name an existing workspace file in the task "
                    "(for example: 'add a docstring to a.py') and I will "
                    "read it and propose a minimal real edit through the "
                    "normal permission and checkpoint path. For real work, "
                    "configure a provider with 'daemon auth add'."), []
        return None, [_call(messages, "read_file", {"path": target})]

    last = results[-1]
    if last.tool_name == "read_file":
        return _after_read(request, last, messages)
    if last.tool_name == "edit_file":
        if last.is_error:
            return (f"[{LABEL}] The scripted edit was refused or failed: "
                    f"{last.content}"), []
        return (f"[{LABEL}] Done. The scripted edit was applied through "
                "the real permission + checkpoint path. This was a "
                "demonstration run - no model was involved."), []
    return (f"[{LABEL}] Scripted scenario finished after "
            f"'{last.tool_name}'."), []


def _after_read(request: ChatRequest, result, messages) -> tuple[str | None, list[ToolCall]]:
    path = _call_argument(messages, result.tool_call_id, "path") or ""
    if result.is_error:
        return (f"[{LABEL}] The named file could not be read "
                f"({result.content.strip()}). Scripted demo ends here - "
                "no edits were made."), []
    if not path.endswith(".py"):
        lines = len(_file_body(result.content).splitlines())
        return (f"[{LABEL}] Read {path} ({lines} lines). The scripted "
                "demo only edits Python files (a docstring on the first "
                "undocumented top-level function); no edit was made."), []
    body = _file_body(result.content)
    edit = _docstring_edit(body)
    if edit is None:
        return (f"[{LABEL}] Read {path}. Every top-level function "
                "already has a docstring (or there are no functions), so "
                "the scripted demo made no edit."), []
    old_text, new_text = edit
    return None, [_call(messages, "edit_file", {
        "path": path, "old_text": old_text, "new_text": new_text})]


def _scripted_plan(prompt: str, root: Path) -> str:
    """The demo's answer to a planner prompt: one scripted task.

    The goal is the prompt's first content line (after the marker);
    files are the dotted tokens in it that resolve to real workspace
    files. Labeled scripted in the summary so nobody mistakes this
    for a model's plan.
    """
    goal = prompt[len(_PLANNER_MARKER):].strip().splitlines()
    goal_text = next((line.strip() for line in goal if line.strip()),
                     "the stated goal")
    files: list[str] = []
    for match in _FILE_TOKEN.finditer(goal_text):
        token = match.group(0).rstrip(".,;:)")
        if "://" in token:
            continue
        try:
            resolved = (root / token).resolve()
        except OSError:
            continue
        if resolved.is_file() and resolved.is_relative_to(root):
            rel = resolved.relative_to(root).as_posix()
            if rel not in files:
                files.append(rel)
    return json.dumps({
        "summary": (f"[{LABEL}] Scripted single-task plan for: "
                    f"{goal_text[:120]}"),
        "tasks": [{
            "id": "1",
            "title": f"Execute: {goal_text[:80]}",
            "description": ("Scripted plan: the demo provider does not "
                            "plan. This single task carries the goal "
                            "verbatim so the plan gate can be exercised "
                            "offline."),
            "files": files,
            "depends_on": [],
            "verification": ("Run the task; verify the named file(s) "
                             "changed as described."),
            "risk": "low",
        }],
    })


def _task_text(messages: list[Message]) -> str:
    for message in messages:
        if message.role == "user" and message.text:
            return message.text
    return ""


def _file_named_in(task: str, root: Path) -> str | None:
    """The workspace-relative path of the file the task names, or None.

    A dotted token counts as a file ONLY when it resolves to an
    existing file inside the workspace (review round 2, V11).
    Round 1 returned the first dotted token outright: "example.com"
    became a read attempt on a path the user never named,
    "../outside.py" was mangled to "outside.py", and
    "/etc/cron.d/evil.py" lost its root and became a relative read.
    Non-file tokens are skipped and the scan continues, so a task
    naming a real file after a dotted non-file still finds it.
    """
    for match in _FILE_TOKEN.finditer(task):
        token = match.group(0).rstrip(".,;:)")
        if "://" in token:
            continue
        try:
            resolved = (root / token).resolve()
        except OSError:
            continue
        if resolved.is_file() and resolved.is_relative_to(root):
            return resolved.relative_to(root).as_posix()
    return None


def _call(messages: list[Message], name: str, arguments: dict) -> ToolCall:
    prior = sum(len(message.tool_calls) for message in messages)
    return ToolCall(id=f"demo-{prior + 1}", name=name, arguments=arguments)


def _call_argument(messages: list[Message], call_id: str, key: str):
    for message in messages:
        for call in message.tool_calls:
            if call.id == call_id:
                value = (call.arguments or {}).get(key)
                return value if isinstance(value, str) else None
    return None


def _file_body(content: str) -> str:
    """Strip the read_file location header, returning the file's text."""
    body = _READ_HEADER.sub("", content, count=1)
    if body.startswith("[truncated"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
    return body


def _docstring_edit(source: str) -> tuple[str, str] | None:
    """(old_text, new_text) adding a docstring to the first top-level
    function that lacks one, or None when there is nothing to document."""
    lines = source.split("\n")
    for i, line in enumerate(lines):
        match = _DEF_START.match(line)
        if match is None:
            continue
        # Find the end of the (possibly multi-line) signature.
        j = i
        while j < len(lines) and not lines[j].rstrip().endswith(":"):
            j += 1
        if j >= len(lines):
            return None
        # First non-blank body line decides whether a docstring exists.
        k = j + 1
        while k < len(lines) and not lines[k].strip():
            k += 1
        if k < len(lines) and lines[k].lstrip().startswith(('"""', "'''")):
            continue
        old_text = "\n".join(lines[i:j + 1])
        if source.count(old_text) != 1:
            continue  # edit_file needs a unique anchor; try the next def
        summary = match.group(1).replace("_", " ").capitalize()
        new_text = old_text + f'\n    """{summary}."""'
        return old_text, new_text
    return None
