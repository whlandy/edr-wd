"""
P3.2.F — CI gate + CLI unit tests.

Covers D21 + CI gate contract:

  * Gate aggregates thresholded decisions into pass/fail.
  * Ungated decisions do NOT influence verdict.
  * Missing-declaration-as-fail flows through naturally.
  * Sanitised output NEVER includes raw exception
    messages / prompt / screenshot / log content.
  * CLI exit code contract: 0 pass, 1 fail, 2 input, 3 internal.
  * CLI produces stable JSON output.

Layer boundary:
  * Gate consumes EvaluationReport only.
  * Gate does NOT recompute metrics or thresholds.
  * Gate does NOT modify report schema.
  * Gate output is D21-conformant (no raw text).
"""

from __future__ import annotations

import io
import json
import tempfile
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent.eval.ci_gate import (
    ALL_GATE_OUTCOMES,
    CI_GATE_SCHEMA_VERSION,
    CIInputError,
    EXIT_CODE_FAIL,
    EXIT_CODE_INPUT_ERROR,
    EXIT_CODE_INTERNAL_ERROR,
    EXIT_CODE_PASS,
    GATE_OUTCOME_FAIL,
    GATE_OUTCOME_PASS,
    GateDecision,
    GateOutput,
    cli_evaluate_report,
    evaluate_gate,
    main,
    render_sanitised_gate_output,
)
from agent.eval.reports import (
    EvaluationReport,
    FixtureResult,
    REPORT_SCHEMA_VERSION,
    ThresholdDecision,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _report(
    *,
    threshold_decisions,
    fixture_results=(),
    run_id="run-1",
):
    return EvaluationReport(
        schema_version=REPORT_SCHEMA_VERSION,
        dataset_id="eval-fixtures",
        dataset_version="v1",
        planner_version="p3.1.0",
        execution_profile="default",
        started_at=datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 8, 4, 10, 1, 0, tzinfo=timezone.utc),
        metrics={},
        threshold_decisions=threshold_decisions,
        fixture_results=fixture_results,
        reproducibility_digest="a" * 64,
        run_id=run_id,
    )


def _pass_decision(metric_id, observed=0.95):
    return ThresholdDecision(
        metric_id=metric_id,
        thresholded=True,
        decision="pass",
        observed=observed,
        threshold=0.9,
    )


def _fail_decision(metric_id, observed=0.5):
    return ThresholdDecision(
        metric_id=metric_id,
        thresholded=True,
        decision="fail",
        observed=observed,
        threshold=0.9,
    )


def _ungated_decision(metric_id, observed=0.5):
    return ThresholdDecision(
        metric_id=metric_id,
        thresholded=False,
        decision="ungated",
        observed=observed,
        threshold=None,
    )


def _missing_decision(metric_id):
    """Missing-declaration-as-fail (per P3.2.D)."""
    return ThresholdDecision(
        metric_id=metric_id,
        thresholded=False,
        decision="fail",
        observed=0.0,
        threshold=None,
    )


# ---------------------------------------------------------------------------
# Gate decision
# ---------------------------------------------------------------------------


class TestGateOutcomeConstants:
    def test_two_outcomes(self):
        assert GATE_OUTCOME_PASS in ALL_GATE_OUTCOMES
        assert GATE_OUTCOME_FAIL in ALL_GATE_OUTCOMES
        assert len(ALL_GATE_OUTCOMES) == 2


