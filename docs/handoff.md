# Release handoff — daemon-cli-agent v0.9.0

_Written 2026-11-25, Phase 5 of the v0.9.0 plan
(`docs/specs/v0.9.0-plan.md`). Phases 1–4 complete; hostile review round 4
passed with zero FAILs. **Nothing in this release is pushed** — merge to
main, push, and the GitHub Release all wait for the user's explicit
confirmation (session rule)._

## State

- **Release branch:** `v090-cli`, HEAD at the Phase 5 docs commit (this
  file). Tag **`v0.9.0` created locally** at that HEAD — annotated,
  message "Release v0.9.0 — testable CLI: event kernel, selftest, front
  door, plan mode, environments, diffs/undo, pluggable sandbox".
- **`origin/main` is unchanged:** still `6831d65` (v0.4.1). v0.9.0 has
  NOT been merged to main. The v0.4.1 tag and the v0.5.0 branch are
  untouched, still held locally as before.
- **Version:** 0.9.0 in `pyproject.toml` and `daemon/__init__.py`
  (verified: `daemon --version` prints `daemon 0.9.0`).
- **Suite:** 680 collected — 679 pass, 1 skip, 0 fail. Ruff clean.
- **Evals:** 12/12 pass offline (`daemon eval --target demo=demo-scripted
  --tasks daemon/evals/v090_tasks.json`).
- **Hostile review:** 4 rounds (`docs/specs/v0.9.0-review.md`). Round 1:
  4 FAILs (plan bypass, env leak, sandbox hardlink escape, dead code).
  Round 2: 1 FAIL remaining (plan bypass, narrowed). Round 3: 1 FAIL
  (REPL `/edit-plan` never dispatched). Round 4: **0 FAILs — release
  unblocked.** Every fix round re-ran the reviewer's exact reproduction
  to prove refusal before the next review.
- **Wheel:** `/tmp/daemon-dist/daemon_agent-0.9.0-py3-none-any.whl`
  (336,668 bytes, sha256
  `e93ae5e156e9153ca85a7e533fc8df8a8adb1604f1528e2469da4638a21f3d16`).
  Rebuild with `python -m pip wheel . --no-deps -w /tmp/daemon-dist`
  if `/tmp` is wiped by a host swap.

## What shipped in v0.9.0

- **Phase 1 — event kernel:** append-only SQLite store
  (`~/.daemon/events.db`, 12 event types incl. `plan.edited`),
  projections for messages/plan/checkpoints, `daemon replay <run_id>`
  (deterministic, byte-identical across invocations; tampering flagged).
- **Phase 2 — six deliverables:** `daemon selftest` (8 legs, verdict
  PASS/PARTIAL/FAIL, exits 0/1/2); front door (credential-free REPL
  offers the labeled demo, `daemon init` scaffolds `.daemon/` +
  `AGENTS.md` + `config.toml`); plan mode (persist + stop, broker
  approval anchored to the kernel creation record, recorded edit path
  `daemon plan --edit` / REPL `/edit-plan`, `--yes` never substitutes);
  environments (`daemon env create|use|reset|list|show`, byte-identical
  reset, per-env memory/Reflex scoping); diffs at ASK (200-line/~8k-token
  budget, `… N more lines`) + `daemon undo` through kernel checkpoint
  events; pluggable sandbox (`SandboxBackend`, container-first, bwrap
  re-homed, Seatbelt/Landlock stubs).
- **Phase 3 — cross-cutting:** per-provider capability manifest
  (DeepSeek reasoning traces stored separately, never replayed);
  malformed tool-call repair ladder (validate → repair → retry →
  plan-only fallback); cache discipline (`PrefixGuard`, system prompt
  frozen per run); 12 new eval tasks.

## Selftest on this machine — verbatim (2026-11-25)

```
$ daemon selftest
[PASS] doctor         20 checks, 3 warning(s)
[PASS] demo run       demo run completed, docstring added, verification passed
[PASS] offline eval   1/1 eval tasks passed
[PASS] repomap        a.py ranked #1 of 1
[PASS] memory         add/read/invalidate round-trip
[PASS] worktree       create/list/remove round-trip
[PASS] sandbox probe  denied read blocked, secret env absent, hardlink read refused, trivial command ran (bwrap: filesystem isolation only (no network isolation on this host); filesystem isolation only (network namespace unavailable on this host))
[SKIP] model leg      no credentials configured
selftest: PARTIAL (7 pass, 1 skip, 0 fail)
exit=1
```

