"""
P2.3.B — render_report + reconcile_totals + ReportStatus tests
(architecture §Checkpoint P2.3 / R1).

Covers:
  * test_reconciliation_raises_on_mismatch
  * test_report_status_two_axis
  * test_totals_from_manifests
  * test_compute_aggregate_status
  * test_render_basic
  * test_render_with_reruns
  * test_render_missing_manifest
  * test_render_corrupt_manifest
  * test_evidence_severity_matrix (basic)

Run:
    cd /Users/edr-test/edr-wd
    PYTHONPATH=target:. python3 -m pytest test_case/test_runs/test_render.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.trace.render_report import (
    AttemptSplit,
    ManifestCorruptError,
    ManifestMissingError,
    ReconciliationError,
    ReportStatus,
    Totals,
    compute_aggregate_status,
    compute_attempt_split,
    reconcile_totals,
    render_report,
)
from agent.trace.runs import (
    RunContext,
    atomic_write_json,
)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _write_case_attempt_manifest(
    attempt_dir: Path,
    safe_case_id: str,
    attempt_id: str,
    trace_id: str,
    terminal_status: str = "passed",
    duration_ms: int = 1000,
) -> None:
    """Write a minimal case-attempt-manifest.json for renderer to read."""
    data = {
        "trace_id": trace_id,
        "branch_heads": [],
        "catalog_digest": "x" * 64,
        "evidence_counts": {},
        "terminal_status": terminal_status,
        "integrity_verification_result": {
            "ok": True,
            "issues": [],
            "verified_count": 0,
        },
    }
    atomic_write_json(attempt_dir / "case-attempt-manifest.json", data)


def _finalize_run_with_cases(
    tmp_path: Path,
    run_id: str,
    case_attempts: list[tuple[str, str, str, int]],
) -> tuple[RunContext, list]:
    """Create a finalized RunContext with given case attempts.

    case_attempts = [(case_id, trace_id, terminal_status, duration_ms), ...]
    duration_ms is unused by RunContext but kept for documentation.
    """
    ctx = RunContext(tmp_path, run_id)
    refs = []
    for case_id, trace_id, _ts, _dur in case_attempts:
        ref = ctx.add_case_attempt(case_id, trace_id)
        refs.append(ref)
    ctx.finalize()
    return ctx, refs


# ----------------------------------------------------------------------
# test_totals_from_manifests
# ----------------------------------------------------------------------

class TestTotalsFromManifests:
    def test_empty_returns_zero_totals(self):
        t = Totals.from_manifests([])
        assert t.passed == 0
        assert t.failed == 0
        assert t.blocked == 0
        assert t.skipped == 0
        assert t.total == 0

    def test_single_passed(self):
        m = [{"safe_case_id": "A", "attempt_id": "attempt-0001",
              "terminal_status": "passed"}]
        t = Totals.from_manifests(m)
        assert t.passed == 1
        assert t.total == 1

    def test_mixed_statuses(self):
        m = [
            {"safe_case_id": "A", "attempt_id": "attempt-0001",
             "terminal_status": "passed"},
            {"safe_case_id": "B", "attempt_id": "attempt-0001",
             "terminal_status": "failed"},
            {"safe_case_id": "C", "attempt_id": "attempt-0001",
             "terminal_status": "blocked"},
            {"safe_case_id": "D", "attempt_id": "attempt-0001",
             "terminal_status": "skipped"},
        ]
        t = Totals.from_manifests(m)
        assert t.passed == 1
        assert t.failed == 1
        assert t.blocked == 1
        assert t.skipped == 1
        assert t.total == 4

    def test_rerun_first_vs_final(self):
        """First attempt failed, final attempt passed."""
        m = [
            {"safe_case_id": "A", "attempt_id": "attempt-0001",
             "terminal_status": "failed"},
            {"safe_case_id": "A", "attempt_id": "attempt-0002",
             "terminal_status": "passed"},
        ]
        split = compute_attempt_split(m)
        assert split.first.failed == 1
        assert split.first.passed == 0
        assert split.final.passed == 1
        assert split.final.failed == 0
        # Totals.from_manifests counts all attempts
        t = Totals.from_manifests(m)
        assert t.passed == 1
        assert t.failed == 1

    def test_total_property(self):
        m = [
            {"safe_case_id": "A", "attempt_id": "attempt-0001",
             "terminal_status": "passed"},
            {"safe_case_id": "B", "attempt_id": "attempt-0001",
             "terminal_status": "failed"},
            {"safe_case_id": "C", "attempt_id": "attempt-0001",
             "terminal_status": "blocked"},
        ]
        t = Totals.from_manifests(m)
        assert t.total == 3


# ----------------------------------------------------------------------
# test_compute_aggregate_status
# ----------------------------------------------------------------------

class TestComputeAttemptSplit:
    """First vs Final Attempt split (FR-P2.3-03, review-required)."""

    def test_first_vs_final_attempt_split(self):
        """Spec acceptance test: attempt-0001 failed, attempt-0002
        passed. First totals = failed=1; Final totals = passed=1."""
        manifests = [
            {"safe_case_id": "A", "attempt_id": "attempt-0001",
             "terminal_status": "failed"},
            {"safe_case_id": "A", "attempt_id": "attempt-0002",
             "terminal_status": "passed"},
        ]
        split = compute_attempt_split(manifests)
        assert isinstance(split, AttemptSplit)
        # First = attempt-0001 (failed)
        assert split.first.failed == 1
        assert split.first.passed == 0
        assert split.first.total == 1
        # Final = attempt-0002 (passed)
        assert split.final.passed == 1
        assert split.final.failed == 0
        assert split.final.total == 1

    def test_single_attempt_case_is_both_first_and_final(self):
        """A case with only one attempt contributes 1 to both
        first and final totals."""
        manifests = [
            {"safe_case_id": "A", "attempt_id": "attempt-0001",
             "terminal_status": "passed"},
        ]
        split = compute_attempt_split(manifests)
        assert split.first.passed == 1
        assert split.final.passed == 1

    def test_multiple_cases(self):
        """case_A first=passed/final=passed; case_B first=failed/final=passed.
        First: passed=1, failed=1. Final: passed=2, failed=0."""
        manifests = [
            {"safe_case_id": "A", "attempt_id": "attempt-0001",
             "terminal_status": "passed"},
            {"safe_case_id": "B", "attempt_id": "attempt-0001",
             "terminal_status": "failed"},
            {"safe_case_id": "B", "attempt_id": "attempt-0002",
             "terminal_status": "passed"},
        ]
        split = compute_attempt_split(manifests)
        assert split.first.passed == 1
        assert split.first.failed == 1
        assert split.final.passed == 2
        assert split.final.failed == 0

    def test_intermediate_attempts_excluded(self):
        """A case with 3 attempts — only attempt-0001 and attempt-0003
        contribute to first/final."""
        manifests = [
            {"safe_case_id": "A", "attempt_id": "attempt-0001",
             "terminal_status": "failed"},
            {"safe_case_id": "A", "attempt_id": "attempt-0002",
             "terminal_status": "failed"},  # not counted in split
            {"safe_case_id": "A", "attempt_id": "attempt-0003",
             "terminal_status": "passed"},
        ]
        split = compute_attempt_split(manifests)
        assert split.first.failed == 1  # only attempt-0001
        assert split.final.passed == 1  # only attempt-0003
        # Totals (NOT split) counts everything
        t = Totals.from_manifests(manifests)
        assert t.failed == 2
        assert t.passed == 1

    def test_empty_input(self):
        """No manifests → both first and final totals are zero."""
        split = compute_attempt_split([])
        assert split.first.total == 0
        assert split.final.total == 0


# ----------------------------------------------------------------------
# test_compute_aggregate_status
# ----------------------------------------------------------------------

class TestComputeAggregateStatus:
    def test_empty_is_passed(self):
        assert compute_aggregate_status([]) == "passed"

    def test_only_passed(self):
        assert compute_aggregate_status(["passed", "passed"]) == "passed"

    def test_any_failed_makes_failed(self):
        assert compute_aggregate_status(["passed", "failed"]) == "failed"

    def test_blocked_without_failed(self):
        assert compute_aggregate_status(["passed", "blocked"]) == "blocked"

    def test_priority_order(self):
        assert compute_aggregate_status(
            ["passed", "blocked", "failed", "passed"]
        ) == "failed"

    def test_unknown_ranks_below_passed(self):
        """unknown should not upgrade passed."""
        assert compute_aggregate_status(["passed", "unknown"]) == "passed"


# ----------------------------------------------------------------------
# test_reconciliation_raises_on_mismatch (D2 / FR-P2.3-02)
# ----------------------------------------------------------------------

class TestReconciliationRaisesOnMismatch:
    def test_matching_totals_passes(self):
        manifests = [
            {"safe_case_id": "A", "attempt_id": "attempt-0001",
             "terminal_status": "passed"},
            {"safe_case_id": "B", "attempt_id": "attempt-0001",
             "terminal_status": "failed"},
        ]
        headline = Totals(passed=1, failed=1, blocked=0, skipped=0)
        # No raise
        reconcile_totals(manifests, headline)

    def test_mismatch_raises(self):
        """Headline says passed=5 but only 3 manifests are passed —
        must raise, not silently fix."""
        manifests = [
            {"safe_case_id": "A", "attempt_id": "attempt-0001",
             "terminal_status": "passed"},
            {"safe_case_id": "B", "attempt_id": "attempt-0001",
             "terminal_status": "passed"},
            {"safe_case_id": "C", "attempt_id": "attempt-0001",
             "terminal_status": "passed"},
        ]
        headline = Totals(passed=5, failed=0, blocked=0, skipped=0)
        with pytest.raises(ReconciliationError, match="disagree"):
            reconcile_totals(manifests, headline)

    def test_no_silent_fix(self):
        """Confirm: mismatch raises, does NOT silently change values."""
        manifests = [
            {"safe_case_id": "A", "attempt_id": "attempt-0001",
             "terminal_status": "passed"},
        ]
        headline = Totals(passed=10, failed=0, blocked=0, skipped=0)
        with pytest.raises(ReconciliationError):
            reconcile_totals(manifests, headline)
        # Headline must not have been mutated
        assert headline.passed == 10


# ----------------------------------------------------------------------
# test_report_status_two_axis (D10)
# ----------------------------------------------------------------------

class TestReportStatusTwoAxis:
    def test_valid_complete_banner(self):
        rs = ReportStatus(report_status="valid", evidence_status="complete")
        rs_line, es_line = rs.as_banner_lines()
        assert "valid" in rs_line
        assert "complete" in es_line

    def test_invalid_banner_distinct(self):
        rs = ReportStatus(report_status="invalid", evidence_status="degraded")
        rs_line, es_line = rs.as_banner_lines()
        assert "invalid" in rs_line
        assert "degraded" in es_line
        assert "NOT trustworthy" in rs_line

    def test_two_axis_are_independent(self):
        """report_status and evidence_status are independent dimensions."""
        # valid + degraded is a legitimate state
        rs = ReportStatus(report_status="valid", evidence_status="degraded")
        assert rs.report_status == "valid"
        assert rs.evidence_status == "degraded"


# ----------------------------------------------------------------------
# test_render_basic
# ----------------------------------------------------------------------

class TestRenderBasic:
    def test_render_empty_run(self, tmp_path: Path):
        ctx = RunContext(tmp_path, "run-empty")
        ctx.finalize()
        body, status = render_report(ctx, attempts=())
        assert status.report_status == "valid"
        assert "run-empty" in body
        assert "## Headline Totals" in body
        assert "*Renderer: p2.3 | Reconciliation: passed*" in body

    def test_render_three_case_statuses(self, tmp_path: Path):
        """3 cases with passed/failed/blocked."""
        ctx, refs = _finalize_run_with_cases(tmp_path, "run-3", [
            ("case_A", "trace_a1", "passed", 1000),
            ("case_B", "trace_b1", "failed", 2000),
            ("case_C", "trace_c1", "blocked", 500),
        ])
        # Add case-attempt-manifest.json for each
        for ref, (cid, tid, ts, dur) in zip(
            refs, [
                ("case_A", "trace_a1", "passed", 1000),
                ("case_B", "trace_b1", "failed", 2000),
                ("case_C", "trace_c1", "blocked", 500),
            ]
        ):
            _write_case_attempt_manifest(
                ref.path, cid, ref.attempt_id, tid, ts, dur
            )
        body, status = render_report(ctx, attempts=refs)
        assert status.report_status == "valid"
        assert "case_A" in body
        assert "case_B" in body
        assert "case_C" in body
        # Headline counts
        assert "| Passed | 1 |" in body
        assert "| Failed | 1 |" in body
        assert "| Blocked | 1 |" in body
        # Failures section
        assert "## Failures And Blocks" in body
        assert "(failed)" in body

    def test_render_with_reruns(self, tmp_path: Path):
        """Same case appears twice (rerun additive)."""
        ctx = RunContext(tmp_path, "run-rerun")
        r1 = ctx.add_case_attempt("case_A", "trace_001")
        r2 = ctx.add_case_attempt("case_A", "trace_002")
        _write_case_attempt_manifest(
            r1.path, "case_A", r1.attempt_id, "trace_001",
            terminal_status="failed", duration_ms=1000
        )
        _write_case_attempt_manifest(
            r2.path, "case_A", r2.attempt_id, "trace_002",
            terminal_status="passed", duration_ms=2000
        )
        ctx.finalize()
        body, status = render_report(ctx, attempts=[r1, r2])
        assert status.report_status == "valid"
        assert "| 2 |" in body  # Per-case Attempts column: 2 attempts
        assert "| passed |" in body  # Final status column shows "passed"
        # First vs Final Attempt section: first failed, final passed
        assert "| Failed | 1 | 0 |" in body
        assert "| Passed | 0 | 1 |" in body
        # Evidence warnings appear because trace.md / step-results.json missing
        assert "## Warnings" in body

    def test_render_deterministic(self, tmp_path: Path):
        """Same inputs + same frozen_generated_at = same output (FR-05)."""
        ctx, refs = _finalize_run_with_cases(tmp_path, "run-det", [
            ("case_A", "trace_a1", "passed", 1000),
        ])
        for ref in refs:
            _write_case_attempt_manifest(
                ref.path, ref.safe_case_id, ref.attempt_id,
                ref.trace_id, "passed", 1000
            )
        body1, _ = render_report(
            ctx, attempts=refs, frozen_generated_at="2026-08-03T10:00:00.000Z"
        )
        body2, _ = render_report(
            ctx, attempts=refs, frozen_generated_at="2026-08-03T10:00:00.000Z"
        )
        assert body1 == body2

    def test_render_frozen_generated_at_appears(self, tmp_path: Path):
        ctx = RunContext(tmp_path, "run-frozen")
        ctx.finalize()
        body, _ = render_report(
            ctx, attempts=(),
            frozen_generated_at="2026-08-03T12:34:56.789Z"
        )
        assert "2026-08-03T12:34:56.789Z" in body


# ----------------------------------------------------------------------
# test_render_missing_manifest (D6 fail severity)
# ----------------------------------------------------------------------

class TestRenderManifestErrors:
    def test_missing_manifest_raises(self, tmp_path: Path):
        """No manifest.json → ManifestMissingError (fail severity)."""
        ctx = RunContext(tmp_path, "run-no-manifest")
        # Don't finalize — no manifest.json
        with pytest.raises(ManifestMissingError):
            render_report(ctx, attempts=())

    def test_corrupt_manifest_raises(self, tmp_path: Path):
        """manifest.json with invalid JSON → ManifestCorruptError."""
        ctx = RunContext(tmp_path, "run-corrupt")
        ctx.finalize()
        # Overwrite with corrupt JSON
        (ctx.run_dir / "manifest.json").write_text("{not json")
        with pytest.raises(ManifestCorruptError):
            render_report(ctx, attempts=())

    def test_unsupported_schema_version_returns_invalid_status(self, tmp_path: Path):
        """schema_version 0.9 → render returns invalid status (D8)."""
        ctx = RunContext(tmp_path, "run-old-schema")
        ctx.finalize()
        # Tamper schema_version
        path = ctx.run_dir / "manifest.json"
        data = json.loads(path.read_text())
        data["schema_version"] = "0.9"
        atomic_write_json(path, data)
        body, status = render_report(ctx, attempts=())
        assert status.report_status == "invalid"


# ----------------------------------------------------------------------
# test_evidence_severity_matrix (D6)
# ----------------------------------------------------------------------

class TestEvidenceSeverityMatrix:
    def test_missing_trace_md_warning(self, tmp_path: Path):
        """trace.md missing → warning, but render succeeds (warn severity)."""
        ctx, refs = _finalize_run_with_cases(tmp_path, "run-evidence", [
            ("case_A", "trace_a1", "passed", 1000),
        ])
        # Write case-attempt-manifest but NOT trace.md
        for ref in refs:
            _write_case_attempt_manifest(
                ref.path, ref.safe_case_id, ref.attempt_id,
                ref.trace_id, "passed"
            )
        body, status = render_report(ctx, attempts=refs)
        # Status still valid (totals reconcilable), evidence degraded
        assert status.report_status == "valid"
        assert status.evidence_status == "degraded"
        assert "trace.md missing" in body

    def test_complete_evidence(self, tmp_path: Path):
        """All evidence present → evidence_status = complete."""
        ctx, refs = _finalize_run_with_cases(tmp_path, "run-complete", [
            ("case_A", "trace_a1", "passed", 1000),
        ])
        for ref in refs:
            _write_case_attempt_manifest(
                ref.path, ref.safe_case_id, ref.attempt_id,
                ref.trace_id, "passed"
            )
            # Also write trace.md and step-results.json
            ref.trace_md_path().write_text("# Trace\n")
            ref.step_results_path().write_text("[]")
        body, status = render_report(ctx, attempts=refs)
        assert status.evidence_status == "complete"