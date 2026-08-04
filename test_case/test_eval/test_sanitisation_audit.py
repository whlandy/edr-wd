"""
P3.2.B round 2 — Sensitive-content audit layer tests.

These tests verify the SANITISATION AUDIT LAYER
(`agent/eval/sanitisation_audit.py`) which is SEPARATE
from the D18 schema parser. Key properties:

  * The audit walks ONLY paths that pass through a scoped
    segment (`DEFAULT_SCOPED_PATHS`). It does NOT scan
    `metric_id`, `dataset_id`, `unit`, `direction`, etc.
  * The audit RETURNS FINDINGS; it NEVER raises. Callers
    decide policy.
  * The audit is deterministic — same input → same findings.
  * Patterns are conservative; false negatives err on the
    side of recording a finding (caller may opt out).

Critical anti-false-positive tests:

  * A 64-char hex `metric_id` MUST NOT trigger a token
    finding (the audit does not walk the `metric_id`
    path).
  * A `metric_id = "system_latency"` MUST NOT trigger a
    prompt finding (the audit does not walk the `metric_id`
    path).
  * A 500-char `dataset_id` MUST NOT trigger any finding
    (the audit does not walk the `dataset_id` path).

These tests guard the SCOPE-DISCIPLINE boundary that
prevents the schema from bleeding detection logic in.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from agent.eval.reports import (
    REPORT_SCHEMA_VERSION,
    EvaluationReport,
    FixtureResult,
    MetricValue,
    ThresholdDecision,
)
from agent.eval.sanitisation_audit import (
    DEFAULT_SCOPED_PATHS,
    SensitiveAuditResult,
    SensitiveFinding,
    audit_report_sensitive_content,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_report_dict(
    *,
    metrics: dict[str, dict[str, Any]] | None = None,
    fixture_results: list[dict[str, Any]] | None = None,
    dataset_id: str = "eval-fixtures",
    run_id: str = "run-2026-08-04-001",
) -> dict[str, Any]:
    """Build a serialised report dict (like to_dict() output)
    for audit tests. Bypasses the dataclass layer so tests
    can inject raw content."""
    if metrics is None:
        metrics = {}
    if fixture_results is None:
        fixture_results = []
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "dataset_id": dataset_id,
        "dataset_version": "v1",
        "planner_version": "p3.1.0",
        "execution_profile": "default",
        "started_at": "2026-08-04T10:00:00+00:00",
        "ended_at": "2026-08-04T10:00:30+00:00",
        "metrics": metrics,
        "threshold_decisions": [],
        "fixture_results": fixture_results,
        "reproducibility_digest": "",
        "run_id": run_id,
    }


# ---------------------------------------------------------------------------
# Scope discipline — these are the critical anti-false-positive
# tests (M3 + the core B1 / B2 corrections).
# ---------------------------------------------------------------------------


class TestScopeDiscipline:
    """The audit MUST NOT walk non-scoped fields. These
    tests verify the SCOPE BOUNDARY itself."""

    def test_clean_report_no_findings(self) -> None:
        d = make_report_dict(
            metrics={
                "parse_success_rate": {
                    "metric_id": "parse_success_rate",
                    "value": 0.95,
                    "unit": "ratio",
                    "direction": "higher_is_better",
                }
            }
        )
        result = audit_report_sensitive_content(d)
        assert not result.findings

    def test_long_hex_metric_id_not_scanned(self) -> None:
        """A 40+ char hex metric_id MUST NOT trigger a
        token finding — the audit does not walk the
        `metric_id` path."""
        long_hex = "a" * 40 + "bcdef1234" * 4
        d = make_report_dict(
            metrics={
                long_hex: {
                    "metric_id": long_hex,
                    "value": 0.95,
                    "unit": "ratio",
                    "direction": "higher_is_better",
                }
            }
        )
        result = audit_report_sensitive_content(d)
        assert not result.findings

    def test_system_latency_metric_id_not_scanned(self) -> None:
        """A metric_id containing the word 'system' MUST NOT
        trigger a prompt finding — the audit does not walk
        the `metric_id` path."""
        d = make_report_dict(
            metrics={
                "system_latency": {
                    "metric_id": "system_latency",
                    "value": 42.0,
                    "unit": "ms",
                    "direction": "lower_is_better",
                }
            }
        )
        result = audit_report_sensitive_content(d)
        assert not result.findings

    def test_long_dataset_description_not_scanned(self) -> None:
        """A 500-char dataset_id MUST NOT trigger a
        length-based finding — the audit does not walk the
        `dataset_id` path."""
        long_id = "x" * 500
        d = make_report_dict(dataset_id=long_id)
        result = audit_report_sensitive_content(d)
        assert not result.findings

    def test_hex_dataset_id_not_scanned(self) -> None:
        """A 64-char hex dataset_id MUST NOT trigger a
        token finding."""
        hex_id = "aabbccddeeff00112233445566778899" * 2
        d = make_report_dict(dataset_id=hex_id)
        result = audit_report_sensitive_content(d)
        assert not result.findings

    def test_xpath_in_run_id_not_scanned(self) -> None:
        """A run_id containing a path-like substring MUST
        NOT trigger a selector finding — the audit does
        not walk the `run_id` path."""
        d = make_report_dict(run_id="run-//button[@id='x']-001")
        result = audit_report_sensitive_content(d)
        assert not result.findings


# ---------------------------------------------------------------------------
# Pattern detection — patterns DO fire when content is in
# a scoped path.
# ---------------------------------------------------------------------------


class TestPatternDetection:
    """Patterns are detected when content appears under a
    scoped path. Severity model: `block` for selectors /
    long tokens / screenshots / prompt leakage; `warn`
    for short base64 strings."""

    def test_xpath_in_expected_blocked(self) -> None:
        d = make_report_dict(
            fixture_results=[
                {
                    "fixture_id": "x",
                    "status": "completed",
                    "expected": {"selector": "//button[@id='submit']"},
                    "observed": {"value": 1.0},
                }
            ]
        )
        result = audit_report_sensitive_content(d)
        assert result.has_block_findings
        assert any(
            f.severity == "block"
            and "selector" in f.path.lower()
            and "//button" in f.matched_text
            for f in result.findings
        )

    def test_long_hex_token_in_observed_blocked(self) -> None:
        long_hex = "a" * 40 + "bcdef1234" * 4
        d = make_report_dict(
            fixture_results=[
                {
                    "fixture_id": "x",
                    "status": "completed",
                    "expected": {},
                    "observed": {"digest": long_hex},
                }
            ]
        )
        result = audit_report_sensitive_content(d)
        assert result.has_block_findings

    def test_query_selector_blocked(self) -> None:
        d = make_report_dict(
            fixture_results=[
                {
                    "fixture_id": "x",
                    "status": "completed",
                    "expected": {},
                    "observed": {
                        "selector": "document.querySelector('.btn')"
                    },
                }
            ]
        )
        result = audit_report_sensitive_content(d)
        assert result.has_block_findings

    def test_llm_prompt_in_observed_blocked(self) -> None:
        d = make_report_dict(
            fixture_results=[
                {
                    "fixture_id": "x",
                    "status": "completed",
                    "expected": {},
                    "observed": {"text": "You are an AI assistant"},
                }
            ]
        )
        result = audit_report_sensitive_content(d)
        assert result.has_block_findings
        assert any("You are an AI" in f.matched_text for f in result.findings)

    def test_system_role_marker_blocked(self) -> None:
        d = make_report_dict(
            fixture_results=[
                {
                    "fixture_id": "x",
                    "status": "completed",
                    "expected": {},
                    "observed": {"text": "system: do the thing"},
                }
            ]
        )
        result = audit_report_sensitive_content(d)
        assert result.has_block_findings

    def test_chat_template_token_blocked(self) -> None:
        d = make_report_dict(
            fixture_results=[
                {
                    "fixture_id": "x",
                    "status": "completed",
                    "expected": {},
                    "observed": {"text": "render <|im_start|>system"},
                }
            ]
        )
        result = audit_report_sensitive_content(d)
        assert result.has_block_findings

    def test_base64_screenshot_blocked(self) -> None:
        d = make_report_dict(
            fixture_results=[
                {
                    "fixture_id": "x",
                    "status": "completed",
                    "expected": {},
                    "observed": {
                        "screenshot": "data:image/png;base64,iVBORw0KGgo..."
                    },
                }
            ]
        )
        result = audit_report_sensitive_content(d)
        assert result.has_block_findings

    def test_short_base64_in_observed_warned(self) -> None:
        """Short base64 strings are warned, not blocked."""
        d = make_report_dict(
            fixture_results=[
                {
                    "fixture_id": "x",
                    "status": "completed",
                    "expected": {},
                    "observed": {
                        "value": "Q" * 50
                    },  # 50-char base64 → warn, not block
                }
            ]
        )
        result = audit_report_sensitive_content(d)
        assert result.has_warn_findings
        assert not result.has_block_findings


# ---------------------------------------------------------------------------
# Audit semantics — never raises; deterministic; configurable
# scope.
# ---------------------------------------------------------------------------


class TestAuditSemantics:
    def test_audit_never_raises(self) -> None:
        """The audit MUST NEVER raise. Even with garbage
        inputs it returns an empty result rather than
        crashing."""
        # Garbage input: not a Mapping at all.
        result = audit_report_sensitive_content("garbage")  # type: ignore[arg-type]
        assert isinstance(result, SensitiveAuditResult)

    def test_audit_returns_empty_for_non_mapping(self) -> None:
        result = audit_report_sensitive_content([1, 2, 3])  # type: ignore[arg-type]
        assert not result.findings

    def test_audit_is_deterministic(self) -> None:
        d = make_report_dict(
            fixture_results=[
                {
                    "fixture_id": "x",
                    "status": "completed",
                    "expected": {"selector": "//button"},
                    "observed": {"text": "system: "},
                }
            ]
        )
        a = audit_report_sensitive_content(d)
        b = audit_report_sensitive_content(d)
        assert a.findings == b.findings

    def test_audit_with_empty_dict(self) -> None:
        result = audit_report_sensitive_content({})
        assert not result.findings

    def test_audit_with_custom_scope(self) -> None:
        """Caller can override the scope. With an empty
        scope, nothing is scanned."""
        d = make_report_dict(
            fixture_results=[
                {
                    "fixture_id": "x",
                    "status": "completed",
                    "expected": {},
                    "observed": {"text": "You are an AI"},
                }
            ]
        )
        # Empty scope → no findings.
        result = audit_report_sensitive_content(d, scoped_paths=frozenset())
        assert not result.findings

    def test_audit_scoped_to_extra_path(self) -> None:
        """Adding 'metrics' to scope lets the audit scan
        metric values."""
        d = make_report_dict(
            metrics={
                "x": {
                    "metric_id": "x",
                    "value": 0.95,
                    "unit": "ratio",
                    "direction": "higher_is_better",
                }
            }
        )
        result = audit_report_sensitive_content(
            d, scoped_paths=frozenset({"metrics"})
        )
        # No patterns fire because the metric value is clean.
        assert not result.findings


# ---------------------------------------------------------------------------
# Finding properties
# ---------------------------------------------------------------------------


class TestFindingProperties:
    def test_block_findings(self) -> None:
        d = make_report_dict(
            fixture_results=[
                {
                    "fixture_id": "x",
                    "status": "completed",
                    "expected": {"selector": "//button"},
                    "observed": {},
                }
            ]
        )
        result = audit_report_sensitive_content(d)
        assert result.block_findings
        assert all(f.severity == "block" for f in result.block_findings)

    def test_warn_findings(self) -> None:
        d = make_report_dict(
            fixture_results=[
                {
                    "fixture_id": "x",
                    "status": "completed",
                    "expected": {},
                    "observed": {"value": "Q" * 50},  # warn
                }
            ]
        )
        result = audit_report_sensitive_content(d)
        assert result.warn_findings
        assert all(f.severity == "warn" for f in result.warn_findings)

    def test_severity_validation(self) -> None:
        with pytest.raises(ValueError, match="severity MUST be"):
            SensitiveFinding(
                path="x",
                severity="unknown",
                pattern="x",
                matched_text="x",
                message="x",
            )

    def test_bool_conversion(self) -> None:
        clean = SensitiveAuditResult(findings=())
        assert not clean

        dirty = SensitiveAuditResult(
            findings=(
                SensitiveFinding(
                    path="x",
                    severity="block",
                    pattern="x",
                    matched_text="x",
                    message="x",
                ),
            )
        )
        assert dirty


# ---------------------------------------------------------------------------
# End-to-end: real report dict via to_dict() round-trips
# through the audit with clean expected/observed.
# ---------------------------------------------------------------------------


class TestEndToEndWithRealReport:
    def test_real_report_clean_audit(self) -> None:
        """A report built via the dataclass layer and
        serialised to dict MUST audit clean when its
        expected/observed are structured Mappings with
        short identifiers."""
        start = datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 8, 4, 10, 0, 30, tzinfo=timezone.utc)
        report = EvaluationReport(
            schema_version=REPORT_SCHEMA_VERSION,
            dataset_id="eval-fixtures",
            dataset_version="v1",
            planner_version="p3.1.0",
            execution_profile="default",
            started_at=start,
            ended_at=end,
            metrics={
                "parse_success_rate": MetricValue(
                    metric_id="parse_success_rate",
                    value=0.95,
                    unit="ratio",
                    direction="higher_is_better",
                )
            },
            threshold_decisions=(
                ThresholdDecision(
                    metric_id="parse_success_rate",
                    thresholded=True,
                    decision="pass",
                    observed=0.95,
                    threshold=0.90,
                ),
            ),
            fixture_results=(
                FixtureResult(
                    fixture_id="click_login",
                    status="completed",
                    expected={"kind": "stage_reached", "name": "login"},
                    observed={"kind": "stage_reached", "name": "login"},
                ),
            ),
            reproducibility_digest="",
            run_id="run-001",
        )
        result = audit_report_sensitive_content(report.to_dict())
        assert not result.findings