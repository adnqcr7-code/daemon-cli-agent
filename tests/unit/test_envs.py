"""Tests for v0.9.0 §2.4 environments: state bundles, byte-identical
reset, per-env isolation, DENY-floor validation, env in the event log."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from daemon.agent.reflex import ReflexEntry, ReflexStore
from daemon.cli import main
from daemon.config import Config, active_env_name
from daemon.envs import EnvError, create_env, reset_env, use_env, validate_fragment
from daemon.memory import MemoryStore


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.py").write_text("def add(a, b):\n    return a + b\n",
                                    encoding="utf-8")
    return workspace


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ------------------------------------------------------------ create / list


def test_create_list_show(tmp_path) -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["env", "create", "dev",
                                  "--template", "developer"])
    assert result.exit_code == 0, result.output
    assert "created from template developer" in result.output
    env_path = Path(result.output.split("fragment: ")[1].split("\n")[0]).parent
    assert (env_path / "config.toml").exists()
    assert "max_cost_usd = 10.0" in (env_path / "config.toml").read_text("utf-8")
    manifest = json.loads((env_path / "manifest.json").read_text("utf-8"))
    assert manifest["template"] == "developer"
    assert manifest["files"]["memory.jsonl"] == _sha(
        env_path / "snapshot" / "state" / "memory.jsonl")
    assert manifest["files"]["reflex.json"] == _sha(
        env_path / "snapshot" / "state" / "reflex.json")
    listed = runner.invoke(main, ["env", "list"])
    assert "dev" in listed.output and "developer" in listed.output
    shown = runner.invoke(main, ["env", "show", "dev"])
    assert shown.exit_code == 0, shown.output
    assert "template:    developer" in shown.output
    assert "memory.jsonl" in shown.output


def test_create_rejects_bad_names_and_duplicates() -> None:
    with pytest.raises(EnvError):
        create_env("../escape")
    with pytest.raises(EnvError):
        create_env("UPPER")
    create_env("dev")
    with pytest.raises(EnvError, match="already exists"):
        create_env("dev")
    with pytest.raises(EnvError, match="unknown template"):
        create_env("other", template="nope")


# ------------------------------------------------------- use + config layer


def test_use_scopes_config_and_deactivates(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    create_env("dev", template="developer")
    assert Config.load(workspace).get("limits.max_cost_usd") is None
    assert use_env("dev") == "dev"
    assert active_env_name() == "dev"
    # The env fragment layers in: cost cap 10.0.
    assert Config.load(workspace).get("limits.max_cost_usd") == 10.0
    # The project file is the most local layer and still wins.
    (workspace / ".daemon").mkdir()
    (workspace / ".daemon" / "config.toml").write_text(
        "[limits]\nmax_cost_usd = 3.0\n", encoding="utf-8")
    assert Config.load(workspace).get("limits.max_cost_usd") == 3.0
    # Deactivate: the fragment drops out.
    assert use_env(None) is None
    assert Config.load(workspace).get("limits.max_cost_usd") == 3.0
    (workspace / ".daemon" / "config.toml").unlink()
    assert Config.load(workspace).get("limits.max_cost_usd") is None


def test_use_refuses_missing_env() -> None:
    with pytest.raises(EnvError, match="no environment"):
        use_env("ghost")


# ------------------------------------------------------------- byte-identical


def test_reset_is_byte_identical(tmp_path) -> None:
    _workspace(tmp_path)
    info = create_env("dev")
    env_path = info.path
    manifest = json.loads((env_path / "manifest.json").read_text("utf-8"))
    state = env_path / "state"
    # Mutate the live state: junk in memory, a new workspace file,
    # a reflex entry.
    (state / "memory.jsonl").write_text(
        '{"id": "junk", "content": "junk"}\n', encoding="utf-8")
    extra = state / "workspaces" / "abcdef123456"
    extra.mkdir(parents=True)
    (extra / "memory.jsonl").write_text("junk\n", encoding="utf-8")
    (state / "reflex.json").write_text('{"entries": [{"id": "x"}]}\n',
                                       encoding="utf-8")
    restored = reset_env("dev")
    assert sorted(restored) == ["memory.jsonl", "reflex.json"]
    for rel, digest in manifest["files"].items():
        assert _sha(state / rel) == digest
    assert not (state / "workspaces").exists()
    assert (state / "memory.jsonl").read_bytes() == b""
    assert json.loads((state / "reflex.json").read_text("utf-8")) \
        == {"entries": []}


def test_reset_refuses_corrupt_snapshot(tmp_path) -> None:
    info = create_env("dev")
    state = info.path / "state"
    (state / "memory.jsonl").write_text("live\n", encoding="utf-8")
    (info.path / "snapshot" / "state" / "memory.jsonl").write_bytes(b"evil")
    with pytest.raises(EnvError, match="does not match the manifest"):
        reset_env("dev")
    # The live state is untouched by the refused reset.
    assert (state / "memory.jsonl").read_text("utf-8") == "live\n"


# ------------------------------------------------------------ isolation


def test_memory_and_reflex_do_not_leak_across_envs(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    env_a = create_env("env-a")
    env_b = create_env("env-b")
    store_a = MemoryStore(workspace, env_dir=env_a.path)
    store_b = MemoryStore(workspace, env_dir=env_b.path)
    entry = store_a.add("fact only in A", kind="fact", scope="user")
    assert entry.id in {item.id for item in store_a.entries()}
    assert entry.id not in {item.id for item in store_b.entries()}
    project_entry = store_a.add("project fact A", kind="fact",
                                scope="project")
    assert project_entry.id not in {
        item.id for item in store_b.entries()}
    # Reflex: a certified entry in A never appears in B.
    reflex_a = ReflexStore(workspace, env_dir=env_a.path)
    reflex_b = ReflexStore(workspace, env_dir=env_b.path)
    assert reflex_a.path != reflex_b.path
    reflex_a.add(ReflexEntry(
        result="a.py defines add(a, b)",
        pattern={"files": ["a.py"]}, kind="structural",
        status="certified"))
    assert len(reflex_a.entries()) == 1
    assert reflex_b.entries() == []
    # And neither env sees the other's state after a reset of A.
    reset_env("env-a")
    assert MemoryStore(workspace, env_dir=env_a.path).entries() == []
    assert entry.id not in {item.id for item in store_b.entries()}


# ------------------------------------------------------- DENY floor validation


def test_fragment_deny_floor() -> None:
    # The researcher template (deny rules only) validates.
    validate_fragment({"permissions": {"rules": [
        {"tool": "delete_path", "effect": "deny"}]}})
    # An ALLOW rule matching a denied command is rejected.
    with pytest.raises(EnvError, match="DENY floor"):
        validate_fragment({"permissions": {"rules": [
            {"tool": "run_command", "effect": "allow",
             "command": "rm -rf /"}]}})
    with pytest.raises(EnvError, match="DENY floor"):
        validate_fragment({"permissions": {"rules": [
            {"tool": "run_command", "effect": "allow",
             "command": "sudo"}]}})
    # An ALLOW on a credential tool is rejected.
    with pytest.raises(EnvError, match="credential"):
        validate_fragment({"permissions": {"rules": [
            {"tool": "read_credentials", "effect": "allow"}]}})
    # A narrow, safe ALLOW passes.
    validate_fragment({"permissions": {"rules": [
        {"tool": "run_command", "effect": "allow",
         "command": "pytest"}]}})


def test_use_revalidates_hand_edited_fragment(tmp_path) -> None:
    create_env("dev")
    from daemon.config import envs_root

    fragment = envs_root() / "dev" / "config.toml"
    fragment.write_text(
        fragment.read_text("utf-8")
        + '\n[[permissions.rules]]\ntool = "run_command"\n'
          'effect = "allow"\ncommand = "rm -rf /"\n',
        encoding="utf-8")
    with pytest.raises(EnvError, match="DENY floor"):
        use_env("dev")


# ------------------------------------------- review round 1, R1-4 (env leak)


def test_fragment_rejects_outside_path_allow() -> None:
    """R1-4: a path ALLOW reaching outside the workspace is rejected
    — the reviewer's case (read_file on /tmp/*) and its siblings."""
    for pattern in ("/tmp/*", "/etc/*", "/home/other-user/*",
                    "~/.ssh/*"):
        with pytest.raises(EnvError, match="outside the workspace"):
            validate_fragment({"permissions": {"rules": [
                {"tool": "read_file", "effect": "allow",
                 "path": pattern}]}})
    # A relative traversal is rejected too.
    with pytest.raises(EnvError, match=r"\.\."):
        validate_fragment({"permissions": {"rules": [
            {"tool": "read_file", "effect": "allow",
             "path": "../secret.txt"}]}})
    # A relative path inside the workspace is fine.
    validate_fragment({"permissions": {"rules": [
        {"tool": "read_file", "effect": "allow", "path": "src/*"}]}})


