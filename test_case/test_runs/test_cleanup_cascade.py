"""
P2.4.C — Renderer cascade + Cleanup Warnings section tests.

Covers:
  * `compute_display_status()` cascade rule (D12).
  * `has_cleanup_failure()` helper.
  * Cleanup Warnings markdown section (rendered/omitted).
  * Integration with render_report pipeline.

References:
  docs/requirements/P2-cleanup-escape-design.md §5.3 + §9.3
"""

from __future__ import annotations

from typing import Any, Mapping

import pytest

from agent.trace.cleanup_cascade import (
    CLEANUP_FAILED,
    CLEANUP_PASSED,
    CLEANUP_UNKNOWN,
    compute_display_status,
    has_cleanup_failure,
)
from agent.trace.runs import CaseAttemptRef, RunContext
from agent.trace.render_report import (
    render_report,
    write_run_report,
)


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------


def _manifest(
    *,
    terminal: str = "passed",
    cleanup_status: str = CLEANUP_UNKNOWN,
    cleanup_outcome_critical: bool = False,
    case_id: str = "case_x",
) -> dict[str, Any]:
    """Construct a minimal case-attempt manifest dict for cascade tests."""
    return {
        "safe_case_id": case_id,
        "attempt_id": "attempt-0001",
        "trace_id": "trace_x",
        "terminal_status": terminal,
        "cleanup_status": cleanup_status,
        "cleanup_outcome_critical": cleanup_outcome_critical,
        "evidence_counts": {},
        "integrity_verification_result": {"ok": True, "issues": []},
    }


# ---------------------------------------------------------------------
# compute_display_status — cascade rule
# ---------------------------------------------------------------------


class TestComputeDisplayStatus:
    """D12 cascade rule: passed→failed iff (terminal=passed AND cleanup=failed AND outcome_critical=True)."""

    def test_terminal_passed_cleanup_passed_returns_passed(self) -> None:
        """Both passed → no cascade."""
        m = _manifest(
            terminal="passed",
            cleanup_status=CLEANUP_PASSED,
            cleanup_outcome_critical=True,
        )
        assert compute_display_status(m) == "passed"

    def test_terminal_passed_cleanup_unknown_returns_passed(self) -> None:
        """Cleanup 'unknown' → no cascade (D13 backward compat)."""
        m = _manifest(
            terminal="passed",
            cleanup_status=CLEANUP_UNKNOWN,
            cleanup_outcome_critical=False,
        )
        assert compute_display_status(m) == "passed"

    def test_terminal_passed_cleanup_failed_noncritical_returns_passed(self) -> None:
        """Cleanup failed but non-critical → no cascade."""
        m = _manifest(
            terminal="passed",
            cleanup_status=CLEANUP_FAILED,
            cleanup_outcome_critical=False,
        )
        assert compute_display_status(m) == "passed"

    def test_terminal_passed_cleanup_failed_critical_returns_failed(self) -> None:
        """Cleanup failed AND critical → cascade to failed."""
        m = _manifest(
            terminal="passed",
            cleanup_status=CLEANUP_FAILED,
            cleanup_outcome_critical=True,
        )
        assert compute_display_status(m) == "failed"

    def test_terminal_failed_cleanup_passed_returns_failed(self) -> None:
        """Terminal already failed → no cascade (cascade only adds)."""
        m = _manifest(
            terminal="failed",
            cleanup_status=CLEANUP_PASSED,
            cleanup_outcome_critical=True,
        )
        assert compute_display_status(m) == "failed"

    def test_terminal_failed_cleanup_failed_no_change(self) -> None:
        """cleanup=failed+critical, terminal=failed → display=failed (no change)."""
        m = _manifest(
            terminal="failed",
            cleanup_status=CLEANUP_FAILED,
            cleanup_outcome_critical=True,
        )
        assert compute_display_status(m) == "failed"

    def test_terminal_blocked_returns_blocked(self) -> None:
        """Terminal blocked → blocked (no cascade)."""
        m = _manifest(
            terminal="blocked",
            cleanup_status=CLEANUP_FAILED,
            cleanup_outcome_critical=True,
        )
        assert compute_display_status(m) == "blocked"

    def test_terminal_skipped_returns_skipped(self) -> None:
        """Terminal skipped → skipped (no cascade)."""
        m = _manifest(
            terminal="skipped",
            cleanup_status=CLEANUP_FAILED,
            cleanup_outcome_critical=True,
        )
        assert compute_display_status(m) == "skipped"

    def test_missing_cleanup_fields_use_defaults(self) -> None:
        """Backward compat: missing cleanup_* → unknown/False → no cascade."""
        m = {"terminal_status": "passed"}
        assert compute_display_status(m) == "passed"

    def test_missing_terminal_returns_unknown(self) -> None:
        """Missing terminal_status → 'unknown'."""
        m: Mapping[str, Any] = {}
        assert compute_display_status(m) == "unknown"

    def test_does_not_mutate_input(self) -> None:
        """compute_display_status is read-time only (D17)."""
        m = _manifest(
            terminal="passed",
            cleanup_status=CLEANUP_FAILED,
            cleanup_outcome_critical=True,
        )
        original_terminal = m["terminal_status"]
        original_cleanup = m["cleanup_status"]
        _ = compute_display_status(m)
        assert m["terminal_status"] == original_terminal
        assert m["cleanup_status"] == original_cleanup


