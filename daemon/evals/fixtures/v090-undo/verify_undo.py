import os
import subprocess
import sys
from pathlib import Path


def run_cli(args, *, env, cwd=None, timeout=600):
    """Run the real daemon CLI; return (returncode, stdout, stderr)."""
    result = subprocess.run(
        [sys.executable, "-m", "daemon.cli", *args],
        capture_output=True, text=True, timeout=timeout, env=env, cwd=cwd)
    return result.returncode, result.stdout, result.stderr

def main():
    ws = Path.cwd()
    env = dict(os.environ)  # inherit DAEMON_HOME: the run's home
    code, out, err = run_cli(
        ["undo", "--yes", "--workspace", str(ws)], env=env, timeout=120)
    assert code == 0, out + err
    assert (ws / "a.py").read_text(encoding="utf-8") == (
        "def add(a, b):\n    return a + b\n"), "undo did not restore exact bytes"
    print("undo restored the checkpointed bytes")


main()
