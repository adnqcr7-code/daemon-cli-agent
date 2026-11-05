# Changelog

All notable changes to Daemon are documented here.
The format follows Keep a Changelog; versioning is semantic.

## [Unreleased]

### Fixed
- **/undo now removes files created after a checkpoint** — created paths
  are recorded in the checkpoint (`created.json`) and deleted on restore,
  workspace-contained only; the REPL prompt and `git_rollback` report it.
- **Injection scan covers full tool output** — the runtime scanned only
  the first 30,000 characters (web fetch: 20,000). Scanning is now
  chunked with overlap, so a payload buried past any limit is caught.
- **Plugin loader implemented** — `daemon/plugins.py` loads local
  `.daemon/plugins/` modules listed in `[plugins] enabled` and
  `daemon.tools` entry points, as the docs already claimed. The
  `daemon.providers` / `daemon.agents` entry-point groups remain planned,
  not loaded; the docs now say so.
- **Docs match the code** — the README tree now correctly lists
  `plugins.py` (it exists now); `cryptography` added to the `dev`
  extra so a fresh clone runs the whole suite (10 tests failed without
  it: vault + end-to-end credential storage).
- **doctor no longer uses the deprecated Click `__version__`** —
  versions now come from `importlib.metadata`.

### Added
- **Enforced verification** — after a turn that changes files, Daemon
  runs `compileall` on the changed Python files and the project's test
  runner scoped to the touched package, and reports pass/fail in the
  result (`[daemon verification] ...`) before claiming done. Config:
  `[verification] enabled / run_tests / timeout_s`.
- **Lint + CI** — ruff config in `pyproject.toml` and
  `.github/workflows/ci.yml` (pytest + ruff on ubuntu / windows / macos,
  Python 3.11–3.13). The tree is ruff-clean as of this release.
- **Context honesty** — the current step's tool results are kept in
  full; only older results are shortened when a request is built (with
  an explicit marker). Compaction summaries now say they were built
  from 600-character previews, and the old-turns fallback leaves a
  visible notice instead of silently dropping turns.

### Changed
- **Catalog claim verified by test** — every one of the 31 catalogued
  providers resolves to a real protocol adapter (OpenAI-compatible,
  Anthropic, Gemini); a new test proves it, so "30+ providers" is no
  longer a claim the code cannot back.

## [0.2.0] - 2026-11-03

First installable GitHub release. The wheel is attached to the GitHub Release;
Daemon is not currently published on PyPI.

### Added
- **Model-driven context compaction**: the model can compact older
  conversation history when context usage is high; automatic compaction
  remains as a safety net.
- **Task evaluations**: `daemon eval` runs repository tasks against selected
  model profiles and reports correctness, tests, tool calls, latency,
  permissions and token usage.
- **Optional language-server support**: symbol, reference and diagnostic
  requests can use configured LSP servers, with the existing analysis as a
  fallback.
- **Release update notices**: the interactive startup checks GitHub Releases
  daily and shows a direct wheel-install command; checks can be disabled.
- Repository metadata now includes discoverability topics and package URLs.

### Changed
- Tool-result de-duplication preserves complete tool-call/result pairs.
- Compaction and evaluation reports include provider-reported usage where
  available.

## [0.1.0] - 2026-10-31

Initial public release.

### Added
- **Agent runtime**: streaming tool-calling loop with permission gates on
  every call, checkpointed writes, context compaction, usage/cost
  accounting with configurable ceilings, clean Ctrl+C cancellation.
- **Provider layer**: 3 protocol adapters (OpenAI-compatible incl. Azure,
  Anthropic Messages, Gemini generateContent) covering a catalog of 30
  providers with local API-key fingerprinting, single-question ambiguity
  handling and no-credential-probing verification.
- **Authentication**: multiple accounts/profiles, environment-variable
  import, keyless local servers; credential storage ladder (OS keychain →
  AES-256-GCM vault → warned 0600 fallback); process-wide secret
  redaction incl. tracebacks; audit log.
- **Model registry**: live discovery per profile with 24 h cache, curated
  capability/pricing catalog (unknown stays unknown), task-based
  recommendations with reasons, role models.
- **Tool ecosystem (30+ tools)**: bounded file read/write/edit, unified
  diff parse/apply with fuzzy matching, glob/search/tree, shell execution
  (bash/PowerShell, scrubbed env, streaming output), git suite, Python AST
  symbols/references + heuristic scanner for 10+ languages, diagnostics
  via installed linters, formatters, test-runner detection, web search
  (5 backends) and citation-friendly page fetching.
- **Permissions**: allow/ask/deny engine, static dangerous-command
  analysis with hard deny floors, session "always allow", user/project
  rules, non-interactive fail-closed mode.
- **Agents**: Daemon Build plus Architect/Developer/Debugger/Reviewer/
  Tester/Security/Researcher subagents (read-only roles parallelize);
  **Daemon Research** as a separate restricted, citation-driven agent with
  the Archive theme.
- **MCP**: stdio + Streamable HTTP client (session headers, protocol
  negotiation, tools/resources/prompts, OAuth PKCE helpers), server
  manager with untrusted-until-reviewed flow.
- **Terminal experience**: Obsidian/Archive/Minimal/Classic themes,
  streaming renderer, approval panels, diff colouring, prompt_toolkit
  history/completion (optional), NO_COLOR/ASCII fallbacks.
- **Commands**: init, run (--json), plan (--execute), review, research,
  auth/models/mcp/tools/agents/sessions/memory/config, doctor, version;
  25+ slash commands in the REPL.
- **Project intelligence**: incremental index (files, languages,
  dependency manifests, entrypoints, build/test commands), memory layers,
  file-level checkpoints with /undo.
- **Testing**: 227 automated tests — unit, protocol-level integration
  against local mock servers, security suite, and an end-to-end CLI
  workflow (auth → detection → discovery → real repo fix → real pytest
  run → checkpoint undo) requiring no paid API keys.
