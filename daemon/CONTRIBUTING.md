# Contributing to Daemon

Thank you for improving Daemon. This project values **reliability over
demonstrations** — a merged feature is a tested feature.

## Ground rules

1. **Every feature ships with tests.** Unit tests for pure logic;
   integration tests against scripted mock servers for anything touching
   providers, MCP or the agent loop; security tests for anything touching
   permissions, paths or secrets. No test may require a real API key.
2. **Never claim untested behaviour.** If a limitation exists, document it
   (README "Honest limitations" or the relevant doc) rather than papering
   over it.
3. **Security decisions are deliberate.** Changes to the permission
   engine, redaction, or the vault require a test demonstrating the new
   behaviour AND that the old guarantees still hold (see
   `tests/security/`).
4. **Keep the architecture layered.** Providers are data + thin adapters;
   tools are self-contained classes; the runtime never special-cases a
   vendor or a tool.

## Development setup

```bash
git clone <repository> && cd daemon
python -m venv .venv && source .venv/bin/activate
pip install -e .[dev,keyring,crypto,repl]
python -m pytest            # 227 tests should pass
daemon doctor               # healthy-installation check
```

## Layout map

- `daemon/providers/` — adapters, catalog, key detection
- `daemon/auth/` — vault backends, profiles, flows
- `daemon/tools/` — the tool ecosystem
- `daemon/agent/` — runtime, prompts, sessions, planner, orchestration
- `daemon/ui/` — themes, renderer, REPL
- `daemon/permissions.py`, `daemon/security.py` — the safety layer
- `tests/{unit,integration,security,e2e}/` — the safety net

## Workflow

1. Fork/branch, implement, add tests.
2. `python -m pytest` green.
3. Update docs if user-facing (`docs/`, `README.md`, `CHANGELOG.md`).
4. PR with a description of **what changed, why, and how it is tested**.

For provider additions: one `catalog.py` entry for standard endpoints;
a thin adapter only when the protocol genuinely differs. For tools: see
`docs/tool-development.md`. For agents: `docs/agent-development.md`.

## Reporting security issues

Please use the process in [SECURITY.md](SECURITY.md) rather than a public
issue.
