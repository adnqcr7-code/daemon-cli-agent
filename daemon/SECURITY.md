# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x (development) | yes |

## Reporting a vulnerability

Please report vulnerabilities privately to the maintainers via GitHub
Security Advisories ("Report a vulnerability" button on this repository)
rather than opening a public issue. Include reproduction steps where
possible; the team aims to respond within a week.

Please **do not** test vulnerabilities against providers or services you
do not own.

## Security design summary

Details and guarantees: [docs/security.md](docs/security.md). Highlights:

- Allow/ask/deny permission engine on every tool call, with static command
  analysis and non-negotiable deny floors for credential access and
  machine-destructive commands.
- Workspace containment for all filesystem tools (including symlink
  escapes).
- Checkpoints before every mutating action; `/undo` restore.
- Credential storage ladder: OS keychain → AES-256-GCM vault → warned
  0600 fallback. Secrets are never printed, logged or persisted in
  transcripts; a process-wide redactor covers logs, tracebacks and output.
- Prompt-injection defence in depth: fenced tool output, injection
  scanning with visible warnings, and a permission engine that does not
  live inside the model.
- MCP servers are untrusted until explicitly reviewed and enabled.
- An audit log records permission decisions and security-relevant events.

## Scope notes

- Static command classification is a heuristic safety net, not a sandbox.
  For untrusted code execution, run Daemon inside a container/VM you
  control.
- Windows file permissions rely on user-profile ACLs plus the keychain
  extra; on Linux/macOS the vault uses 0600 modes.
