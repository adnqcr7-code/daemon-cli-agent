"""v0.5.0 §5: gated bubblewrap shell sandbox (Linux, opt-in).

Default OFF. When on, availability is probed by really running
bwrap - never faked: missing bwrap / non-Linux / failed probe means
commands run unsandboxed with a one-time loud warning and a doctor
entry. Escape tests run only when a plan is actually available on
this host; the network-namespace tests additionally require the
full level (some container kernels reject --unshare-net, in which
case the sandbox is filesystem-only and says so).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import daemon.sandbox as sandbox_module
from daemon.config import Config
from daemon.doctor import run_doctor
from daemon.errors import SandboxError
from daemon.log import setup_logging
from daemon.sandbox import SandboxPlan, find_outside_hardlinks, plan_for, sandbox_status
from daemon.tools.base import ToolContext
from daemon.tools.shell import RunCommandTool, run_shell


def _config(enabled: bool) -> Config:
    return Config({"sandbox": {"shell": enabled}},
                  user_path=None, project_path=None)


def _run(command: str, cwd: Path, plan=None):
    return asyncio.run(run_shell(command, cwd=cwd, timeout_s=60,
                                 sandbox_plan=plan))


def _plan_or_skip(workspace: Path) -> SandboxPlan:
    plan = plan_for(_config(True), workspace)
    if plan is None:
        pytest.skip("bubblewrap sandbox not actually available "
                    "on this host")
    return plan


# ------------------------------------------------------------ status / argv


def test_sandbox_off_by_default():
    status = sandbox_status(_config(False))
    assert status.level == "off"
    assert plan_for(_config(False), Path("/tmp")) is None
    assert plan_for(None, Path("/tmp")) is None


def test_wrap_argv_shape(tmp_path):
    plan = SandboxPlan(workspace=tmp_path, network_isolated=True)
    argv = plan.wrap(["/bin/echo", "hi"], cwd=tmp_path)
    assert argv[0].endswith("bwrap")
    assert "--unshare-net" in argv
    assert "--ro-bind" in argv
    bind_at = argv.index("--bind")
    assert argv[bind_at + 1] == str(tmp_path)
    assert argv[bind_at + 2] == str(tmp_path)
    assert argv[-3:] == ["--", "/bin/echo", "hi"]

    fs_only = SandboxPlan(workspace=tmp_path, network_isolated=False)
    assert "--unshare-net" not in fs_only.wrap(["/bin/true"],
                                               cwd=tmp_path)


def test_plan_for_matches_probed_status(tmp_path):
    status = sandbox_status(_config(True))
    plan = plan_for(_config(True), tmp_path)
    if status.level in ("off", "unavailable"):
        assert plan is None
    else:
        assert plan is not None
        assert plan.network_isolated == (status.level == "full")


def test_unavailable_warns_once_and_runs_unsandboxed(
        tmp_path, home, monkeypatch):
    monkeypatch.setattr(sandbox_module, "_bwrap_path", lambda: None)
    monkeypatch.setattr(sandbox_module, "_probe_result", None)
    monkeypatch.setattr(sandbox_module, "_warned", set())
    setup_logging(home)
    assert plan_for(_config(True), tmp_path) is None
    assert plan_for(_config(True), tmp_path) is None
    audit_log = (home / "logs" / "audit.log").read_text(encoding="utf-8")
    assert audit_log.count("sandbox-unavailable") == 1


def test_doctor_reports_sandbox_status(tmp_path):
    report = run_doctor(_config(False), tmp_path)
    rows = [row for row in report.rows if row[1] == "shell sandbox"]
    assert rows and "off" in rows[0][2]

    report = run_doctor(_config(True), tmp_path)
    rows = [row for row in report.rows if row[1] == "shell sandbox"]
    assert rows
    status = sandbox_status(_config(True))
    if status.level == "full":
        assert rows[0][0] == "ok"
    elif status.level in ("filesystem", "unavailable"):
        assert rows[0][0] == "warn"  # never a green tick for a gap


# ------------------------------------------------------------ escapes (real)


def test_sandbox_blocks_read_outside_workspace(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "ok.txt").write_text("inside-data", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("topsecret", encoding="utf-8")
    plan = _plan_or_skip(workspace)

    code, output, _ = _run("cat ok.txt", workspace, plan)
    assert code == 0 and "inside-data" in output

    code, output, _ = _run(f"cat {secret}", workspace, plan)
    assert code != 0
    assert "topsecret" not in output


def test_sandbox_blocks_write_outside_allows_inside(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    plan = _plan_or_skip(workspace)

    code, _output, _ = _run("echo wrote > note.txt", workspace, plan)
    assert code == 0
    assert (workspace / "note.txt").read_text(encoding="utf-8") == \
        "wrote\n"

    # A path under the private /tmp tmpfs "succeeds" inside the box
    # but never reaches the host filesystem.
    _run(f"echo pwned > {outside}", workspace, plan)
    assert not outside.exists()
    # A path on the read-only system bind fails outright.
    code, _output, _ = _run("echo pwned > /etc/pwn-marker",
                            workspace, plan)
    assert code != 0
    assert not Path("/etc/pwn-marker").exists()


def test_sandbox_hides_home(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    marker = fake_home / "marker.txt"
    marker.write_text("home-marker", encoding="utf-8")
    monkeypatch.setenv("HOME", str(fake_home))
    plan = _plan_or_skip(workspace)

    code, output, _ = _run(f"cat {marker}", workspace, plan)
    assert code != 0
    assert "home-marker" not in output


def test_run_command_tool_is_sandboxed_when_enabled(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("topsecret", encoding="utf-8")
    _plan_or_skip(workspace)  # skip cleanly when unavailable
    ctx = ToolContext(workspace, _config(True))
    output = asyncio.run(RunCommandTool().run(
        {"command": f"cat {secret}"}, ctx))
    assert output.is_error
    assert "topsecret" not in output.content


# ------------------------------------------- review round 1, R1-6 (hardlink)


def test_find_outside_hardlinks(tmp_path):
    """The scan flags only links with an end outside the workspace:
    a plain file is clean, an internal-only hardlink is clean, a
    link to an outside file is flagged."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert find_outside_hardlinks(workspace) == []
    os.link(workspace / "a.py", workspace / "a-copy.py")
    assert find_outside_hardlinks(workspace) == []
    secret = tmp_path / "outside.txt"
    secret.write_text("outside\n", encoding="utf-8")
    os.link(secret, workspace / "hard-out.txt")
    assert find_outside_hardlinks(workspace) == [Path("hard-out.txt")]


