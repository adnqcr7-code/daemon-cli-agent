import json
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
    with tempfile.TemporaryDirectory(prefix="v090-selftest-") as home:
        env = dict(os.environ, DAEMON_HOME=home)
        code, out, err = run_cli(["selftest", "--json"], env=env, cwd=home)
        payload = json.loads(out)
        assert code == 1, out + err
        assert payload["verdict"] == "PARTIAL", payload
        legs = {leg["leg"]: leg for leg in payload["legs"]}
        assert legs["model leg"]["status"] == "SKIP"
        assert "no credentials" in legs["model leg"]["detail"]
        for name, leg in legs.items():
            if name != "model leg":
                assert leg["status"] == "PASS", f"{name}: {leg['detail']}"
    print("selftest PARTIAL as expected (model leg SKIP)")


main()
