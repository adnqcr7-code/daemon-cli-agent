"""Tests for `daemon selftest` (v0.9.0 §2.1)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from click.testing import CliRunner

from daemon.cli import main
from daemon.selftest import LegResult, run_selftest, verdict_of


def test_verdict_algebra() -> None:
    assert verdict_of([LegResult("a", "PASS")]) == "PASS"
    assert verdict_of([LegResult("a", "PASS"), LegResult("b", "SKIP")]) \
        == "PARTIAL"
    assert verdict_of([LegResult("a", "SKIP"), LegResult("b", "FAIL")]) \
        == "FAIL"
    # An empty leg list is a FAIL: a gate that ran nothing must
    # never green-light a release (review round 1).
    assert verdict_of([]) == "FAIL"


def _fake_python(tmp_path: Path, body: str) -> str:
    script = tmp_path / "fake-python"
    script.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


def test_broken_install_fails_with_leg_named(tmp_path) -> None:
    """A deliberately broken install FAILs, naming the broken leg."""
    fake = _fake_python(
        tmp_path,
        "echo \"ModuleNotFoundError: No module named 'daemon'\" >&2\nexit 1")
    report = run_selftest(python=fake)
    assert report.verdict == "FAIL"
    assert report.exit_code == 2
    by_name = {leg.name: leg for leg in report.legs}
    assert by_name["demo run"].status == "FAIL"
    assert by_name["offline eval"].status == "FAIL"
    # The model leg has no credentials in the test home: SKIP, not PASS.
    assert by_name["model leg"].status == "SKIP"
    # And a SKIP never flips a FAIL into a PARTIAL.
    assert "PASS" not in report.to_json()["verdict"]


def test_leg_timeout_fails_named(tmp_path) -> None:
    fake = _fake_python(tmp_path, "sleep 30")
    report = run_selftest(python=fake, timeouts={"demo run": 1.0,
                                                 "offline eval": 1.0})
    by_name = {leg.name: leg for leg in report.legs}
    assert by_name["demo run"].status == "FAIL"
    assert "timed out" in by_name["demo run"].detail
    assert report.verdict == "FAIL"


def test_full_selftest_is_partial_without_credentials() -> None:
    """The real run on this machine: every leg passes except the
    model leg, which SKIPs for lack of credentials — verdict PARTIAL,
    naming the skipped leg. Never a false green."""
    report = run_selftest()
    assert report.verdict == "PARTIAL"
    assert report.exit_code == 1
    by_name = {leg.name: leg for leg in report.legs}
    assert by_name["model leg"].status == "SKIP"
    assert "no credentials" in by_name["model leg"].detail
    for name, leg in by_name.items():
        if name != "model leg":
            assert leg.status == "PASS", f"{name}: {leg.detail}"


def test_selftest_cli_json() -> None:
    """CLI wiring: --json emits the verdict and exits 1 (PARTIAL)."""
    result = CliRunner().invoke(main, ["selftest", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["verdict"] == "PARTIAL"
    assert payload["exit_code"] == 1
    legs = {leg["leg"]: leg for leg in payload["legs"]}
    assert legs["model leg"]["status"] == "SKIP"
    assert all(leg["status"] in ("PASS", "FAIL", "SKIP")
               for leg in payload["legs"])
