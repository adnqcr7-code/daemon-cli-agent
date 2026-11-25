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
        plan = json.loads(plans[0].read_text(encoding="utf-8"))
        assert plan.get("tasks"), plan
        assert any("a.py" in (task.get("files") or [])
                   for task in plan["tasks"]), plan["tasks"]
        content = (ws / "a.py").read_text(encoding="utf-8")
        assert '"""Add."""' not in content, "plan executed instead of stopping"
        assert "def add" in content
    print("plan created and persisted; nothing executed")


main()
