import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def run_cli(args, *, env, cwd=None, timeout=600):
    """Run the real daemon CLI; return (returncode, stdout, stderr)."""
    result = subprocess.run(
        [sys.executable, "-m", "daemon.cli", *args],
        capture_output=True, text=True, timeout=timeout, env=env, cwd=cwd)
    return result.returncode, result.stdout, result.stderr

def main():
    ws = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="v090-plan-") as home:
        env = dict(os.environ, DAEMON_HOME=home)
        code, out, err = run_cli(
            ["plan", "add a docstring to a.py", "--provider", "demo",
             "--workspace", str(ws)], env=env, timeout=120)
        assert code == 0, out + err
        plans = list((ws / ".daemon" / "plans").glob("*.json"))
        assert plans, "no plan file created"
        plan_id = plans[0].stem
        # --json mode has no approver: the broker gate refuses outright.
        code, out, err = run_cli(
            ["run", "--execute-plan", plan_id, "--profile", "demo",
             "--json", "--workspace", str(ws)], env=env, timeout=120)
        assert code == 1, out + err
        payload = json.loads(out)
        assert payload.get("ok") is False, payload
        assert "not approved" in payload.get("error", ""), payload
        content = (ws / "a.py").read_text(encoding="utf-8")
        assert '"""Add."""' not in content, "denied plan still executed"
    print("denied plan approval: zero tool calls, nothing changed")


main()
