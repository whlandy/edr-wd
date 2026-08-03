"""
P2.4.B — Runner propagation contract tests for cleanup fields.

Covers:
  * `MockCleanupRunner` deterministic behavior per contract mode.
  * `apply_cleanup_to_manifest()` propagation function.
  * Contract guarantees (D11 / D12 / D13):
      - cleanup_status="unknown" iff cleanup not executed
      - cleanup_status="passed" iff cleanup ran and all steps succeeded
      - cleanup_status="failed" iff cleanup ran with at least one failure
      - cleanup_outcome_critical is verbatim from TestCase (D11)

References:
  docs/requirements/P2-cleanup-escape-design.md §5.5 + §9.2
"""

from __future__ import annotations

import dataclasses

import pytest

from agent.execution.cleanup_contract import (
    CleanupExecutionResult,
    CleanupSkippedError,
    MockCleanupRunner,
    apply_cleanup_to_manifest,
)
from agent.trace.integrity import IntegrityReport
from agent.trace.manifest import (
    CLEANUP_STATUS_FAILED,
    CLEANUP_STATUS_PASSED,
    CLEANUP_STATUS_UNKNOWN,
    ManifestRecord,
)
from target.protocol_models.models import (
    AtomicTestStep,
    TestCase,
)


# ---------------------------------------------------------------------
# TestCase + Manifest fixtures
# ---------------------------------------------------------------------


def _make_testcase(
    *,
    case_id: str = "case_test",
    title: str = "Test Case",
    cleanup_steps: tuple[AtomicTestStep, ...] = (),
    cleanup_outcome_critical: bool = False,
) -> TestCase:
    """Construct a TestCase with optional cleanup steps + outcome-critical flag."""
    return TestCase(
        case_id=case_id,
        title=title,
        cleanup=cleanup_steps,
        cleanup_outcome_critical=cleanup_outcome_critical,
    )


def _make_manifest(
    *,
    terminal_status: str = "passed",
    cleanup_status: str = CLEANUP_STATUS_UNKNOWN,
    cleanup_outcome_critical: bool = False,
) -> ManifestRecord:
    """Construct a ManifestRecord for propagation tests."""
    return ManifestRecord(
        trace_id="trace_test",
        branch_heads=(),
        catalog_digest="a" * 64,
        evidence_counts={},
        terminal_status=terminal_status,
        integrity_verification_result=IntegrityReport(ok=True, issues=()),
        cleanup_status=cleanup_status,
        cleanup_outcome_critical=cleanup_outcome_critical,
    )


# ---------------------------------------------------------------------
# CleanupExecutionResult dataclass
# ---------------------------------------------------------------------


