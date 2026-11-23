"""Optimization reporting: accumulation, persistence, comparison."""

import json

from daemon.agent.optimizer import ContextOptimizationStats
from daemon.agent.reporting import (
    OptimizationReport,
    OptimizationReporter,
    compare_reports,
    list_reports,
    load_report,
    save_report,
)


def _reporter(**kwargs):
    return OptimizationReporter("sess-1", model="test-model",
                                provider="test-provider", **kwargs)


def test_reporter_accumulates_correctly():
    reporter = _reporter()
    reporter.record_context(ContextOptimizationStats(
        original_tokens=1000, optimized_tokens=880, tokens_saved=120))
    reporter.record_context(30)  # plain int form
    reporter.record_tool_selection(original_count=30, selected_count=12,
                                   tokens_saved=45)
    reporter.record_tool_call()
    reporter.record_tool_call()
    reporter.record_tool_call(failed=True)
    reporter.record_failure_loop()
    reporter.record_failure_loop()
    reporter.record_verified_facts(4)
    reporter.record_verified_facts(2)
    reporter.set_usage(1204, 356, cost_usd=0.0091)
    reporter.set_verification(True)
    reporter.add_files_changed(3)
    reporter.add_files_changed(1)

    report = reporter.finalize(duration_s=12.5)

    assert report.session_id == "sess-1"
    assert report.model == "test-model"
    assert report.provider == "test-provider"
    assert report.optimization_enabled is True
    assert report.total_input_tokens == 1204
    assert report.total_output_tokens == 356
    assert report.context_tokens_saved == 150
    assert report.tool_tokens_saved == 45
    assert report.total_tool_calls == 3
    assert report.failed_tool_calls == 1
    assert report.failure_loops_detected == 2
    assert report.verified_facts_used == 6
    assert report.files_changed == 4
    assert report.verification_passed is True
    assert report.estimated_cost_usd == 0.0091
    assert report.duration_s == 12.5
    assert report.created > 0


def test_reporter_defaults_are_empty_not_invented():
    report = OptimizationReporter("sess-empty").finalize()

    assert report.total_input_tokens == 0
    assert report.total_output_tokens == 0
    assert report.total_tool_calls == 0
    assert report.verification_passed is None
    assert report.estimated_cost_usd is None
    assert report.duration_s == 0


def test_report_json_round_trip():
    report = _reporter(optimization_enabled=False).finalize(duration_s=1.0)
    restored = OptimizationReport.from_json(
        json.loads(json.dumps(report.to_json())))

    assert restored == report


def test_save_load_round_trip(tmp_path):
    reporter = _reporter()
    reporter.set_usage(10, 5, cost_usd=0.001)
    report = reporter.finalize(duration_s=2.0)

    path = save_report(report, tmp_path)

    assert path == tmp_path / ".daemon" / "reports" / "sess-1.json"
    assert path.exists()
    loaded = load_report(tmp_path, "sess-1")
    assert loaded == report
    assert load_report(tmp_path, "no-such-session") is None


def test_list_reports_newest_first_and_limit(tmp_path):
    for index, created in enumerate((100.0, 300.0, 200.0)):
        save_report(OptimizationReport(session_id=f"sess-{index}",
                                       created=created), tmp_path)

    reports = list_reports(tmp_path)

    assert [report.session_id for report in reports] == [
        "sess-1", "sess-2", "sess-0"]
    assert [report.session_id for report in list_reports(tmp_path, limit=2)] == [
        "sess-1", "sess-2"]
    assert list_reports(tmp_path / "missing") == []


def _report(session_id, *, enabled, **overrides):
    values = {
        "total_input_tokens": 1000,
        "total_output_tokens": 500,
        "context_tokens_saved": 0,
        "tool_tokens_saved": 0,
        "total_tool_calls": 10,
        "failed_tool_calls": 1,
        "failure_loops_detected": 1,
        "files_changed": 2,
        "duration_s": 20.0,
    }
    values.update(overrides)
    return OptimizationReport(session_id=session_id,
                              optimization_enabled=enabled, **values)


def test_compare_reports_shows_deltas_and_enabled_state():
    baseline = _report("base", enabled=False)
    candidate = _report(
        "cand", enabled=True, total_input_tokens=700, total_output_tokens=400,
        context_tokens_saved=200, tool_tokens_saved=50, total_tool_calls=8,
        failure_loops_detected=0, duration_s=15.0,
        estimated_cost_usd=0.02)
    baseline.estimated_cost_usd = 0.03

    text = compare_reports(baseline, candidate)

    assert "base" in text and "cand" in text
    assert "optimization disabled" in text  # baseline
    assert "optimization enabled" in text  # candidate
    assert "-400" in text  # total tokens 1500 -> 1100
    assert "+250" in text  # tokens saved 0 -> 250
    assert "-2" in text  # tool calls 10 -> 8
    assert "-1" in text  # failure loops 1 -> 0
    assert "-5.00" in text  # duration delta
    assert "-0.0100" in text  # cost delta, both known


def test_compare_reports_unknown_cost_invents_nothing():
    baseline = _report("base", enabled=False)
    candidate = _report("cand", enabled=True, estimated_cost_usd=0.02)

    text = compare_reports(baseline, candidate)

    assert "unknown" in text
    # The candidate's lone cost must not be presented as a comparison.
    assert "0.0200 ->" not in text
    assert "-> 0.0200" not in text


def test_summary_shows_unknown_for_missing_cost():
    report = _report("sess-x", enabled=True)

    summary = report.summary()

    assert "sess-x" in summary
    assert "unknown" in summary
    assert "$" not in summary


def test_summary_reports_known_cost_and_verification():
    report = _report("sess-y", enabled=False, verification_passed=False,
                     estimated_cost_usd=0.0091)

    summary = report.summary()

    assert "$0.0091" in summary
    assert "FAILED" in summary
    assert "disabled" in summary