def test_plan_for_refuses_outside_hardlink(tmp_path):
    """R1-6: plan_for refuses (never falls back to unsandboxed) when
    the workspace holds a hardlink to outside content."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    secret = tmp_path / "outside.txt"
    secret.write_text("outside\n", encoding="utf-8")
    os.link(secret, workspace / "hard-out.txt")
    if sandbox_status(_config(True)).level in ("off", "unavailable"):
        pytest.skip("bubblewrap sandbox not actually available "
                    "on this host")
    with pytest.raises(SandboxError, match="hardlinks to files outside"):
        plan_for(_config(True), workspace)


def test_run_command_tool_refuses_outside_hardlink(tmp_path):
    """R1-6: the shell tool — the path the agent actually uses —
    refuses instead of running the command unsandboxed."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    secret = tmp_path / "outside.txt"
    secret.write_text("outside\n", encoding="utf-8")
    os.link(secret, workspace / "hard-out.txt")
    # Availability check without plan_for: plan_for itself refuses
    # on this workspace, which is the behaviour under test.
    if sandbox_status(_config(True)).level in ("off", "unavailable"):
        pytest.skip("bubblewrap sandbox not actually available "
                    "on this host")
    ctx = ToolContext(workspace, _config(True))
    with pytest.raises(SandboxError, match="hardlinks to files outside"):
        asyncio.run(RunCommandTool().run({"command": "echo hi"}, ctx))


# ------------------------------------------------------------ network (full)


