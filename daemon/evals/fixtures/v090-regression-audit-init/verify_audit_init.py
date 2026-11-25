
import subprocess
import sys

# Regression (Phase 3 bug): the selftest doctor leg failed when run
# alone because the audit check reads process-global state and no
# setup_logging had run in that process. In a fresh process that
# never invoked the CLI, the leg must still PASS.
CODE = """
import sys
from daemon.selftest import _Context, _leg_doctor
ctx = _Context(python=sys.executable)
try:
    leg = _leg_doctor(ctx)
    assert leg.status == "PASS", leg.detail
    print("doctor leg PASS with no prior setup_logging")
finally:
    ctx.cleanup()
"""


def main():
    result = subprocess.run([sys.executable, "-c", CODE],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    print(result.stdout.strip())


main()
