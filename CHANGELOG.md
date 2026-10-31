# Changelog

All notable changes to Daemon are documented here.
The format follows Keep a Changelog; versioning is semantic.

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
