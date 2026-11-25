import os
import subprocess
import sys
import tempfile


def run_cli(args, *, env, cwd=None, timeout=600):
    """Run the real daemon CLI; return (returncode, stdout, stderr)."""
    result = subprocess.run(
        [sys.executable, "-m", "daemon.cli", *args],
        capture_output=True, text=True, timeout=timeout, env=env, cwd=cwd)
    return result.returncode, result.stdout, result.stderr

def main():
    with tempfile.TemporaryDirectory(prefix="v090-env-") as home:
        env = dict(os.environ, DAEMON_HOME=home)
        for args in (["env", "create", "alpha", "--template", "minimal"],
                     ["env", "create", "beta", "--template", "minimal"],
                     ["env", "use", "alpha"],
                     ["memory", "add-verified", "fact in alpha"],
                     ["env", "use", "beta"]):
            code, out, err = run_cli(args, env=env, timeout=120)
            assert code == 0, f"{args}: {out}{err}"
        code, out, err = run_cli(["memory", "list"], env=env, timeout=120)
        assert "fact in alpha" not in out, "env B leaked env A's memory"
        code, out, err = run_cli(["env", "use", "alpha"], env=env, timeout=120)
        assert code == 0, out + err
        code, out, err = run_cli(["memory", "list"], env=env, timeout=120)
        assert "fact in alpha" in out, out
    print("env memory is scoped: A invisible in B, present in A")


main()
