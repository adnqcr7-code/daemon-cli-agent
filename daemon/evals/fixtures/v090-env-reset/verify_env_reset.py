import hashlib
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


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    with tempfile.TemporaryDirectory(prefix="v090-env-") as home:
        env = dict(os.environ, DAEMON_HOME=home)
        for args in (["env", "create", "dev", "--template", "developer"],
                     ["env", "use", "dev"],
                     ["memory", "add-verified", "eval fact"]):
            code, out, err = run_cli(args, env=env, timeout=120)
            assert code == 0, f"{args}: {out}{err}"
        code, out, err = run_cli(["memory", "list"], env=env, timeout=120)
        assert "eval fact" in out, out
        env_path = Path(home) / "envs" / "dev"
        manifest = json.loads((env_path / "manifest.json").read_text())
        assert manifest["files"], "manifest has no baseline files"
        code, out, err = run_cli(["env", "reset", "dev"], env=env, timeout=120)
        assert code == 0, out + err
        assert "hashes verified" in out, out
        for rel, expected in manifest["files"].items():
            actual = sha256(env_path / "state" / rel)
            assert actual == expected, f"{rel} not byte-identical after reset"
        code, out, err = run_cli(["memory", "list"], env=env, timeout=120)
        assert "eval fact" not in out, out
        assert "Memory is empty." in out, out
    print("env reset byte-identical to the baseline manifest")


main()
