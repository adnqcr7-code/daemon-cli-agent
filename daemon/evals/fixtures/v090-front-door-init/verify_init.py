import hashlib
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


def snapshot(root):
    out = {}
    for path in sorted(Path(root).rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()).hexdigest()
    return out


def main():
    with tempfile.TemporaryDirectory(prefix="v090-init-") as tmp:
        target = Path(tmp) / "proj"
        env = dict(os.environ, DAEMON_HOME=str(Path(tmp) / "home"))
        code, out, err = run_cli(["init", str(target)], env=env)
        assert code == 0, out + err
        first = snapshot(target)
        code, out, err = run_cli(["init", str(target)], env=env)
        assert code == 0, out + err
        assert snapshot(target) == first, "second init changed state"
        assert (target / "AGENTS.md").is_file()
        assert (target / ".daemon" / "config.toml").is_file()
    print("init idempotent: second run left identical state")


main()