def _serve_once(payload: bytes):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):  # silence
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def test_sandbox_network_namespace_isolates_but_keeps_inner_loopback(
        tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    plan = _plan_or_skip(workspace)
    if not plan.network_isolated:
        pytest.skip("network namespace unavailable on this host; "
                    "sandbox is filesystem-only here")
    server, port = _serve_once(b"parent-net")
    try:
        # A server in the PARENT namespace is unreachable.
        code, output, _ = _run(
            "python3 -c \"import urllib.request; "
            f"print(urllib.request.urlopen('http://127.0.0.1:{port}/', "
            "timeout=5).read().decode())\"",
            workspace, plan)
        assert code != 0
        assert "parent-net" not in output
    finally:
        server.shutdown()

    # A server started INSIDE the sandbox answers on its own loopback
    # (the test-suite mock-server pattern).
    inner = (
        "python3 -c \""
        "import threading, urllib.request; "
        "from http.server import BaseHTTPRequestHandler, HTTPServer; "
        "H = type('H', (BaseHTTPRequestHandler,), {"
        "'do_GET': lambda s: (s.send_response(200), s.end_headers(), "
        "s.wfile.write(b'inner-ok')), "
        "'log_message': lambda s, *a: None}); "
        "srv = HTTPServer(('127.0.0.1', 0), H); "
        "threading.Thread(target=srv.serve_forever, daemon=True).start(); "
        "print(urllib.request.urlopen("
        "'http://127.0.0.1:%d/' % srv.server_address[1], "
        "timeout=5).read().decode())\"")
    code, output, _ = _run(inner, workspace, plan)
    assert code == 0
    assert "inner-ok" in output


def test_sandbox_confines_reads_to_the_workspace(tmp_path):
    # Review round 2, V1: round 1 bound the whole host read-only, so
    # only $HOME and /tmp were actually unreadable. The minimal view
    # must hide files ANYWHERE outside the workspace - here, one in
    # /var/tmp (outside the workspace, outside $HOME, outside /tmp)
    # and one in $HOME outside the workspace.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "ok.txt").write_text("inside-data", encoding="utf-8")
    plan = _plan_or_skip(workspace)

    outside_dir = Path("/var/tmp") / f"daemon-v1-{os.getpid()}"
    outside_dir.mkdir(exist_ok=True)
    outside = outside_dir / "secret.txt"
    outside.write_text("OUTSIDE-SECRET", encoding="utf-8")
    home_secret = Path.home() / f".daemon-v1-secret-{os.getpid()}.txt"
    home_secret.write_text("HOME-SECRET", encoding="utf-8")
    try:
        for target, marker in ((outside, "OUTSIDE-SECRET"),
                               (home_secret, "HOME-SECRET")):
            code, output, _ = _run(f"cat {target}", workspace, plan)
            assert code != 0
            assert marker not in output
        # The workspace test pattern still runs: python executes and
        # reads/writes workspace files.
        code, output, _ = _run(
            "python3 -c \"open('made.txt', 'w').write('py-made'); "
            "print(open('ok.txt').read())\"", workspace, plan)
        assert code == 0
        assert "inside-data" in output
        assert (workspace / "made.txt").read_text(
            encoding="utf-8") == "py-made"
    finally:
        outside.unlink(missing_ok=True)
        outside_dir.rmdir()
        home_secret.unlink(missing_ok=True)


def test_wrap_argv_has_no_blanket_bind(tmp_path):
    # The enumerated minimal view, pinned: no "--ro-bind / /" may
    # ever come back.
    plan = SandboxPlan(workspace=tmp_path, network_isolated=False)
    argv = plan.wrap(["/bin/true"], cwd=tmp_path)
    for index, token in enumerate(argv):
        if token in ("--ro-bind", "--bind", "--dev-bind"):
            assert argv[index + 1] != "/" or argv[index + 2] != "/"


def test_probe_exercises_the_real_wrap(tmp_path, monkeypatch):
    # Review round 2, V7: a bwrap shim that happily runs reduced
    # probe-shaped invocations but fails on the real wrap's
    # --unshare-pid must report the sandbox UNAVAILABLE - round 1's
    # reduced probe claimed a working sandbox on exactly such a host.
    real_bwrap = shutil.which("bwrap")
    if real_bwrap is None:
        pytest.skip("bwrap not installed on this host")
    shim_dir = tmp_path / "shimbin"
    shim_dir.mkdir()
    shim = shim_dir / "bwrap"
    shim.write_text(
        "#!/bin/sh\n"
        "for a in \"$@\"; do\n"
        "  if [ \"$a\" = \"--unshare-pid\" ]; then exit 1; fi\n"
        "done\n"
        f"exec {real_bwrap} \"$@\"\n", encoding="utf-8")
    shim.chmod(0o755)
    monkeypatch.setenv(
        "PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(sandbox_module, "_probe_result", None)
    monkeypatch.setattr(sandbox_module, "_warned", set())

    status = sandbox_status(_config(True))
    assert status.level == "unavailable"
    assert plan_for(_config(True), tmp_path) is None


def test_secret_env_still_stripped_inside_sandbox(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    plan = _plan_or_skip(workspace)
    code, output, _ = _run('echo "key=$OPENAI_API_KEY"', workspace, plan)
    assert code == 0
    assert "sk-should-not-leak" not in output


def test_custom_named_secret_stripped_inside_sandbox(tmp_path,
                                                     monkeypatch):
    # Review round 2, V10 (the reviewer's probe): a credential whose
    # name matches no known exact name must not survive into the
    # sandbox either - pattern-based stripping covers it.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("MY_CUSTOM_DEPLOY_TOKEN", "custom-leak-value")
    plan = _plan_or_skip(workspace)
    code, output, _ = _run(
        'echo "custom=$MY_CUSTOM_DEPLOY_TOKEN"', workspace, plan)
    assert code == 0
    assert "custom-leak-value" not in output
