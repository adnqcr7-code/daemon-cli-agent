# Changelog

All notable changes to Daemon are documented here.
The format follows Keep a Changelog; versioning is semantic.

## [Unreleased]

### Added
- **Task-aware tool pruning**: each request advertises only the tools the
  task and current session make relevant, keeping the read/edit/search/shell
  core and anything already used. Pruned tools still execute if called, so
  capability is unchanged. (`optimization.tools`, on by default.)
- **Failure-loop intervention**: when one call fails repeatedly with the same
  error and arguments, the next request gets a short instruction to change
  approach and try the tools that usually replace the failing one.
  (`optimization.failure_detection`, on by default.)
- **Design references from BITON**: `daemon design search|show` and
  `daemon build from <slug|url>` read a documented interface study (palette,
  type scale, spacing, components and ordered flow) and start the agent with
  it as the task. The Build agent also gets read-only `design_search` and
  `design_get` tools plus an in-session `/design` command.

### Changed
- The optimization engine now runs all three of its mechanisms by default:
  context optimization (already on), tool pruning and failure-loop detection.

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
