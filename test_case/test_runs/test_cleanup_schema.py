"""
P2.4.A — Schema extension tests for cleanup fields.

Covers the schema additions in P2.4.A:
  * TestCase.cleanup_outcome_critical field
  * ManifestRecord.{cleanup_status, cleanup_outcome_critical} fields
  * Backward compat: missing cleanup fields → safe defaults

References:
  docs/requirements/P2-cleanup-escape-design.md §9.1
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from agent.trace.integrity import IntegrityReport
from agent.trace.manifest import (
    CLEANUP_STATUS_FAILED,
    CLEANUP_STATUS_PASSED,
    CLEANUP_STATUS_UNKNOWN,
    ManifestRecord,
    SCHEMA_VERSION,
    from_dict as manifest_from_dict,
)
from target.protocol_models.models import (
    AtomicTestStep,
    ProtocolModelError,
    TestCase,
)


# ---------------------------------------------------------------------
# TestCase.cleanup_outcome_critical
# ---------------------------------------------------------------------


class TestTestCaseCleanupOutcomeCritical:
    """TestCase schema extension: cleanup_outcome_critical field."""

    def test_testcase_cleanup_outcome_critical_default_false(self) -> None:
        """Default cleanup_outcome_critical is False (non-critical)."""
        tc = TestCase(
            case_id="case_A",
            title="Case A",
        )
        assert tc.cleanup_outcome_critical is False

    def test_testcase_cleanup_outcome_critical_explicit_true(self) -> None:
        """cleanup_outcome_critical=True is accepted."""
        tc = TestCase(
            case_id="case_B",
            title="Case B",
            cleanup_outcome_critical=True,
        )
        assert tc.cleanup_outcome_critical is True

    def test_testcase_strict_from_dict_accepts_new_field(self) -> None:
        """from_dict accepts cleanup_outcome_critical in input."""
        tc = TestCase.from_dict({
            "case_id": "case_C",
            "title": "Case C",
            "cleanup_outcome_critical": True,
        })
        assert tc.cleanup_outcome_critical is True

    def test_testcase_strict_from_dict_default_when_omitted(self) -> None:
        """from_dict without cleanup_outcome_critical → default False."""
        tc = TestCase.from_dict({
            "case_id": "case_D",
            "title": "Case D",
        })
        assert tc.cleanup_outcome_critical is False

    def test_testcase_strict_from_dict_rejects_non_bool(self) -> None:
        """from_dict rejects non-bool cleanup_outcome_critical (strict mode)."""
        with pytest.raises(ProtocolModelError) as exc_info:
            TestCase.from_dict({
                "case_id": "case_E",
                "title": "Case E",
                "cleanup_outcome_critical": "yes",  # str, not bool
            })
        assert exc_info.value.code == "type_error"
        assert exc_info.value.path == "cleanup_outcome_critical"

    def test_testcase_dataclass_rejects_non_bool_at_construction(self) -> None:
        """__post_init__ rejects non-bool cleanup_outcome_critical."""
        with pytest.raises(ProtocolModelError) as exc_info:
            TestCase(
                case_id="case_F",
                title="Case F",
                cleanup_outcome_critical=1,  # type: ignore[arg-type]
            )
        assert exc_info.value.code == "type_error"
        assert exc_info.value.path == "cleanup_outcome_critical"

    def test_testcase_frozen_dataclass_prevents_mutation(self) -> None:
        """TestCase is frozen; cannot mutate cleanup_outcome_critical."""
        tc = TestCase(case_id="case_G", title="Case G")
        with pytest.raises(FrozenInstanceError):
            tc.cleanup_outcome_critical = True  # type: ignore[misc]

    def test_testcase_with_cleanup_steps_and_outcome_critical(self) -> None:
        """Realistic case: cleanup steps + outcome-critical flag."""
        step = AtomicTestStep(step_id="s1", action_id="close_session")
        tc = TestCase(
            case_id="case_H",
            title="Case H with cleanup",
            cleanup=(step,),
            cleanup_outcome_critical=True,
        )
        assert len(tc.cleanup) == 1
        assert tc.cleanup_outcome_critical is True


# ---------------------------------------------------------------------
# ManifestRecord schema
# ---------------------------------------------------------------------


def _make_inv_report() -> IntegrityReport:
    return IntegrityReport(ok=True, issues=())


class TestManifestRecordCleanupSchema:
    """ManifestRecord schema extension: cleanup_status + cleanup_outcome_critical."""

    def test_manifest_record_cleanup_status_default_unknown(self) -> None:
        """Default cleanup_status is 'unknown' (backward compat, D13)."""
        rec = ManifestRecord(
            trace_id="trace_001",
            branch_heads=(),
            catalog_digest="a" * 64,
            evidence_counts={},
            terminal_status="passed",
            integrity_verification_result=_make_inv_report(),
        )
        assert rec.cleanup_status == CLEANUP_STATUS_UNKNOWN

    def test_manifest_record_cleanup_outcome_critical_default_false(self) -> None:
        """Default cleanup_outcome_critical is False (non-critical, D13)."""
        rec = ManifestRecord(
            trace_id="trace_002",
            branch_heads=(),
            catalog_digest="a" * 64,
            evidence_counts={},
            terminal_status="passed",
            integrity_verification_result=_make_inv_report(),
        )
        assert rec.cleanup_outcome_critical is False

    def test_manifest_record_to_dict_includes_cleanup_fields(self) -> None:
        """to_dict writes cleanup_status + cleanup_outcome_critical."""
        rec = ManifestRecord(
            trace_id="trace_003",
            branch_heads=(),
            catalog_digest="a" * 64,
            evidence_counts={},
            terminal_status="passed",
            integrity_verification_result=_make_inv_report(),
            cleanup_status=CLEANUP_STATUS_FAILED,
            cleanup_outcome_critical=True,
        )
        d = rec.to_dict()
        assert d["cleanup_status"] == "failed"
        assert d["cleanup_outcome_critical"] is True
        # Schema version unchanged (D16).
        assert d["schema_version"] == SCHEMA_VERSION

    def test_manifest_record_rejects_invalid_cleanup_status(self) -> None:
        """__post_init__ rejects cleanup_status outside enum."""
        with pytest.raises(ValueError) as exc_info:
            ManifestRecord(
                trace_id="trace_004",
                branch_heads=(),
                catalog_digest="a" * 64,
                evidence_counts={},
                terminal_status="passed",
                integrity_verification_result=_make_inv_report(),
                cleanup_status="error",  # invalid enum value
            )
        assert "cleanup_status" in str(exc_info.value)

    def test_manifest_record_rejects_non_bool_outcome_critical(self) -> None:
        """__post_init__ rejects non-bool cleanup_outcome_critical."""
        with pytest.raises(TypeError):
            ManifestRecord(
                trace_id="trace_005",
                branch_heads=(),
                catalog_digest="a" * 64,
                evidence_counts={},
                terminal_status="passed",
                integrity_verification_result=_make_inv_report(),
                cleanup_outcome_critical="yes",  # type: ignore[arg-type]
            )

    def test_manifest_record_frozen_dataclass(self) -> None:
        """ManifestRecord is frozen; cannot mutate cleanup fields."""
        rec = ManifestRecord(
            trace_id="trace_006",
            branch_heads=(),
            catalog_digest="a" * 64,
            evidence_counts={},
            terminal_status="passed",
            integrity_verification_result=_make_inv_report(),
        )
        with pytest.raises(FrozenInstanceError):
            rec.cleanup_status = CLEANUP_STATUS_PASSED  # type: ignore[misc]


class TestManifestRecordFromDictBackwardCompat:
    """from_dict defaults for missing cleanup fields (D13)."""

    def test_from_dict_missing_cleanup_fields_uses_default(self) -> None:
        """P2.3-era manifest without cleanup_* fields → safe defaults."""
        legacy = {
            "trace_id": "trace_legacy",
            "branch_heads": [],
            "catalog_digest": "b" * 64,
            "evidence_counts": {},
            "terminal_status": "passed",
            "integrity_verification_result": {"ok": True, "issues": []},
        }
        rec = manifest_from_dict(legacy)
        assert rec.cleanup_status == CLEANUP_STATUS_UNKNOWN
        assert rec.cleanup_outcome_critical is False

    def test_from_dict_with_cleanup_fields_round_trips(self) -> None:
        """Manifest with cleanup fields round-trips through from_dict/to_dict."""
        original = ManifestRecord(
            trace_id="trace_rt",
            branch_heads=("main",),
            catalog_digest="c" * 64,
            evidence_counts={"screenshots": 2},
            terminal_status="passed",
            integrity_verification_result=_make_inv_report(),
            cleanup_status=CLEANUP_STATUS_PASSED,
            cleanup_outcome_critical=True,
        )
        d = original.to_dict()
        restored = manifest_from_dict(d)
        assert restored.cleanup_status == "passed"
        assert restored.cleanup_outcome_critical is True
        assert restored.terminal_status == "passed"
        assert restored.trace_id == "trace_rt"

    def test_from_dict_partial_cleanup_status_only(self) -> None:
        """cleanup_status present, cleanup_outcome_critical missing → False."""
        partial = {
            "trace_id": "trace_partial",
            "branch_heads": [],
            "catalog_digest": "d" * 64,
            "evidence_counts": {},
            "terminal_status": "passed",
            "cleanup_status": "failed",
            # cleanup_outcome_critical MISSING
            "integrity_verification_result": {"ok": True, "issues": []},
        }
        rec = manifest_from_dict(partial)
        assert rec.cleanup_status == "failed"
        # outcome-critical missing → False → NO CASCADE (D13)
        assert rec.cleanup_outcome_critical is False

    def test_from_dict_partial_cleanup_outcome_critical_only(self) -> None:
        """cleanup_outcome_critical present, cleanup_status missing → 'unknown'."""
        partial = {
            "trace_id": "trace_partial2",
            "branch_heads": [],
            "catalog_digest": "e" * 64,
            "evidence_counts": {},
            "terminal_status": "passed",
            "cleanup_outcome_critical": True,
            # cleanup_status MISSING
            "integrity_verification_result": {"ok": True, "issues": []},
        }
        rec = manifest_from_dict(partial)
        # cleanup_status missing → "unknown" → NO CASCADE (D13)
        assert rec.cleanup_status == CLEANUP_STATUS_UNKNOWN
        assert rec.cleanup_outcome_critical is True


# ---------------------------------------------------------------------
# P2.3 manifests still validate
# ---------------------------------------------------------------------


class TestP23ManifestStillValidates:
    """Backward compat: P2.3-era manifests deserialize cleanly."""

    def test_p23_manifest_minimal_loads_with_safe_defaults(
        self, tmp_path: Path,
    ) -> None:
        """A P2.3-era manifest (no cleanup fields) loads cleanly."""
        legacy_path = tmp_path / "case-attempt-manifest.json"
        legacy_path.write_text(
            '{"schema_version": "1.0.0", "trace_id": "trace_p23", '
            '"branch_heads": [], "catalog_digest": "' + "f" * 64 + '", '
            '"evidence_counts": {}, "terminal_status": "passed", '
            '"integrity_verification_result": {"ok": true, "issues": []}}',
            encoding="utf-8",
        )
        import json
        data = json.loads(legacy_path.read_text(encoding="utf-8"))
        rec = manifest_from_dict(data)
        assert rec.cleanup_status == CLEANUP_STATUS_UNKNOWN
        assert rec.cleanup_outcome_critical is False
        # P2.3.A renderer (TestAttemptRow) would see terminal_status
        # unchanged; no cascade.
        assert rec.terminal_status == "passed"