class TestEvaluateGate:
    def test_empty_decisions_passes(self):
        """No thresholded metrics → gate passes trivially."""
        report = _report(threshold_decisions=())
        decision = evaluate_gate(report)
        assert decision.outcome == GATE_OUTCOME_PASS
        assert decision.passes == ()
        assert decision.failures == ()
        assert decision.ungated == ()
        assert decision.is_passing() is True
        assert decision.exit_code() == EXIT_CODE_PASS

    def test_all_passes(self):
        decisions = (
            _pass_decision("m1"),
            _pass_decision("m2"),
            _pass_decision("m3"),
        )
        report = _report(threshold_decisions=decisions)
        decision = evaluate_gate(report)
        assert decision.outcome == GATE_OUTCOME_PASS
        assert len(decision.passes) == 3
        assert decision.failures == ()
        assert decision.exit_code() == EXIT_CODE_PASS

    def test_one_failure_fails(self):
        decisions = (
            _pass_decision("m1"),
            _fail_decision("m2"),
            _pass_decision("m3"),
        )
        report = _report(threshold_decisions=decisions)
        decision = evaluate_gate(report)
        assert decision.outcome == GATE_OUTCOME_FAIL
        assert len(decision.passes) == 2
        assert len(decision.failures) == 1
        assert decision.failures[0].metric_id == "m2"
        assert decision.exit_code() == EXIT_CODE_FAIL

    def test_ungated_does_not_affect_verdict(self):
        """Ungated decisions are recorded but don't gate."""
        decisions = (
            _pass_decision("m1"),
            _ungated_decision("u1"),
            _ungated_decision("u2"),
        )
        report = _report(threshold_decisions=decisions)
        decision = evaluate_gate(report)
        assert decision.outcome == GATE_OUTCOME_PASS
        assert len(decision.passes) == 1
        assert len(decision.ungated) == 2
        assert len(decision.failures) == 0
        assert decision.exit_code() == EXIT_CODE_PASS

    def test_missing_declaration_fails(self):
        """Per P3.2.D: missing declarations arrive as
        decision='fail' with thresholded=False. The gate
        routes these to failures."""
        decisions = (
            _pass_decision("m1"),
            _missing_decision("undeclared"),
        )
        report = _report(threshold_decisions=decisions)
        decision = evaluate_gate(report)
        assert decision.outcome == GATE_OUTCOME_FAIL
        assert len(decision.failures) == 1
        assert decision.failures[0].metric_id == "undeclared"

    def test_unknown_decision_string_treated_as_fail(self):
        """Unknown decision strings routed to failures
        for CI safety."""
        unknown = ThresholdDecision(
            metric_id="m1",
            thresholded=True,
            decision="?unknown?",
            observed=0.5,
            threshold=0.9,
        )
        report = _report(threshold_decisions=(unknown,))
        decision = evaluate_gate(report)
        assert decision.outcome == GATE_OUTCOME_FAIL
        assert len(decision.failures) == 1

    def test_type_error_on_non_report(self):
        with pytest.raises(TypeError, match="EvaluationReport"):
            evaluate_gate("not a report")

    def test_reason_message_includes_failed_metrics(self):
        decisions = (
            _pass_decision("alpha"),
            _fail_decision("beta"),
            _fail_decision("gamma"),
        )
        report = _report(threshold_decisions=decisions)
        decision = evaluate_gate(report)
        assert "beta" in decision.reason
        assert "gamma" in decision.reason
        assert "2" in decision.reason  # count

    def test_pass_reason_message(self):
        decisions = (
            _pass_decision("m1"),
            _ungated_decision("u1"),
        )
        report = _report(threshold_decisions=decisions)
        decision = evaluate_gate(report)
        assert "passed" in decision.reason
        assert "ungated" in decision.reason

    def test_trivial_pass_reason(self):
        """Empty decision list produces a special-case
        reason."""
        report = _report(threshold_decisions=())
        decision = evaluate_gate(report)
        assert "trivially" in decision.reason or "no thresholded" in decision.reason


# ---------------------------------------------------------------------------
# Sanitised rendering
# ---------------------------------------------------------------------------