# ---------------------------------------------------------------------
# has_cleanup_failure
# ---------------------------------------------------------------------


class TestHasCleanupFailure:
    """has_cleanup_failure() — informational helper for Cleanup Warnings section."""

    def test_cleanup_passed_returns_false(self) -> None:
        assert has_cleanup_failure(
            _manifest(cleanup_status=CLEANUP_PASSED)
        ) is False

    def test_cleanup_unknown_returns_false(self) -> None:
        assert has_cleanup_failure(
            _manifest(cleanup_status=CLEANUP_UNKNOWN)
        ) is False

    def test_cleanup_failed_returns_true(self) -> None:
        assert has_cleanup_failure(
            _manifest(cleanup_status=CLEANUP_FAILED)
        ) is True

    def test_missing_cleanup_status_returns_false(self) -> None:
        """Backward compat: missing cleanup_status → False."""
        m = {"terminal_status": "passed"}
        assert has_cleanup_failure(m) is False


# ---------------------------------------------------------------------
# render_report integration — Cleanup Warnings section
# ---------------------------------------------------------------------


def _setup_run_with_cleanup_scenario(
    tmp_path,
    *,
    cleanup_status: str = CLEANUP_UNKNOWN,
    cleanup_outcome_critical: bool = False,
    terminal: str = "passed",
):
    """Create a RunContext with one case-attempt manifest for cleanup tests."""
    run_dir = tmp_path / "run_test"
    run_dir.mkdir()
    run = RunContext(root=tmp_path, run_id="run_test")
    run.finalize()

    # Write run-level manifest.json (P2.3.A contract).
    (run_dir / "manifest.json").write_text(
        '{"schema_version": "1.0", "run_id": "run_test", '
        '"started_at": "2024-01-01T00:00:00Z", "ended_at": '
        '"2024-01-01T00:01:00Z", "renderer_version": "p2.4", '
        '"evidence_status": "complete"}',
        encoding="utf-8",
    )

    # Write case-attempt manifest with cleanup fields.
    case_id = "case_clean"
    safe_cid = "case_clean_safe"
    attempt_id = "attempt-0001"
    case_dir = run_dir / case_id / attempt_id
    case_dir.mkdir(parents=True)
    import json
    (case_dir / "case-attempt-manifest.json").write_text(
        json.dumps({
            "schema_version": "1.0.0",
            "trace_id": "trace_clean",
            "branch_heads": [],
            "catalog_digest": "a" * 64,
            "evidence_counts": {},
            "terminal_status": terminal,
            "cleanup_status": cleanup_status,
            "cleanup_outcome_critical": cleanup_outcome_critical,
            "integrity_verification_result": {"ok": True, "issues": []},
        }),
        encoding="utf-8",
    )

    attempt = CaseAttemptRef(
        safe_case_id=safe_cid,
        attempt_id=attempt_id,
        trace_id="trace_clean",
        path=case_dir,
    )
    return run, [attempt]


class TestCleanupWarningsSection:
    """Cleanup Warnings section renders/omits per cascade."""

    def test_cleanup_warnings_section_omitted_when_all_passed(
        self, tmp_path,
    ) -> None:
        """No cleanup failures → no Cleanup Warnings section."""
        run, attempts = _setup_run_with_cleanup_scenario(
            tmp_path,
            cleanup_status=CLEANUP_PASSED,
            cleanup_outcome_critical=False,
        )
        body, _ = render_report(run, attempts=attempts)
        assert "## Cleanup Warnings" not in body

    def test_cleanup_warnings_section_renders_when_failed_present(
        self, tmp_path,
    ) -> None:
        """Cleanup failed → Cleanup Warnings section appears."""
        run, attempts = _setup_run_with_cleanup_scenario(
            tmp_path,
            cleanup_status=CLEANUP_FAILED,
            cleanup_outcome_critical=False,
        )
        body, _ = render_report(run, attempts=attempts)
        assert "## Cleanup Warnings" in body
        # Case appears in the warnings table.
        assert "case_clean" in body
        # Outcome critical flag surfaced.
        assert "no" in body  # outcome_critical=False → "no"

    def test_cleanup_warnings_shows_outcome_critical_yes(
        self, tmp_path,
    ) -> None:
        """Outcome critical=yes → table shows yes."""
        run, attempts = _setup_run_with_cleanup_scenario(
            tmp_path,
            cleanup_status=CLEANUP_FAILED,
            cleanup_outcome_critical=True,
        )
        body, _ = render_report(run, attempts=attempts)
        assert "## Cleanup Warnings" in body
        assert "yes" in body