def test_fragment_rejects_ask_command_allow() -> None:
    """R1-4: an ALLOW matching an ask-level command is rejected —
    the ASK floor is still a floor."""
    for command in ("npm install", "rm -rf /tmp/foo",
                    "git push --force", "git reset --hard"):
        with pytest.raises(EnvError, match="ASK floor"):
            validate_fragment({"permissions": {"rules": [
                {"tool": "run_command", "effect": "allow",
                 "command": command}]}})
    # A safe command still passes.
    validate_fragment({"permissions": {"rules": [
        {"tool": "run_command", "effect": "allow",
         "command": "pytest"}]}})


def test_env_origin_rule_cannot_relax_floor(tmp_path) -> None:
    """R1-4: even a fragment that somehow carries a floor-widening
    rule cannot widen the floor at runtime — origin "env" never
    relaxes the outside-workspace ASK. The user-origin control shows
    the same rule WOULD relax it, so the origin is doing the work."""
    from daemon.permissions import PermissionEngine

    workspace = _workspace(tmp_path)
    leaky = {"tool": "read_file", "path": "/tmp/*", "effect": "allow",
             "description": "env fragment: allow reading /tmp"}
    env_engine = PermissionEngine(
        workspace, rules=[{**leaky, "origin": "env"}])
    decision = env_engine.check(
        "read_file", {"path": "/tmp/outside-secret.txt"})
    assert decision.effect.value == "ask"
    user_engine = PermissionEngine(
        workspace, rules=[{**leaky, "origin": "user"}])
    decision = user_engine.check(
        "read_file", {"path": "/tmp/outside-secret.txt"})
    assert decision.effect.value == "allow"


