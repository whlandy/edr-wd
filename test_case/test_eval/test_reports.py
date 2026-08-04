"""
P3.2.B round 2 — Evaluation result schema unit tests.

Layer separation per round 2 review:

  * D18 reports.py only owns STRUCTURAL schema (required
    fields, types, format, JSON round-trip).
  * D21 sanitisation kinds live in
    `agent/eval/sanitisation.py` (P3.2.D scope).
  * Sensitive-content audit lives in
    `agent/eval/sanitisation_audit.py` and is NOT invoked
    by to_dict / from_dict.

This file covers only the structural layer. Audit
coverage is in `test_sanitisation_audit.py`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from agent.eval.reports import (
    REPORT_SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    EvaluationReport,
    FixtureResult,
    MetricValue,
    RawTextPayloadError,
    ThresholdDecision,
    from_json,
    to_json,
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
        fixture_results=fixture_results,
        reproducibility_digest=reproducibility_digest,
        run_id=run_id,
    )


# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------


class TestSchemaVersion:
    def test_initial_schema_version(self) -> None:
        assert REPORT_SCHEMA_VERSION == "report_schema.v1"

    def test_supported_versions_is_frozen_set(self) -> None:
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
        report = make_report()
        text_a = to_json(report)
        text_b = to_json(report)
        assert text_a == text_b

    def test_from_dict_rejects_non_dict(self) -> None:
        with pytest.raises(ValueError, match="MUST be a JSON object"):
            from_json("[1, 2, 3]")

    def test_no_security_heuristic_on_round_trip(self) -> None:
        """D18 must NOT invoke security heuristics during
        to_dict / from_dict. A historical artefact with a
        long hex string in metric_id MUST remain
        parseable."""
        report = make_report(
            metrics={
                "aabbccddeeff00112233445566778899aabbccddeeff": (
                    MetricValue(
                        metric_id="aabbccddeeff00112233445566778899aabbccddeeff",
                        value=0.95,
                        unit="ratio",
                        direction="higher_is_better",
                    )
                )
            }
        )
        text = to_json(report)
        restored = from_json(text)
        assert restored == report


# ---------------------------------------------------------------------------
# Anti-raw-text rule (D18 layer boundary)
# ---------------------------------------------------------------------------


class TestAntiRawTextRule:
    """Per D18, expected / observed MUST be a Mapping, not
    a raw string / screenshot / prompt. The schema enforces
    this at the top level only — nested strings are
    legitimate identifiers."""

    def test_fixture_result_expected_must_be_mapping(self) -> None:
        with pytest.raises(RawTextPayloadError, match="MUST be a Mapping"):
            FixtureResult(
                fixture_id="x",
                status="completed",
                expected="click login button",  # type: ignore[arg-type]
                observed={},
            )

    def test_fixture_result_observed_must_be_mapping(self) -> None:
        with pytest.raises(RawTextPayloadError, match="MUST be a Mapping"):
            FixtureResult(
                fixture_id="x",
                status="completed",
                expected={},
                observed="You are an AI assistant",  # type: ignore[arg-type]
            )

    def test_fixture_result_accepts_mapping_with_nested_strings(self) -> None:
        """Nested strings are legitimate identifiers (e.g.
        {'name': 'login'}); only TOP-LEVEL raw strings are
        rejected by the anti-raw-text rule."""
        fr = FixtureResult(
            fixture_id="x",
            status="completed",
            expected={"kind": "threshold", "value": 0.95},
            observed={"kind": "value", "summary": {"name": "login"}},
        )
        assert fr.expected == {
            "kind": "threshold",
            "value": 0.95,
        }

    def test_from_dict_rejects_raw_string_expected(self) -> None:
        d = make_report().to_dict()
        d["fixture_results"] = [
            {
                "fixture_id": "x",
                "status": "completed",
                "expected": "click login button",
                "observed": {},
            }
        ]
        with pytest.raises(RawTextPayloadError):
            from_json(_safe_dumps(d))


def _safe_dumps(d: dict[str, Any]) -> str:
    from json import dumps

    return dumps(d, sort_keys=True)


# ---------------------------------------------------------------------------
# False-positive tests (M3 — legitimate data must not be
# rejected by the schema)
# ---------------------------------------------------------------------------


class TestNoFalsePositivesInSchema:
    """The schema layer MUST remain permissive enough that
    legitimate artefacts with content that LOOKS like a
    sensitive pattern (but isn't) parse without error.

    The audit layer (separately tested) handles real
    detection; this class ensures the schema does NOT
    bleed detection logic in."""

    def test_metric_id_with_system_word_allowed(self) -> None:
        report = make_report(
            metrics={
                "system_latency": MetricValue(
                    metric_id="system_latency",
                    value=42.0,
                    unit="ms",
                    direction="lower_is_better",
                )
            }
        )
        text = to_json(report)
        restored = from_json(text)
        assert "system_latency" in restored.metrics

    def test_long_dataset_description_allowed(self) -> None:
        """D18 schema does NOT enforce a max string length
        on arbitrary fields. A 500-char dataset_id is
        permitted (the schema only enforces non-empty)."""
        long_id = "x" * 500
        report = make_report(dataset_id=long_id)
        text = to_json(report)
        restored = from_json(text)
        assert restored.dataset_id == long_id

    def test_hex_identifier_in_dataset_id_allowed(self) -> None:
        """A 64-char hex dataset_id MUST be parseable; the
        schema does not flag token-shaped fields."""
        hex_id = "aabbccddeeff00112233445566778899" * 2
        report = make_report(dataset_id=hex_id)
        text = to_json(report)
        restored = from_json(text)
        assert restored.dataset_id == hex_id

    def test_metric_id_with_xpath_like_word_allowed(self) -> None:
        """A metric_id that mentions a path-like substring
        MUST NOT be flagged by the schema (audit is
        separate)."""
        report = make_report(
            metrics={
                "test_xpath_perf": MetricValue(
                    metric_id="test_xpath_perf",
                    value=1.0,
                    unit="count",
                    direction="lower_is_better",
                )
            }
        )
        text = to_json(report)
        restored = from_json(text)
        assert "test_xpath_perf" in restored.metrics


# ---------------------------------------------------------------------------
# Placeholder dataclass flexibility (M1, M2 — D20 / status
# extension belong elsewhere)
# ---------------------------------------------------------------------------


class TestPlaceholderFlexibility:
    """D18 must NOT freeze D20 or status taxonomy. The
    placeholder dataclasses accept any string values so
    D20 (P3.2.D) and the runner (P3.2.E) can evolve
    without schema changes."""

    def test_threshold_decision_accepts_free_decision(self) -> None:
        """Per M1, thresholded-vs-decision cross-field
        validation is removed. D20 owns that rule."""
        td = ThresholdDecision(
            metric_id="x",
            thresholded=True,
            decision="unknown_decision_value",
            observed=0.5,
            threshold=0.95,
        )
        assert td.decision == "unknown_decision_value"

    def test_threshold_decision_ungated_threshold_set(self) -> None:
        """The cross-field invariant is removed; both
        shapes (thresholded+threshold, ungated+None,
        thresholded+None, ungated+threshold) are accepted
        by D18. P3.2.D enforces the canonical rule."""
        td = ThresholdDecision(
            metric_id="x",
            thresholded=False,
            decision="ungated",
            observed=0.5,
            threshold=0.95,
        )
        assert td.thresholded is False
        assert td.threshold == 0.95

    def test_metric_value_accepts_arbitrary_direction(self) -> None:
        """D19 owns direction taxonomy. D18 is permissive."""
        mv = MetricValue(
            metric_id="x", value=1.0, unit="ratio", direction="custom_dir"
        )
        assert mv.direction == "custom_dir"

    def test_fixture_result_accepts_unknown_status(self) -> None:
        """Per M2, status MUST accept extension. Future
        statuses (skipped, timeout, infrastructure_error)
        do not require a D18 change."""
        fr = FixtureResult(
            fixture_id="x",
            status="skipped",
            expected={"kind": "stage_reached", "name": "validation"},
            observed={"kind": "stage_missed", "name": "validation"},
        )
        assert fr.status == "skipped"

    def test_fixture_result_accepts_infrastructure_error_status(self) -> None:
        fr = FixtureResult(
            fixture_id="x",
            status="infrastructure_error",
            expected={},
            observed={"kind": "errored", "reason": "ci_outage"},
        )
        assert fr.status == "infrastructure_error"


# ---------------------------------------------------------------------------
# End-to-end report round-trip with sanitised-shape payloads
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
                    expected={"kind": "stage_reached", "name": "login"},
                    observed={"kind": "stage_reached", "name": "login"},
                ),
                FixtureResult(
                    fixture_id="delete_account",
                    status="aborted",
                    expected={"category": "danger_button"},
                    observed={
                        "category": "wrong_target",
                        "digest": "sha256:abc123",
                    },
                ),
            ),
            reproducibility_digest="sha256:fake-digest-pending",
        )
        text = to_json(report)
        restored = from_json(text)
        assert restored == report
        assert restored.fixture_results[0].expected == {
            "kind": "stage_reached",
            "name": "login",
        }
        assert restored.fixture_results[1].observed["category"] == "wrong_target"
        assert restored.threshold_decisions[0].thresholded is True
        assert restored.threshold_decisions[1].thresholded is False