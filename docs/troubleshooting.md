# Troubleshooting

Always start with `daemon doctor` (add `--net` to probe provider
endpoints). It checks Python, dependencies, optional extras, credential
storage, profiles, cached models, git, the shell, dev tools, the workspace
index and MCP server configuration, and prints a fix hint for every
warning.

## Authentication

**"No provider credentials configured"**
Run `daemon auth add`. For local models: `daemon auth add --provider ollama`.

**"This key format is shared by multiple providers"**
Detection is being honest. Answer the single question, or pass
`--provider <id>` / `--base-url <url>` to skip it.

**Verification fails with HTTP 401 for a freshly pasted key**
Either the key is wrong/revoked, or (for ambiguous formats) the wrong
provider was chosen. `daemon auth remove <profile>` and re-add with an
explicit `--provider`.

**"Cannot open the encrypted vault"**
The master key (`~/.daemon/vault.key`) is missing or corrupt — it cannot
be recovered by design. Remove `~/.daemon/vault.bin` and re-add your
credentials.

**credential storage shows "restrictive file permissions only"**
Install an optional extra: `pip install 'daemon-agent[keyring]'` or
`'daemon-agent[crypto]'`.

## Providers

**Streaming stopped / timeouts**
`limits.request_timeout_s` (default 180 s) and `limits.retries` govern the
transport. Retries happen with exponential backoff for 429/5xx/network
errors; mid-stream failures are not retried (no duplicated output).

**No models listed**
`daemon models list --refresh`. Some gateways need `--provider` +
`--base-url` (custom endpoint). For Azure, set the model to your
*deployment name*: `daemon config set models.coding <deployment>`.

**"Configured build model ... is not available"**
List models and copy the exact id. Then configure all roles used by the
interactive and one-shot commands:

```powershell
daemon models list --refresh
$model = Read-Host "Exact model id"
daemon models set default $model
daemon models set build $model
daemon models set coding $model
```

**Cost shows "unknown"**
The curated catalog has no pricing data for that model. Contributions
welcome (see `daemon/models.py`).

## Permissions

**The agent asked for something I want pre-approved**
Add a rule to `~/.daemon/config.toml`:

```toml
[[permissions.rules]]
tool = "run_command"
command = "^go test ./..."
effect = "allow"
```

**An action was denied in `daemon run`**
Non-interactive mode fails closed on "ask". Pass `--yes` deliberately
(audited), or add a rule above.

**"Path ... escapes the workspace"**
The tool refused a path outside the trusted workspace. Add an explicit
permission rule if it is genuinely intended.

## MCP

**Server shows "unreviewed"**
New servers are untrusted by design. Review it, then
`daemon mcp enable <id>`.

**`mcp test` fails to start the command**
Check the executable and arguments; env entries support `${VAR}`
expansion only if the variable is set in your environment.

## Terminal

**Colours or Unicode glyphs look wrong**
`NO_COLOR` forces plain output; `DAEMON_ASCII=1` forces ASCII glyphs;
`daemon config set ui.theme classic` uses 8-colour-safe named colours.

**REPL history/autocomplete missing**
Install `pip install 'daemon-agent[repl]'`.

## Logs

- `~/.daemon/logs/daemon.log` — operational debug log (set
  `DAEMON_LOG_LEVEL=DEBUG`).
- `~/.daemon/logs/audit.log` — permission decisions, tool executions,
  credential events (secret-free).

Still stuck? Open an issue with the output of `daemon doctor` (secrets are
never included).