def test_config_load_skips_invalid_env_fragment(tmp_path) -> None:
    """R1-4: a fragment hand-edited after `env use` to breach the
    floor is caught again on every Config.load — the layer is
    skipped, so the leaky rule never reaches the broker."""
    from daemon.config import Config, envs_root

    workspace = _workspace(tmp_path)
    create_env("dev")
    use_env("dev")
    fragment = envs_root() / "dev" / "config.toml"
    fragment.write_text(
        fragment.read_text("utf-8")
        + '\n[[permissions.rules]]\ntool = "read_file"\n'
          'effect = "allow"\npath = "/tmp/*"\n',
        encoding="utf-8")
    config = Config.load(workspace)
    rules = config.get("permissions.rules") or []
    assert not any(isinstance(rule, dict)
                   and rule.get("path") == "/tmp/*" for rule in rules)


def test_config_load_tags_env_rules(tmp_path) -> None:
    """R1-4: env rules reach the merged config tagged origin "env"
    — the tag the broker keys the floor on."""
    from daemon.config import Config

    workspace = _workspace(tmp_path)
    create_env("res", template="researcher")
    use_env("res")
    config = Config.load(workspace)
    rules = config.get("permissions.rules") or []
    assert rules, "researcher fragment rules did not load"
    assert all(isinstance(rule, dict)
               and rule.get("origin") == "env" for rule in rules)
    assert {rule.get("tool") for rule in rules} == {
        "delete_path", "git_commit"}


# ------------------------------------------------------------ env in the log


def test_run_records_env_in_event_log(tmp_path) -> None:
    """A run under an active env records it in run.started."""
    import asyncio

    from daemon.config import Config
    from daemon.context import build_context
    from daemon.events import EventStore

    workspace = _workspace(tmp_path)
    create_env("dev", template="developer")
    use_env("dev")
    config = Config.load(workspace)
    assert config.get("limits.max_cost_usd") == 10.0
    ctx = build_context(workspace=workspace, config=config,
                        interactive=False, profile_id="demo")
    assert ctx.env_name == "dev"
    assert ctx.session.meta.env == "dev"
    runtime = ctx.build_runtime(ui=None)
    assert runtime.env_name == "dev"
    try:
        asyncio.run(runtime.run("add a docstring to a.py"))
    finally:
        ctx.close()
    events = EventStore().read(ctx.session.meta.id)
    started = [event for event in events if event.type == "run.started"]
    assert started, "no run.started event"
    assert started[0].payload.get("env") == "dev"