Verdict, stated plainly: **PARTIAL is the honest result here** — the
model leg SKIPs because this machine has no provider credentials, and
the verdict rules never print PASS on a SKIP. Nothing failed. A PASS
verdict requires a credential-bearing machine; the exact command is:

```
daemon auth add            # paste a provider key (or: daemon auth add --provider ollama)
daemon selftest            # expect: PASS, exit 0, model leg [PASS]
```

## Container-backend limitation — read before claiming it

The container backend is **not proven live on this host**. Podman is
not installed system-wide here and the sandbox kernel rejects container
namespaces (crun fails on SIOCSIFFLAGS, runc on setns; even
`--network=host` wedges). Selection degrades to bwrap and says so in
`daemon selftest` and `daemon doctor`. The container argv is covered by
fake-CLI tests only. **Live container proof needs a machine with a
working Podman/Docker runtime plus `sandbox.container_image` set**
(e.g. `localhost/veil-os:latest` once podman is restored). Until then,
do not claim the container backend as verified.

## README state

- The install section now leads with the v0.9.0 GitHub Release wheel
  and carries the same one-line note as v0.4.1: PyPI publishing is
  pending trusted-publisher setup. The release-wheel link goes live
  when the v0.9.0 tag is pushed and the GitHub Release is created with
  the wheel attached.
- **PyPI state: unchanged** — `daemon-agent` is still not on PyPI.
  `.github/workflows/publish.yml` (Trusted Publishing / OIDC,
  environment `pypi`, triggers on a published GitHub Release) is ready.
  The only blocker is the one-time trusted-publisher registration,
  which only the account owner can do (see "Next action" below).

## Next action — the user's, exactly

1. **Decide the push order.** Held locally, in dependency order:
   - v0.4.1 tag (`6831d65`) + README/docs commits (`8a40f29`,
     `c006896`) + GitHub Release v0.4.1 with its wheel — from the
     2026-11-24 reconciliation, still unanswered.
   - v0.5.0 branch (`v050-ultimate-harness`, 22 commits) — still
     unpushed.
   - **v0.9.0 branch (`v090-cli`) + tag `v0.9.0`** — this release.
     It is built on top of the v0.5.0 tip, so merging v0.9.0 to main
     brings the v0.5.0 content with it; the v0.4.1 tag/release is a
     separate decision (the release must target `6831d65` with the
     v0.4.1 wheel attached, BEFORE v0.5.0/v0.9.0 lands on main, if the
     v0.4.1 release is to happen at all).
2. **PyPI trusted publisher (five minutes, only the owner can do it):**
   open https://pypi.org/manage/account/publishing/ and add a **pending
   publisher** with exactly:
   - PyPI Project Name: `daemon-agent`
   - Owner: `adnqcr7-code`
   - Repository name: `daemon-cli-agent`
   - Workflow name: `publish.yml`
   - Environment name: `pypi`
   Register the pending publisher *before* publishing the release, and
   the release will also trigger `publish.yml` to publish v0.9.0 to
   PyPI.
3. After the tag is pushed, create the **GitHub Release v0.9.0** and
   attach `daemon_agent-0.9.0-py3-none-any.whl` (see "Wheel" above;
   rebuild if `/tmp` was wiped).

---

# Previous release — v0.4.1 (held, unpushed)

_Written 2026-11-24 during the release-reconciliation session. Steps 0–5 of
the reconciliation plan, in order, each verified before the next began._

## State

- **Current main SHA:** `6831d65` ("v0.4.1: Windows CI fixes") — the tree on
  `origin/main` is v0.4.1 in `pyproject.toml` and `daemon/__init__.py`.
- **CI:** green — run #34, all 9 matrix jobs passed (ubuntu / windows /
  macOS × Python 3.11 / 3.12 / 3.13).
- **Tier 1 / runtime fix:** on `origin/main`. Proven by content, not by SHA:
  the tree of the top Tier 1 commit on origin/main (`469e4e7`) is
  byte-identical to the local `tier1-v0.3.0` commit (`9fb813f`) that contains
  the runtime fix (`git diff` between the two trees is empty). No Tier 1 work
  exists only on a local branch.

## Tag

- `v0.4.1` annotated tag **created locally** at `6831d65` with message
  "Release v0.4.1 — runtime fix, Tier 1 (worktrees, repomap, verified
  memory, hard verify loop)". Note the tag also covers the Reflex Phase 1
  work shipped as v0.4.0, because no v0.4.0 tag was ever created.
