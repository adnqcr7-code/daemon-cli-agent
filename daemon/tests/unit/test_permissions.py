"""Permission engine decision matrix, floors and session overrides."""

from pathlib import Path

from daemon.permissions import Effect, PermissionEngine, Rule


def _engine(tmp_path, **kw):
    return PermissionEngine(Path(tmp_path), **kw)


def test_read_tools_allowed(tmp_path):
    engine = _engine(tmp_path)
    for tool in ("read_file", "glob", "search_files", "git_status",
                 "find_symbols", "diagnostics"):
        decision = engine.check(tool, {"path": "src/x.py"}, read_only=True)
        assert decision.effect is Effect.ALLOW, tool


def test_workspace_writes_allowed_and_checkpointed(tmp_path):
    engine = _engine(tmp_path)
    decision = engine.check("write_file", {"path": "src/app.py",
                                           "content": "x"}, mutating=True)
    assert decision.effect is Effect.ALLOW
    assert decision.reversible is True


def test_outside_workspace_writes_ask(tmp_path):
    engine = _engine(tmp_path)
    decision = engine.check("write_file", {"path": "/etc/passwd",
                                           "content": "x"}, mutating=True)
    assert decision.effect is Effect.ASK
    assert any("outside" in reason for reason in decision.reasons)


def test_deletes_ask(tmp_path):
    engine = _engine(tmp_path)
    assert engine.check("delete_path", {"path": "x"},
                        mutating=True).effect is Effect.ASK


def test_safe_shell_allowed(tmp_path):
    engine = _engine(tmp_path)
    for command in ("pytest -q", "npm test", "make test"):
        decision = engine.check("run_command", {"command": command},
                                mutating=True)
        assert decision.effect is Effect.ALLOW, command


def test_unknown_shell_asks(tmp_path):
    engine = _engine(tmp_path)
    decision = engine.check("run_command", {"command": "weird-binary --go"},
                            mutating=True)
    assert decision.effect is Effect.ASK


def test_dependency_install_asks(tmp_path):
    engine = _engine(tmp_path)
    decision = engine.check("run_command", {"command": "pip install requests"},
                            mutating=True)
    assert decision.effect is Effect.ASK
    assert any("installs" in reason for reason in decision.reasons)


def test_credential_reading_denied(tmp_path):
    engine = _engine(tmp_path)
    decision = engine.check("run_command", {"command": "cat ~/.ssh/id_rsa"},
                            mutating=True)
    assert decision.effect is Effect.DENY


def test_dangerous_rm_denied_even_with_session_override(tmp_path):
    engine = _engine(tmp_path)
    decision = engine.check("run_command", {"command": "rm -rf /"},
                            mutating=True)
    engine.remember(decision.key, Effect.ALLOW)
    after = engine.check("run_command", {"command": "rm -rf /"}, mutating=True)
    assert after.effect is Effect.DENY


def test_user_rule_can_allow_dependency_install(tmp_path):
    engine = _engine(tmp_path, rules=[
        Rule(tool="run_command", command=r"^pip install", effect=Effect.ALLOW,
             origin="user", description="trusted installs")])
    decision = engine.check("run_command", {"command": "pip install requests"},
                            mutating=True)
    assert decision.effect is Effect.ALLOW
    assert any("trusted installs" in reason for reason in decision.reasons)


def test_user_rule_cannot_allow_credential_read(tmp_path):
    engine = _engine(tmp_path, rules=[
        Rule(tool="run_command", command=r"cat ~/.ssh", effect=Effect.ALLOW,
             origin="user")])
    decision = engine.check("run_command", {"command": "cat ~/.ssh/id_rsa"},
                            mutating=True)
    assert decision.effect is Effect.DENY  # hard floor


def test_noninteractive_resolution_fails_closed(tmp_path):
    engine = _engine(tmp_path)  # no approver, no assume_yes
    decision = engine.check("run_command", {"command": "pip install x"},
                            mutating=True)
    resolved = engine.resolve(decision)
    assert resolved.effect is Effect.DENY


def test_assume_yes_resolves_ask(tmp_path):
    engine = _engine(tmp_path, assume_yes=True)
    decision = engine.check("run_command", {"command": "pip install x"},
                            mutating=True)
    assert engine.resolve(decision).effect is Effect.ALLOW


def test_approver_interaction(tmp_path):
    answers = []

    def approver(decision):
        answers.append(decision.summary)
        return True, True

    engine = _engine(tmp_path, approver=approver)
    decision = engine.check("run_command", {"command": "pip install x"},
                            mutating=True)
    resolved = engine.resolve(decision)
    assert resolved.effect is Effect.ALLOW
    assert answers and "pip install x" in answers[0]
    # remembered: second time auto-allowed
    again = engine.check("run_command", {"command": "pip install x"},
                         mutating=True)
    assert again.effect is Effect.ALLOW


def test_web_tools_ask_by_default(tmp_path):
    engine = _engine(tmp_path)
    decision = engine.check("web_search", {"query": "x"}, network=True)
    assert decision.effect is Effect.ASK


def test_mcp_tools_ask_until_trusted(tmp_path):
    engine = _engine(tmp_path)
    decision = engine.check("mcp_server_echo", {"text": "hi"})
    assert decision.effect is Effect.ASK


def test_decision_summary_is_informative(tmp_path):
    engine = _engine(tmp_path)
    decision = engine.check("run_command", {"command": "npm test -- -k login"},
                            mutating=True)
    assert "npm test -- -k login" in decision.summary