class TestCleanupExecutionResultDataclass:
    """CleanupExecutionResult validates inputs at construction."""

    def test_constructs_with_passed_status(self) -> None:
        r = CleanupExecutionResult(
            status="passed", outcome_critical=False, steps_run=2,
        )
        assert r.status == "passed"
        assert r.outcome_critical is False
        assert r.steps_run == 2

    def test_rejects_unknown_status_enum_value(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            CleanupExecutionResult(
                status="skipped",  # invalid
                outcome_critical=False,
                steps_run=1,
            )
        assert "status" in str(exc_info.value)

    def test_rejects_non_bool_outcome_critical(self) -> None:
        with pytest.raises(TypeError):
            CleanupExecutionResult(
                status="passed",
                outcome_critical="yes",  # type: ignore[arg-type]
                steps_run=1,
            )

    def test_rejects_negative_steps_run(self) -> None:
        with pytest.raises(ValueError):
            CleanupExecutionResult(
                status="passed",
                outcome_critical=False,
                steps_run=-1,
            )

    def test_frozen_dataclass(self) -> None:
        r = CleanupExecutionResult(
            status="passed", outcome_critical=False, steps_run=1,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            r.status = "failed"  # type: ignore[misc]


# ---------------------------------------------------------------------
# MockCleanupRunner contract
# ---------------------------------------------------------------------


class TestMockCleanupRunnerContract:
    """MockCleanupRunner honors the contract per simulate mode."""

    def test_no_cleanup_steps_returns_unknown(self) -> None:
        """Contract: empty cleanup → status='unknown' regardless of simulate."""
        tc = _make_testcase(cleanup_steps=(), cleanup_outcome_critical=True)
        for simulate in ("pass", "fail", "unknown"):
            runner = MockCleanupRunner(simulate=simulate)
            result = runner.run(tc)
            assert result.status == CLEANUP_STATUS_UNKNOWN
            assert result.steps_run == 0
            # outcome_critical is verbatim (D11)
            assert result.outcome_critical is True

    def test_simulate_pass_returns_passed(self) -> None:
        """Contract: simulate='pass' + has steps → status='passed'."""
        tc = _make_testcase(
            cleanup_steps=(AtomicTestStep(step_id="s1", action_id="close"),),
            cleanup_outcome_critical=False,
        )
        runner = MockCleanupRunner(simulate="pass")
        result = runner.run(tc)
        assert result.status == CLEANUP_STATUS_PASSED
        assert result.steps_run == 1
        assert result.outcome_critical is False

    def test_simulate_fail_returns_failed(self) -> None:
        """Contract: simulate='fail' + has steps → status='failed'."""
        tc = _make_testcase(
            cleanup_steps=(AtomicTestStep(step_id="s1", action_id="close"),),
            cleanup_outcome_critical=True,
        )
        runner = MockCleanupRunner(simulate="fail")
        result = runner.run(tc)
        assert result.status == CLEANUP_STATUS_FAILED
        assert result.steps_run == 1
        assert result.outcome_critical is True

    def test_simulate_unknown_returns_unknown(self) -> None:
        """Contract: simulate='unknown' → status='unknown'."""
        tc = _make_testcase(
            cleanup_steps=(AtomicTestStep(step_id="s1", action_id="close"),),
            cleanup_outcome_critical=True,
        )
        runner = MockCleanupRunner(simulate="unknown")
        result = runner.run(tc)
        assert result.status == CLEANUP_STATUS_UNKNOWN
        assert result.steps_run == 0
        # outcome_critical is still copied verbatim even if cleanup
        # didn't run (D11: data ownership — runner doesn't infer).
        assert result.outcome_critical is True

    def test_invalid_simulate_raises(self) -> None:
        with pytest.raises(ValueError):
            MockCleanupRunner(simulate="abort")  # type: ignore[arg-type]

    def test_outcome_critical_verbatim_from_testcase(self) -> None:
        """D11: outcome_critical is read from TestCase verbatim."""
        step = AtomicTestStep(step_id="s1", action_id="close")
        # True case
        tc_true = _make_testcase(
            cleanup_steps=(step,), cleanup_outcome_critical=True,
        )
        result_true = MockCleanupRunner(simulate="pass").run(tc_true)
        assert result_true.outcome_critical is True

        # False case
        tc_false = _make_testcase(
            cleanup_steps=(step,), cleanup_outcome_critical=False,
        )
        result_false = MockCleanupRunner(simulate="pass").run(tc_false)
        assert result_false.outcome_critical is False

    def test_steps_run_reports_actual_count(self) -> None:
        """steps_run == len(testcase.cleanup) when executed."""
        steps = tuple(
            AtomicTestStep(step_id=f"s{i}", action_id=f"act{i}")
            for i in range(3)
        )
        tc = _make_testcase(cleanup_steps=steps, cleanup_outcome_critical=True)
        runner = MockCleanupRunner(simulate="pass")
        result = runner.run(tc)
        assert result.steps_run == 3


# ---------------------------------------------------------------------
# apply_cleanup_to_manifest propagation
# ---------------------------------------------------------------------


class TestApplyCleanupToManifest:
    """apply_cleanup_to_manifest() layers cleanup into manifest."""

    def test_applies_passed_status(self) -> None:
        """Runner result 'passed' → manifest.cleanup_status='passed'."""
        m = _make_manifest()
        r = CleanupExecutionResult(
            status=CLEANUP_STATUS_PASSED,
            outcome_critical=False,
            steps_run=1,
        )
        new_m = apply_cleanup_to_manifest(m, r)
        assert new_m.cleanup_status == "passed"
        assert new_m.cleanup_outcome_critical is False
        # Other fields preserved.
        assert new_m.terminal_status == m.terminal_status
        assert new_m.trace_id == m.trace_id

    def test_applies_failed_status_with_critical_flag(self) -> None:
        """Runner result 'failed' + outcome_critical=True → manifest updated."""
        m = _make_manifest(terminal_status="passed")
        r = CleanupExecutionResult(
            status=CLEANUP_STATUS_FAILED,
            outcome_critical=True,
            steps_run=2,
        )
        new_m = apply_cleanup_to_manifest(m, r)
        assert new_m.cleanup_status == "failed"
        assert new_m.cleanup_outcome_critical is True
        # terminal_status is NOT modified by propagation.
        assert new_m.terminal_status == "passed"

    def test_applies_unknown_status(self) -> None:
        """Runner result 'unknown' → manifest.cleanup_status='unknown'."""
        m = _make_manifest()
        r = CleanupExecutionResult(
            status=CLEANUP_STATUS_UNKNOWN,
            outcome_critical=False,
            steps_run=0,
        )
        new_m = apply_cleanup_to_manifest(m, r)
        assert new_m.cleanup_status == "unknown"

    def test_does_not_mutate_input_manifest(self) -> None:
        """apply_cleanup_to_manifest returns new manifest, original unchanged."""
        m = _make_manifest()
        original_cleanup_status = m.cleanup_status
        original_critical = m.cleanup_outcome_critical
        r = CleanupExecutionResult(
            status=CLEANUP_STATUS_FAILED,
            outcome_critical=True,
            steps_run=1,
        )
        _ = apply_cleanup_to_manifest(m, r)
        # Original manifest unchanged (frozen + dataclasses.replace).
        assert m.cleanup_status == original_cleanup_status
        assert m.cleanup_outcome_critical == original_critical

    def test_persists_cleanup_fields_through_to_dict(self) -> None:
        """Propagation result serializes cleanup fields in to_dict."""
        m = _make_manifest()
        r = CleanupExecutionResult(
            status=CLEANUP_STATUS_FAILED,
            outcome_critical=True,
            steps_run=1,
        )
        new_m = apply_cleanup_to_manifest(m, r)
        d = new_m.to_dict()
        assert d["cleanup_status"] == "failed"
        assert d["cleanup_outcome_critical"] is True


# ---------------------------------------------------------------------
# End-to-end contract: TestCase → MockCleanupRunner → ManifestRecord
# ---------------------------------------------------------------------


class TestEndToEndPropagation:
    """Full contract: TestCase + MockCleanupRunner → ManifestRecord."""

    def test_propagation_passed_scenario(self) -> None:
        """Full chain: testcase cleanup → runner 'pass' → manifest records 'passed'."""
        step = AtomicTestStep(step_id="s1", action_id="close_session")
        tc = _make_testcase(
            case_id="case_pass",
            cleanup_steps=(step,),
            cleanup_outcome_critical=False,
        )
        runner = MockCleanupRunner(simulate="pass")
        result = runner.run(tc)
        m = _make_manifest(terminal_status="passed")
        final_m = apply_cleanup_to_manifest(m, result)

        assert final_m.cleanup_status == "passed"
        assert final_m.cleanup_outcome_critical is False

    def test_propagation_failed_scenario(self) -> None:
        """Full chain: testcase cleanup + critical → runner 'fail' → cascade-ready."""
        step = AtomicTestStep(step_id="s1", action_id="close_session")
        tc = _make_testcase(
            case_id="case_crit",
            cleanup_steps=(step,),
            cleanup_outcome_critical=True,
        )
        runner = MockCleanupRunner(simulate="fail")
        result = runner.run(tc)
        m = _make_manifest(terminal_status="passed")
        final_m = apply_cleanup_to_manifest(m, result)

        # Renderer-side cascade rule (D12) would now flip this to 'failed'.
        # P2.4.B only verifies the data is correctly propagated.
        assert final_m.cleanup_status == "failed"
        assert final_m.cleanup_outcome_critical is True

    def test_propagation_unknown_when_no_cleanup_steps(self) -> None:
        """No cleanup steps → runner returns 'unknown' → manifest 'unknown'."""
        tc = _make_testcase(
            case_id="case_nocleanup",
            cleanup_steps=(),
            cleanup_outcome_critical=True,
        )
        runner = MockCleanupRunner(simulate="pass")  # even with simulate=pass
        result = runner.run(tc)
        m = _make_manifest()
        final_m = apply_cleanup_to_manifest(m, result)

        assert final_m.cleanup_status == "unknown"
        # D11: outcome_critical copied verbatim regardless.
        assert final_m.cleanup_outcome_critical is True


# ---------------------------------------------------------------------
# Contract invariants for future P2.5 implementation
# ---------------------------------------------------------------------


class TestContractInvariants:
    """Properties P2.5 must preserve when replacing MockCleanupRunner."""

    def test_unknown_iff_not_executed(self) -> None:
        """Contract: status='unknown' iff cleanup was not executed.

        Verified for: empty cleanup list, simulate='unknown'.
        Both paths produce status='unknown' with steps_run=0.
        """
        step = AtomicTestStep(step_id="s1", action_id="close")

        # Path 1: empty cleanup list.
        tc_empty = _make_testcase(cleanup_steps=())
        r_empty = MockCleanupRunner(simulate="pass").run(tc_empty)
        assert r_empty.status == "unknown"
        assert r_empty.steps_run == 0

        # Path 2: simulate='unknown'.
        tc_with = _make_testcase(cleanup_steps=(step,))
        r_with = MockCleanupRunner(simulate="unknown").run(tc_with)
        assert r_with.status == "unknown"
        assert r_with.steps_run == 0

    def test_passed_iff_ran_all_success(self) -> None:
        """Contract: status='passed' iff cleanup ran and all steps succeeded."""
        step = AtomicTestStep(step_id="s1", action_id="close")
        tc = _make_testcase(cleanup_steps=(step,))
        result = MockCleanupRunner(simulate="pass").run(tc)
        assert result.status == "passed"
        # Contract: if passed, steps_run > 0.
        assert result.steps_run > 0

    def test_failed_iff_ran_with_failure(self) -> None:
        """Contract: status='failed' iff cleanup ran and ≥1 step failed."""
        step = AtomicTestStep(step_id="s1", action_id="close")
        tc = _make_testcase(cleanup_steps=(step,))
        result = MockCleanupRunner(simulate="fail").run(tc)
        assert result.status == "failed"
        # Contract: if failed, steps_run > 0.
        assert result.steps_run > 0