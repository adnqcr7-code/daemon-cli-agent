# Daemon — project status

_Last updated 2026-11-25 (v0.9.0 Phase 5, release prep; see
`docs/handoff.md` for the full record)._

- **Version:** 0.9.0 — on local branch `v090-cli` (built on the v0.5.0
  tip), tag `v0.9.0` created locally, **not yet pushed**. `origin/main`
  is still `6831d65` (v0.4.1); nothing is merged to main.
- **Shipped in v0.9.0:** minimal append-only event kernel (SQLite,
  12 event types, `daemon replay`); `daemon selftest` (8 legs, verdict
  PASS/PARTIAL/FAIL); front door (credential-free REPL demo offer,
  `daemon init`); plan mode (broker approval anchored to the kernel
  creation record, recorded edit path); environments (byte-identical
  reset, per-env scoping); diffs at ASK + `daemon undo` through kernel
  checkpoint events; pluggable sandbox (container-first, bwrap
  re-homed); capability manifest; tool-call repair ladder; cache
  discipline; 12 new eval tasks.
- **Verification:** suite 680 collected — 679 pass, 1 skip, 0 fail;
  ruff clean; evals 12/12 offline. Hostile review round 4: **0 FAILs,
  release unblocked** (`docs/specs/v0.9.0-review.md`).
- **Selftest on this machine: PARTIAL** (7 pass, 1 skip, 0 fail) — the
  model leg SKIPs with no credentials configured. Never PASS on an
  untested leg: a PASS verdict needs a credential-bearing machine
  (`daemon auth add`, then `daemon selftest`).
- **Container backend: not proven live on this host** — Podman is not
  installed system-wide and the kernel rejects container namespaces;
  selection degrades to bwrap and says so. Live container proof needs
  a machine with a working Podman/Docker runtime plus
  `sandbox.container_image` set.
- **Release state:** `v0.9.0` tag local-only, awaiting owner
  confirmation to merge `v090-cli` to main, push, and create the
  GitHub Release with the wheel attached. Nothing is published on
  PyPI — `publish.yml` (trusted publishing) is ready; the one-time
  pending-publisher registration on pypi.org is the owner's next step
  (exact fields in `docs/handoff.md`).
- **Also still held (unchanged):** `v0.4.1` tag at `6831d65` + README/
  docs commits (`8a40f29`, `c006896`) + the v0.5.0 branch
  (`v050-ultimate-harness`, 22 commits) — all local, unpushed. Merging
  v0.9.0 to main brings the v0.5.0 content with it; the v0.4.1
  release is a separate decision.
- **Secrets:** triaged 2026-11-24 — no real API key exists in the tree or
  any history; all `sk-ant` matches are redactor patterns and test
  fixtures.
- **Known gates for future work:** Reflex Phase 2 must capture structured
  tool metadata instead of parsing tool-output text before any new
  capability is built on it (review #5, finding R5-1). Hard signals stay
  off until then.
