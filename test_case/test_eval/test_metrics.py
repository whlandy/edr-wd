"""
P3.2.C — Metric registry and cross-cutting metrics unit tests.

Covers D19 invariants:
  * metric_id is the single source of truth.
  * Mutation policy: same id + different spec fingerprint
    is rejected.
  * Spec and formula MUST share metric_id.
  * Owner = accountable component (non-empty string).
  * Spec version is monotonic.
  * Re-registration with the same fingerprint is
    idempotent.

Covers D23 invariants:
  * Every metric has ≥1 category from the 4-element
    taxonomy.
  * Cross-cutting metrics span ≥3 categories.
  * Default registry covers all 4 categories
    (Efficiency is not missing — per M3 in design gate
    round 2).
  * Each of the 6 default cross-cutting metrics spans
    exactly 3 categories with different subsets.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from agent.eval.metrics import (
    ALL_METRIC_CATEGORIES,
    ALL_METRIC_DIRECTIONS,
    CROSS_CUTTING_CATEGORY_THRESHOLD,
    METRIC_CATEGORY_BEHAVIOUR,
    METRIC_CATEGORY_COVERAGE,
    METRIC_CATEGORY_EFFICIENCY,
    METRIC_CATEGORY_QUALITY,
    METRIC_DIRECTION_HIGHER_IS_BETTER,
    METRIC_DIRECTION_LOWER_IS_BETTER,
    MetricFormula,
    MetricRegistry,
    MetricRegistryError,
    MetricReusedIdError,
    MetricSpec,
    MetricSpecFormulaMismatchError,
    build_default_registry,
    compute_all_metrics,
    compute_metric,
)
from agent.eval.reports import (
    REPORT_SCHEMA_VERSION,
    EvaluationReport,
    FixtureResult,
    MetricValue,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_spec(**overrides: Any) -> MetricSpec:
    base: dict[str, Any] = {
        "metric_id": "test_metric",
        "categories": frozenset(
            {METRIC_CATEGORY_QUALITY, METRIC_CATEGORY_COVERAGE}
        ),
        "formula_summary": "test formula",
        "unit": "ratio",
        "direction": METRIC_DIRECTION_HIGHER_IS_BETTER,
        "owner": "test_component",
        "version": 1,
    }
    base.update(overrides)
    return MetricSpec(**base)


def _noop_compute(report: EvaluationReport) -> float:
    return 0.0


def _formula_for(spec: MetricSpec) -> MetricFormula:
    return MetricFormula(
        metric_id=spec.metric_id,
        compute=_noop_compute,
        notes="",
    )


def _make_report(
    fixture_results: tuple[FixtureResult, ...] = (),
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
        metrics={},
        threshold_decisions=(),
        fixture_results=fixture_results,
        reproducibility_digest="",
        run_id="run-001",
    )


# ---------------------------------------------------------------------------
# D23 taxonomy
# ---------------------------------------------------------------------------


class TestMetricTaxonomy:
    def test_all_four_categories_present(self) -> None:
        assert METRIC_CATEGORY_COVERAGE in ALL_METRIC_CATEGORIES
        assert METRIC_CATEGORY_QUALITY in ALL_METRIC_CATEGORIES
        assert METRIC_CATEGORY_EFFICIENCY in ALL_METRIC_CATEGORIES
        assert METRIC_CATEGORY_BEHAVIOUR in ALL_METRIC_CATEGORIES

    def test_all_metric_categories_is_frozenset(self) -> None:
        from typing import FrozenSet

        assert isinstance(ALL_METRIC_CATEGORIES, frozenset)

    def test_directions_defined(self) -> None:
        assert METRIC_DIRECTION_HIGHER_IS_BETTER in ALL_METRIC_DIRECTIONS
        assert METRIC_DIRECTION_LOWER_IS_BETTER in ALL_METRIC_DIRECTIONS

    def test_cross_cutting_threshold_is_three(self) -> None:
        assert CROSS_CUTTING_CATEGORY_THRESHOLD == 3


# ---------------------------------------------------------------------------
# D19 MetricSpec
# ---------------------------------------------------------------------------


class TestMetricSpec:
    def test_metric_id_required(self) -> None:
        with pytest.raises(ValueError, match="metric_id MUST be non-empty"):
            _valid_spec(metric_id="")

    def test_categories_required(self) -> None:
        with pytest.raises(
            ValueError, match="MUST have ≥1 category"
        ):
            _valid_spec(categories=frozenset())

    def test_invalid_category_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid categories"):
            _valid_spec(categories=frozenset({"UnknownCategory"}))

    def test_formula_summary_required(self) -> None:
        with pytest.raises(ValueError, match="formula_summary"):
            _valid_spec(formula_summary="")

    def test_unit_required(self) -> None:
        with pytest.raises(ValueError, match="unit"):
            _valid_spec(unit="")

    def test_invalid_direction_rejected(self) -> None:
        with pytest.raises(ValueError, match="direction MUST be"):
            _valid_spec(direction="custom_direction")

    def test_owner_required(self) -> None:
        with pytest.raises(ValueError, match="MUST have an owner"):
            _valid_spec(owner="")

    def test_version_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="version MUST be ≥1"):
            _valid_spec(version=0)

    def test_spec_is_frozen(self) -> None:
        spec = _valid_spec()
        with pytest.raises(Exception):
            spec.metric_id = "other"  # type: ignore[misc]

    def test_is_cross_cutting_with_three_categories(self) -> None:
        spec = _valid_spec(
            categories=frozenset(
                {METRIC_CATEGORY_QUALITY, METRIC_CATEGORY_COVERAGE,
                 METRIC_CATEGORY_EFFICIENCY}
            )
        )
        assert spec.is_cross_cutting is True

    def test_is_not_cross_cutting_with_two_categories(self) -> None:
        spec = _valid_spec(
            categories=frozenset(
                {METRIC_CATEGORY_QUALITY, METRIC_CATEGORY_COVERAGE}
            )
        )
        assert spec.is_cross_cutting is False

    def test_fingerprint_stable(self) -> None:
        s1 = _valid_spec()
        s2 = _valid_spec()
        assert s1.fingerprint == s2.fingerprint

    def test_fingerprint_changes_on_semantic_shift(self) -> None:
        s1 = _valid_spec(formula_summary="old formula")
        s2 = _valid_spec(formula_summary="new formula")
        assert s1.fingerprint != s2.fingerprint

    def test_fingerprint_independent_of_category_order(self) -> None:
        s1 = _valid_spec(
            categories=frozenset(
                {METRIC_CATEGORY_QUALITY, METRIC_CATEGORY_COVERAGE}
            )
        )
        s2 = _valid_spec(
            categories=frozenset(
                {METRIC_CATEGORY_COVERAGE, METRIC_CATEGORY_QUALITY}
            )
        )
        assert s1.fingerprint == s2.fingerprint

    def test_fingerprint_changes_on_version_bump(self) -> None:
        s1 = _valid_spec(version=1)
        s2 = _valid_spec(version=2)
        assert s1.fingerprint != s2.fingerprint


# ---------------------------------------------------------------------------
# D19 MetricRegistry
# ---------------------------------------------------------------------------


class TestMetricRegistryMutationPolicy:
    def test_register_and_get(self) -> None:
        registry = MetricRegistry()
        spec = _valid_spec(metric_id="m1")
        registry.register(spec, _formula_for(spec))
        got_spec, got_formula = registry.get("m1")
        assert got_spec == spec
        assert got_formula.metric_id == "m1"

    def test_re_registration_same_fingerprint_is_idempotent(self) -> None:
        registry = MetricRegistry()
        spec = _valid_spec(metric_id="m1")
        registry.register(spec, _formula_for(spec))
        # Same spec again → no-op.
        registry.register(spec, _formula_for(spec))
        assert len(registry) == 1

    def test_reuse_with_different_fingerprint_rejected(self) -> None:
        registry = MetricRegistry()
        spec_v1 = _valid_spec(
            metric_id="m1",
            formula_summary="old",
            version=1,
        )
        registry.register(spec_v1, _formula_for(spec_v1))

        spec_v2 = _valid_spec(
            metric_id="m1",
            formula_summary="new",
            version=2,
        )
        with pytest.raises(MetricReusedIdError) as exc_info:
            registry.register(spec_v2, _formula_for(spec_v2))
        assert exc_info.value.metric_id == "m1"
        assert exc_info.value.existing_fingerprint == spec_v1.fingerprint
        assert exc_info.value.new_fingerprint == spec_v2.fingerprint

    def test_reuse_different_owner_rejected(self) -> None:
        registry = MetricRegistry()
        spec_v1 = _valid_spec(metric_id="m1", owner="component_a")
        registry.register(spec_v1, _formula_for(spec_v1))
        spec_v2 = _valid_spec(metric_id="m1", owner="component_b")
        with pytest.raises(MetricReusedIdError):
            registry.register(spec_v2, _formula_for(spec_v2))

    def test_reuse_different_categories_rejected(self) -> None:
        registry = MetricRegistry()
        spec_v1 = _valid_spec(
            metric_id="m1",
            categories=frozenset({METRIC_CATEGORY_QUALITY}),
        )
        registry.register(spec_v1, _formula_for(spec_v1))
        spec_v2 = _valid_spec(
            metric_id="m1",
            categories=frozenset({METRIC_CATEGORY_COVERAGE}),
        )
        with pytest.raises(MetricReusedIdError):
            registry.register(spec_v2, _formula_for(spec_v2))

    def test_spec_formula_id_mismatch_rejected(self) -> None:
        registry = MetricRegistry()
        spec = _valid_spec(metric_id="m1")
        formula = MetricFormula(
            metric_id="m2",
            compute=_noop_compute,
        )
        with pytest.raises(MetricSpecFormulaMismatchError) as exc_info:
            registry.register(spec, formula)
        assert exc_info.value.spec_id == "m1"
        assert exc_info.value.formula_id == "m2"


class TestMetricRegistryQueries:
    def test_has_spec(self) -> None:
        registry = MetricRegistry()
        spec = _valid_spec(metric_id="m1")
        registry.register(spec, _formula_for(spec))
        assert registry.has_spec("m1")
        assert not registry.has_spec("m2")

    def test_contains(self) -> None:
        registry = MetricRegistry()
        spec = _valid_spec(metric_id="m1")
        registry.register(spec, _formula_for(spec))
        assert "m1" in registry
        assert "m2" not in registry

    def test_len(self) -> None:
        registry = MetricRegistry()
        for i in range(3):
            spec = _valid_spec(metric_id=f"m{i}")
            registry.register(spec, _formula_for(spec))
        assert len(registry) == 3

    def test_get_unknown_raises_keyerror(self) -> None:
        registry = MetricRegistry()
        with pytest.raises(KeyError):
            registry.get("m1")

    def test_all_specs(self) -> None:
        registry = MetricRegistry()
        for i in range(3):
            spec = _valid_spec(metric_id=f"m{i}")
            registry.register(spec, _formula_for(spec))
        assert len(registry.all_specs()) == 3

    def test_cross_cutting_specs_filters_by_category_count(self) -> None:
        registry = MetricRegistry()
        registry.register(
            _valid_spec(
                metric_id="cc",
                categories=frozenset(
                    {METRIC_CATEGORY_QUALITY, METRIC_CATEGORY_COVERAGE,
                     METRIC_CATEGORY_EFFICIENCY}
                ),
            ),
            _formula_for(_valid_spec(metric_id="cc")),
        )
        registry.register(
            _valid_spec(
                metric_id="single",
                categories=frozenset({METRIC_CATEGORY_QUALITY}),
            ),
            _formula_for(_valid_spec(metric_id="single")),
        )
        cc = registry.cross_cutting_specs()
        assert len(cc) == 1
        assert cc[0].metric_id == "cc"

    def test_categories_in_use(self) -> None:
        registry = MetricRegistry()
        registry.register(
            _valid_spec(
                metric_id="m1",
                categories=frozenset({METRIC_CATEGORY_QUALITY}),
            ),
            _formula_for(_valid_spec(metric_id="m1")),
        )
        assert registry.categories_in_use() == frozenset(
            {METRIC_CATEGORY_QUALITY}
        )


# ---------------------------------------------------------------------------
# Default registry — covers D23 cross-cutting requirements
# ---------------------------------------------------------------------------


class TestDefaultRegistry:
    def test_default_registry_has_six_metrics(self) -> None:
        registry = build_default_registry()
        assert len(registry) == 6

    def test_all_default_metrics_are_cross_cutting(self) -> None:
        registry = build_default_registry()
        for spec in registry.all_specs():
            assert spec.is_cross_cutting, (
                f"default metric {spec.metric_id!r} MUST be "
                f"cross-cutting (≥3 categories) per D23; "
                f"got {len(spec.categories)} categories"
            )

    def test_default_registry_covers_all_four_categories(self) -> None:
        """Per M3 from design gate round 2: Efficiency MUST
        not be missing from the cross-cutting coverage."""
        registry = build_default_registry()
        in_use = registry.categories_in_use()
        assert in_use == ALL_METRIC_CATEGORIES, (
            f"default registry MUST cover all 4 categories; "
            f"got {sorted(in_use)}"
        )

    def test_each_default_metric_spans_three_categories(self) -> None:
        registry = build_default_registry()
        for spec in registry.all_specs():
            assert len(spec.categories) == 3, (
                f"{spec.metric_id!r} spans "
                f"{len(spec.categories)} categories; "
                f"default cross-cutting metrics span exactly 3"
            )

    def test_default_metric_owners_are_components(self) -> None:
        registry = build_default_registry()
        for spec in registry.all_specs():
            assert spec.owner, (
                f"{spec.metric_id!r} has no owner; per D19 "
                f"owner = accountable component, MUST be non-empty"
            )
            # Owners in default registry are component names.
            assert spec.owner in {
                "planner",
                "validator",
                "confirmation_gate",
                "executor",
                "runner",
            }, f"unexpected owner {spec.owner!r}"

    def test_default_metric_ids_are_unique(self) -> None:
        registry = build_default_registry()
        ids = [spec.metric_id for spec in registry.all_specs()]
        assert len(ids) == len(set(ids)), (
            f"default registry has duplicate metric_ids: {ids}"
        )

    def test_default_metric_units_and_directions_are_valid(self) -> None:
        registry = build_default_registry()
        for spec in registry.all_specs():
            assert spec.unit
            assert spec.direction in ALL_METRIC_DIRECTIONS

    def test_efficiency_dimension_has_at_least_one_metric(self) -> None:
        """Per M3 — Efficiency must appear in cross-cutting
        coverage."""
        registry = build_default_registry()
        efficiency_specs = [
            spec for spec in registry.all_specs()
            if METRIC_CATEGORY_EFFICIENCY in spec.categories
        ]
        assert len(efficiency_specs) >= 1

    def test_efficiency_metric_present_by_id(self) -> None:
        """The Efficiency metric added per M3 is
        planner_latency_cost."""
        registry = build_default_registry()
        assert registry.has_spec("planner_latency_cost")


# ---------------------------------------------------------------------------
# Compute helpers — read from fixture_results.observed
# ---------------------------------------------------------------------------


class TestComputeParseSuccessRate:
    def test_no_fixtures_returns_zero(self) -> None:
        report = _make_report()
        assert compute_metric(report, "parse_success_rate") == 0.0

    def test_all_parsed(self) -> None:
        report = _make_report(
            (
                FixtureResult(
                    fixture_id="f1",
                    status="completed",
                    expected={},
                    observed={"parse_status": "parsed"},
                ),
                FixtureResult(
                    fixture_id="f2",
                    status="completed",
                    expected={},
                    observed={"parse_status": "parsed"},
                ),
            )
        )
        assert compute_metric(report, "parse_success_rate") == 1.0

    def test_partial(self) -> None:
        report = _make_report(
            (
                FixtureResult(
                    fixture_id="f1",
                    status="completed",
                    expected={},
                    observed={"parse_status": "parsed"},
                ),
                FixtureResult(
                    fixture_id="f2",
                    status="completed",
                    expected={},
                    observed={"parse_status": "failed"},
                ),
                FixtureResult(
                    fixture_id="f3",
                    status="completed",
                    expected={},
                    observed={"parse_status": "parsed"},
                ),
            )
        )
        # 2/3 ≈ 0.6667
        result = compute_metric(report, "parse_success_rate")
        assert abs(result - 2 / 3) < 1e-9

    def test_fixtures_without_parse_status_excluded(self) -> None:
        report = _make_report(
            (
                FixtureResult(
                    fixture_id="f1",
                    status="completed",
                    expected={},
                    observed={"parse_status": "parsed"},
                ),
                FixtureResult(
                    fixture_id="f2",
                    status="completed",
                    expected={},
                    observed={"other_key": "value"},
                ),
            )
        )
        # Only 1 fixture has parse_status → 1/1 = 1.0
        assert compute_metric(report, "parse_success_rate") == 1.0


class TestComputeValidationRejectionAccuracy:
    def test_perfect_accuracy(self) -> None:
        report = _make_report(
            (
                FixtureResult(
                    fixture_id="f1",
                    status="completed",
                    expected={},
                    observed={
                        "needs_rejection": True,
                        "validator_decision": "rejected",
                    },
                ),
                FixtureResult(
                    fixture_id="f2",
                    status="completed",
                    expected={},
                    observed={
                        "needs_rejection": False,
                        "validator_decision": "accepted",
                    },
                ),
            )
        )
        assert compute_metric(
            report, "validation_rejection_accuracy"
        ) == 1.0

    def test_false_negative(self) -> None:
        report = _make_report(
            (
                FixtureResult(
                    fixture_id="f1",
                    status="completed",
                    expected={},
                    observed={
                        "needs_rejection": True,
                        "validator_decision": "accepted",
                    },
                ),
            )
        )
        assert compute_metric(
            report, "validation_rejection_accuracy"
        ) == 0.0

    def test_false_positive(self) -> None:
        report = _make_report(
            (
                FixtureResult(
                    fixture_id="f1",
                    status="completed",
                    expected={},
                    observed={
                        "needs_rejection": False,
                        "validator_decision": "rejected",
                    },
                ),
            )
        )
        assert compute_metric(
            report, "validation_rejection_accuracy"
        ) == 0.0


class TestComputeConfirmationCorrectness:
    def test_perfect_correctness(self) -> None:
        report = _make_report(
            (
                FixtureResult(
                    fixture_id="f1",
                    status="completed",
                    expected={},
                    observed={
                        "confirmation_prompted": True,
                        "confirmation_outcome": "confirmed",
                    },
                ),
                FixtureResult(
                    fixture_id="f2",
                    status="completed",
                    expected={},
                    observed={
                        "confirmation_prompted": False,
                        "confirmation_outcome": "aborted",
                    },
                ),
            )
        )
        assert compute_metric(
            report, "confirmation_correctness"
        ) == 1.0

    def test_wrong_outcome(self) -> None:
        report = _make_report(
            (
                FixtureResult(
                    fixture_id="f1",
                    status="completed",
                    expected={},
                    observed={
                        "confirmation_prompted": True,
                        "confirmation_outcome": "aborted",
                    },
                ),
            )
        )
        assert compute_metric(
            report, "confirmation_correctness"
        ) == 0.0


class TestComputeExecutionDivergenceRate:
    def test_no_divergence(self) -> None:
        report = _make_report(
            (
                FixtureResult(
                    fixture_id="f1",
                    status="completed",
                    expected={},
                    observed={"execution_diverged": False},
                ),
            )
        )
        assert compute_metric(
            report, "execution_divergence_rate"
        ) == 0.0

    def test_full_divergence(self) -> None:
        report = _make_report(
            (
                FixtureResult(
                    fixture_id="f1",
                    status="completed",
                    expected={},
                    observed={"execution_diverged": True},
                ),
                FixtureResult(
                    fixture_id="f2",
                    status="completed",
                    expected={},
                    observed={"execution_diverged": True},
                ),
            )
        )
        assert compute_metric(
            report, "execution_divergence_rate"
        ) == 1.0


class TestComputeEvaluationCompletenessRate:
    def test_all_completed(self) -> None:
        report = _make_report(
            (
                FixtureResult(
                    fixture_id="f1",
                    status="completed",
                    expected={},
                    observed={},
                ),
                FixtureResult(
                    fixture_id="f2",
                    status="completed",
                    expected={},
                    observed={},
                ),
            )
        )
        assert compute_metric(
            report, "evaluation_completeness_rate"
        ) == 1.0

    def test_partial(self) -> None:
        report = _make_report(
            (
                FixtureResult(
                    fixture_id="f1",
                    status="completed",
                    expected={},
                    observed={},
                ),
                FixtureResult(
                    fixture_id="f2",
                    status="aborted",
                    expected={},
                    observed={},
                ),
                FixtureResult(
                    fixture_id="f3",
                    status="errored",
                    expected={},
                    observed={},
                ),
            )
        )
        # 1/3 ≈ 0.333
        result = compute_metric(
            report, "evaluation_completeness_rate"
        )
        assert abs(result - 1 / 3) < 1e-9

    def test_extension_status_not_counted(self) -> None:
        """Per M2: status is free-form; only 'completed'
        counts as evaluated. 'infrastructure_error' and
        'skipped' are NOT counted."""
        report = _make_report(
            (
                FixtureResult(
                    fixture_id="f1",
                    status="completed",
                    expected={},
                    observed={},
                ),
                FixtureResult(
                    fixture_id="f2",
                    status="infrastructure_error",
                    expected={},
                    observed={},
                ),
                FixtureResult(
                    fixture_id="f3",
                    status="skipped",
                    expected={},
                    observed={},
                ),
            )
        )
        assert compute_metric(
            report, "evaluation_completeness_rate"
        ) == 1 / 3


class TestComputePlannerLatencyCost:
    def test_no_latency_data_returns_zero(self) -> None:
        report = _make_report(
            (
                FixtureResult(
                    fixture_id="f1",
                    status="completed",
                    expected={},
                    observed={},
                ),
            )
        )
        assert compute_metric(report, "planner_latency_cost") == 0.0

    def test_average(self) -> None:
        report = _make_report(
            (
                FixtureResult(
                    fixture_id="f1",
                    status="completed",
                    expected={},
                    observed={"planner_latency_ms": 100.0},
                ),
                FixtureResult(
                    fixture_id="f2",
                    status="completed",
                    expected={},
                    observed={"planner_latency_ms": 200.0},
                ),
                FixtureResult(
                    fixture_id="f3",
                    status="completed",
                    expected={},
                    observed={"planner_latency_ms": 300.0},
                ),
            )
        )
        assert compute_metric(report, "planner_latency_cost") == 200.0

    def test_missing_key_excluded(self) -> None:
        report = _make_report(
            (
                FixtureResult(
                    fixture_id="f1",
                    status="completed",
                    expected={},
                    observed={"planner_latency_ms": 100.0},
                ),
                FixtureResult(
                    fixture_id="f2",
                    status="completed",
                    expected={},
                    observed={},
                ),
            )
        )
        assert compute_metric(report, "planner_latency_cost") == 100.0


# ---------------------------------------------------------------------------
# compute_all_metrics — full coverage
# ---------------------------------------------------------------------------


class TestComputeAllMetrics:
    def test_returns_all_six_metrics(self) -> None:
        report = _make_report(
            (
                FixtureResult(
                    fixture_id="f1",
                    status="completed",
                    expected={},
                    observed={
                        "parse_status": "parsed",
                        "needs_rejection": False,
                        "validator_decision": "accepted",
                        "confirmation_prompted": False,
                        "confirmation_outcome": "aborted",
                        "execution_diverged": False,
                        "planner_latency_ms": 50.0,
                    },
                ),
            )
        )
        result = compute_all_metrics(report)
        assert set(result.keys()) == {
            "parse_success_rate",
            "validation_rejection_accuracy",
            "confirmation_correctness",
            "execution_divergence_rate",
            "evaluation_completeness_rate",
            "planner_latency_cost",
        }
        assert all(isinstance(v, float) for v in result.values())

    def test_uses_custom_registry(self) -> None:
        custom = MetricRegistry()
        spec = _valid_spec(metric_id="custom_metric")
        custom.register(
            spec,
            MetricFormula(
                metric_id="custom_metric",
                compute=lambda r: 42.0,
            ),
        )
        report = _make_report()
        assert compute_metric(
            report, "custom_metric", registry=custom
        ) == 42.0


# ---------------------------------------------------------------------------
# Anti-false-positive tests — D19 owner / category
# validations prevent malformed specs.
# ---------------------------------------------------------------------------


class TestSpecValidationBoundaries:
    def test_categories_must_be_set_like(self) -> None:
        with pytest.raises(ValueError, match="invalid categories"):
            _valid_spec(
                categories=frozenset({"Quality", "MadeUpCategory"})
            )

    def test_owner_must_be_component_name(self) -> None:
        """Per D19 round 2 — owner is a component, not a
        person. The registry does NOT enforce naming
        conventions (that's P3.2.D's job if needed); it
        only enforces non-emptiness."""
        spec = _valid_spec(owner="any_non_empty_string")
        assert spec.owner == "any_non_empty_string"

    def test_zero_categories_rejected(self) -> None:
        with pytest.raises(ValueError, match="≥1 category"):
            _valid_spec(categories=frozenset())

    def test_direction_value_must_match_enum(self) -> None:
        with pytest.raises(ValueError, match="direction MUST be"):
            _valid_spec(direction="higher")