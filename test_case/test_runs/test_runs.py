"""
P2.3.A — RunContext, RunState, CaseAttemptRef, Manifest, sanitize_identifier,
MetricRecord unit tests (architecture §Checkpoint P2.3 / R1).

Covers:
  * test_sanitize_identifier_strict
  * test_attempt_id_monotonic
  * test_metric_redaction
  * test_runcontext_ownership
  * test_run_state_machine
  * test_manifest_schema_version

Run:
    cd /Users/edr-test/edr-wd
    PYTHONPATH=target:. python3 -m pytest test_case/test_runs -q
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.trace.runs import (
    ALLOWED_METRIC_KEYS,
    CaseAttemptRef,
    InvalidStateTransitionError,
    MANIFEST_SCHEMA_VERSION,
    Manifest,
    MetricKeyForbiddenError,
    MetricRecord,
    MetricValueTypeError,
    RunContext,
    RunState,
    SUPPORTED_MANIFEST_SCHEMA_VERSIONS,
    UnsupportedManifestSchemaError,
    atomic_write_json,
    make_attempt_id,
    sanitize_identifier,
)


# ----------------------------------------------------------------------
# test_sanitize_identifier_strict (D4)
# ----------------------------------------------------------------------

class TestSanitizeIdentifierStrict:
    """Per D4: sanitize_identifier allows only [a-zA-Z0-9_-].
    Empty, separators, control chars, absolute paths raise."""

    def test_alphanumeric_passes(self):
        assert sanitize_identifier("abc123") == "abc123"

    def test_hyphen_underscore_passes(self):
        assert sanitize_identifier("a-b_c-1_2") == "a-b_c-1_2"

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            sanitize_identifier("")

    def test_dotdot_raises(self):
        with pytest.raises(ValueError, match="forbidden"):
            sanitize_identifier("..")

    def test_slash_raises(self):
        with pytest.raises(ValueError, match="forbidden"):
            sanitize_identifier("a/b")

    def test_backslash_raises(self):
        with pytest.raises(ValueError, match="forbidden"):
            sanitize_identifier("a\\b")

    def test_absolute_path_raises(self):
        with pytest.raises(ValueError, match="forbidden"):
            sanitize_identifier("/etc/passwd")

    def test_control_char_raises(self):
        with pytest.raises(ValueError, match="forbidden"):
            sanitize_identifier("a\nb")

    def test_null_byte_raises(self):
        with pytest.raises(ValueError, match="forbidden"):
            sanitize_identifier("a\x00b")

    def test_space_raises(self):
        with pytest.raises(ValueError, match="forbidden"):
            sanitize_identifier("a b")

    def test_colon_raises(self):
        with pytest.raises(ValueError, match="forbidden"):
            sanitize_identifier("a:b")

    def test_dot_raises(self):
        with pytest.raises(ValueError, match="forbidden"):
            sanitize_identifier("a.b")

    def test_non_string_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            sanitize_identifier(None)  # type: ignore[arg-type]

    def test_int_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            sanitize_identifier(123)  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# test_attempt_id_monotonic (D3)
# ----------------------------------------------------------------------

class TestAttemptIdMonotonic:
    """Per D3: attempt-NNNN monotonic per (run_id, safe_case_id).
    Refuses to overwrite existing attempt directory."""

    def test_make_attempt_id_format(self):
        assert make_attempt_id(1) == "attempt-0001"
        assert make_attempt_id(2) == "attempt-0002"
        assert make_attempt_id(9999) == "attempt-9999"

    def test_make_attempt_id_rejects_zero(self):
        with pytest.raises(ValueError):
            make_attempt_id(0)

    def test_make_attempt_id_rejects_negative(self):
        with pytest.raises(ValueError):
            make_attempt_id(-1)

    def test_runcontext_increments_per_case(self, tmp_path: Path):
        ctx = RunContext(tmp_path, "run-A")
        r1 = ctx.add_case_attempt("case_A", "trace_001")
        assert r1.attempt_id == "attempt-0001"
        r2 = ctx.add_case_attempt("case_A", "trace_002")
        assert r2.attempt_id == "attempt-0002"
        r3 = ctx.add_case_attempt("case_A", "trace_003")
        assert r3.attempt_id == "attempt-0003"

    def test_runcontext_per_case_independent_counter(self, tmp_path: Path):
        """Each case has its own monotonic counter, starting at 1."""
        ctx = RunContext(tmp_path, "run-A")
        a1 = ctx.add_case_attempt("case_A", "trace_a1")
        b1 = ctx.add_case_attempt("case_B", "trace_b1")
        a2 = ctx.add_case_attempt("case_A", "trace_a2")
        assert a1.attempt_id == "attempt-0001"
        assert b1.attempt_id == "attempt-0001"
        assert a2.attempt_id == "attempt-0002"

    def test_runcontext_refuses_overwrite(self, tmp_path: Path):
        """Re-running the same case + trace_id produces a new attempt
        dir (attempt-0002-...) — does NOT overwrite attempt-0001."""
        ctx = RunContext(tmp_path, "run-A")
        r1 = ctx.add_case_attempt("case_A", "trace_001")
        # Calling again with same case_id + trace_id — counter must
        # increment to 0002, creating a new attempt dir alongside.
        r2 = ctx.add_case_attempt("case_A", "trace_001")
        assert r1.attempt_id == "attempt-0001"
        assert r2.attempt_id == "attempt-0002"
        assert r1.path != r2.path
        # Both attempt dirs still exist on disk (FR-P2.3-01 additive).
        assert r1.path.exists()
        assert r2.path.exists()

    def test_runcontext_attempt_dir_layout(self, tmp_path: Path):
        ctx = RunContext(tmp_path, "run-A")
        ref = ctx.add_case_attempt("case_A", "trace_001")
        assert ref.path.exists()
        assert ref.path.name == "attempt-0001-trace_001"
        assert ref.path.parent.name == "case_A"
        assert ref.path.parent.parent.name == "cases"

    def test_runcontext_case_attempts_returns_tuple(self, tmp_path: Path):
        ctx = RunContext(tmp_path, "run-A")
        ctx.add_case_attempt("case_A", "t1")
        ctx.add_case_attempt("case_B", "t2")
        attempts = ctx.case_attempts()
        assert isinstance(attempts, tuple)
        assert len(attempts) == 2


# ----------------------------------------------------------------------
# test_metric_redaction (D5)
# ----------------------------------------------------------------------

class TestMetricRedaction:
    """Per D5 / FR-P2.3-08: MetricRecord factory rejects keys outside
    the whitelist. screen_text, command_args, env, credentials cannot
    be added via any constructor path."""

    def test_allowed_keys_accepted(self):
        for key in ALLOWED_METRIC_KEYS:
            m = MetricRecord.make(key, 1)
            assert m.key == key
            assert m.value == 1

    def test_int_value_accepted(self):
        m = MetricRecord.make("duration_ms", 1234)
        assert m.value == 1234

    def test_str_value_accepted(self):
        m = MetricRecord.make("outcome", "passed")
        assert m.value == "passed"

    def test_forbidden_screen_text_raises(self):
        with pytest.raises(MetricKeyForbiddenError, match="screen_text"):
            MetricRecord.make("screen_text", "hello world")

    def test_forbidden_command_args_raises(self):
        with pytest.raises(MetricKeyForbiddenError, match="command_args"):
            MetricRecord.make("command_args", "--token=secret")

    def test_forbidden_environment_raises(self):
        with pytest.raises(MetricKeyForbiddenError, match="env"):
            MetricRecord.make("env", "PATH=/usr/bin")

    def test_forbidden_ocr_raises(self):
        with pytest.raises(MetricKeyForbiddenError, match="ocr"):
            MetricRecord.make("ocr", "extracted text")

    def test_forbidden_credentials_raises(self):
        with pytest.raises(MetricKeyForbiddenError, match="credentials"):
            MetricRecord.make("credentials", "secret")

    def test_forbidden_token_raises(self):
        with pytest.raises(MetricKeyForbiddenError, match="token"):
            MetricRecord.make("token", "abc123")

    def test_bool_value_rejected(self):
        with pytest.raises(MetricValueTypeError):
            MetricRecord.make("step_count", True)

    def test_list_value_rejected(self):
        with pytest.raises(MetricValueTypeError):
            MetricRecord.make("step_count", [1, 2, 3])  # type: ignore[arg-type]

    def test_dict_value_rejected(self):
        with pytest.raises(MetricValueTypeError):
            MetricRecord.make("step_count", {"x": 1})  # type: ignore[arg-type]

    def test_runcontext_records_metric(self, tmp_path: Path):
        ctx = RunContext(tmp_path, "run-A")
        ctx.record_metric("duration_ms", 5000)
        ctx.record_metric("outcome", "passed")
        assert len(ctx.metrics()) == 2

    def test_runcontext_metric_forbidden_key_raises(self, tmp_path: Path):
        ctx = RunContext(tmp_path, "run-A")
        with pytest.raises(MetricKeyForbiddenError):
            ctx.record_metric("screen_text", "leaked")


# ----------------------------------------------------------------------
# test_run_state_machine (D9)
# ----------------------------------------------------------------------

class TestRunStateMachine:
    """Per D9: CREATED → RUNNING (first add_case_attempt) → FINALIZED
    (finalize). Post-FINALIZED mutators raise InvalidStateTransitionError."""

    def test_initial_state_is_created(self, tmp_path: Path):
        ctx = RunContext(tmp_path, "run-A")
        assert ctx.state == RunState.CREATED

    def test_state_transitions_to_running_on_first_attempt(self, tmp_path: Path):
        ctx = RunContext(tmp_path, "run-A")
        assert ctx.state == RunState.CREATED
        ctx.add_case_attempt("case_A", "t1")
        assert ctx.state == RunState.RUNNING

    def test_state_stays_running_with_more_attempts(self, tmp_path: Path):
        ctx = RunContext(tmp_path, "run-A")
        ctx.add_case_attempt("case_A", "t1")
        ctx.add_case_attempt("case_A", "t2")
        assert ctx.state == RunState.RUNNING

    def test_state_transitions_to_finalized(self, tmp_path: Path):
        ctx = RunContext(tmp_path, "run-A")
        ctx.add_case_attempt("case_A", "t1")
        ctx.finalize()
        assert ctx.state == RunState.FINALIZED

    def test_finalize_empty_run_allowed(self, tmp_path: Path):
        """A run with no attempts can still be finalized."""
        ctx = RunContext(tmp_path, "run-A")
        assert ctx.state == RunState.CREATED
        ctx.finalize()
        assert ctx.state == RunState.FINALIZED

    def test_add_case_attempt_after_finalize_raises(self, tmp_path: Path):
        ctx = RunContext(tmp_path, "run-A")
        ctx.add_case_attempt("case_A", "t1")
        ctx.finalize()
        with pytest.raises(InvalidStateTransitionError, match="FINALIZED"):
            ctx.add_case_attempt("case_A", "t2")

    def test_record_metric_after_finalize_raises(self, tmp_path: Path):
        ctx = RunContext(tmp_path, "run-A")
        ctx.finalize()
        with pytest.raises(InvalidStateTransitionError, match="FINALIZED"):
            ctx.record_metric("duration_ms", 100)

    def test_double_finalize_raises(self, tmp_path: Path):
        ctx = RunContext(tmp_path, "run-A")
        ctx.finalize()
        with pytest.raises(InvalidStateTransitionError, match="FINALIZED"):
            ctx.finalize()

    def test_record_environment_after_finalize_raises(self, tmp_path: Path):
        ctx = RunContext(tmp_path, "run-A")
        ctx.finalize()
        with pytest.raises(InvalidStateTransitionError, match="FINALIZED"):
            ctx.record_environment({"k": "v"})

    def test_record_targets_after_finalize_raises(self, tmp_path: Path):
        ctx = RunContext(tmp_path, "run-A")
        ctx.finalize()
        with pytest.raises(InvalidStateTransitionError, match="FINALIZED"):
            ctx.record_requested_targets(["target1"])


# ----------------------------------------------------------------------
# test_runcontext_ownership (D1)
# ----------------------------------------------------------------------

class TestRunContextOwnership:
    """Per D1: RunContext is sole lifecycle owner of the run directory.
    Verifies the API surface mutates state only through this class —
    no other module can write to the run dir."""

    def test_run_id_validated_in_constructor(self, tmp_path: Path):
        with pytest.raises(ValueError):
            RunContext(tmp_path, "run/with/slash")

    def test_run_id_invalid_dotdot_raises(self, tmp_path: Path):
        with pytest.raises(ValueError):
            RunContext(tmp_path, "..")

    def test_run_dir_created_on_init(self, tmp_path: Path):
        ctx = RunContext(tmp_path, "run-A")
        assert ctx.run_dir.exists()
        assert ctx.run_dir.is_dir()
        assert ctx.run_dir.name == "run-A"

    def test_finalize_writes_manifest(self, tmp_path: Path):
        ctx = RunContext(tmp_path, "run-A")
        ctx.add_case_attempt("case_A", "t1")
        m = ctx.finalize()
        assert m.schema_version == MANIFEST_SCHEMA_VERSION
        manifest_path = ctx.run_dir / "manifest.json"
        assert manifest_path.exists()
        data = json.loads(manifest_path.read_text())
        assert data["schema_version"] == "1.0"
        assert data["run_id"] == "run-A"

    def test_aggregate_status_empty_run_is_passed(self, tmp_path: Path):
        ctx = RunContext(tmp_path, "run-A")
        m = ctx.finalize()
        assert m.aggregate_status == "passed"

    def test_aggregate_status_no_attempt_manifests_is_passed(self, tmp_path: Path):
        """When no case-attempt-manifest exists, status defaults to
        'passed' (no failures recorded)."""
        ctx = RunContext(tmp_path, "run-A")
        ctx.add_case_attempt("case_A", "t1")
        m = ctx.finalize()
        assert m.aggregate_status == "passed"

    def test_case_attempt_ref_helpers(self, tmp_path: Path):
        ref = CaseAttemptRef(
            safe_case_id="case_A",
            attempt_id="attempt-0001",
            trace_id="t1",
            path=tmp_path / "case_A" / "attempt-0001-t1",
        )
        assert ref.trace_md_path() == tmp_path / "case_A" / "attempt-0001-t1" / "trace.md"
        assert ref.step_results_path() == tmp_path / "case_A" / "attempt-0001-t1" / "step-results.json"
        assert ref.case_attempt_manifest_path() == tmp_path / "case_A" / "attempt-0001-t1" / "case-attempt-manifest.json"

    def test_atomic_write_json_no_leftover(self, tmp_path: Path):
        path = tmp_path / "test.json"
        atomic_write_json(path, {"k": "v"})
        assert path.exists()
        leftovers = list(tmp_path.glob("*.tmp"))
        assert leftovers == []

    def test_atomic_write_json_overwrites_existing(self, tmp_path: Path):
        path = tmp_path / "test.json"
        atomic_write_json(path, {"k": 1})
        atomic_write_json(path, {"k": 2})
        assert json.loads(path.read_text())["k"] == 2


# ----------------------------------------------------------------------
# test_manifest_schema_version (D8)
# ----------------------------------------------------------------------

class TestManifestSchemaVersion:
    """Per D8: schema_version is mandatory; only '1.0' accepted.
    Unknown / missing raises UnsupportedManifestSchemaError."""

    def test_supported_set_contains_1_0(self):
        assert "1.0" in SUPPORTED_MANIFEST_SCHEMA_VERSIONS
        assert MANIFEST_SCHEMA_VERSION == "1.0"

    def test_manifest_default_schema_version_is_1_0(self, tmp_path: Path):
        ctx = RunContext(tmp_path, "run-A")
        m = ctx.finalize()
        assert m.schema_version == "1.0"

    def test_from_dict_accepts_1_0(self):
        data = {
            "schema_version": "1.0",
            "run_id": "run-A",
            "started_at": "2026-08-03T10:00:00.000Z",
            "ended_at": "2026-08-03T10:15:00.000Z",
            "requested_targets": [],
            "environment": {},
            "case_attempts": [],
            "aggregate_status": "passed",
            "report_status": "valid",
            "evidence_status": "complete",
            "metrics_summary": [],
            "schema_versions": {},
            "renderer_version": "p2.3",
        }
        m = Manifest.from_dict(data)
        assert m.schema_version == "1.0"

    def test_from_dict_rejects_0_9(self):
        data = {"schema_version": "0.9", "run_id": "x"}
        with pytest.raises(UnsupportedManifestSchemaError, match="0.9"):
            Manifest.from_dict(data)

    def test_from_dict_rejects_missing_schema_version(self):
        data = {"run_id": "x"}
        with pytest.raises(UnsupportedManifestSchemaError, match="missing"):
            Manifest.from_dict(data)

    def test_from_dict_rejects_unknown_version(self):
        data = {"schema_version": "99.99", "run_id": "x"}
        with pytest.raises(UnsupportedManifestSchemaError, match="99.99"):
            Manifest.from_dict(data)

    def test_from_dict_rejects_non_mapping(self):
        with pytest.raises(UnsupportedManifestSchemaError):
            Manifest.from_dict([1, 2, 3])  # type: ignore[arg-type]

    def test_round_trip_via_disk(self, tmp_path: Path):
        """Write manifest via RunContext, read back via Manifest.from_dict."""
        ctx = RunContext(tmp_path, "run-A")
        ctx.add_case_attempt("case_A", "t1")
        ctx.record_metric("duration_ms", 5000)
        m1 = ctx.finalize()
        m2 = ctx.load_manifest()
        assert m1.schema_version == m2.schema_version == "1.0"
        assert m2.run_id == "run-A"
        assert len(m2.case_attempts) == 1
        assert m2.metrics_summary[0].key == "duration_ms"

    def test_to_dict_round_trip(self, tmp_path: Path):
        ctx = RunContext(tmp_path, "run-A")
        ctx.add_case_attempt("case_A", "t1")
        m1 = ctx.finalize()
        d = m1.to_dict()
        assert d["schema_version"] == "1.0"
        # round-trip via JSON
        encoded = json.dumps(d)
        decoded = json.loads(encoded)
        m2 = Manifest.from_dict(decoded)
        assert m2.run_id == m1.run_id
        assert m2.schema_version == m1.schema_version
        assert m2.report_status == m1.report_status
        assert m2.evidence_status == m1.evidence_status