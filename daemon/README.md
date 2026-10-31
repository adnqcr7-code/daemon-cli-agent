# Daemon

**An open-source, provider-agnostic AI engineering agent that lives in your terminal.**

Daemon is not a chatbot wrapper. It is an autonomous engineering agent that
understands real repositories, plans substantial work, edits code, runs
commands and tests, verifies its changes, and reports honestly — with a
permission system, checkpoints and secret hygiene built in from the first
line of code.

```
$ daemon auth add
Paste your API key: **********
Analyzing credentials...
Provider identified: Anthropic (starts with the registered 'Anthropic' key prefix)
Verifying credentials against Anthropic ...
Found 12 accessible models.
Authentication successful. Profile: anthropic
```

```
$ daemon
DAEMON
the engineering agent
model   claude-sonnet-4-5
provider anthropic
› fix the failing test in this repo
▸ read_file  path='test_bug.py'
▸ edit_file  path='bug.py'
▸ run_tests
◆ 1,204 in / 356 out · $0.0091 · session $0.0244
Fixed add() in bug.py (was subtracting). Verified: 1 passed.
changed: bug.py
```

---

## Why Daemon

| Principle | What it means here |
|---|---|
| **Provider independence** | 30+ providers catalogued across 3 protocol adapters (OpenAI-compatible, Anthropic Messages, Gemini). No vendor is hardcoded into the engine. |
| **Intelligent authentication** | Paste a key; Daemon fingerprints it locally, asks at most ONE question when formats genuinely overlap, and verifies against exactly one known endpoint. |
| **Powerful tools, controlled permissions** | 30+ built-in tools behind an allow/ask/deny permission engine with static command analysis, workspace containment and an audit trail. |
| **Reliability over demos** | Every mutating action is checkpointed (`/undo`), tool output is fenced as untrusted data, secrets are redacted everywhere, failures are reported — never hidden. |
| **Separate Research agent** | A deliberately smaller, read-only, citation-driven research agent with its own theme (`Archive`) and its own policy. |
| **First-class MCP** | Real Model Context Protocol client over stdio and Streamable HTTP, with untrusted-by-default server review. |
| **Terminal-native** | No web UI, no Electron. Four themes, `NO_COLOR` support, non-interactive mode for scripting and CI. |

## Quick start

The easiest setup is **Ollama Cloud**: no model download and no powerful GPU
needed. Daemon also works with local Ollama models if you want everything to
stay on this computer.

### Windows: the 3-command setup

This is the recommended setup for a computer without a powerful GPU. It uses
Ollama Cloud through the Ollama app, so you do not paste an API key into
Daemon:

```powershell
# 1. Enter the project and install Daemon
cd C:\path\to\daemon; if (!(Test-Path .venv)) { py -3.11 -m venv .venv }; .\.venv\Scripts\python.exe -m pip install -e ".[crypto]"

# 2. Switch Ollama accounts, sign in, and configure Gemma 4
ollama signout; ollama signin; .\.venv\Scripts\python.exe -m daemon.cli auth add --provider ollama; .\.venv\Scripts\python.exe -m daemon.cli config set default_model gemma4:cloud; .\.venv\Scripts\python.exe -m daemon.cli config set models.coding gemma4:cloud

# 3. Run Daemon
.\.venv\Scripts\python.exe -m daemon.cli run "Inspect this project and fix the failing tests." --model gemma4:cloud
```

The second command opens Ollama's sign-in flow. Complete it with the account
you want to use. Cloud models require Ollama usage credits or a paid plan.
`.\.venv\Scripts\python.exe` is used directly so PowerShell activation is not
needed.

For a fully local setup, replace `gemma4:cloud` with `gemma4:e2b`, run
`ollama pull gemma4:e2b` once, and use `--provider ollama`. Local models need
no account or API key but may be slow on computers with limited GPU memory.

### Change a direct cloud API key

To replace a direct Ollama Cloud API key, create a new key at
<https://ollama.com/settings/keys>, then run these commands in PowerShell:

```powershell
$env:OLLAMA_API_KEY = "paste-the-new-key-here"
.\.venv\Scripts\python.exe -m daemon.cli auth remove ollama-cloud
.\.venv\Scripts\python.exe -m daemon.cli auth add `
  --provider ollama-cloud `
  --env OLLAMA_API_KEY
.\.venv\Scripts\python.exe -m daemon.cli auth test
```

For future PowerShell windows, save the new key as a Windows user environment
variable, then open a new terminal:

```powershell
[Environment]::SetEnvironmentVariable(
  "OLLAMA_API_KEY", "paste-the-new-key-here", "User")
```

Never commit API keys, put them in source files, or paste them into project
configuration.

### Change the Ollama CLI account

This is separate from Daemon's API-key profile. Run these commands yourself;
Daemon will not sign in or handle your credentials:

```powershell
ollama signout
ollama signin
ollama run gemma4:cloud
```

For direct cloud API use, create or revoke keys at
<https://ollama.com/settings/keys>. For the current setup, use the
`OLLAMA_API_KEY` steps above.

### Useful checks

```powershell
.\.venv\Scripts\python.exe -m daemon.cli auth list
.\.venv\Scripts\python.exe -m daemon.cli auth models
.\.venv\Scripts\python.exe -m daemon.cli config get default_model
.\.venv\Scripts\python.exe -m daemon.cli config get models.coding
.\.venv\Scripts\python.exe -m daemon.cli doctor
```