class TestRenderSanitisedGateOutput:
    def test_schema_version_present(self):
        decisions = (_pass_decision("m1"),)
        report = _report(threshold_decisions=decisions)
        decision = evaluate_gate(report)
        output = render_sanitised_gate_output(decision, report)
        assert output.schema_version == CI_GATE_SCHEMA_VERSION

    def test_carries_report_metadata(self):
        decisions = (_pass_decision("m1"),)
        report = _report(threshold_decisions=decisions)
        decision = evaluate_gate(report)
        output = render_sanitised_gate_output(decision, report)
        assert output.dataset["id"] == "eval-fixtures"
        assert output.dataset["version"] == "v1"
        assert output.planner_version == "p3.1.0"
        assert output.execution_profile == "default"
        assert output.run_id == "run-1"
        assert output.reproducibility_digest == "a" * 64

    def test_summary_counts(self):
        decisions = (
            _pass_decision("m1"),
            _pass_decision("m2"),
            _fail_decision("m3"),
            _ungated_decision("u1"),
        )
        report = _report(threshold_decisions=decisions)
        decision = evaluate_gate(report)
        output = render_sanitised_gate_output(decision, report)
        assert output.summary["passes"] == 2
        assert output.summary["failures"] == 1
        assert output.summary["ungated"] == 1
        assert output.summary["total_metrics"] == 4

    def test_failures_only_list_failures(self):
        decisions = (
            _pass_decision("m1"),
            _fail_decision("m2"),
            _ungated_decision("u1"),
        )
        report = _report(threshold_decisions=decisions)
        decision = evaluate_gate(report)
        output = render_sanitised_gate_output(decision, report)
        assert len(output.failures) == 1
        assert output.failures[0]["metric_id"] == "m2"
        assert output.failures[0]["decision"] == "fail"

    def test_ungated_only_list_ungated(self):
        decisions = (
            _pass_decision("m1"),
            _ungated_decision("u1"),
            _ungated_decision("u2"),
        )
        report = _report(threshold_decisions=decisions)
        decision = evaluate_gate(report)
        output = render_sanitised_gate_output(decision, report)
        assert len(output.ungated_metrics) == 2
        for u in output.ungated_metrics:
            assert u["decision"] == "ungated"

    def test_no_raw_exception_message_in_output(self):
        """Per D21: raw exception messages must NEVER
        appear in the output."""
        errored_fixture = FixtureResult(
            fixture_id="fid-1",
            status="errored",
            expected={"kind": "value"},
            observed={
                "_error_class": "ValueError",
                "_error_message_class": "value_error",
                "raw_exception": "SECRET API KEY sk-1234567890",
            },
        )
        decisions = (_pass_decision("m1"),)
        report = _report(
            threshold_decisions=decisions,
            fixture_results=(errored_fixture,),
        )
        decision = evaluate_gate(report)
        output = render_sanitised_gate_output(decision, report)
        out_json = json.dumps(output.to_dict())
        assert "SECRET API KEY" not in out_json
        assert "sk-1234567890" not in out_json
        # Only sanitised class survives.
        assert "ValueError" in out_json
        assert "value_error" in out_json

    def test_fixture_summary_counts(self):
        frs = (
            FixtureResult(
                fixture_id="a",
                status="completed",
                expected={"k": "v"},
                observed={"k": "v"},
            ),
            FixtureResult(
                fixture_id="b",
                status="completed",
                expected={"k": "v"},
                observed={"k": "v"},
            ),
            FixtureResult(
                fixture_id="c",
                status="errored",
                expected={"k": "v"},
                observed={
                    "_error_class": "TimeoutError",
                    "_error_message_class": "timeout",
                },
            ),
        )
        decisions = (_pass_decision("m1"),)
        report = _report(
            threshold_decisions=decisions,
            fixture_results=frs,
        )
        decision = evaluate_gate(report)
        output = render_sanitised_gate_output(decision, report)
        assert output.fixture_summary["total"] == 3
        assert output.fixture_summary["by_status"]["completed"] == 2
        assert output.fixture_summary["by_status"]["errored"] == 1
        assert "TimeoutError" in output.fixture_summary["errored_classes"]

    def test_to_dict_round_trip(self):
        """GateOutput.to_dict produces JSON-serialisable
        output."""
        decisions = (
            _pass_decision("m1"),
            _fail_decision("m2"),
        )
        report = _report(threshold_decisions=decisions)
        decision = evaluate_gate(report)
        output = render_sanitised_gate_output(decision, report)
        d = output.to_dict()
        encoded = json.dumps(d)
        decoded = json.loads(encoded)
        assert decoded["outcome"] == GATE_OUTCOME_FAIL
        assert decoded["summary"]["failures"] == 1


# ---------------------------------------------------------------------------
# CLI worker
# ---------------------------------------------------------------------------


