# Changelog

All notable changes to Daemon are documented here.
The format follows Keep a Changelog; versioning is semantic.

## [Unreleased]

## [0.4.1] - 2026-11-24

Windows fixes. CI for 0.4.0 was green on Linux and macOS but failed
on windows-latest; this release fixes what those runs exposed.

### Fixed

- **Reflex store locking on Windows**: `ReflexStore._locked` was a
  no-op on platforms without `fcntl`, so concurrent writers raced
  their read-modify-write cycles and silently lost entries (a
  two-writer test kept only 25 of 50). The store now serializes on
  Windows with an `msvcrt` byte-range lock on the same lock file,
  saves go to a per-write unique tmp file (the old shared tmp path
  collided - `PermissionError` inside the write, before replace),
  and `os.replace` retries briefly while a concurrent reader holds
  the destination open. POSIX behavior is unchanged.
- **Verified-facts ordering on timestamp ties**: `MemoryStore`
  sorted `entries()` / `get_verified_facts()` on timestamps alone,
  so facts created inside one clock tick - easy where `time.time()`
  granularity is coarse, notably Windows - came back oldest-first,
  against the documented newest-first order. Both orderings are now
  total (a tie goes to the later-appended entry), hence
  deterministic on every platform.
- **Location rows gated to references** (review 5, R5-2): the reflex
  symbol parser no longer accepts `path:line:col` location rows
  from `find_symbols` output; that shape is a references-LSP shape
  only.

### Tests

- The seven planted-filename injection tests are skipped on
  Windows: filenames there cannot contain control characters, so
  the fixture is unrepresentable (and the real tools can never emit
  such lines from real files). POSIX coverage is unchanged.
- The concurrent-writer store test now also asserts that no writer
  thread raised, and a regression test pins verified-facts ordering
  on exact timestamp ties.

## [0.4.0] - 2026-11-23

Reflex Phase 1 (design v4): soft signals over certified project facts.

### Added

- **Reflex engine** (`daemon/agent/reflex.py`): a dedicated store
  (`.daemon/reflex.json`, flock-ed writes, in-memory index — not
  MemoryStore) plus a failure-detector-idiom engine that watches the
  agent's trajectory and offers certified structural facts back as one
  short `[reflex]` note appended to the conversation tail at request
  build. The system prompt is never mutated by a firing (prompt-cache
  invariant, covered by an explicit test), and the note is never
  written into the session transcript.
- **Certification gate**: an entry is certified only by execution of
  the claim (codeintel tool output that *is* the fact, recorded with
  its provenance), by two independent derivations agreeing, or by
  explicit user approval. Everything else is an unverified proposal
  that can never fire. Live/time-indexed claims can never certify.
  Reflex entries cannot be minted through `daemon memory
  add-verified` — the reflex store has its own write paths only.
- **Quarantine on disagreement**: a certified entry contradicted by a
  later tool result, or bound to a file that changed, stops firing
  immediately pending re-certification or a user decision.
- **`daemon reflex list|proposals|approve <id>|purge`**: inspect every
  entry and its counters, approve proposals, purge the store.
- **Reflex reporting**: per-session optimization reports gain event
  counters — firings, followed-by-no-read, overrides, quarantines
  (events only; no per-fact token accounting, by design).
- Config under `optimization.reflex.*` (`enabled`,
  `max_signals_per_turn`, `entry_ttl_days`, and `hard_signals`,
  default false — deterministic artifact serves are Phase 2 and are
  not implemented; no tool result is ever substituted).

## [0.3.0] - 2026-11-23

Tier 1: worktree isolation, a ranked repo map, verified memory with
optimization reporting, and a hard verification loop.

### Added

- **Git worktree isolation**: `daemon worktree create/list/status/remove`
  manages per-task worktrees under `.daemon/worktrees/` on
  `daemon/<task>` branches (registered in `.git/info/exclude`, never by
  editing the user's `.gitignore`). `daemon run --worktree "<task>"`
  runs a task in a fresh worktree and keeps it for review/merge.
- **Ranked repo map**: `daemon repomap [task]` ranks files by symbol and
  path matches with the task, import centrality, entrypoints, and git
  recency — every score carries a human-readable reason. A budgeted map
  is injected into the build agent's system prompt.
- **Verified memory**: memory entries gain confidence, source location,
  verified/supersede/invalidate tracking (old JSONL still loads).
  `daemon memory add-verified / verified / invalidate`, contradiction
  warnings on save, and a verified-facts block in the project context.
- **Optimization reporting**: every run writes
  `.daemon/reports/<session>.json` (tokens, tool calls, failures,
  failure loops, verified facts, verification outcome, cost when
  known — never invented). `daemon optimize list/show/compare`.
- **Hard verification loop**: verification now also runs the project's
  lint (ruff / npm lint / go vet) and typecheck (mypy / pyright /
  tsc) when configured and installed, and a failure is fed back to
  the agent for repair attempts (`verification.repair_attempts`,
  default 1) before the turn may finish.
- **PyPI publishing**: trusted-publishing workflow
  (`.github/workflows/publish.yml`); install with
  `pipx install daemon-agent[...]` (see README).

### Fixed

- The agent runtime at 0.2.x was missing imports and two methods the
  release shipped references to (`ContextOptimizer`,
  `FailureLoopDetector`, context helpers, `_tool_specs`,
  `_build_request` never sent messages/tools, and
  `register_design_tools` was called without being imported), so the
  shipped runtime could not complete a run. All restored, with the
  failure-loop nudge and tool pruning wired as designed.
- Verification could silently test stale bytecode: timestamp `.pyc`
  invalidation uses second-resolution mtimes, so two same-size edits
  within one second left tests importing the old code. Verification
  now drops changed files' cached bytecode first.

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
