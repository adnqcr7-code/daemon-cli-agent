"""v0.5.0 §3: broker completeness - every decision on the record.

- check() resolutions are ALL audit-logged (ALLOW, ASK, DENY), not
  just ASK outcomes passing through resolve().
- audit() cannot silently no-op in a real run: logging is initialised
  at the CLI entry, a failed audit-log open is recorded in
  audit_status(), doctor FAILS loudly, and session meta carries the
  flag.
- Grants expire: config rules and session grants may carry
  ttl_minutes; expired grants fall back to ASK.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from daemon.agent.runtime import AgentRuntime
from daemon.agent.session import Session, SessionMeta
from daemon.checkpoints import CheckpointManager
from daemon.config import Config
from daemon.context import build_context
from daemon.doctor import run_doctor
from daemon.log import audit_status, setup_logging
from daemon.memory import MemoryStore
from daemon.permissions import Effect, PermissionEngine
from daemon.project import ProjectIndex
from daemon.providers.demo import DEMO_MODEL_ID, DemoAdapter
from daemon.providers.registry import AdapterRegistry
from daemon.tools import default_registry


def _audit_lines(home: Path) -> list[str]:
    log = home / "logs" / "audit.log"
    if not log.exists():
        return []
    return log.read_text(encoding="utf-8").splitlines()


def _decisions(home: Path) -> list[str]:
    return [line for line in _audit_lines(home)
            if "permission-decision" in line]


def test_check_allow_is_audit_logged(home, workspace):
    setup_logging(home)
    engine = PermissionEngine(workspace)
    decision = engine.check("read_file", {"path": "bug.py"},
                            read_only=True)
    assert decision.effect is Effect.ALLOW
    lines = _decisions(home)
    assert len(lines) == 1
    assert "tool=read_file" in lines[0]
    assert "effect=allow" in lines[0]
    assert "target=bug.py" in lines[0]


def test_check_deny_is_audit_logged(home, workspace):
    setup_logging(home)
    engine = PermissionEngine(workspace)
    decision = engine.check("read_credentials", {})
    assert decision.effect is Effect.DENY
    lines = _decisions(home)
    assert len(lines) == 1
    assert "tool=read_credentials" in lines[0]
    assert "effect=deny" in lines[0]
    assert "credential" in lines[0]  # the classifier/floor reason


def test_check_ask_is_audit_logged(home, workspace):
    setup_logging(home)
    engine = PermissionEngine(workspace)
    decision = engine.check("delete_path", {"path": "bug.py"},
                            mutating=True)
    assert decision.effect is Effect.ASK
    lines = _decisions(home)
    assert len(lines) == 1
    assert "effect=ask" in lines[0]


def test_scripted_run_logs_every_tool_decision(home, workspace):
    # The §3 done-when: a scripted run's audit log contains a record
    # for every tool call's decision. The demo run makes exactly two
    # tool calls (read_file, edit_file), both ALLOW.
    setup_logging(home)
    (workspace / "a.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8")
    adapter = AdapterRegistry().create_by_id("demo")
    assert isinstance(adapter, DemoAdapter)
    model = asyncio.run(adapter.list_models())[0]
    session = Session(meta=SessionMeta(workspace=str(workspace),
                                       provider="demo",
                                       model=DEMO_MODEL_ID))
    index = ProjectIndex(workspace)
    index.build()
    runtime = AgentRuntime(
        adapter=adapter, model=model, registry=default_registry(),
        permissions=PermissionEngine(workspace, assume_yes=True),
        session=session, workspace=workspace,
        memory=MemoryStore(workspace),
        checkpoints=CheckpointManager(workspace), index=index)

    result = asyncio.run(runtime.run("add a docstring to a.py"))

    assert result.tool_calls == 2
    lines = _decisions(home)
    by_tool = {}
    for line in lines:
        for tool in ("read_file", "edit_file"):
            if f"tool={tool}" in line:
                by_tool[tool] = line
    assert set(by_tool) == {"read_file", "edit_file"}
    assert all("effect=allow" in line for line in by_tool.values())


# ------------------------------------------------------------- audit status


def test_audit_status_ok_after_setup(home):
    setup_logging(home)
    ok, error = audit_status()
    assert ok is True
    assert error is None


def test_audit_failure_is_recorded_and_doctor_fails(tmp_path, monkeypatch):
    broken = tmp_path / "broken-home"
    (broken / "logs").mkdir(parents=True)
    (broken / "logs" / "audit.log").mkdir()  # a directory: cannot open
    monkeypatch.setenv("DAEMON_HOME", str(broken))
    setup_logging(broken)

    ok, error = audit_status()
    assert ok is False
    assert error

    report = run_doctor(Config.load(tmp_path), tmp_path)
    audit_rows = [row for row in report.rows if row[1] == "audit log"]
    assert audit_rows and audit_rows[0][0] == "fail"
    assert report.failures >= 1


def test_doctor_audit_row_ok_in_healthy_home(home, workspace):
    setup_logging(home)
    report = run_doctor(Config.load(workspace), workspace)
    audit_rows = [row for row in report.rows if row[1] == "audit log"]
    assert audit_rows and audit_rows[0][0] == "ok"


def test_audit_status_not_ok_before_setup(monkeypatch):
    # Review round 2, V9: before setup_logging runs, audit() drops
    # everything - audit_status() must say not-ok, not the round-1
    # initialised True.
    import daemon.log as log_module

    monkeypatch.setattr(log_module, "_audit_ok", None)
    monkeypatch.setattr(log_module, "_audit_error", None)
    ok, error = audit_status()
    assert ok is False
    assert error


def test_subagent_session_meta_records_real_audit_status(
        home, workspace, monkeypatch):
    # V9: subagent session metas asserted audit_ok=true
    # unconditionally in round 1. With audit_status forced broken,
    # the persisted subagent meta must say False.
    import daemon.agent.orchestration as orchestration
    from daemon.agent.orchestration import Orchestrator, SubagentSpec

    monkeypatch.setattr(orchestration, "audit_status",
                        lambda: (False, "forced for test"))
    adapter = AdapterRegistry().create_by_id("demo")
    model = asyncio.run(adapter.list_models())[0]
    orchestrator = Orchestrator(
        adapter_factory=lambda _model: adapter, models={},
        default_model=model, workspace=workspace)
    result = asyncio.run(orchestrator.run_subagent(
        SubagentSpec(role="reviewer", brief="review the workspace")))
    assert result.ok, result.error
    metas = list((home / "sessions").glob("*/meta.json"))
    assert metas, "subagent session was not persisted"
    import json as _json

    meta = _json.loads(metas[0].read_text(encoding="utf-8"))
    assert meta["audit_ok"] is False


def test_session_meta_records_audit_state(home, workspace):
    ctx = build_context(workspace=workspace, interactive=False,
                        profile_id="demo")
    try:
        assert ctx.session.meta.audit_ok is True
        restored = SessionMeta.from_json(ctx.session.meta.to_json())
        assert restored.audit_ok is True
        broken = SessionMeta.from_json({**ctx.session.meta.to_json(),
                                        "audit_ok": False})
        assert broken.audit_ok is False
    finally:
        ctx.close()


# ------------------------------------------------------------------- TTL


def test_session_grant_expires_back_to_ask(workspace):
    engine = PermissionEngine(workspace)
    first = engine.check("delete_path", {"path": "x"}, mutating=True)
    assert first.effect is Effect.ASK
    engine.remember(first.key, Effect.ALLOW, ttl_minutes=60)
    granted = engine.check("delete_path", {"path": "x"}, mutating=True)
    assert granted.effect is Effect.ALLOW

    # Force the grant's window closed: expiry falls back to ASK and
    # the grant is forgotten.
    engine.session_overrides[first.key] = (Effect.ALLOW, time.time() - 1)
    expired = engine.check("delete_path", {"path": "x"}, mutating=True)
    assert expired.effect is Effect.ASK
    assert any("expired" in reason for reason in expired.reasons)
    assert first.key not in engine.session_overrides


def test_session_grant_default_ttl_from_engine_config(workspace):
    engine = PermissionEngine(workspace, session_grant_ttl_minutes=30)
    engine.remember("some-key", Effect.ALLOW)
    _effect, expires_at = engine.session_overrides["some-key"]
    assert expires_at is not None
    assert time.time() + 29 * 60 < expires_at <= time.time() + 30 * 60


def test_session_grant_without_ttl_lasts_the_session(workspace):
    engine = PermissionEngine(workspace)
    engine.remember("some-key", Effect.ALLOW)
    assert engine.session_overrides["some-key"] == (Effect.ALLOW, None)


def test_config_rule_grant_expires_back_to_ask(home, workspace):
    rules = [{"tool": "web_fetch", "effect": "allow", "ttl_minutes": 60,
              "description": "temporary web grant"}]
    engine = PermissionEngine(workspace, rules=rules)
    fresh = engine.check("web_fetch", {"url": "https://example.com"},
                         network=True)
    assert fresh.effect is Effect.ALLOW

    # Age the PERSISTED grant past its window, then build a NEW
    # engine - what every new CLI invocation does. Round 1 measured
    # TTL from engine construction, so the fresh engine silently
    # restarted the window and the grant allowed again (V6).
    import json as _json

    state_path = home / "grant_state.json"
    data = _json.loads(state_path.read_text(encoding="utf-8"))
    assert data["grants"]  # the grant's creation time was persisted
    data["grants"] = {key: time.time() - 61 * 60
                      for key in data["grants"]}
    state_path.write_text(_json.dumps(data), encoding="utf-8")

    restarted = PermissionEngine(workspace, rules=rules)
    expired = restarted.check("web_fetch",
                              {"url": "https://example.com"},
                              network=True)
    assert expired.effect is Effect.ASK
    assert any("expired" in reason for reason in expired.reasons)


def test_config_rule_grant_window_survives_restart_within_ttl(
        home, workspace):
    # The other half of V6: a restart INSIDE the window keeps the
    # grant (it is the same window, not a restarted one).
    rules = [{"tool": "web_fetch", "effect": "allow", "ttl_minutes": 60,
              "description": "temporary web grant"}]
    PermissionEngine(workspace, rules=rules)
    restarted = PermissionEngine(workspace, rules=rules)
    decision = restarted.check("web_fetch",
                               {"url": "https://example.com"},
                               network=True)
    assert decision.effect is Effect.ALLOW


def test_config_rule_nonpositive_ttl_fails_closed(home, workspace):
    rules = [{"tool": "web_fetch", "effect": "allow", "ttl_minutes": 0,
              "description": "stillborn grant"}]
    engine = PermissionEngine(workspace, rules=rules)
    decision = engine.check("web_fetch", {"url": "https://example.com"},
                            network=True)
    assert decision.effect is Effect.ASK


def test_config_rule_grant_bad_timestamp_fails_closed(home, workspace):
    # Review round 3, R2-6: persisted grant timestamps are input,
    # and the loader type-filtered but never sanity-checked them.
    # A 2036 date, NaN, or Infinity (the last two are accepted by
    # json.loads) each made `now >= created + ttl` false forever -
    # a ttl_minutes=60 grant ALLOWed indefinitely. Every unusable
    # value must fail closed: the grant is treated as expired and a
    # fresh engine falls back to ASK.
    import json as _json

    rules = [{"tool": "web_fetch", "effect": "allow", "ttl_minutes": 60,
              "description": "temporary web grant"}]
    bad_values = [
        time.time() + 10 * 365 * 24 * 3600,  # dated ~10 years out
        float("nan"),
        float("inf"),
        "not-a-timestamp",
    ]
    state_path = home / "grant_state.json"
    for bad in bad_values:
        PermissionEngine(workspace, rules=rules)  # a grant persists
        data = _json.loads(state_path.read_text(encoding="utf-8"))
        assert data["grants"]
        data["grants"] = {key: bad for key in data["grants"]}
        state_path.write_text(_json.dumps(data), encoding="utf-8")
        restarted = PermissionEngine(workspace, rules=rules)
        decision = restarted.check(
            "web_fetch", {"url": "https://example.com"}, network=True)
        assert decision.effect is Effect.ASK, bad


def test_config_rule_grant_one_bad_entry_leaves_other_windows(
        home, workspace):
    # R2-6, second half: validation is PER ENTRY. Corrupting one
    # rule's timestamp must neither immortalize it nor reset the
    # other rule's window.
    import json as _json

    fetch = {"tool": "web_fetch", "effect": "allow", "ttl_minutes": 60,
             "description": "fetch grant"}
    search = {"tool": "web_search", "effect": "allow",
              "ttl_minutes": 60, "description": "search grant"}
    PermissionEngine(workspace, rules=[fetch])
    state_path = home / "grant_state.json"
    fetch_keys = set(_json.loads(
        state_path.read_text(encoding="utf-8"))["grants"])
    assert len(fetch_keys) == 1
    PermissionEngine(workspace, rules=[fetch, search])
    data = _json.loads(state_path.read_text(encoding="utf-8"))
    assert len(data["grants"]) == 2
    # Age the fetch grant 30 minutes (inside its window); corrupt
    # the search grant's timestamp into the far future.
    data["grants"] = {
        key: (time.time() - 30 * 60 if key in fetch_keys
              else time.time() + 10 * 365 * 24 * 3600)
        for key in data["grants"]}
    state_path.write_text(_json.dumps(data), encoding="utf-8")

    restarted = PermissionEngine(workspace, rules=[fetch, search])
    fetch_decision = restarted.check(
        "web_fetch", {"url": "https://example.com"}, network=True)
    assert fetch_decision.effect is Effect.ALLOW  # window intact
    search_decision = restarted.check(
        "web_search", {"query": "daemon"}, network=True)
    assert search_decision.effect is Effect.ASK  # failed closed


def test_grant_state_save_merges_concurrent_engines(home, workspace):
    # Review round 4, R3-4 (the reviewer's interleaved repro):
    # grant-state saves used to be last-writer-wins full-file
    # writes. Engine B loads its snapshot (its rules lack grant G)
    # BEFORE engine A records G and saves; B's later save - the
    # sanitize-rewrite and first-seen paths use the same save -
    # wrote B's stale dict, G vanished from grant_state.json, and
    # the next engine re-recorded it as first-seen-now, restarting
    # a half-spent TTL window. The save is now reload-merge-write
    # under the store lock: both engines' grants survive, and G
    # keeps A's original timestamp.
    import json as _json

    fetch = {"tool": "web_fetch", "effect": "allow", "ttl_minutes": 60,
             "description": "fetch grant"}
    search = {"tool": "web_search", "effect": "allow",
              "ttl_minutes": 60, "description": "search grant"}
    engine_b = PermissionEngine(workspace, rules=[search])
    snapshot_b = dict(engine_b._grant_created)  # lacks the fetch grant
    PermissionEngine(workspace, rules=[fetch])  # engine A: records G
    state_path = home / "grant_state.json"
    after_a = _json.loads(state_path.read_text(encoding="utf-8"))["grants"]
    assert len(after_a) == 2  # B's search grant + A's fetch grant

    # B saves its STALE snapshot - the exact lost-update interleave.
    engine_b._save_grant_state(snapshot_b)
    merged = _json.loads(state_path.read_text(encoding="utf-8"))["grants"]
    assert merged == after_a  # nothing dropped, no window restarted

    # And the surviving grant still ALLOWs in a fresh engine: its
    # window is A's original one, not a restarted one.
    restarted = PermissionEngine(workspace, rules=[fetch])
    decision = restarted.check(
        "web_fetch", {"url": "https://example.com"}, network=True)
    assert decision.effect is Effect.ALLOW


def test_grant_state_lock_held_by_another_process_does_not_hang(
        home, workspace):
    # Review round 5, R4-3: the R3-4 store lock was a BLOCKING
    # flock with no timeout - a live-but-wedged holder hung
    # PermissionEngine construction >10s, against the module's
    # best-effort contract. Acquisition is now bounded (~2s); on
    # timeout the save proceeds WITHOUT the lock (load-merge-write
    # best-effort) with a warning, and checks still work.
    import subprocess
    import sys

    lock_path = home / "grant_state.json.lock"
    ready = home / "holder-ready"
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl, sys, time\n"
         "from pathlib import Path\n"
         "fd = open(sys.argv[1], 'w')\n"
         "fcntl.flock(fd, fcntl.LOCK_EX)\n"
         "Path(sys.argv[2]).write_text('held')\n"
         "time.sleep(60)\n",
         str(lock_path), str(ready)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if ready.exists():
                break
            time.sleep(0.05)
        assert ready.exists(), "lock holder never acquired"

        fetch = {"tool": "web_fetch", "effect": "allow",
                 "ttl_minutes": 60, "description": "fetch grant"}
        started = time.monotonic()
        engine = PermissionEngine(workspace, rules=[fetch])
        elapsed = time.monotonic() - started
        assert elapsed < 5.0, f"construction hung {elapsed:.1f}s"

        # Checks still work, and the grant was recorded anyway via
        # the unlocked best-effort save.
        decision = engine.check(
            "web_fetch", {"url": "https://example.com"}, network=True)
        assert decision.effect is Effect.ALLOW
        assert (home / "grant_state.json").exists()
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_config_rule_ttl_parsing_and_non_allow_rules_unaffected(workspace):
    rules = [{"tool": "delete_path", "effect": "ask", "ttl_minutes": 1,
              "description": "policy, not a grant"}]
    engine = PermissionEngine(workspace, rules=rules)
    assert engine.rules[-1].ttl_minutes == 1.0
    # An ASK rule is policy, not a grant: TTL does not retire it (and
    # the outcome is ASK either way here).
    decision = engine.check("delete_path", {"path": "x"}, mutating=True)
    assert decision.effect is Effect.ASK