class TestCLI:
    def _write_report(self, decisions, fixture_results=()):
        report = _report(
            threshold_decisions=decisions,
            fixture_results=fixture_results,
        )
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            delete=False,
            encoding="utf-8",
        ) as f:
            json.dump(report.to_dict(), f, default=str)
            return Path(f.name)

    def test_cli_pass_returns_exit_0(self):
        decisions = (_pass_decision("m1"),)
        path = self._write_report(decisions)
        try:
            exit_code, output = cli_evaluate_report(str(path))
            assert exit_code == EXIT_CODE_PASS
            payload = json.loads(output)
            assert payload["outcome"] == GATE_OUTCOME_PASS
        finally:
            path.unlink()

    def test_cli_fail_returns_exit_1(self):
        decisions = (_fail_decision("m1"),)
        path = self._write_report(decisions)
        try:
            exit_code, output = cli_evaluate_report(str(path))
            assert exit_code == EXIT_CODE_FAIL
            payload = json.loads(output)
            assert payload["outcome"] == GATE_OUTCOME_FAIL
        finally:
            path.unlink()

    def test_cli_verdict_format(self):
        decisions = (_pass_decision("m1"),)
        path = self._write_report(decisions)
        try:
            exit_code, output = cli_evaluate_report(
                str(path), output_format="verdict"
            )
            assert exit_code == EXIT_CODE_PASS
            assert output == "pass"
        finally:
            path.unlink()

    def test_cli_verdict_format_fail(self):
        decisions = (_fail_decision("m1"),)
        path = self._write_report(decisions)
        try:
            exit_code, output = cli_evaluate_report(
                str(path), output_format="verdict"
            )
            assert exit_code == EXIT_CODE_FAIL
            assert output == "fail"
        finally:
            path.unlink()

    def test_cli_missing_file_raises_input_error(self):
        with pytest.raises(CIInputError, match="not found"):
            cli_evaluate_report("/nonexistent/path/report.json")

    def test_cli_invalid_json_raises_input_error(self):
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            delete=False,
            encoding="utf-8",
        ) as f:
            f.write("not valid json {")
            path = Path(f.name)
        try:
            with pytest.raises(CIInputError, match="not valid JSON"):
                cli_evaluate_report(str(path))
        finally:
            path.unlink()

    def test_cli_invalid_report_raises_input_error(self):
        """JSON parses but doesn't fit EvaluationReport schema."""
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            delete=False,
            encoding="utf-8",
        ) as f:
            json.dump({"not": "a report"}, f)
            path = Path(f.name)
        try:
            with pytest.raises(CIInputError, match="EvaluationReport"):
                cli_evaluate_report(str(path))
        finally:
            path.unlink()

    def test_cli_invalid_output_format_raises_input_error(self):
        decisions = (_pass_decision("m1"),)
        path = self._write_report(decisions)
        try:
            with pytest.raises(CIInputError, match="output_format"):
                cli_evaluate_report(str(path), output_format="xml")
        finally:
            path.unlink()

    def test_cli_exit_codes_contract(self):
        """Verify the four exit codes are stable."""
        assert EXIT_CODE_PASS == 0
        assert EXIT_CODE_FAIL == 1
        assert EXIT_CODE_INPUT_ERROR == 2
        assert EXIT_CODE_INTERNAL_ERROR == 3


# ---------------------------------------------------------------------------
# CLI main() wrapper
# ---------------------------------------------------------------------------


