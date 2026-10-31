# Permissions and security

Security in Daemon is an architectural layer, not a README paragraph. Every
claim below is enforced by code and covered by automated tests
(`tests/security/`, `tests/integration/test_agent_runtime.py`).

## The permission engine

Every tool invocation passes through an allow / ask / deny decision built
from, in order of specificity:

1. **Session overrides** — what you approved with "always this session".
2. **User / project rules** — `[permissions] rules` in config.
3. **Static command analysis** — classification of shell/git command text.
4. **Built-in defaults** — the policy below.

### Built-in defaults

| Action | Default |
|---|---|
| Reading / searching / listing project files | **allow** |
| Writing / editing files inside the workspace | **allow**, checkpointed before every change |
| Running the project's tests, builds, linters | **allow** |
| Ordinary safe shell commands (`pytest`, `npm test`, `make`, `cargo test`, `git status`, …) | **allow** |
| Unfamiliar shell commands | **ask** (fail closed) |
| Installing dependencies (`pip install`, `npm install`, `npx`, …) | **ask** |
| Destructive commands (`rm -rf build/`, `git reset --hard`, force push, …) | **ask**, with the reason shown |
| Deleting files, moving paths, git commits | **ask** |
| Accessing paths outside the workspace | **ask** |
| Network tools (`web_search`, `web_fetch`) in the Build agent | **ask** |
| MCP server tools | **ask** until the server is reviewed and enabled |
| Credential access (`~/.ssh`, `.pem`, `cat ~/.aws/credentials`, `sudo`, shutdown/mkfs/fork bombs, `rm -rf /`) | **deny** — hard floor |

### Hard floors

Deny-level classifications (credential material, machine destruction) can
**not** be overridden by user rules or session approvals. This is a
deliberate design decision: no agent task requires reading your SSH keys.

### Approval prompts

Interactive approvals show exactly what will run, which files may change,
why the engine is asking, and whether the action is reversible:

```
┌────────────── APPROVAL REQUIRED ──────────────┐
│ action      Run: pip install requests         │
│ why         installs or removes dependencies  │
│ reversible  maybe                             │
└───────────────────────────────────────────────┘
allow? [y]es / [n]o / [a]lways this session >
```

Non-interactive mode (`daemon run`) fails **closed** on "ask" unless you
explicitly pass `--yes` (audited as `permission-assumed-yes`).

### Custom rules

```toml
[permissions]
# TOML array-of-tables syntax:
[[permissions.rules]]
tool = "run_command"
command = "^go test"
effect = "allow"
description = "go tests are safe here"
```

Rules support `tool` (glob), `command` (regex on the command text) and
`path` (glob) matchers. User rules may relax *ask* floors (your machine,
your call) but never the *deny* floor.

## Checkpoints and undo

Every mutating tool (write, edit, patch, delete, move, format) snapshots the
affected files into `<workspace>/.daemon/checkpoints/<timestamp>/` **before**
changing anything. No git objects, no repository mutation - just copies with
the original relative paths.

- `/diff` shows the session's changes as unified diffs.
- `/undo` restores the latest checkpoint (with confirmation).
- A rolling window of ~20 checkpoints is kept.

## Workspace containment

All filesystem tool paths resolve inside the trusted workspace:

- `..` traversal, absolute outside paths and **symlink escapes** are
  rejected (resolution happens before the containment check).
- Outside access requires an explicit permission rule.

## Prompt-injection defence

Tool output — file contents, web pages, command output, MCP results — is
**data, never instructions**:

1. Every tool result is wrapped in `<tool_output>` fences, and the system
   prompt instructs the model to treat fenced content strictly as data.
2. Untrusted content is scanned for injection patterns ("ignore previous
   instructions", "reveal your system prompt", exfiltration attempts,
   `curl | sh` suggestions). Findings are flagged **inside the fence** so
   the model sees the warning, and the attempt is recorded.
3. Most importantly: even a fully tricked model still faces the permission
   engine, which does not live in the model. A hostile web page that
   convinces the agent to run `rm -rf /` still gets **deny**.

## Secret hygiene

- Storage ladder: OS keychain → AES-256-GCM vault → 0600 file (warned).
- A process-wide redactor scrubs every log line, traceback, session record
  and rendered output; known key formats are pattern-redacted even when the
  live value was never registered.
- Child processes receive a scrubbed environment (provider key variables
  removed).
- An audit log (`~/.daemon/logs/audit.log`) records permission decisions,
  tool executions, credential additions and MCP trust changes — redacted.

## Agent-isolation details

- **Subagents**: read-only roles (Architect, Reviewer, Security, Researcher)
  can run in parallel; mutating roles (Developer, Debugger, Tester) always
  run sequentially — two agents never edit the same files concurrently.
- **Research agent**: its tool registry contains no write/shell/git tools at
  all, and a role-scoped policy denies them anyway (defence in depth).
- **MCP servers** are untrusted until you review and
  `daemon mcp enable` them; their tools stay invisible until then.

## Reporting vulnerabilities

See [SECURITY.md](../SECURITY.md).