class TestCascadeIntegration:
    """End-to-end cascade: cleanup failure + critical → failed case status."""

    def test_cascade_flips_headline_totals(self, tmp_path) -> None:
        """cleanup=failed+critical → totals shift from passed to failed."""
        run, attempts = _setup_run_with_cleanup_scenario(
            tmp_path,
            cleanup_status=CLEANUP_FAILED,
            cleanup_outcome_critical=True,
            terminal="passed",
        )
        body, _ = render_report(run, attempts=attempts)
        # Headline totals row.
        assert "| Passed | 0 |" in body
        assert "| Failed | 1 |" in body

    def test_no_cascade_when_cleanup_failed_noncritical(
        self, tmp_path,
    ) -> None:
        """cleanup=failed+non-critical → case stays passed (no cascade)."""
        run, attempts = _setup_run_with_cleanup_scenario(
            tmp_path,
            cleanup_status=CLEANUP_FAILED,
            cleanup_outcome_critical=False,
            terminal="passed",
        )
        body, _ = render_report(run, attempts=attempts)
        # Case is still passed in headline.
        assert "| Passed | 1 |" in body
        assert "| Failed | 0 |" in body
        # But Cleanup Warnings section appears (informational).
        assert "## Cleanup Warnings" in body

    def test_cascade_does_not_affect_unknown_cleanup(self, tmp_path) -> None:
        """cleanup=unknown → no cascade (backward compat)."""
        run, attempts = _setup_run_with_cleanup_scenario(
            tmp_path,
            cleanup_status=CLEANUP_UNKNOWN,
            cleanup_outcome_critical=True,  # even with crit=True, unknown → no cascade
            terminal="passed",
        )
        body, _ = render_report(run, attempts=attempts)
        assert "| Passed | 1 |" in body
        # No Cleanup Warnings (status is unknown, not failed).
        assert "## Cleanup Warnings" not in body

    def test_cascade_in_failures_section_marks_cascaded_origin(
        self, tmp_path,
    ) -> None:
        """Failure from cascade → annotated as cascaded from cleanup."""
        run, attempts = _setup_run_with_cleanup_scenario(
            tmp_path,
            cleanup_status=CLEANUP_FAILED,
            cleanup_outcome_critical=True,
            terminal="passed",
        )
        body, _ = render_report(run, attempts=attempts)
        # Failures section appears AND is annotated as cascaded.
        assert "## Failures And Blocks" in body
        assert "cascaded from cleanup failure" in body


class TestUnknownCleanupStatusNoCascade:
    """D13 + D12 explicit: cleanup_status='unknown' never cascades."""

    def test_explicit_unknown_no_cascade_with_critical_flag(
        self, tmp_path,
    ) -> None:
        """Even with cleanup_outcome_critical=True, explicit 'unknown' → no cascade."""
        run, attempts = _setup_run_with_cleanup_scenario(
            tmp_path,
            cleanup_status=CLEANUP_UNKNOWN,
            cleanup_outcome_critical=True,
            terminal="passed",
        )
        body, _ = render_report(run, attempts=attempts)
        # Case stays passed.
        assert "| Passed | 1 |" in body
        # No Cleanup Warnings (status is unknown).
        assert "## Cleanup Warnings" not in body

    def test_missing_cleanup_status_no_cascade(
        self, tmp_path,
    ) -> None:
        """P2.3-era manifest without cleanup_* fields → no cascade."""
        run_dir = tmp_path / "run_legacy"
        run_dir.mkdir()
        run = RunContext(root=tmp_path, run_id="run_legacy")
        run.finalize()
        (run_dir / "manifest.json").write_text(
            '{"schema_version": "1.0", "run_id": "run_legacy", '
            '"started_at": "2024-01-01T00:00:00Z", "ended_at": '
            '"2024-01-01T00:01:00Z", "renderer_version": "p2.4", '
            '"evidence_status": "complete"}',
            encoding="utf-8",
        )

        case_id = "case_legacy"
        case_dir = run_dir / case_id / "attempt-0001"
        case_dir.mkdir(parents=True)
        import json
        # P2.3-era manifest — NO cleanup_* fields.
        (case_dir / "case-attempt-manifest.json").write_text(
            json.dumps({
                "schema_version": "1.0.0",
                "trace_id": "trace_legacy",
                "branch_heads": [],
                "catalog_digest": "b" * 64,
                "evidence_counts": {},
                "terminal_status": "passed",
                "integrity_verification_result": {"ok": True, "issues": []},
            }),
            encoding="utf-8",
        )

        attempt = CaseAttemptRef(
            safe_case_id="case_legacy_safe",
            attempt_id="attempt-0001",
            trace_id="trace_legacy",
            path=case_dir,
        )
        body, _ = render_report(run, attempts=[attempt])
        # Legacy manifest → case stays passed (no cascade).
        assert "| Passed | 1 |" in body
        # No Cleanup Warnings (status defaults to unknown).
        assert "## Cleanup Warnings" not in body