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
    # Regression (§2.1 bug): the selftest demo leg once ran in the
    # caller's cwd instead of its throwaway workspace. A decoy a.py
    # in the cwd must survive the selftest untouched.
    with tempfile.TemporaryDirectory(prefix="v090-decoy-") as tmp:
        decoy = Path(tmp) / "decoy"
        decoy.mkdir()
        (decoy / "a.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8")
        before = (decoy / "a.py").read_bytes()
        env = dict(os.environ, DAEMON_HOME=str(Path(tmp) / "home"))
        code, out, err = run_cli(["selftest", "--json"], env=env,
                                 cwd=str(decoy))
        payload = json.loads(out)
        assert payload["verdict"] == "PARTIAL", out + err
        assert (decoy / "a.py").read_bytes() == before, (
            "selftest demo leg edited the cwd")
    print("decoy untouched; selftest used its throwaway workspace")


main()
