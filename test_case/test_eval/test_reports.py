"""
P3.2.B — Evaluation result schema unit tests.

Covers D18 invariants:
  * Required top-level fields.
  * JSON-parseable round-trip.
  * Initial schema version `report_schema.v1`.
  * No observed screen text / args / selectors /
    confirmation tokens / LLM prompts.
  * UTC timezone-aware datetimes.
  * Within (dataset_version, planner_version) pair, schema
    evolves additively only (enforced by SUPPORTED_SCHEMA_VERSIONS
    being a frozen set with version increment required).
  * Sanitised types only for expected/observed (D21 contract).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from agent.eval.reports import (
    EXPECTATION_KINDS,
    OBSERVED_KINDS,
    EvaluationReport,
    FixtureResult,
    MetricValue,
    REPORT_SCHEMA_VERSION,
    REPORT_SCHEMA_VERSION as _RSV,
    SanitisedObserved,
    SensitiveContentError,
    StructuredExpectation,
    SUPPORTED_SCHEMA_VERSIONS,
    ThresholdDecision,
    from_json,
    to_json,
    validate_no_sensitive_content,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_report(
    *,
    schema_version: str = REPORT_SCHEMA_VERSION,
    dataset_id: str = "eval-fixtures",
    dataset_version: str = "v1",
    planner_version: str = "p3.1.0",
    execution_profile: str = "default",
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    metrics: dict[str, MetricValue] | None = None,
    threshold_decisions: tuple[ThresholdDecision, ...] = (),
    reproducibility_digest: str = "",
    fixture_results: tuple[FixtureResult, ...] = (),
    run_id: str = "run-2026-08-04-001",
) -> EvaluationReport:
    if started_at is None:
        started_at = datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc)
    if ended_at is None:
        ended_at = started_at + timedelta(seconds=10)
    if metrics is None:
        metrics = {
            "parse_success_rate": MetricValue(
                metric_id="parse_success_rate",
                value=0.95,
                unit="ratio",
                direction="higher_is_better",
            ),
        }
    return EvaluationReport(
        schema_version=schema_version,
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        planner_version=planner_version,
        execution_profile=execution_profile,
        started_at=started_at,
        ended_at=ended_at,
        metrics=metrics,
        threshold_decisions=threshold_decisions,
        reproducibility_digest=reproducibility_digest,
        fixture_results=fixture_results,
        run_id=run_id,
    )


# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------


class TestSchemaVersion:
    def test_initial_schema_version(self) -> None:
        assert REPORT_SCHEMA_VERSION == "report_schema.v1"

    def test_supported_versions_is_frozen_set(self) -> None:
        """SUPPORTED_SCHEMA_VERSIONS MUST be a frozen set
        so callers cannot mutate it."""
        assert isinstance(SUPPORTED_SCHEMA_VERSIONS, frozenset)
        assert REPORT_SCHEMA_VERSION in SUPPORTED_SCHEMA_VERSIONS

    def test_unknown_schema_version_rejected(self) -> None:
        with pytest.raises(ValueError, match="schema_version MUST be one of"):
            make_report(schema_version="report_schema.v999")


# ---------------------------------------------------------------------------
# Required fields
# ---------------------------------------------------------------------------


class TestRequiredFields:
    def test_dataset_id_required(self) -> None:
        with pytest.raises(ValueError, match="dataset_id MUST be non-empty"):
            make_report(dataset_id="")

    def test_dataset_version_required(self) -> None:
        with pytest.raises(ValueError, match="dataset_version MUST be non-empty"):
            make_report(dataset_version="")

    def test_planner_version_required(self) -> None:
        with pytest.raises(ValueError, match="planner_version MUST be non-empty"):
            make_report(planner_version="")

    def test_execution_profile_required(self) -> None:
        with pytest.raises(ValueError, match="execution_profile MUST be non-empty"):
            make_report(execution_profile="")

    def test_run_id_required(self) -> None:
        with pytest.raises(ValueError, match="run_id MUST be non-empty"):
            make_report(run_id="")


# ---------------------------------------------------------------------------
# Datetimes
# ---------------------------------------------------------------------------


class TestDatetimes:
    def test_started_at_must_be_timezone_aware(self) -> None:
        naive = datetime(2026, 8, 4, 10, 0, 0)
        with pytest.raises(ValueError, match="started_at MUST be timezone-aware"):
            make_report(started_at=naive)

    def test_ended_at_must_be_timezone_aware(self) -> None:
        aware = datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc)
        with pytest.raises(ValueError, match="ended_at MUST be timezone-aware"):
            make_report(started_at=aware, ended_at=aware.replace(tzinfo=None))

    def test_non_utc_timezone_rejected(self) -> None:
        # UTC+8.
        tz_plus8 = timezone(timedelta(hours=8))
        with pytest.raises(ValueError, match="started_at MUST be UTC"):
            make_report(
                started_at=datetime(2026, 8, 4, 10, 0, 0, tzinfo=tz_plus8)
            )

    def test_ended_at_before_started_at_rejected(self) -> None:
        start = datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc)
        with pytest.raises(ValueError, match="ended_at MUST be >= started_at"):
            make_report(
                started_at=start, ended_at=start - timedelta(seconds=1)
            )

    def test_utc_datetimes_accepted(self) -> None:
        start = datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 8, 4, 10, 0, 10, tzinfo=timezone.utc)
        report = make_report(started_at=start, ended_at=end)
        assert report.started_at == start
        assert report.ended_at == end


# ---------------------------------------------------------------------------
# JSON round-trip
# ---------------------------------------------------------------------------


class TestJsonRoundTrip:
    def test_to_dict_has_required_fields(self) -> None:
        report = make_report()
        d = report.to_dict()
        for field in (
            "schema_version",
            "dataset_id",
            "dataset_version",
            "planner_version",
            "execution_profile",
            "started_at",
            "ended_at",
            "metrics",
            "threshold_decisions",
            "reproducibility_digest",
            "run_id",
            "fixture_results",
        ):
            assert field in d

    def test_round_trip_equality(self) -> None:
        report = make_report()
        d = report.to_dict()
        restored = EvaluationReport.from_dict(d)
        assert restored == report

    def test_round_trip_via_json_string(self) -> None:
        report = make_report()
        text = to_json(report)
        restored = from_json(text)
        assert restored == report

    def test_deterministic_json_keys(self) -> None:
        """Same report → same JSON bytes (sort_keys=True
        for digest computation in P3.2.E)."""
        report = make_report()
        text_a = to_json(report)
        text_b = to_json(report)
        assert text_a == text_b

    def test_from_dict_rejects_non_dict(self) -> None:
        with pytest.raises(ValueError, match="MUST be a JSON object"):
            from_json("[1, 2, 3]")


# ---------------------------------------------------------------------------
# Sensitive content detection (D18 + D21)
# ---------------------------------------------------------------------------


class TestSensitiveContentDetection:
    def test_validate_clean_dict_passes(self) -> None:
        report = make_report()
        validate_no_sensitive_content(report.to_dict())

    def test_observed_screen_text_rejected(self) -> None:
        """Long string fields are flagged as likely observed
        screen text or LLM prompt leakage."""
        long_text = "x" * 300
        d = make_report().to_dict()
        d["metrics"]["smuggled"] = {"smuggled_value": long_text}
        with pytest.raises(
            SensitiveContentError, match="exceeds.*chars"
        ):
            validate_no_sensitive_content(d)

    def test_xpath_selector_rejected(self) -> None:
        d = make_report().to_dict()
        d["metrics"]["smuggled"] = {"xpath": "//button[@id='submit']"}
        with pytest.raises(
            SensitiveContentError, match="forbidden pattern"
        ):
            validate_no_sensitive_content(d)

    def test_long_hex_token_rejected(self) -> None:
        d = make_report().to_dict()
        # 40 hex chars — D14 token shape.
        d["metrics"]["smuggled"] = {"token": "a" * 40 + "bcdef12345"}
        with pytest.raises(
            SensitiveContentError, match="forbidden pattern"
        ):
            validate_no_sensitive_content(d)

    def test_base64_token_rejected(self) -> None:
        d = make_report().to_dict()
        d["metrics"]["smuggled"] = {"token": "Q" * 50}
        with pytest.raises(
            SensitiveContentError, match="forbidden pattern"
        ):
            validate_no_sensitive_content(d)

    def test_llm_prompt_leakage_rejected(self) -> None:
        d = make_report().to_dict()
        d["metrics"]["smuggled"] = {"prompt": "You are an AI assistant"}
        with pytest.raises(
            SensitiveContentError, match="forbidden pattern"
        ):
            validate_no_sensitive_content(d)

    def test_system_role_marker_rejected(self) -> None:
        d = make_report().to_dict()
        d["metrics"]["smuggled"] = {"prompt": "system: do thing"}
        with pytest.raises(
            SensitiveContentError, match="forbidden pattern"
        ):
            validate_no_sensitive_content(d)

    def test_query_selector_rejected(self) -> None:
        d = make_report().to_dict()
        d["metrics"]["smuggled"] = {"sel": "document.querySelector('.x')"}
        with pytest.raises(
            SensitiveContentError, match="forbidden pattern"
        ):
            validate_no_sensitive_content(d)

    def test_error_path_is_actionable(self) -> None:
        """The error message MUST identify the offending
        path so audit logs are actionable."""
        d = make_report().to_dict()
        d["metrics"]["smuggled"] = {"xpath": "//button"}
        with pytest.raises(SensitiveContentError) as exc_info:
            validate_no_sensitive_content(d)
        assert exc_info.value.path  # non-empty path

    def test_from_dict_rejects_sensitive_content(self) -> None:
        """A report dict with smuggled selectors MUST be
        rejected at deserialisation."""
        d = make_report().to_dict()
        d["metrics"]["smuggled"] = {"xpath": "//button[@id='x']"}
        with pytest.raises(SensitiveContentError):
            from_json(_safe_dumps(d))


def _safe_dumps(d: dict[str, Any]) -> str:
    """json.dumps wrapper that round-trips through the
    report schema. Used in tests where we need to construct
    a JSON string containing forbidden content."""
    from json import dumps as _dumps

    return _dumps(d, sort_keys=True)


# ---------------------------------------------------------------------------
# Sanitised payload types (D21)
# ---------------------------------------------------------------------------


class TestSanitisedPayloads:
    def test_structured_expectation_kinds(self) -> None:
        assert "threshold" in EXPECTATION_KINDS
        assert "stage_reached" in EXPECTATION_KINDS
        assert "match_target" in EXPECTATION_KINDS

    def test_sanitised_observed_kinds(self) -> None:
        assert "value" in OBSERVED_KINDS
        assert "mismatch" in OBSERVED_KINDS
        assert "stage_missed" in OBSERVED_KINDS

    def test_invalid_expectation_kind_rejected(self) -> None:
        with pytest.raises(ValueError, match="kind MUST be one of"):
            StructuredExpectation(kind="free_form", value={})

    def test_invalid_observed_kind_rejected(self) -> None:
        with pytest.raises(ValueError, match="kind MUST be one of"):
            SanitisedObserved(kind="raw_text", summary={})

    def test_structured_expectation_accepts_valid(self) -> None:
        exp = StructuredExpectation(
            kind="threshold", value={"value": 0.95}
        )
        assert exp.kind == "threshold"
        assert exp.value == {"value": 0.95}


# ---------------------------------------------------------------------------
# MetricValue + ThresholdDecision + FixtureResult invariants
# ---------------------------------------------------------------------------


class TestMetricValue:
    def test_invalid_direction_rejected(self) -> None:
        with pytest.raises(ValueError, match="direction MUST be"):
            MetricValue(
                metric_id="x", value=1.0, unit="ratio", direction="unknown"
            )

    def test_valid_directions_accepted(self) -> None:
        MetricValue(
            metric_id="x", value=1.0, unit="ratio", direction="higher_is_better"
        )
        MetricValue(
            metric_id="x", value=1.0, unit="ratio", direction="lower_is_better"
        )


class TestThresholdDecision:
    def test_invalid_decision_rejected(self) -> None:
        with pytest.raises(ValueError, match="decision MUST be"):
            ThresholdDecision(
                metric_id="x",
                thresholded=True,
                decision="unknown",
                observed=0.5,
                threshold=0.95,
            )

    def test_thresholded_requires_threshold(self) -> None:
        with pytest.raises(ValueError, match="thresholded=True requires threshold"):
            ThresholdDecision(
                metric_id="x",
                thresholded=True,
                decision="pass",
                observed=0.5,
                threshold=None,
            )

    def test_ungated_must_not_have_threshold(self) -> None:
        with pytest.raises(ValueError, match="ungated"):
            ThresholdDecision(
                metric_id="x",
                thresholded=False,
                decision="ungated",
                observed=0.5,
                threshold=0.95,
            )

    def test_thresholded_pass(self) -> None:
        td = ThresholdDecision(
            metric_id="x",
            thresholded=True,
            decision="pass",
            observed=0.96,
            threshold=0.95,
        )
        assert td.decision == "pass"

    def test_ungated(self) -> None:
        td = ThresholdDecision(
            metric_id="x",
            thresholded=False,
            decision="ungated",
            observed=0.5,
            threshold=None,
        )
        assert td.decision == "ungated"


class TestFixtureResult:
    def test_invalid_status_rejected(self) -> None:
        with pytest.raises(ValueError, match="status MUST be"):
            FixtureResult(
                fixture_id="x",
                status="unknown",
                expected=StructuredExpectation(kind="stage_reached", value={"name": "x"}),
                observed=SanitisedObserved(kind="stage_reached", summary={"name": "x"}),
            )

    def test_valid_statuses(self) -> None:
        for s in ("completed", "aborted", "errored"):
            FixtureResult(
                fixture_id="x",
                status=s,
                expected=StructuredExpectation(kind="stage_reached", value={"name": "x"}),
                observed=SanitisedObserved(kind="stage_reached", summary={"name": "x"}),
            )


# ---------------------------------------------------------------------------
# End-to-end report round-trip with sanitised payloads
# ---------------------------------------------------------------------------


class TestEndToEndReport:
    def test_full_report_round_trip(self) -> None:
        start = datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 8, 4, 10, 0, 30, tzinfo=timezone.utc)
        report = make_report(
            started_at=start,
            ended_at=end,
            metrics={
                "parse_success_rate": MetricValue(
                    metric_id="parse_success_rate",
                    value=0.95,
                    unit="ratio",
                    direction="higher_is_better",
                ),
                "execution_divergence_rate": MetricValue(
                    metric_id="execution_divergence_rate",
                    value=0.05,
                    unit="ratio",
                    direction="lower_is_better",
                ),
            },
            threshold_decisions=(
                ThresholdDecision(
                    metric_id="parse_success_rate",
                    thresholded=True,
                    decision="pass",
                    observed=0.95,
                    threshold=0.90,
                ),
                ThresholdDecision(
                    metric_id="experimental_metric",
                    thresholded=False,
                    decision="ungated",
                    observed=0.42,
                    threshold=None,
                ),
            ),
            fixture_results=(
                FixtureResult(
                    fixture_id="click_login",
                    status="completed",
                    expected=StructuredExpectation(
                        kind="stage_reached", value={"name": "login"}
                    ),
                    observed=SanitisedObserved(
                        kind="stage_reached", summary={"name": "login"}
                    ),
                ),
                FixtureResult(
                    fixture_id="delete_account",
                    status="aborted",
                    expected=StructuredExpectation(
                        kind="match_target",
                        value={"category": "danger_button"},
                    ),
                    observed=SanitisedObserved(
                        kind="mismatch",
                        summary={"category": "wrong_target", "digest": "sha256:abc123"},
                    ),
                ),
            ),
            reproducibility_digest="sha256:fake-digest-pending",
        )
        text = to_json(report)
        restored = from_json(text)
        assert restored == report
        # Sanitised kinds preserved.
        assert restored.fixture_results[0].expected.kind == "stage_reached"
        assert restored.fixture_results[1].observed.kind == "mismatch"
        # Thresholded vs ungated preserved.
        assert restored.threshold_decisions[0].thresholded is True
        assert restored.threshold_decisions[1].thresholded is False
        assert restored.threshold_decisions[1].threshold is None


# ---------------------------------------------------------------------------
# Sanity check: ensure _RSV alias matches constant
# ---------------------------------------------------------------------------


def test_schema_version_alias_matches() -> None:
    assert _RSV == REPORT_SCHEMA_VERSION