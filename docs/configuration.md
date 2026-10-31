# Configuration

Daemon layers configuration with the most specific source winning:

```
built-in defaults  ←  ~/.daemon/config.toml (user)  ←  <workspace>/.daemon/config.toml (project)  ←  environment
```

## Files

- **User config**: `~/.daemon/config.toml` (or `$DAEMON_HOME/config.toml`)
- **Project config**: `<workspace>/.daemon/config.toml` — create the
  skeleton with `daemon init`. Commit it to share team settings; the
  `.daemon/checkpoints/` directory is git-ignored by `init`.

Manage with commands or by editing the files directly:

```bash
daemon config path                 # where is my user config?
daemon config get ui.theme
daemon config set ui.theme archive
daemon config set limits.max_turns 30
daemon config set models.coding gpt-4o --project      # writes project file
daemon config set active_profile ollama                # switch an auth profile
daemon config list
```

`active_profile` is a convenience alias for `daemon auth switch`; it updates
the credential store rather than writing an unrelated configuration key.

## Reference (defaults shown)

```toml
# Provider/model selection
default_provider = null            # taken from the active auth profile
default_model = null
[models]
coding = null                      # role-specific model ids
planning = null
research = null
review = null

[ui]
theme = "obsidian"                 # obsidian | archive | minimal | classic
unicode = true                     # set DAEMON_ASCII=1 to force ASCII glyphs
spinner = true

[limits]
warn_cost_usd = 1.0                # notice when a session passes this
max_cost_usd = null                # hard ceiling; the run aborts (never silent)
max_turns = 40                     # agent steps per user turn
tool_timeout_s = 120
request_timeout_s = 180
connect_timeout_s = 10
retries = 3                        # HTTP retries with backoff

[permissions]
assume_yes = false                 # only honoured in non-interactive mode
# rules = [ ... ]                  # see docs/security.md

[research]
search_backend = "duckduckgo"      # duckduckgo | searxng | tavily | serper | brave
searxng_url = null                 # required for the searxng backend
max_results = 8
fetch_limit_kb = 48

[index]
extra_ignore = []                  # additional directory names to skip
max_files = 20000

[mcp.servers]                      # see docs/mcp.md - usually managed via CLI
# example = { transport = "stdio", command = "uvx", args = ["some-server"] }

[plugins]
enabled = []                       # opt-in local plugin modules

[sessions]
keep = 50
```

## Environment variables

| Variable | Effect |
|---|---|
| `DAEMON_HOME` | Move all user state (config, vault, sessions, logs) |
| `DAEMON_PROVIDER` / `DAEMON_MODEL` | Override active provider/model |
| `DAEMON_THEME` | Override theme |
| `DAEMON_TOOL_TIMEOUT` | Override `limits.tool_timeout_s` |
| `DAEMON_MAX_COST` | Override `limits.max_cost_usd` |
| `DAEMON_VAULT_BACKEND` | Force `keyring` / `encrypted` / `plain` storage |
| `DAEMON_LOG_LEVEL` | `DEBUG` / `INFO` / … for `~/.daemon/logs/daemon.log` |
| `DAEMON_SEARCH_BACKEND` | Override `research.search_backend` |
| `NO_COLOR` | Strip all colour output |
| `DAEMON_ASCII` | Force ASCII glyphs |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY`, … | Provider keys Daemon can import during `auth add` (see docs/authentication.md) |

## Token/cost awareness

Usage is reported per turn (`in / out · $estimate · session $total`).
Estimates come from the curated model catalog and are marked approximate;
models without known pricing show "cost unknown". `limits.warn_cost_usd`
prints a warning; `limits.max_cost_usd` aborts the run with a clear error.
Daemon never switches models silently.
