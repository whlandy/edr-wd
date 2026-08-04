"""
metrics.py — P3.2.C metric registry and cross-cutting metrics.

Implements P3.2 design gate contracts:

    * D19 — Metric lifecycle / ownership:
      - Single source of truth for `metric_id`.
      - Owner = accountable component (not a person).
      - `metric_id` MUST NOT be reused with different
        semantics across registrations (mutation policy
        enforced via spec fingerprint).
      - Spec version is additive within a
        `(dataset_version, planner_version)` pair.

    * D23 — Metric taxonomy:
      - Four categories: Coverage, Quality, Efficiency,
        Behaviour.
      - Every metric MUST be tagged with ≥1 category.
      - Cross-cutting metrics MUST span ≥3 of the four
        categories, ensuring evaluation considers all
        dimensions together (per round 2 design review).

Scope boundary (per round 2 review):

    * C owns metric DEFINITION: spec, registry, taxonomy,
      formula signatures.
    * Threshold / policy / CI gate behaviour are owned
      by P3.2.D and P3.2.F respectively.
    * The runner (P3.2.E) populates `fixture_results`
      such that the formulas below produce meaningful
      values. The contract for what `observed` MUST
      contain for each formula is documented per
      formula in the `notes` field of `MetricFormula`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import (
    Callable,
    FrozenSet,
    Mapping,
    Sequence,
)

from agent.eval.reports import EvaluationReport, FixtureResult


# ---------------------------------------------------------------------------
# D23 metric taxonomy
# ---------------------------------------------------------------------------

METRIC_CATEGORY_COVERAGE: str = "Coverage"
METRIC_CATEGORY_QUALITY: str = "Quality"
METRIC_CATEGORY_EFFICIENCY: str = "Efficiency"
METRIC_CATEGORY_BEHAVIOUR: str = "Behaviour"

ALL_METRIC_CATEGORIES: FrozenSet[str] = frozenset(
    {
        METRIC_CATEGORY_COVERAGE,
        METRIC_CATEGORY_QUALITY,
        METRIC_CATEGORY_EFFICIENCY,
        METRIC_CATEGORY_BEHAVIOUR,
    }
)

METRIC_DIRECTION_HIGHER_IS_BETTER: str = "higher_is_better"
METRIC_DIRECTION_LOWER_IS_BETTER: str = "lower_is_better"

ALL_METRIC_DIRECTIONS: FrozenSet[str] = frozenset(
    {
        METRIC_DIRECTION_HIGHER_IS_BETTER,
        METRIC_DIRECTION_LOWER_IS_BETTER,
    }
)

# A metric is "cross-cutting" iff it spans ≥3 of the 4
# categories. This is the threshold required by D23.
CROSS_CUTTING_CATEGORY_THRESHOLD: int = 3


# ---------------------------------------------------------------------------
# D19 metric spec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricSpec:
    """Static specification of a single metric.

    Per D19:
      * `metric_id` is the single source of truth. Once
        registered, it MUST NOT be reused with different
        semantics. The registry enforces this via a
        spec fingerprint.
      * `categories` is the D23 taxonomy tag set.
        ≥1 required; cross-cutting metrics use ≥3.
      * `owner` is the accountable component name (per
        D19 round 2: not a person).
      * `version` is the additive version within a
        `(dataset_version, planner_version)` pair. Pure
        version bumps keep the spec fingerprint stable;
        semantic shifts MUST bump `version` AND change
        the spec.
    """

    metric_id: str
    categories: FrozenSet[str]
    formula_summary: str
    unit: str
    direction: str
    owner: str
    version: int = 1

    def __post_init__(self) -> None:
        if not self.metric_id:
            raise ValueError("metric_id MUST be non-empty")
        if not self.categories:
            raise ValueError(
                f"metric {self.metric_id!r} MUST have ≥1 category "
                f"(D23 taxonomy)"
            )
        invalid_categories = self.categories - ALL_METRIC_CATEGORIES
        if invalid_categories:
            raise ValueError(
                f"metric {self.metric_id!r} has invalid categories "
                f"{sorted(invalid_categories)}; allowed: "
                f"{sorted(ALL_METRIC_CATEGORIES)}"
            )
        if not self.formula_summary:
            raise ValueError(
                f"metric {self.metric_id!r} MUST have a "
                f"formula_summary (D19)"
            )
        if not self.unit:
            raise ValueError(
                f"metric {self.metric_id!r} MUST have a unit (D19)"
            )
        if self.direction not in ALL_METRIC_DIRECTIONS:
            raise ValueError(
                f"metric {self.metric_id!r} direction MUST be one of "
                f"{sorted(ALL_METRIC_DIRECTIONS)}; got "
                f"{self.direction!r}"
            )
        if not self.owner:
            raise ValueError(
                f"metric {self.metric_id!r} MUST have an owner "
                f"(D19 — accountable component)"
            )
        if self.version < 1:
            raise ValueError(
                f"metric {self.metric_id!r} version MUST be ≥1; "
                f"got {self.version}"
            )

    @property
    def is_cross_cutting(self) -> bool:
        """True iff this metric spans ≥3 categories (D23)."""
        return len(self.categories) >= CROSS_CUTTING_CATEGORY_THRESHOLD

    @property
    def fingerprint(self) -> str:
        """Stable hash of the semantically meaningful fields.

        Used by the registry to detect metric_id reuse with
        different semantics. Two specs with identical
        metric_id but different fingerprints MUST NOT
        coexist in the same registry.
        """
        return _spec_fingerprint(self)


def _spec_fingerprint(spec: MetricSpec) -> str:
    """Compute a SHA-256 hex fingerprint of the
    semantically meaningful spec fields.

    Order of categories and other iterables is
    canonicalised via sorted() to ensure the fingerprint
    is stable across runs.
    """
    payload = {
        "metric_id": spec.metric_id,
        "categories": sorted(spec.categories),
        "formula_summary": spec.formula_summary,
        "unit": spec.unit,
        "direction": spec.direction,
        "owner": spec.owner,
        "version": spec.version,
    }
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# ---------------------------------------------------------------------------
# D19 metric formula
# ---------------------------------------------------------------------------

# A metric formula is a pure function over an
# EvaluationReport. The runner (P3.2.E) populates
# `fixture_results` such that the formula can read its
# inputs from `observed` keys. The contract for each
# formula's required `observed` keys is documented in
# the `notes` field.
MetricComputeFn = Callable[[EvaluationReport], float]


@dataclass(frozen=True)
class MetricFormula:
    """Reference to the computation that derives a metric
    value from an EvaluationReport.

    Per D19 the formula is a pure function — no I/O, no
    randomness, no clock. Two reports with identical
    `fixture_results` MUST produce identical metric
    values for the same formula.

    The `notes` field documents the contract the runner
    (P3.2.E) MUST satisfy when populating `fixture_results
    [].observed` for this formula.
    """

    metric_id: str
    compute: MetricComputeFn
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.metric_id:
            raise ValueError("MetricFormula.metric_id MUST be non-empty")
        if not callable(self.compute):
            raise TypeError(
                f"MetricFormula.compute MUST be callable; got "
                f"{type(self.compute).__name__}"
            )


# ---------------------------------------------------------------------------
# D19 metric registry
# ---------------------------------------------------------------------------


class MetricRegistryError(ValueError):
    """Base error for metric registry violations."""


class MetricReusedIdError(MetricRegistryError):
    """Raised when `metric_id` is registered with a
    different semantic fingerprint than an existing
    registration. Per D19, `metric_id` MUST NOT be reused
    with different semantics across registrations."""

    def __init__(
        self, metric_id: str, existing_fingerprint: str, new_fingerprint: str
    ) -> None:
        super().__init__(
            f"metric_id {metric_id!r} is already registered with a "
            f"different spec (existing fingerprint "
            f"{existing_fingerprint[:16]}..., new fingerprint "
            f"{new_fingerprint[:16]}...); per D19, metric_id MUST "
            f"NOT be reused with different semantics"
        )
        self.metric_id = metric_id
        self.existing_fingerprint = existing_fingerprint
        self.new_fingerprint = new_fingerprint


class MetricSpecFormulaMismatchError(MetricRegistryError):
    """Raised when a spec and formula carry different
    `metric_id` values."""

    def __init__(self, spec_id: str, formula_id: str) -> None:
        super().__init__(
            f"spec.metric_id={spec_id!r} does not match "
            f"formula.metric_id={formula_id!r}"
        )
        self.spec_id = spec_id
        self.formula_id = formula_id


@dataclass(frozen=True)
class _Registration:
    fingerprint: str
    spec: MetricSpec


@dataclass(frozen=True)
class MetricRegistry:
    """Single source of truth for metric_id (per D19).

    Stores `(fingerprint, spec)` per metric_id and a
    `MetricFormula` for each. Enforces the mutation
    policy: re-registering a metric_id with a different
    fingerprint is rejected (`MetricReusedIdError`).

    Re-registering with the same fingerprint is
    idempotent — useful for plugin loaders that may
    import the same metric twice.

    Layer boundary (per round 2 review): this registry
    is for metric DEFINITION. Threshold declarations
    live in P3.2.D. CI gate policy lives in P3.2.F.
    """

    _entries: dict[str, tuple[_Registration, MetricFormula]] = field(
        default_factory=dict
    )

    def register(
        self, spec: MetricSpec, formula: MetricFormula
    ) -> None:
        """Register a metric spec + formula pair.

        Idempotent if the existing entry has the same
        fingerprint. Rejects if a different fingerprint
        is provided for an existing metric_id.
        """
        if spec.metric_id != formula.metric_id:
            raise MetricSpecFormulaMismatchError(
                spec.metric_id, formula.metric_id
            )
        fp = spec.fingerprint
        existing = self._entries.get(spec.metric_id)
        if existing is not None:
            existing_reg, existing_formula = existing
            if existing_reg.fingerprint != fp:
                raise MetricReusedIdError(
                    spec.metric_id, existing_reg.fingerprint, fp
                )
            # Same fingerprint: no-op re-registration.
            return
        self._entries[spec.metric_id] = (
            _Registration(fingerprint=fp, spec=spec),
            formula,
        )

    def get(
        self, metric_id: str
    ) -> tuple[MetricSpec, MetricFormula]:
        """Retrieve the spec and formula for a metric_id.

        Raises `KeyError` if the metric_id is not
        registered.
        """
        entry = self._entries[metric_id]
        return entry[0].spec, entry[1]

    def has_spec(self, metric_id: str) -> bool:
        return metric_id in self._entries

    def all_specs(self) -> tuple[MetricSpec, ...]:
        return tuple(reg.spec for reg, _ in self._entries.values())

    def all_formulas(self) -> tuple[MetricFormula, ...]:
        return tuple(formula for _, formula in self._entries.values())

    def cross_cutting_specs(self) -> tuple[MetricSpec, ...]:
        """Specs that span ≥3 categories (D23)."""
        return tuple(
            spec for spec in self.all_specs() if spec.is_cross_cutting
        )

    def single_dimension_specs(self) -> tuple[MetricSpec, ...]:
        """Specs that span <3 categories (D23)."""
        return tuple(
            spec for spec in self.all_specs() if not spec.is_cross_cutting
        )

    def categories_in_use(self) -> FrozenSet[str]:
        """Union of categories across all registered specs.

        A healthy registry covering all 4 D23 categories
        returns a 4-element set. This is the property
        used by the design review's "Efficiency must not
        be missing" check.
        """
        result: set[str] = set()
        for spec in self.all_specs():
            result.update(spec.categories)
        return frozenset(result)

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, metric_id: object) -> bool:
        return isinstance(metric_id, str) and metric_id in self._entries

    def __iter__(self):
        return iter(self._entries)


# ---------------------------------------------------------------------------
# Default registry — 6 cross-cutting metrics
# ---------------------------------------------------------------------------
#
# Per D23 (round 2): cross-cutting metrics MUST span ≥3
# of the 4 categories (Coverage / Quality / Efficiency /
# Behaviour). Each of the 6 metrics below spans exactly
# 3 categories, with different subsets, ensuring the
# default registry covers all 4 dimensions.
#
# Category coverage:
#   Quality     6/6 metrics
#   Behaviour   5/6 metrics
#   Coverage    5/6 metrics
#   Efficiency  3/6 metrics   ← per M3 (efficiency dimension)
#
# Owners are accountable components (per D19 round 2),
# not persons.


def _spec_parse_success_rate() -> MetricSpec:
    """Fraction of planner outputs that parsed successfully.

    Contract for runner (P3.2.E):
      * Each `FixtureResult.observed` MUST contain
        `"parse_status": "parsed"` for parseable outputs
        and `"parse_status": "failed"` for parse failures.
      * Fixtures without `"parse_status"` are excluded
        from both numerator and denominator.
    """
    return MetricSpec(
        metric_id="parse_success_rate",
        categories=frozenset(
            {METRIC_CATEGORY_QUALITY, METRIC_CATEGORY_BEHAVIOUR,
             METRIC_CATEGORY_COVERAGE}
        ),
        formula_summary=(
            "count(observed.parse_status=='parsed') / "
            "count(observed has 'parse_status')"
        ),
        unit="ratio",
        direction=METRIC_DIRECTION_HIGHER_IS_BETTER,
        owner="planner",
    )


def _compute_parse_success_rate(report: EvaluationReport) -> float:
    numerator = 0
    denominator = 0
    for fr in report.fixture_results:
        if "parse_status" not in fr.observed:
            continue
        denominator += 1
        if fr.observed.get("parse_status") == "parsed":
            numerator += 1
    if denominator == 0:
        return 0.0
    return numerator / denominator


def _spec_validation_rejection_accuracy() -> MetricSpec:
    """Fraction of invalid plans correctly rejected by the
    validator.

    Contract for runner (P3.2.E):
      * Each `FixtureResult.observed` MUST contain
        `"needs_rejection": bool` (ground truth) and
        `"validator_decision": "rejected" | "accepted"`
        (validator output).
    """
    return MetricSpec(
        metric_id="validation_rejection_accuracy",
        categories=frozenset(
            {METRIC_CATEGORY_QUALITY, METRIC_CATEGORY_COVERAGE,
             METRIC_CATEGORY_EFFICIENCY}
        ),
        formula_summary=(
            "count(needs_rejection == rejected) / "
            "count(needs_rejection)"
        ),
        unit="ratio",
        direction=METRIC_DIRECTION_HIGHER_IS_BETTER,
        owner="validator",
    )


def _compute_validation_rejection_accuracy(
    report: EvaluationReport,
) -> float:
    numerator = 0
    denominator = 0
    for fr in report.fixture_results:
        observed = fr.observed
        if "needs_rejection" not in observed:
            continue
        denominator += 1
        needs = bool(observed.get("needs_rejection"))
        decided = observed.get("validator_decision")
        if needs and decided == "rejected":
            numerator += 1
        elif not needs and decided == "accepted":
            numerator += 1
    if denominator == 0:
        return 0.0
    return numerator / denominator


def _spec_confirmation_correctness() -> MetricSpec:
    """Fraction of confirmation prompts that were resolved
    correctly.

    Contract for runner (P3.2.E):
      * Each `FixtureResult.observed` MUST contain
        `"confirmation_prompted": bool` (ground truth) and
        `"confirmation_outcome": "confirmed" |
        "aborted"` (user + gate combined outcome).
    """
    return MetricSpec(
        metric_id="confirmation_correctness",
        categories=frozenset(
            {METRIC_CATEGORY_QUALITY, METRIC_CATEGORY_BEHAVIOUR,
             METRIC_CATEGORY_COVERAGE}
        ),
        formula_summary=(
            "count(correct confirmation outcomes) / "
            "count(confirmation prompts)"
        ),
        unit="ratio",
        direction=METRIC_DIRECTION_HIGHER_IS_BETTER,
        owner="confirmation_gate",
    )


def _compute_confirmation_correctness(
    report: EvaluationReport,
) -> float:
    numerator = 0
    denominator = 0
    for fr in report.fixture_results:
        observed = fr.observed
        if "confirmation_prompted" not in observed:
            continue
        denominator += 1
        prompted = bool(observed.get("confirmation_prompted"))
        outcome = observed.get("confirmation_outcome")
        # Correct = (prompted & confirmed) | (not prompted
        # & aborted). The exact correctness rule is owned
        # by the runner; this is the placeholder reading.
        if prompted and outcome == "confirmed":
            numerator += 1
        elif not prompted and outcome == "aborted":
            numerator += 1
    if denominator == 0:
        return 0.0
    return numerator / denominator


def _spec_execution_divergence_rate() -> MetricSpec:
    """Fraction of executions that diverged from the
    planned action sequence.

    Contract for runner (P3.2.E):
      * Each `FixtureResult.observed` MUST contain
        `"execution_diverged": bool`.
    """
    return MetricSpec(
        metric_id="execution_divergence_rate",
        categories=frozenset(
            {METRIC_CATEGORY_BEHAVIOUR, METRIC_CATEGORY_QUALITY,
             METRIC_CATEGORY_EFFICIENCY}
        ),
        formula_summary=(
            "count(observed.execution_diverged == True) / "
            "count(observed has 'execution_diverged')"
        ),
        unit="ratio",
        direction=METRIC_DIRECTION_LOWER_IS_BETTER,
        owner="executor",
    )


def _compute_execution_divergence_rate(
    report: EvaluationReport,
) -> float:
    numerator = 0
    denominator = 0
    for fr in report.fixture_results:
        observed = fr.observed
        if "execution_diverged" not in observed:
            continue
        denominator += 1
        if bool(observed.get("execution_diverged")):
            numerator += 1
    if denominator == 0:
        return 0.0
    return numerator / denominator


def _spec_evaluation_completeness_rate() -> MetricSpec:
    """Fraction of scheduled fixtures that were evaluated
    (not skipped / not errored in infrastructure).

    Contract for runner (P3.2.E):
      * Every fixture in the dataset MUST produce a
        `FixtureResult` (the dataset identity comes from
        P3.2.A).
      * `status == 'completed'` is counted as evaluated.
        `aborted`, `errored`, `infrastructure_error`,
        etc. are NOT counted.
    """
    return MetricSpec(
        metric_id="evaluation_completeness_rate",
        categories=frozenset(
            {METRIC_CATEGORY_COVERAGE, METRIC_CATEGORY_QUALITY,
             METRIC_CATEGORY_BEHAVIOUR}
        ),
        formula_summary=(
            "count(status == 'completed') / "
            "count(fixture_results)"
        ),
        unit="ratio",
        direction=METRIC_DIRECTION_HIGHER_IS_BETTER,
        owner="runner",
    )


def _compute_evaluation_completeness_rate(
    report: EvaluationReport,
) -> float:
    total = len(report.fixture_results)
    if total == 0:
        return 0.0
    completed = sum(
        1 for fr in report.fixture_results if fr.status == "completed"
    )
    return completed / total


def _spec_planner_latency_cost() -> MetricSpec:
    """Average planner latency in milliseconds per fixture.

    Per M3 (design gate round 2): this is the Efficiency
    cross-cutting metric added in implementation phase to
    ensure the Efficiency dimension is not zero in the
    default registry.

    Contract for runner (P3.2.E):
      * Each `FixtureResult.observed` MAY contain
        `"planner_latency_ms": float` (positive).
      * Fixtures without the key are excluded.
    """
    return MetricSpec(
        metric_id="planner_latency_cost",
        categories=frozenset(
            {METRIC_CATEGORY_EFFICIENCY, METRIC_CATEGORY_BEHAVIOUR,
             METRIC_CATEGORY_COVERAGE}
        ),
        formula_summary=(
            "mean(observed.planner_latency_ms)"
        ),
        unit="ms",
        direction=METRIC_DIRECTION_LOWER_IS_BETTER,
        owner="planner",
    )


def _compute_planner_latency_cost(report: EvaluationReport) -> float:
    latencies: list[float] = []
    for fr in report.fixture_results:
        v = fr.observed.get("planner_latency_ms")
        if v is None:
            continue
        try:
            latencies.append(float(v))
        except (TypeError, ValueError):
            continue
    if not latencies:
        return 0.0
    return sum(latencies) / len(latencies)


# Default cross-cutting metric specs (factory functions so
# callers can re-construct them deterministically).
DEFAULT_CROSS_CUTTING_SPEC_BUILDERS: tuple[
    Callable[[], MetricSpec], ...
] = (
    _spec_parse_success_rate,
    _spec_validation_rejection_accuracy,
    _spec_confirmation_correctness,
    _spec_execution_divergence_rate,
    _spec_evaluation_completeness_rate,
    _spec_planner_latency_cost,
)


def build_default_registry() -> MetricRegistry:
    """Construct a fresh registry pre-populated with the
    6 default cross-cutting metrics.

    Each call returns a NEW registry — there is no
    shared mutable state, so test fixtures can build
    independent copies without interference.
    """
    registry = MetricRegistry()
    builders_with_compute: tuple[
        tuple[Callable[[], MetricSpec], Callable[[EvaluationReport], float],
              str],
        ...
    ] = (
        (
            _spec_parse_success_rate,
            _compute_parse_success_rate,
            "Each observed MUST carry 'parse_status' ∈ "
            "{'parsed', 'failed'}.",
        ),
        (
            _spec_validation_rejection_accuracy,
            _compute_validation_rejection_accuracy,
            "Each observed MUST carry 'needs_rejection' "
            "(bool) and 'validator_decision' ∈ "
            "{'rejected', 'accepted'}.",
        ),
        (
            _spec_confirmation_correctness,
            _compute_confirmation_correctness,
            "Each observed MUST carry "
            "'confirmation_prompted' (bool) and "
            "'confirmation_outcome' ∈ {'confirmed', "
            "'aborted'}.",
        ),
        (
            _spec_execution_divergence_rate,
            _compute_execution_divergence_rate,
            "Each observed MUST carry 'execution_diverged' "
            "(bool).",
        ),
        (
            _spec_evaluation_completeness_rate,
            _compute_evaluation_completeness_rate,
            "Each FixtureResult MUST carry 'status' "
            "(string); 'completed' is counted as evaluated.",
        ),
        (
            _spec_planner_latency_cost,
            _compute_planner_latency_cost,
            "Each observed MAY carry 'planner_latency_ms' "
            "(float).",
        ),
    )
    for spec_fn, compute_fn, notes in builders_with_compute:
        spec = spec_fn()
        formula = MetricFormula(
            metric_id=spec.metric_id,
            compute=compute_fn,
            notes=notes,
        )
        registry.register(spec, formula)
    return registry


# ---------------------------------------------------------------------------
# Public compute helpers
# ---------------------------------------------------------------------------


def compute_metric(
    report: EvaluationReport,
    metric_id: str,
    *,
    registry: MetricRegistry | None = None,
) -> float:
    """Compute a single metric value from a report."""
    effective = registry if registry is not None else build_default_registry()
    spec, formula = effective.get(metric_id)
    if spec.metric_id != metric_id:
        # Should not happen — guarded by register().
        raise MetricRegistryError(
            f"registry returned wrong spec for {metric_id!r}"
        )
    return float(formula.compute(report))


def compute_all_metrics(
    report: EvaluationReport,
    *,
    registry: MetricRegistry | None = None,
) -> Mapping[str, float]:
    """Compute every registered metric on a report.

    Returns a dict keyed by metric_id. Values are the
    raw metric readings; threshold gating is the
    responsibility of P3.2.D, not this module.
    """
    effective = registry if registry is not None else build_default_registry()
    return {
        spec.metric_id: float(formula.compute(report))
        for spec, formula in (
            effective.get(s.metric_id) for s in effective.all_specs()
        )
    }


__all__ = [
    # D23 taxonomy.
    "METRIC_CATEGORY_COVERAGE",
    "METRIC_CATEGORY_QUALITY",
    "METRIC_CATEGORY_EFFICIENCY",
    "METRIC_CATEGORY_BEHAVIOUR",
    "ALL_METRIC_CATEGORIES",
    "METRIC_DIRECTION_HIGHER_IS_BETTER",
    "METRIC_DIRECTION_LOWER_IS_BETTER",
    "ALL_METRIC_DIRECTIONS",
    "CROSS_CUTTING_CATEGORY_THRESHOLD",
    # D19 spec.
    "MetricSpec",
    "MetricFormula",
    "MetricComputeFn",
    # D19 registry.
    "MetricRegistry",
    "MetricRegistryError",
    "MetricReusedIdError",
    "MetricSpecFormulaMismatchError",
    # Default registry builders + helpers.
    "DEFAULT_CROSS_CUTTING_SPEC_BUILDERS",
    "build_default_registry",
    "compute_metric",
    "compute_all_metrics",
]