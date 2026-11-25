
import os
import tempfile
from pathlib import Path

from daemon.sandbox_backends import select_backend


def main():
    ws = Path.cwd()
    backend = select_backend()
    assert backend is not None, "no sandbox backend on this host"
    secret = Path(tempfile.gettempdir()) / "v090-sandbox-secret.txt"
    secret.write_text("v090-sandbox-marker", encoding="utf-8")
    try:
        result = backend.run(f"cat {secret}", cwd=ws, timeout=30)
        assert "v090-sandbox-marker" not in result.stdout + result.stderr
        os.environ["DAEMON_V090_API_KEY"] = "v090-sandbox-marker"
        try:
            result = backend.run(
                "env", cwd=ws, timeout=30,
                env={"DAEMON_V090_API_KEY": "v090-sandbox-marker"})
        finally:
            os.environ.pop("DAEMON_V090_API_KEY", None)
        assert "v090-sandbox-marker" not in result.stdout + result.stderr
        result = backend.run("echo sandbox-ok", cwd=ws, timeout=30)
        assert result.exit_code == 0 and "sandbox-ok" in result.stdout
    finally:
        secret.unlink(missing_ok=True)
    print(f"sandbox probe passed via {backend.name}")


main()