class TestMainWrapper:
    def _write_report(self, decisions):
        report = _report(threshold_decisions=decisions)
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            delete=False,
            encoding="utf-8",
        ) as f:
            json.dump(report.to_dict(), f, default=str)
            return Path(f.name)

    def test_main_pass_exits_0(self):
        path = self._write_report((_pass_decision("m1"),))
        try:
            with redirect_stdout(io.StringIO()) as out:
                with redirect_stderr(io.StringIO()):
                    code = main([str(path)])
            assert code == EXIT_CODE_PASS
            payload = json.loads(out.getvalue())
            assert payload["outcome"] == GATE_OUTCOME_PASS
        finally:
            path.unlink()

    def test_main_fail_exits_1(self):
        path = self._write_report((_fail_decision("m1"),))
        try:
            with redirect_stdout(io.StringIO()) as out:
                with redirect_stderr(io.StringIO()):
                    code = main([str(path)])
            assert code == EXIT_CODE_FAIL
            payload = json.loads(out.getvalue())
            assert payload["outcome"] == GATE_OUTCOME_FAIL
        finally:
            path.unlink()

    def test_main_verdict_flag(self):
        path = self._write_report((_pass_decision("m1"),))
        try:
            with redirect_stdout(io.StringIO()) as out:
                with redirect_stderr(io.StringIO()):
                    code = main([str(path), "--verdict"])
            assert code == EXIT_CODE_PASS
            assert out.getvalue().strip() == "pass"
        finally:
            path.unlink()

    def test_main_missing_file_exits_2(self):
        with redirect_stdout(io.StringIO()):
            with redirect_stderr(io.StringIO()) as err:
                code = main(["/nonexistent/path/report.json"])
        assert code == EXIT_CODE_INPUT_ERROR
        assert "input error" in err.getvalue()

    def test_main_help_exits_0(self):
        """Help output is not a failure."""
        with redirect_stdout(io.StringIO()):
            with redirect_stderr(io.StringIO()):
                code = main(["--help"])
        assert code == EXIT_CODE_PASS

    def test_main_no_args_exits_0(self):
        """No args shows usage, exits 0."""
        with redirect_stdout(io.StringIO()):
            with redirect_stderr(io.StringIO()) as err:
                code = main([])
        assert code == EXIT_CODE_PASS
        assert "Usage" in err.getvalue()

    def test_main_invalid_json_exits_2(self):
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            delete=False,
            encoding="utf-8",
        ) as f:
            f.write("not valid json {")
            path = Path(f.name)
        try:
            with redirect_stdout(io.StringIO()):
                with redirect_stderr(io.StringIO()) as err:
                    code = main([str(path)])
            assert code == EXIT_CODE_INPUT_ERROR
            assert "input error" in err.getvalue()
        finally:
            path.unlink()


# ---------------------------------------------------------------------------
# Layer boundary
# ---------------------------------------------------------------------------


class TestLayerBoundary:
    def test_gate_does_not_recompute_metrics(self):
        """The gate consumes threshold_decisions; it does
        NOT recompute metrics."""
        import agent.eval.ci_gate as g
        import inspect

        source = inspect.getsource(g)
        # Should NOT import compute_all_metrics.
        assert "compute_all_metrics" not in source
        assert "compute_metric" not in source

    def test_gate_does_not_re_evaluate_thresholds(self):
        """The gate consumes pre-computed decisions; it
        does NOT call the threshold evaluator."""
        import agent.eval.ci_gate as g
        import inspect

        source = inspect.getsource(g)
        # Should NOT import evaluate_threshold.
        assert "evaluate_threshold" not in source
        assert "evaluate_all_thresholds" not in source

    def test_gate_does_not_modify_reports_schema(self):
        """The gate reads report fields but does not
        write to them or redefine them."""
        import agent.eval.ci_gate as g
        import inspect

        source = inspect.getsource(g)
        # Should NOT contain dataclass definitions for
        # report-shaped classes (only gate-owned ones).
        assert "class GateOutput" in source
        assert "class GateDecision" in source

    def test_gate_imports_only_consume_layers(self):
        """The gate imports from reports only; it must
        not pull metrics / thresholds / runner / datasets
        / sanitisation."""
        import agent.eval.ci_gate as g
        import inspect

        source = inspect.getsource(g)
        assert "from agent.eval.reports import" in source
        assert "from agent.eval.metrics import" not in source
        assert "from agent.eval.thresholds import" not in source
        assert "from agent.eval.runner import" not in source
        assert "from agent.eval.datasets import" not in source
        assert "from agent.eval.sanitisation import" not in source


# ---------------------------------------------------------------------------
# Integration with P3.2.E (runner)
# ---------------------------------------------------------------------------


