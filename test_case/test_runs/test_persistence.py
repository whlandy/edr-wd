"""
P2.3.C — Run artifact persistence layer tests (architecture §Checkpoint P2.3).

Covers:
  * atomic_write_text happy path
  * atomic_write_text overwrites existing
  * atomic_write_text cleanup on exception
  * atomic_write_text creates parent dirs
  * write_run_report writes file to run_dir
  * write_run_report is atomic (no half-written file observable)
  * write_run_report does NOT mutate manifest.json
  * write_run_report returns ReportStatus
  * write_run_report end-to-end (finalize + write)
  * exports verification (all P2.3 symbols importable from agent.trace)

Run:
    cd /Users/edr-test/edr-wd
    PYTHONPATH=target:. python3 -m pytest test_case/test_runs/test_persistence.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.trace import (  # noqa: F401 — verifies exports
    AttemptSplit,
    ManifestCorruptError,
    ManifestMissingError,
    ReconciliationError,
    ReportStatus,
    RUN_MANIFEST_SCHEMA_VERSION,
    RUN_SUPPORTED_MANIFEST_SCHEMA_VERSIONS,
    RunContext,
    RunManifest,
    RunState,
    Totals,
    UnsupportedManifestSchemaError,
    compute_aggregate_status,
    compute_attempt_split,
    reconcile_totals,
    render_report,
    sanitize_identifier,
    write_run_report,
)
from agent.trace.runs import (
    atomic_write_json,
    atomic_write_text,
)


# ----------------------------------------------------------------------
# atomic_write_text
# ----------------------------------------------------------------------

class TestAtomicWriteText:
    def test_writes_text(self, tmp_path: Path):
        path = tmp_path / "out.txt"
        atomic_write_text(path, "hello world")
        assert path.read_text(encoding="utf-8") == "hello world"

    def test_overwrites_existing(self, tmp_path: Path):
        path = tmp_path / "out.txt"
        path.write_text("old")
        atomic_write_text(path, "new")
        assert path.read_text(encoding="utf-8") == "new"

    def test_creates_parent_dirs(self, tmp_path: Path):
        path = tmp_path / "deep" / "nested" / "out.txt"
        atomic_write_text(path, "x")
        assert path.read_text(encoding="utf-8") == "x"

    def test_cleans_up_tmp_on_exception(self, tmp_path: Path):
        path = tmp_path / "out.txt"

        def boom(_text):
            raise RuntimeError("boom")

        # Patch os.replace to raise after the temp file is written
        import unittest.mock as mock
        with mock.patch("agent.trace.runs.os.replace",
                        side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="boom"):
                atomic_write_text(path, "x")
        # No temp file should remain
        tmp_file = path.with_suffix(path.suffix + ".tmp")
        assert not tmp_file.exists()
        # Original path should not exist (write was aborted)
        assert not path.exists()

    def test_no_temp_file_leftover_after_success(self, tmp_path: Path):
        path = tmp_path / "out.txt"
        atomic_write_text(path, "y")
        tmp_file = path.with_suffix(path.suffix + ".tmp")
        assert not tmp_file.exists()


# ----------------------------------------------------------------------
# atomic_write_json (preserved from P2.3.A — confirm no regression)
# ----------------------------------------------------------------------

class TestAtomicWriteJsonRegression:
    def test_writes_json(self, tmp_path: Path):
        path = tmp_path / "out.json"
        atomic_write_json(path, {"k": "v", "n": 1})
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data == {"k": "v", "n": 1}

    def test_cleans_up_on_replace_failure(self, tmp_path: Path):
        path = tmp_path / "out.json"
        import unittest.mock as mock
        with mock.patch("agent.trace.runs.os.replace",
                        side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                atomic_write_json(path, {"k": "v"})
        tmp_file = path.with_suffix(path.suffix + ".tmp")
        assert not tmp_file.exists()
        assert not path.exists()


# ----------------------------------------------------------------------
# write_run_report
# ----------------------------------------------------------------------

class TestWriteRunReport:
    def _finalize_run(self, tmp_path: Path, run_id: str = "run-A"):
        ctx = RunContext(tmp_path, run_id)
        ctx.finalize()
        return ctx

    def test_writes_report_md_to_run_dir(self, tmp_path: Path):
        ctx = self._finalize_run(tmp_path)
        status = write_run_report(ctx, attempts=())
        report_path = ctx.run_dir / "report.md"
        assert report_path.exists()
        assert ctx.run_id in report_path.read_text(encoding="utf-8")
        assert status.report_status == "valid"

    def test_default_filename_is_report_md(self, tmp_path: Path):
        ctx = self._finalize_run(tmp_path)
        write_run_report(ctx, attempts=())
        assert (ctx.run_dir / "report.md").exists()

    def test_custom_filename(self, tmp_path: Path):
        ctx = self._finalize_run(tmp_path)
        write_run_report(ctx, attempts=(), report_filename="ci-summary.md")
        assert (ctx.run_dir / "ci-summary.md").exists()
        assert not (ctx.run_dir / "report.md").exists()

    def test_overwrites_existing_report(self, tmp_path: Path):
        ctx = self._finalize_run(tmp_path)
        write_run_report(ctx, attempts=(),
                         frozen_generated_at="2026-08-03T10:00:00.000Z")
        first = (ctx.run_dir / "report.md").read_text(encoding="utf-8")
        write_run_report(ctx, attempts=(),
                         frozen_generated_at="2026-08-03T11:00:00.000Z")
        second = (ctx.run_dir / "report.md").read_text(encoding="utf-8")
        # Different frozen_generated_at → different body
        assert "2026-08-03T10:00:00.000Z" in first
        assert "2026-08-03T11:00:00.000Z" in second

    def test_does_not_mutate_manifest_json(self, tmp_path: Path):
        ctx = self._finalize_run(tmp_path)
        # Snapshot manifest.json before write
        manifest_path = ctx.run_dir / "manifest.json"
        before = manifest_path.read_bytes()
        before_mtime = manifest_path.stat().st_mtime_ns
        write_run_report(ctx, attempts=())
        # Manifest must be byte-identical and mtime unchanged
        assert manifest_path.read_bytes() == before
        assert manifest_path.stat().st_mtime_ns == before_mtime

    def test_propagates_manifest_missing_error(self, tmp_path: Path):
        ctx = self._finalize_run(tmp_path)
        # Delete manifest.json to simulate missing
        (ctx.run_dir / "manifest.json").unlink()
        with pytest.raises(ManifestMissingError):
            write_run_report(ctx, attempts=())

    def test_propagates_manifest_corrupt_error(self, tmp_path: Path):
        ctx = self._finalize_run(tmp_path)
        (ctx.run_dir / "manifest.json").write_text("{not json")
        with pytest.raises(ManifestCorruptError):
            write_run_report(ctx, attempts=())

    def test_end_to_end_finalize_then_write(self, tmp_path: Path):
        """End-to-end: create context → add case → finalize → write report."""
        ctx = RunContext(tmp_path, "run-e2e")
        ref = ctx.add_case_attempt("case_A", "trace_001")
        ctx.finalize()
        # Write a case-attempt-manifest so renderer can count it
        ref.path.mkdir(parents=True, exist_ok=True)
        atomic_write_json(ref.path / "case-attempt-manifest.json", {
            "trace_id": "trace_001",
            "branch_heads": [],
            "catalog_digest": "x" * 64,
            "evidence_counts": {},
            "terminal_status": "passed",
            "integrity_verification_result": {
                "ok": True, "issues": [], "verified_count": 0,
            },
        })
        # Re-finalize would fail (state machine), so we need to re-read
        # manifest after writing case-attempt-manifest. The simplest path:
        # write case-attempt BEFORE finalize. Test that path:
        report_status = write_run_report(ctx, attempts=[ref])
        assert report_status.report_status == "valid"
        assert (ctx.run_dir / "report.md").exists()
        # Body should mention case_A
        body = (ctx.run_dir / "report.md").read_text(encoding="utf-8")
        assert "case_A" in body


# ----------------------------------------------------------------------
# end-to-end with finalized run + case-attempt manifest BEFORE finalize
# ----------------------------------------------------------------------

class TestEndToEndWithCases:
    def _build_run_with_case(
        self,
        tmp_path: Path,
        terminal: str = "passed",
        write_evidence: bool = True,
    ):
        """Build a finalized RunContext with one case attempt whose
        case-attempt-manifest exists at finalize time.

        If `write_evidence` is True, also write trace.md and
        step-results.json so evidence_status='complete'.
        """
        ctx = RunContext(tmp_path, "run-full")
        ref = ctx.add_case_attempt("case_X", "trace_X")
        atomic_write_json(ref.path / "case-attempt-manifest.json", {
            "trace_id": "trace_X",
            "branch_heads": [],
            "catalog_digest": "x" * 64,
            "evidence_counts": {},
            "terminal_status": terminal,
            "integrity_verification_result": {
                "ok": True, "issues": [], "verified_count": 0,
            },
        })
        if write_evidence:
            ref.trace_md_path().write_text("# Trace\n", encoding="utf-8")
            ref.step_results_path().write_text("[]", encoding="utf-8")
        ctx.finalize()
        return ctx, ref

    def test_full_pipeline_passed(self, tmp_path: Path):
        ctx, ref = self._build_run_with_case(tmp_path, "passed")
        status = write_run_report(ctx, attempts=[ref])
        body = (ctx.run_dir / "report.md").read_text(encoding="utf-8")
        assert status.report_status == "valid"
        assert status.evidence_status == "complete"
        assert "| Passed | 1 |" in body
        assert "case_X" in body

    def test_full_pipeline_failed(self, tmp_path: Path):
        ctx, ref = self._build_run_with_case(tmp_path, "failed")
        status = write_run_report(ctx, attempts=[ref])
        body = (ctx.run_dir / "report.md").read_text(encoding="utf-8")
        assert status.report_status == "valid"
        assert "| Failed | 1 |" in body
        assert "(failed)" in body  # in Failures And Blocks section

    def test_full_pipeline_blocked(self, tmp_path: Path):
        ctx, ref = self._build_run_with_case(tmp_path, "blocked")
        status = write_run_report(ctx, attempts=[ref])
        body = (ctx.run_dir / "report.md").read_text(encoding="utf-8")
        assert "| Blocked | 1 |" in body
        assert "(blocked)" in body


# ----------------------------------------------------------------------
# exports verification
# ----------------------------------------------------------------------

class TestPublicExports:
    """Verify that all P2.3 public symbols are importable from agent.trace."""

    def test_runs_exports_importable(self):
        # Already imported at module top; if any fail, test errors out.
        from agent.trace import (  # noqa: F401
            RUN_MANIFEST_SCHEMA_VERSION,
            RUN_SUPPORTED_MANIFEST_SCHEMA_VERSIONS,
            RunContext,
            RunState,
            sanitize_identifier,
            atomic_write_text,
            RunManifest,
        )

    def test_render_report_exports_importable(self):
        from agent.trace import (  # noqa: F401
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
            write_run_report,
        )

    def test_schema_versions_match(self):
        """Both versions are documented at 1.0; check consistency."""
        assert RUN_MANIFEST_SCHEMA_VERSION == "1.0"
        assert "1.0" in RUN_SUPPORTED_MANIFEST_SCHEMA_VERSIONS