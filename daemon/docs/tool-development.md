# Tool development guide

Tools are the unit of capability in Daemon. A tool is a small class with a
schema, permission metadata and an async `run()` — registered on a
`ToolRegistry`, it immediately becomes available to every agent interface
(REPL, `daemon run`, subagents) with validation, timeouts, error wrapping,
permission gating and audit logging for free.

## A complete tool

```python
# my_tools.py
from daemon.errors import ToolError
from daemon.tools.base import Tool, ToolContext, ToolOutput


class WordCountTool(Tool):
    name = "word_count"
    description = ("Count words in a workspace file. Use this before "
                   "summarising documents.")
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "Workspace-relative file path"},
        },
        "required": ["path"],
    }
    read_only = True          # drives permission defaults + parallelism
    category = "text"
    timeout_s = 30

    async def run(self, args: dict, ctx: ToolContext) -> ToolOutput:
        from daemon.security import resolve_in_root

        path = resolve_in_root(ctx.workspace_root, args["path"])
        if not path.is_file():
            raise ToolError(f"File not found: {args['path']}")
        text = path.read_text("utf-8", errors="replace")
        return ToolOutput(content=f"{len(text.split())} words",
                          metadata={"path": args["path"]})
```

Registration from a plugin (see below) or directly in code:

```python
from daemon.tools import ToolRegistry

registry = ToolRegistry()
registry.register(WordCountTool())
```

## The contracts

**`parameters`** is a JSON Schema object. It is advertised verbatim to the
model and used for argument validation (required keys, basic types) before
`run()` is called.

**Permission metadata**:

- `read_only = True` — allowed by default; may run in parallel with other
  read-only calls in the same step.
- `mutating = True` — executed sequentially; snapshot files with
  `ctx.checkpoints` before changing them.
- `network = True` — network egress; the default policy asks.

**`ToolContext`** gives you `workspace_root`, `config`, `checkpoints`,
`memory` and `await ctx.emit(name, line)` for live progress lines in the
UI.

**`ToolOutput`**:

- `content` — what the model sees. Keep it bounded; the runtime truncates
  very large outputs but you should be a good citizen.
- `is_error` — report failures as tool errors so the model can react.
- `display` — optional human-facing rendering (e.g. a unified diff) shown
  in the terminal.
- `metadata` — machine-readable extras (e.g. `{"path": ...}` powers the
  changed-files tracker).

**Errors**: raise `ToolError` with an actionable message; the registry
converts it into an error result. Anything else is caught, logged, and
reported as an unexpected failure — a tool crash never kills the agent.

**Path safety**: never build paths yourself; use
`daemon.security.resolve_in_root`. It rejects `..` traversal, absolute
outside paths and symlink escapes with clear messages.

## Bounded output rules of thumb

| Kind | Bound |
|---|---|
| File reads | ≤1500 lines / 120 KB per call, offer offset/limit |
| Search results | ≤200 matches |
| Listings | ≤400–500 entries |
| Command output | ≤100 KB (the shell tool enforces this) |

## Local plugins

Put modules under `<workspace>/.daemon/plugins/` and list them in config:

```toml
[plugins]
enabled = ["my_tools"]
```

The plugin loader imports `my_tools` from `.daemon/plugins/my_tools.py`
and calls its `register(registry)` hook if present. Loading local code is
opt-in by design — it executes with your permissions, so Daemon never
auto-loads anything.

## Distributable plugins

Python entry points make tools installable via pip:

```toml
[project.entry-points."daemon.tools"]
my_tools = "my_package.daemon_plugin:register"
```

Every installed distribution exposing `daemon.tools` entry points is
scanned at startup; each callable receives the registry. The same pattern
with the `daemon.providers` group registers provider descriptors, and
`daemon.agents` registers subagent roles.

## Testing your tools

```python
import asyncio
from daemon.tools import ToolContext, ToolRegistry
from my_tools import WordCountTool

def test_word_count(tmp_path):
    (tmp_path / "doc.txt").write_text("hello world again")

    async def go():
        registry = ToolRegistry()
        registry.register(WordCountTool())
        return await registry.execute("word_count",
                                      {"path": "doc.txt"},
                                      ToolContext(tmp_path))

    out = asyncio.run(go())
    assert not out.is_error
    assert out.content == "3 words"
```

`registry.execute` is exactly what the agent calls — validation, timeout,
error wrapping and audit included.