On Linux or macOS, replace the Windows Python path with `python` after
installing the package: `python -m pip install -e '.[crypto]'`.

## Commands

```
daemon                     interactive engineering agent (the main experience)
daemon init                initialize project state (.daemon/)
daemon run "task"          one-shot, non-interactive (CI/scripting; --json)
daemon plan "goal"         create an implementation plan (--execute to run it)
daemon review              reviewer subagent over the current diff
daemon research            the restricted Research agent (REPL or one-shot)
daemon auth …              add / list / remove / test / switch / models
daemon models …            list / recommend / set
daemon mcp …               add / list / remove / test / enable / disable
daemon tools               list every registered tool
daemon agents              list specialized subagents
daemon sessions …          list / show / delete / resume
daemon memory …            list / search / clear
daemon config …            get / set / list / path
daemon doctor              diagnose everything (--net to probe endpoints)
daemon version             version, protocols, provider count
```

Inside the session, type `exit`, `quit`, `/exit`, or `/quit` to leave. Ctrl+D
also exits, and Ctrl+C at the input prompt now exits instead of requiring
repeated presses. Other commands include `/plan /build /review /debug
/research /model /models /agents /tools /permissions /diff /undo /compact
/tasks /memory /remember /stats /theme /session /save /resume /clear /mcp
/skills /help`.

## The agents

**Daemon Build** (default) — a senior software engineer. It reads before it
writes, prefers surgical edits, runs the project's tests, and is forbidden
(by prompt *and* by policy) from claiming unverified things work. It can
delegate to specialized subagents: **Architect, Developer, Debugger,
Reviewer, Tester, Security Analyst, Researcher** — read-only roles run in
parallel, mutating roles always run sequentially so two agents never edit
the same files.

**Daemon Research** (`daemon research`) — a separate, lightweight agent for
technical research: web search, page fetching with citations, source
comparison, durable notes. It is restricted *by construction*: its tool
registry physically contains no write/shell/git tools, and its permission
policy denies them even if a future refactor adds them. It uses the
restrained **Archive** theme.

## Architecture

```
daemon/
├── cli.py                 command router (click)
├── context.py             wiring: one place assembles the runtime
├── config.py              layered TOML config (user ← project ← env)
├── providers/             protocol adapters + provider catalog + key detection
│   ├── catalog.py           30+ provider descriptors (pure data)
│   ├── openai_compat.py     Chat Completions (OpenAI, DeepSeek, Groq, …, Azure)
│   ├── anthropic.py         Messages API
│   ├── gemini.py            generateContent API
│   └── detect.py            local key fingerprinting
├── auth/                  vault backends + credential profiles + flows
├── models.py              model registry, capability catalog, recommendations
├── permissions.py         allow/ask/deny engine with command analysis
├── tools/                 filesystem, shell, git, patches, code intel, web
├── agent/                 runtime loop, prompts, sessions, planner, orchestration
├── research.py            the restricted Research agent
├── mcp.py                 MCP client (stdio + Streamable HTTP) and manager
├── project.py             incremental project index
├── memory.py              user/project memory layers
├── checkpoints.py         file-level snapshots powering /undo
├── security.py            redaction, injection scan, path guard
├── ui/                    themes (Obsidian/Archive/Minimal/Classic), renderer, REPL
└── skills.py, plugins.py  skills and the plugin API
```

Adding a provider = one entry in `catalog.py`. Adding a tool = subclass
`Tool` and register it. See `docs/tool-development.md` and
`docs/providers.md`.

## Testing

227 automated tests run the real code paths — mock HTTP servers speak the
actual wire protocols (SSE streaming, tool-call chunk reassembly), an MCP
echo server speaks real JSON-RPC over stdio, and the end-to-end suite drives
the installed CLI through auth → detection → model discovery → a real repo
fix → a real pytest run → checkpoint undo. No test requires a paid API key.

```bash
python -m pytest          # unit + integration + security + e2e
```

## Honest limitations

- **LSP client**: not implemented in 0.1.0. Code intelligence uses exact
  Python AST analysis plus a documented heuristic scanner for other
  languages, and delegates to installed linters/formatters when present.
- **Provider OAuth**: no provider in the catalog offers OAuth for direct API
  use; MCP HTTP OAuth (authorization code + PKCE) is implemented, with
  PKCE/metadata parsing unit-tested and the live browser flow requiring
  manual verification.
- **Pricing/capability data** in the model catalog is curated and labelled
  approximate; models are discovered live from each provider, and anything
  unknown is reported as unknown — never guessed.
- **Windows PowerShell support** is implemented in the shell tool and
  themes; automated tests execute on Linux, so Windows behaviour is
  best-effort verified (see `docs/installation-windows.md`).

## Documentation

- [Installation](docs/installation.md) · [Windows](docs/installation-windows.md) · [Linux](docs/installation-linux.md) · [macOS](docs/installation-macos.md)
- [Configuration](docs/configuration.md)
- [Providers](docs/providers.md) · [Authentication & key detection](docs/authentication.md)
- [Permissions & security](docs/security.md)
- [MCP integration](docs/mcp.md)
- [Tool development](docs/tool-development.md) · [Agent development](docs/agent-development.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Third-party notices](docs/third-party.md)
- [Contributing](CONTRIBUTING.md) · [Security policy](SECURITY.md) · [Changelog](CHANGELOG.md)

## License

MIT — see [LICENSE](LICENSE). Third-party components: see
[docs/third-party.md](docs/third-party.md).