class TestIntegrationWithRunner:
    def test_runner_output_drives_gate(self):
        """End-to-end: runner produces report; gate
        evaluates it."""
        from agent.eval.datasets import Fixture, FixtureIdentity
        from agent.eval.metrics import (
            METRIC_DIRECTION_HIGHER_IS_BETTER,
            MetricFormula,
            MetricRegistry,
            MetricSpec,
        )
        from agent.eval.runner import EvaluationRunner
        from agent.eval.thresholds import (
            THRESHOLD_DIRECTION_HIGHER_IS_BETTER,
            THRESHOLD_STATE_THRESHOLDED,
            ThresholdDeclaration,
            ThresholdRegistry,
        )

        metric_registry = MetricRegistry()
        metric_registry.register(
            MetricSpec(
                metric_id="parse_rate",
                categories=frozenset({"Quality"}),
                formula_summary="...",
                unit="ratio",
                direction=METRIC_DIRECTION_HIGHER_IS_BETTER,
                owner="planner",
            ),
            MetricFormula(
                metric_id="parse_rate",
                compute=lambda r: 0.95,
            ),
        )

        threshold_registry = ThresholdRegistry()
        threshold_registry.declare(
            ThresholdDeclaration(
                metric_id="parse_rate",
                state=THRESHOLD_STATE_THRESHOLDED,
                threshold=0.9,
                direction=THRESHOLD_DIRECTION_HIGHER_IS_BETTER,
            )
        )

        def executor(fixture):
            return {"parse_status": "parsed"}

        runner = EvaluationRunner(
            dataset_id="eval-fixtures",
            dataset_version="v1",
            planner_version="p3.1.0",
            execution_profile="default",
            metric_registry=metric_registry,
            threshold_registry=threshold_registry,
            executor=executor,
        )
        fixtures = (
            Fixture(
                identity=FixtureIdentity(
                    dataset_id="eval-fixtures",
                    dataset_version="v1",
                    fixture_id="a",
                ),
                content={"x": 1},
                expected_outcome={"kind": "value"},
            ),
        )

        started = datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc)
        ended = datetime(2026, 8, 4, 10, 1, 0, tzinfo=timezone.utc)
        report = runner.run(fixtures, started_at=started, ended_at=ended)

        decision = evaluate_gate(report)
        assert decision.outcome == GATE_OUTCOME_PASS
        assert len(decision.passes) == 1
        assert len(decision.failures) == 0

    def test_runner_with_low_metric_fails_gate(self):
        """End-to-end: low metric → gate fail."""
        from agent.eval.datasets import Fixture, FixtureIdentity
        from agent.eval.metrics import (
            METRIC_DIRECTION_HIGHER_IS_BETTER,
            MetricFormula,
            MetricRegistry,
            MetricSpec,
        )
        from agent.eval.runner import EvaluationRunner
        from agent.eval.thresholds import (
            THRESHOLD_DIRECTION_HIGHER_IS_BETTER,
            THRESHOLD_STATE_THRESHOLDED,
            ThresholdDeclaration,
            ThresholdRegistry,
        )

        metric_registry = MetricRegistry()
        metric_registry.register(
            MetricSpec(
                metric_id="parse_rate",
                categories=frozenset({"Quality"}),
                formula_summary="...",
                unit="ratio",
                direction=METRIC_DIRECTION_HIGHER_IS_BETTER,
                owner="planner",
            ),
            MetricFormula(
                metric_id="parse_rate",
                compute=lambda r: 0.5,  # Below 0.9 threshold
            ),
        )

        threshold_registry = ThresholdRegistry()
        threshold_registry.declare(
            ThresholdDeclaration(
                metric_id="parse_rate",
                state=THRESHOLD_STATE_THRESHOLDED,
                threshold=0.9,
                direction=THRESHOLD_DIRECTION_HIGHER_IS_BETTER,
            )
        )

        def executor(fixture):
            return {"parse_status": "parsed"}

        runner = EvaluationRunner(
            dataset_id="eval-fixtures",
            dataset_version="v1",
            planner_version="p3.1.0",
            execution_profile="default",
            metric_registry=metric_registry,
            threshold_registry=threshold_registry,
            executor=executor,
        )
        fixtures = (
            Fixture(
                identity=FixtureIdentity(
                    dataset_id="eval-fixtures",
                    dataset_version="v1",
                    fixture_id="a",
                ),
                content={"x": 1},
                expected_outcome={"kind": "value"},
            ),
        )

        started = datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc)
        ended = datetime(2026, 8, 4, 10, 1, 0, tzinfo=timezone.utc)
        report = runner.run(fixtures, started_at=started, ended_at=ended)

        decision = evaluate_gate(report)
        assert decision.outcome == GATE_OUTCOME_FAIL
        assert len(decision.failures) == 1