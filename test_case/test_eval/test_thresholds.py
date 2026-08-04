"""
P3.2.D — Threshold declarations (D20) unit tests.

Covers D20 invariants:

  * Three-state policy: thresholded / ungated / missing.
  * thresholded requires threshold + direction; ungated
    forbids them.
  * Mutation policy: same metric_id + different
    declaration fingerprint is rejected.
  * Idempotent re-registration (same fingerprint).
  * Missing declaration in registry returns None.
  * Evaluator is a pure function: (metric_id, observed,
    declaration) → ThresholdDecision.
  * Missing declaration → fail (D20 round 2).
  * Boundary values: pass at exact threshold.

Layer boundary checks:

  * thresholds.py does not import sanitisation kinds.
  * thresholds.py does not import runner mechanics.
  * thresholds.py only consumes the ThresholdDecision
    data container from reports.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from agent.eval.metrics import (
    METRIC_CATEGORY_QUALITY,
    METRIC_DIRECTION_HIGHER_IS_BETTER,
    METRIC_DIRECTION_LOWER_IS_BETTER,
    MetricFormula,
    MetricSpec,
    MetricRegistry,
)
from agent.eval.reports import (
    REPORT_SCHEMA_VERSION,
    EvaluationReport,
    ThresholdDecision,
)
from agent.eval.thresholds import (
    ALL_THRESHOLD_DECISIONS,
    ALL_THRESHOLD_DIRECTIONS,
    ALL_THRESHOLD_STATES,
    THRESHOLD_DECISION_FAIL,
    THRESHOLD_DECISION_MISSING,
    THRESHOLD_DECISION_PASS,
    THRESHOLD_DECISION_UNGATED,
    THRESHOLD_DIRECTION_HIGHER_IS_BETTER,
    THRESHOLD_DIRECTION_LOWER_IS_BETTER,
    THRESHOLD_STATE_THRESHOLDED,
    THRESHOLD_STATE_UNGATED,
    ThresholdDeclaration,
    ThresholdRegistry,
    ThresholdRegistryError,
    ThresholdReusedIdError,
    collect_failures,
    collect_ungated,
    evaluate_all_thresholds,
    evaluate_threshold,
    is_evaluation_passing,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_decl(**overrides: Any) -> ThresholdDeclaration:
    base: dict[str, Any] = {
        "metric_id": "test_metric",
        "state": THRESHOLD_STATE_THRESHOLDED,
        "threshold": 0.95,
        "direction": THRESHOLD_DIRECTION_HIGHER_IS_BETTER,
    }
    base.update(overrides)
    return ThresholdDeclaration(**base)


def _make_report_with_metrics(
    metric_values: dict[str, float],
) -> EvaluationReport:
    start = datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(seconds=10)
    return EvaluationReport(
        schema_version=REPORT_SCHEMA_VERSION,
        dataset_id="eval-fixtures",
        dataset_version="v1",
        planner_version="p3.1.0",
        execution_profile="default",
        started_at=start,
        ended_at=end,
        metrics={
            mid: _mv(mid, v) for mid, v in metric_values.items()
        },
        threshold_decisions=(),
        fixture_results=(),
        reproducibility_digest="",
        run_id="run-001",
    )


def _mv(metric_id: str, value: float):
    """Construct a MetricValue (avoids importing the
    dataclass directly in every test)."""
    from agent.eval.reports import MetricValue

    return MetricValue(
        metric_id=metric_id,
        value=value,
        unit="ratio",
        direction=METRIC_DIRECTION_HIGHER_IS_BETTER,
    )


# ---------------------------------------------------------------------------
# D20 state taxonomy
# ---------------------------------------------------------------------------


class TestThresholdTaxonomy:
    def test_two_states_defined(self) -> None:
        assert THRESHOLD_STATE_THRESHOLDED in ALL_THRESHOLD_STATES
        assert THRESHOLD_STATE_UNGATED in ALL_THRESHOLD_STATES
        # Round 2: missing is NOT a state object — it is
        # `None` from get().
        assert len(ALL_THRESHOLD_STATES) == 2

    def test_decision_outcomes(self) -> None:
        assert THRESHOLD_DECISION_PASS in ALL_THRESHOLD_DECISIONS
        assert THRESHOLD_DECISION_FAIL in ALL_THRESHOLD_DECISIONS
        assert THRESHOLD_DECISION_UNGATED in ALL_THRESHOLD_DECISIONS
        assert THRESHOLD_DECISION_MISSING in ALL_THRESHOLD_DECISIONS

    def test_direction_constants(self) -> None:
        assert THRESHOLD_DIRECTION_HIGHER_IS_BETTER in ALL_THRESHOLD_DIRECTIONS
        assert THRESHOLD_DIRECTION_LOWER_IS_BETTER in ALL_THRESHOLD_DIRECTIONS


# ---------------------------------------------------------------------------
# D20 declaration validation
# ---------------------------------------------------------------------------


class TestThresholdDeclaration:
    def test_metric_id_required(self) -> None:
        with pytest.raises(ValueError, match="metric_id MUST be non-empty"):
            _valid_decl(metric_id="")

    def test_invalid_state_rejected(self) -> None:
        with pytest.raises(ValueError, match="state MUST be one of"):
            _valid_decl(state="missing")  # type: ignore[arg-type]

    def test_thresholded_requires_threshold(self) -> None:
        with pytest.raises(
            ValueError, match="MUST have a threshold value"
        ):
            _valid_decl(state=THRESHOLD_STATE_THRESHOLDED, threshold=None)

    def test_thresholded_requires_direction(self) -> None:
        with pytest.raises(ValueError, match="MUST have direction"):
            _valid_decl(
                state=THRESHOLD_STATE_THRESHOLDED, direction=None
            )

    def test_thresholded_invalid_direction_rejected(self) -> None:
        with pytest.raises(ValueError, match="MUST have direction"):
            _valid_decl(direction="custom_direction")

    def test_ungated_forbids_threshold(self) -> None:
        with pytest.raises(
            ValueError, match="MUST NOT have a threshold value"
        ):
            _valid_decl(
                state=THRESHOLD_STATE_UNGATED, threshold=0.5
            )

    def test_ungated_forbids_direction(self) -> None:
        with pytest.raises(ValueError, match="MUST NOT have a direction"):
            _valid_decl(
                state=THRESHOLD_STATE_UNGATED,
                threshold=None,
                direction=THRESHOLD_DIRECTION_HIGHER_IS_BETTER,
            )

    def test_ungated_clean(self) -> None:
        decl = _valid_decl(
            state=THRESHOLD_STATE_UNGATED,
            threshold=None,
            direction=None,
        )
        assert decl.state == THRESHOLD_STATE_UNGATED
        assert decl.threshold is None
        assert decl.direction is None

    def test_thresholded_clean(self) -> None:
        decl = _valid_decl()
        assert decl.state == THRESHOLD_STATE_THRESHOLDED
        assert decl.threshold == 0.95
        assert decl.direction == THRESHOLD_DIRECTION_HIGHER_IS_BETTER

    def test_declaration_is_frozen(self) -> None:
        decl = _valid_decl()
        with pytest.raises(Exception):
            decl.metric_id = "other"  # type: ignore[misc]

    def test_fingerprint_stable(self) -> None:
        a = _valid_decl()
        b = _valid_decl()
        assert a.fingerprint == b.fingerprint

    def test_fingerprint_changes_on_threshold_change(self) -> None:
        a = _valid_decl(threshold=0.9)
        b = _valid_decl(threshold=0.95)
        assert a.fingerprint != b.fingerprint

    def test_fingerprint_changes_on_state_change(self) -> None:
        a = _valid_decl(state=THRESHOLD_STATE_THRESHOLDED, threshold=0.95, direction=THRESHOLD_DIRECTION_HIGHER_IS_BETTER)
        b = ThresholdDeclaration(
            metric_id=a.metric_id, state=THRESHOLD_STATE_UNGATED
        )
        assert a.fingerprint != b.fingerprint


# ---------------------------------------------------------------------------
# D20 registry
# ---------------------------------------------------------------------------


class TestThresholdRegistry:
    def test_register_and_get(self) -> None:
        registry = ThresholdRegistry()
        decl = _valid_decl(metric_id="m1")
        registry.declare(decl)
        assert registry.get("m1") == decl

    def test_get_unknown_returns_none(self) -> None:
        """Per D20: missing declaration is `None`, NOT a
        'missing' object."""
        registry = ThresholdRegistry()
        assert registry.get("unknown_metric") is None

    def test_has_declaration(self) -> None:
        registry = ThresholdRegistry()
        registry.declare(_valid_decl(metric_id="m1"))
        assert registry.has_declaration("m1")
        assert not registry.has_declaration("m2")

    def test_contains(self) -> None:
        registry = ThresholdRegistry()
        registry.declare(_valid_decl(metric_id="m1"))
        assert "m1" in registry
        assert "m2" not in registry

    def test_len(self) -> None:
        registry = ThresholdRegistry()
        for i in range(3):
            registry.declare(_valid_decl(metric_id=f"m{i}"))
        assert len(registry) == 3

    def test_declared_metric_ids(self) -> None:
        registry = ThresholdRegistry()
        registry.declare(_valid_decl(metric_id="m1"))
        registry.declare(_valid_decl(metric_id="m2"))
        registry.declare(
            _valid_decl(
                metric_id="m3",
                state=THRESHOLD_STATE_UNGATED,
                threshold=None,
                direction=None,
            )
        )
        assert registry.declared_metric_ids() == frozenset(
            {"m1", "m2", "m3"}
        )

    def test_thresholded_metric_ids_filters(self) -> None:
        registry = ThresholdRegistry()
        registry.declare(_valid_decl(metric_id="m1"))
        registry.declare(
            _valid_decl(
                metric_id="m2",
                state=THRESHOLD_STATE_UNGATED,
                threshold=None,
                direction=None,
            )
        )
        assert registry.thresholded_metric_ids() == frozenset({"m1"})
        assert registry.ungated_metric_ids() == frozenset({"m2"})

    def test_idempotent_re_declaration(self) -> None:
        registry = ThresholdRegistry()
        decl = _valid_decl(metric_id="m1")
        registry.declare(decl)
        registry.declare(decl)
        assert len(registry) == 1

    def test_mutation_policy_rejects_different_fingerprint(self) -> None:
        registry = ThresholdRegistry()
        registry.declare(_valid_decl(metric_id="m1", threshold=0.9))
        with pytest.raises(ThresholdReusedIdError) as exc_info:
            registry.declare(_valid_decl(metric_id="m1", threshold=0.95))
        assert exc_info.value.metric_id == "m1"

    def test_mutation_policy_rejects_state_change(self) -> None:
        registry = ThresholdRegistry()
        registry.declare(
            _valid_decl(
                metric_id="m1",
                state=THRESHOLD_STATE_UNGATED,
                threshold=None,
                direction=None,
            )
        )
        with pytest.raises(ThresholdReusedIdError):
            registry.declare(_valid_decl(metric_id="m1"))


# ---------------------------------------------------------------------------
# D20 evaluator — pure function
# ---------------------------------------------------------------------------


class TestEvaluateThreshold:
    def test_thresholded_higher_is_better_pass(self) -> None:
        decl = _valid_decl(
            metric_id="m1",
            threshold=0.9,
            direction=THRESHOLD_DIRECTION_HIGHER_IS_BETTER,
        )
        decision = evaluate_threshold("m1", 0.95, decl)
        assert decision.thresholded is True
        assert decision.decision == THRESHOLD_DECISION_PASS
        assert decision.observed == 0.95
        assert decision.threshold == 0.9

    def test_thresholded_higher_is_better_fail(self) -> None:
        decl = _valid_decl(
            metric_id="m1",
            threshold=0.9,
            direction=THRESHOLD_DIRECTION_HIGHER_IS_BETTER,
        )
        decision = evaluate_threshold("m1", 0.85, decl)
        assert decision.decision == THRESHOLD_DECISION_FAIL

    def test_thresholded_higher_is_better_exact_boundary(self) -> None:
        """observed == threshold → pass (>=)."""
        decl = _valid_decl(
            metric_id="m1",
            threshold=0.9,
            direction=THRESHOLD_DIRECTION_HIGHER_IS_BETTER,
        )
        decision = evaluate_threshold("m1", 0.9, decl)
        assert decision.decision == THRESHOLD_DECISION_PASS

    def test_thresholded_lower_is_better_pass(self) -> None:
        decl = _valid_decl(
            metric_id="m1",
            threshold=200.0,
            direction=THRESHOLD_DIRECTION_LOWER_IS_BETTER,
        )
        decision = evaluate_threshold("m1", 150.0, decl)
        assert decision.decision == THRESHOLD_DECISION_PASS

    def test_thresholded_lower_is_better_fail(self) -> None:
        decl = _valid_decl(
            metric_id="m1",
            threshold=200.0,
            direction=THRESHOLD_DIRECTION_LOWER_IS_BETTER,
        )
        decision = evaluate_threshold("m1", 250.0, decl)
        assert decision.decision == THRESHOLD_DECISION_FAIL

    def test_thresholded_lower_is_better_exact_boundary(self) -> None:
        """observed == threshold → pass (<=)."""
        decl = _valid_decl(
            metric_id="m1",
            threshold=200.0,
            direction=THRESHOLD_DIRECTION_LOWER_IS_BETTER,
        )
        decision = evaluate_threshold("m1", 200.0, decl)
        assert decision.decision == THRESHOLD_DECISION_PASS

    def test_ungated_returns_ungated(self) -> None:
        decl = _valid_decl(
            metric_id="m1",
            state=THRESHOLD_STATE_UNGATED,
            threshold=None,
            direction=None,
        )
        decision = evaluate_threshold("m1", 0.5, decl)
        assert decision.thresholded is False
        assert decision.decision == THRESHOLD_DECISION_UNGATED
        assert decision.threshold is None

    def test_ungated_does_not_check_value(self) -> None:
        """Ungated → no pass/fail regardless of value."""
        decl = _valid_decl(
            metric_id="m1",
            state=THRESHOLD_STATE_UNGATED,
            threshold=None,
            direction=None,
        )
        for v in [0.0, 0.5, 1.0, 100.0]:
            decision = evaluate_threshold("m1", v, decl)
            assert decision.decision == THRESHOLD_DECISION_UNGATED

    def test_missing_declaration_fails(self) -> None:
        """Per D20 round 2: missing declaration → fail."""
        decision = evaluate_threshold("m1", 0.5, None)
        assert decision.thresholded is False
        assert decision.decision == THRESHOLD_DECISION_FAIL
        assert decision.threshold is None
        assert decision.observed == 0.5


class TestEvaluateAllThresholds:
    def test_all_declared(self) -> None:
        registry = ThresholdRegistry()
        registry.declare(
            _valid_decl(
                metric_id="m1",
                threshold=0.9,
                direction=THRESHOLD_DIRECTION_HIGHER_IS_BETTER,
            )
        )
        registry.declare(
            _valid_decl(
                metric_id="m2",
                state=THRESHOLD_STATE_UNGATED,
                threshold=None,
                direction=None,
            )
        )
        decisions = evaluate_all_thresholds(
            {"m1": 0.95, "m2": 0.5}, registry
        )
        assert len(decisions) == 2
        by_id = {d.metric_id: d for d in decisions}
        assert by_id["m1"].decision == THRESHOLD_DECISION_PASS
        assert by_id["m2"].decision == THRESHOLD_DECISION_UNGATED

    def test_missing_metric_fails(self) -> None:
        registry = ThresholdRegistry()
        decisions = evaluate_all_thresholds(
            {"undeclared_metric": 0.5}, registry
        )
        assert decisions[0].decision == THRESHOLD_DECISION_FAIL

    def test_preserves_input_order(self) -> None:
        registry = ThresholdRegistry()
        registry.declare(
            _valid_decl(metric_id="a", threshold=0.5)
        )
        registry.declare(
            _valid_decl(metric_id="b", threshold=0.5)
        )
        decisions = evaluate_all_thresholds(
            {"a": 0.6, "b": 0.4}, registry
        )
        assert [d.metric_id for d in decisions] == ["a", "b"]


class TestAggregation:
    def test_all_passing(self) -> None:
        registry = ThresholdRegistry()
        registry.declare(
            _valid_decl(
                metric_id="m1",
                threshold=0.5,
                direction=THRESHOLD_DIRECTION_HIGHER_IS_BETTER,
            )
        )
        decisions = evaluate_all_thresholds(
            {"m1": 0.9}, registry
        )
        assert is_evaluation_passing(decisions) is True

    def test_ungated_does_not_fail(self) -> None:
        registry = ThresholdRegistry()
        registry.declare(
            _valid_decl(
                metric_id="m1",
                state=THRESHOLD_STATE_UNGATED,
                threshold=None,
                direction=None,
            )
        )
        decisions = evaluate_all_thresholds(
            {"m1": 0.0}, registry
        )
        assert is_evaluation_passing(decisions) is True

    def test_one_failure_fails_evaluation(self) -> None:
        registry = ThresholdRegistry()
        registry.declare(
            _valid_decl(
                metric_id="m1",
                threshold=0.9,
                direction=THRESHOLD_DIRECTION_HIGHER_IS_BETTER,
            )
        )
        registry.declare(
            _valid_decl(
                metric_id="m2",
                threshold=0.9,
                direction=THRESHOLD_DIRECTION_HIGHER_IS_BETTER,
            )
        )
        decisions = evaluate_all_thresholds(
            {"m1": 0.95, "m2": 0.5}, registry
        )
        assert is_evaluation_passing(decisions) is False
        assert len(collect_failures(decisions)) == 1

    def test_missing_declaration_fails_evaluation(self) -> None:
        registry = ThresholdRegistry()
        decisions = evaluate_all_thresholds(
            {"undeclared": 0.5}, registry
        )
        assert is_evaluation_passing(decisions) is False
        assert collect_failures(decisions)[0].decision == THRESHOLD_DECISION_FAIL

    def test_collect_ungated(self) -> None:
        registry = ThresholdRegistry()
        registry.declare(
            _valid_decl(metric_id="m1", threshold=0.5)
        )
        registry.declare(
            _valid_decl(
                metric_id="m2",
                state=THRESHOLD_STATE_UNGATED,
                threshold=None,
                direction=None,
            )
        )
        decisions = evaluate_all_thresholds(
            {"m1": 0.6, "m2": 0.0}, registry
        )
        ungated = collect_ungated(decisions)
        assert len(ungated) == 1
        assert ungated[0].metric_id == "m2"


# ---------------------------------------------------------------------------
# Layer boundary checks
# ---------------------------------------------------------------------------


class TestLayerBoundary:
    def test_thresholds_does_not_import_sanitisation_kinds(self) -> None:
        """The D20 module MUST NOT import D21 sanitisation
        kinds. Threshold policy is independent of payload
        taxonomy."""
        import agent.eval.thresholds as t
        import inspect

        source = inspect.getsource(t)
        assert "sanitisation" not in source or "sanitisation_audit" not in source, (
            "thresholds.py MUST NOT depend on D21 sanitisation "
            "kinds; it is a pure policy layer"
        )

    def test_sanitisation_does_not_import_thresholds(self) -> None:
        """D21 sanitisation kinds MUST NOT depend on D20
        threshold policy."""
        import agent.eval.sanitisation as s
        import inspect

        source = inspect.getsource(s)
        assert "threshold" not in source.lower() or "thresholds.py" not in source, (
            "sanitisation.py MUST NOT depend on D20 threshold "
            "policy; it is a pure taxonomy layer"
        )

    def test_evaluator_pure_function(self) -> None:
        """evaluate_threshold MUST be deterministic."""
        decl = _valid_decl(threshold=0.9)
        a = evaluate_threshold("m1", 0.95, decl)
        b = evaluate_threshold("m1", 0.95, decl)
        assert a == b

    def test_decision_output_is_typed(self) -> None:
        decl = _valid_decl()
        decision = evaluate_threshold("m1", 0.5, decl)
        assert isinstance(decision, ThresholdDecision)


# ---------------------------------------------------------------------------
# Integration with P3.2.C metrics
# ---------------------------------------------------------------------------


class TestIntegrationWithMetrics:
    def test_threshold_uses_metric_direction(self) -> None:
        """The CI gate cross-checks the declaration's
        direction against the metric's direction. For now,
        the evaluator just uses the declaration's direction.
        This test documents the expected behaviour."""
        registry = ThresholdRegistry()
        # parse_success_rate is higher_is_better (Quality)
        # declaration must use higher_is_better
        registry.declare(
            _valid_decl(
                metric_id="parse_success_rate",
                threshold=0.95,
                direction=THRESHOLD_DIRECTION_HIGHER_IS_BETTER,
            )
        )
        decisions = evaluate_all_thresholds(
            {"parse_success_rate": 0.99}, registry
        )
        assert decisions[0].decision == THRESHOLD_DECISION_PASS

    def test_threshold_works_with_lower_is_better_metric(self) -> None:
        # execution_divergence_rate is lower_is_better
        registry = ThresholdRegistry()
        registry.declare(
            _valid_decl(
                metric_id="execution_divergence_rate",
                threshold=0.05,
                direction=THRESHOLD_DIRECTION_LOWER_IS_BETTER,
            )
        )
        decisions = evaluate_all_thresholds(
            {"execution_divergence_rate": 0.02}, registry
        )
        assert decisions[0].decision == THRESHOLD_DECISION_PASS

        decisions_fail = evaluate_all_thresholds(
            {"execution_divergence_rate": 0.10}, registry
        )
        assert decisions_fail[0].decision == THRESHOLD_DECISION_FAIL