- **Not yet pushed** — pushing waits for the user's explicit confirmation
  (session rule). Existing remote tags: only `v0.2.0`. No retroactive
  v0.3.0 tag was created; a missing intermediate tag is harmless.

## Secret triage (Step 1) — CLEAR

- `git log -S "sk-ant" --oneline --all` flags three commits from
  2026-10-31 (`0eb19dd`, `60c2de4` "Add files via upload"; `128052c`
  "Delete daemon directory") — early web uploads of the same benign files.
- A history-wide scan for a real Anthropic key shape
  (`sk-ant-api03-` + 40 or more key characters) across **every commit and
  the working tree returns zero matches**. Every `sk-ant` occurrence is
  6–37 characters: the secret-redactor's detection patterns
  (`daemon/security.py`), the provider prefix (`daemon/providers/catalog.py`),
  documentation (`docs/authentication.md`), and fake keys in test fixtures.
- **No live key is committed. No rotation is needed and no history purge
  (filter-repo) is needed.** The key itself was never printed anywhere in
  this process — only token lengths were inspected.

## README state

- Was wrong: it presented `pipx install "daemon-agent[...]"` (PyPI) as the
  primary install path. Fixed in a local commit ("Step 3: …"): the install
  section now leads with the v0.4.1 GitHub Release wheel and carries a
  one-line note that PyPI publishing is pending trusted-publisher setup.
  The release-wheel link goes live when the v0.4.1 tag is pushed and the
  GitHub Release is created with the wheel attached. **Commit not yet
  pushed** (same confirmation gate as the tag).

## PyPI state

- `daemon-agent` on PyPI: **nothing published** —
  `https://pypi.org/pypi/daemon-agent/json` returns 404 and
  `pip index versions daemon-agent` finds no distribution.
- `.github/workflows/publish.yml` is ready (Trusted Publishing / OIDC,
  environment `pypi`, triggers on a published GitHub Release). The only
  blocker is the one-time trusted-publisher registration, which only the
  account owner can do (see "Next action" below).

## Fresh-venv smoke test (Step 4) — output

Wheel built from the current tree (`daemon_agent-0.4.1-py3-none-any.whl`),
installed into a fresh venv (`/tmp/daemon-smoke`), exercised in a scratch
git repo (`/tmp/scratch`, one file `a.py` containing `x=1`):

```
$ daemon version
daemon 0.4.1 (Python 3.12.3)

$ daemon doctor
16 checks: 9 ok, 5 warnings, 0 failures
[!!]   profiles               none configured - run 'daemon auth add'

$ daemon repomap "add a docstring"
a.py

$ daemon memory list
Memory is empty.

$ daemon run "add a docstring to a.py" --worktree --yes
worktree: /tmp/scratch/.daemon/worktrees/add-a-docstring-to-a-py-7e6610 (branch daemon/add-a-docstring-to-a-py-7e6610)
error: No provider credentials configured. Run 'daemon auth add' and paste an API key - Daemon will detect the provider.
hint: Local models: 'daemon auth add --provider ollama' needs no key.

$ git worktree list        (after the run — worktree was created and listed)
$ git worktree remove …    (removed cleanly; branch deleted)
```

Verdict, stated plainly:

- Install from the built wheel in a clean venv: **works**.
- `daemon version`, `daemon repomap`, `daemon memory`: **respond
  correctly**.
- Worktree create / list / remove: **works** — the isolated worktree and
  its `daemon/<task>` branch were created before the run stopped.
- A full model run: **did not complete in this environment** — it stopped
  at "No provider credentials configured" because this machine has no
  provider API key and no local Ollama. That is an environment
  precondition, not a code failure: the error is clean and actionable, and
  every subsystem that does not need a model verified live. Completing a
  model run needs one `daemon auth add` on a machine with a key.

## Next action — the user's, exactly

1. **PyPI trusted publisher (five minutes, only the owner can do it):**
   open https://pypi.org/manage/account/publishing/ and add a **pending
   publisher** with exactly:
   - PyPI Project Name: `daemon-agent`
   - Owner: `adnqcr7-code`
   - Repository name: `daemon-cli-agent`
   - Workflow name: `publish.yml`
   - Environment name: `pypi`
2. **Confirm the pushes** held by this session's rules: the `v0.4.1` tag,
   the Step 3 README commit, and this handoff commit.
3. After the tag is pushed, create the **GitHub Release v0.4.1** and
   attach `daemon_agent-0.4.1-py3-none-any.whl`. Order matters: register
   the pending publisher (step 1) *before* publishing the release, and
   the release will also trigger `publish.yml` to publish v0.4.1 to PyPI.
