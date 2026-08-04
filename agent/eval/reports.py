"""
reports.py — P3.2.B evaluation result schema.

Implements P3.2 design gate contract **D18** (Evaluation
result schema contract), with strict layer separation:

    * This module owns ONLY the structural schema. It does
      NOT do security heuristics or content taxonomy.
    * The D21 sanitisation kinds (`EXPECTATION_KINDS` /
      `OBSERVED_KINDS`) and structured payload types
      (`StructuredExpectation` / `SanitisedObserved`) live
      in `sanitisation.py` and are out of scope here.
    * The sensitive-content audit (selector / token /
      prompt detection) lives in `sanitisation_audit.py`
      and is invoked separately by the caller, not from
      `to_dict` / `from_dict`.

D18 contract items satisfied by this module:

    * Initial schema version `report_schema.v1`.
    * Required top-level fields:
      `schema_version`, `dataset_id`, `dataset_version`,
      `planner_version`, `execution_profile`, `started_at`,
      `ended_at`, `metrics`, `threshold_decisions`,
      `reproducibility_digest`, `fixture_results`,
      `run_id`.
    * JSON-parseable round-trip (to_dict / from_dict /
      to_json / from_json). Deterministic output
      (`sort_keys=True`) for D22 digest computation.
    * UTC timezone-aware datetimes; `ended_at >= started_at`.
    * Additive schema evolution: `SUPPORTED_SCHEMA_VERSIONS`
      is a frozen set; adding a version is a code change.
    * Top-level anti-raw-text rule: `expected` and
      `observed` MUST be `Mapping`, NOT a raw string /
      screenshot / prompt.

Layer-boundary notes:

    * The schema does NOT validate direction values,
      decision values, or status enum membership —
      those are domain taxonomy, owned by P3.2.C/D.
    * The schema does NOT call any security heuristic.
      Historical artefacts that happen to contain a long
      hex string in a `metric_id` MUST remain parseable.
    * `to_dict` and `from_dict` perform structural
      conversion only. Sensitive-content auditing is a
      separate step; see `sanitisation_audit.py`.

Public API:

    * REPORT_SCHEMA_VERSION — schema version constant.
    * SUPPORTED_SCHEMA_VERSIONS — set of supported versions.
    * MetricValue — single metric reading (placeholder
      shape; expanded in P3.2.C).
    * ThresholdDecision — single threshold decision
      (placeholder shape; rules in P3.2.D).
    * FixtureResult — per-fixture outcome.
    * EvaluationReport — full report.
    * RawTextPayloadError — raised when `expected` /
      `observed` is a raw string at the top level.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from json import dumps, loads
from typing import Any, FrozenSet, Mapping


# ---------------------------------------------------------------------------
# Schema version constants
# ---------------------------------------------------------------------------

REPORT_SCHEMA_VERSION: str = "report_schema.v1"
"""Initial D18 schema version. Add new versions additively
within a `(dataset_version, planner_version)` pair; breaking
changes require a fresh schema version."""

SUPPORTED_SCHEMA_VERSIONS: FrozenSet[str] = frozenset(
    {REPORT_SCHEMA_VERSION}
)
"""The set of schema versions this code base can read. A
report with a `schema_version` outside this set MUST be
rejected by `from_dict`."""


# ---------------------------------------------------------------------------
# Anti-raw-text rule (D18)
# ---------------------------------------------------------------------------


class RawTextPayloadError(ValueError):
    """Raised when an `expected` or `observed` field is a
    raw string at the top level.

    Per D18, `expected` and `observed` MUST be a `Mapping`,
    NOT a raw string / screenshot / prompt. This is the
    only content-shape rule the schema enforces. Sub-pattern
    detection (selectors, tokens, prompts) lives in
    `sanitisation_audit.py` and is invoked separately.
    """

    def __init__(self, message: str, *, path: str) -> None:
        super().__init__(message)
        self.path = path


# ---------------------------------------------------------------------------
# Placeholder payload shapes (extended in P3.2.C / D / E)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricValue:
    """Single metric reading.

    Minimum shape needed for the report to round-trip. The
    full registry and formula shape arrive in P3.2.C (D19).
    `direction` is kept as a free-form string here so D18
    does not constrain D19 taxonomy. Callers that need to
    validate the direction MUST do so at the metric
    registry layer, not the report layer.
    """

    metric_id: str
    value: float
    unit: str  # "ratio", "count", "ms", "bytes"
    direction: str  # convention is "higher_is_better" | "lower_is_better"


@dataclass(frozen=True)
class ThresholdDecision:
    """Single threshold decision (per D20).

    Minimum shape needed for the report to round-trip. The
    full declaration semantics arrive in P3.2.D (D20). D18
    does NOT enforce cross-field invariants (e.g.
    thresholded=True requires threshold) so D20 can evolve
    without modifying D18.
    """

    metric_id: str
    thresholded: bool
    decision: str  # convention: "pass" | "fail" | "ungated"
    observed: float
    threshold: float | None  # convention: None when ungated


@dataclass(frozen=True)
class FixtureResult:
    """Per-fixture outcome.

    `expected` and `observed` MUST be `Mapping` (per D18
    anti-raw-text rule). Free-form strings at the TOP LEVEL
    are rejected by `__post_init__`. The kind taxonomy
    (e.g. threshold / stage_reached / mismatch) belongs to
    P3.2.D and is not constrained here.

    `status` is a free-form string so future statuses
    (`skipped`, `timeout`, `infrastructure_error`, etc.)
    can be added without a schema bump.
    """

    fixture_id: str
    status: str
    expected: Mapping[str, Any]
    observed: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.fixture_id:
            raise ValueError("fixture_id MUST be non-empty")
        _assert_mapping_not_raw_text(self.expected, "expected")
        _assert_mapping_not_raw_text(self.observed, "observed")


def _assert_mapping_not_raw_text(
    value: Mapping[str, Any] | Any, path: str
) -> None:
    """Enforce the D18 anti-raw-text rule at the top level.

    A `Mapping[str, Any]` is required. A raw string (or any
    other non-mapping type) at the TOP LEVEL is rejected.
    The rule applies ONLY to the top level — values inside
    the mapping may be strings, since short identifiers
    (`metric_id`, `name`, `category`, etc.) are legitimate.
    """
    if isinstance(value, str):
        raise RawTextPayloadError(
            f"{path!r} MUST be a Mapping, not a raw string "
            f"(per D18 anti-raw-text rule)",
            path=path,
        )
    if not isinstance(value, Mapping):
        raise RawTextPayloadError(
            f"{path!r} MUST be a Mapping; got {type(value).__name__}",
            path=path,
        )


# ---------------------------------------------------------------------------
# EvaluationReport
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvaluationReport:
    """P3.2 evaluation result (D18 schema v1).

    Required top-level fields per D18:
      `schema_version`, `dataset_id`, `dataset_version`,
      `planner_version`, `execution_profile`, `started_at`,
      `ended_at`, `metrics`, `threshold_decisions`,
      `fixture_results`, `reproducibility_digest`, `run_id`.

    `started_at` and `ended_at` MUST be timezone-aware UTC
    datetimes. JSON serialisation uses ISO 8601.

    `metrics` is keyed by `metric_id`; values are
    `MetricValue`.

    `threshold_decisions` is a tuple of `ThresholdDecision`,
    one per metric (or a subset if some metrics are ungated
    per D20 round 2).

    `reproducibility_digest` is set by P3.2.E (D22). It MAY
    be the empty string in D18-only contexts; consumers
    MUST treat empty as "digest not yet computed".

    `run_id` is the unique run identifier (D22 EXCLUDES it
    from the digest). D18 does not constrain its shape;
    P3.2.E formalises the derivation.
    """

    schema_version: str
    dataset_id: str
    dataset_version: str
    planner_version: str
    execution_profile: str
    started_at: datetime
    ended_at: datetime
    metrics: Mapping[str, MetricValue]
    threshold_decisions: tuple[ThresholdDecision, ...]
    fixture_results: tuple[FixtureResult, ...]
    reproducibility_digest: str
    run_id: str

    def __post_init__(self) -> None:
        if self.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(
                f"schema_version MUST be one of "
                f"{sorted(SUPPORTED_SCHEMA_VERSIONS)}; got "
                f"{self.schema_version!r}"
            )
        if not self.dataset_id:
            raise ValueError("dataset_id MUST be non-empty")
        if not self.dataset_version:
            raise ValueError("dataset_version MUST be non-empty")
        if not self.planner_version:
            raise ValueError("planner_version MUST be non-empty")
        if not self.execution_profile:
            raise ValueError("execution_profile MUST be non-empty")
        if not self.run_id:
            raise ValueError("run_id MUST be non-empty")
        if self.started_at.tzinfo is None:
            raise ValueError("started_at MUST be timezone-aware")
        if self.ended_at.tzinfo is None:
            raise ValueError("ended_at MUST be timezone-aware")
        if (
            self.started_at.utcoffset()
            != timezone.utc.utcoffset(None)
        ):
            raise ValueError("started_at MUST be UTC")
        if (
            self.ended_at.utcoffset()
            != timezone.utc.utcoffset(None)
        ):
            raise ValueError("ended_at MUST be UTC")
        if self.ended_at < self.started_at:
            raise ValueError("ended_at MUST be >= started_at")

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict.

        Pure structural conversion — no security heuristics.
        Callers that need sensitive-content auditing MUST
        invoke `audit_report_sensitive_content` from
        `sanitisation_audit.py` separately.

        Round-trip guarantee: `EvaluationReport.from_dict(
        report.to_dict()) == report`.
        """
        return {
            "schema_version": self.schema_version,
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "planner_version": self.planner_version,
            "execution_profile": self.execution_profile,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat(),
            "metrics": {
                metric_id: {
                    "metric_id": mv.metric_id,
                    "value": mv.value,
                    "unit": mv.unit,
                    "direction": mv.direction,
                }
                for metric_id, mv in self.metrics.items()
            },
            "threshold_decisions": [
                {
                    "metric_id": td.metric_id,
                    "thresholded": td.thresholded,
                    "decision": td.decision,
                    "observed": td.observed,
                    "threshold": td.threshold,
                }
                for td in self.threshold_decisions
            ],
            "fixture_results": [
                {
                    "fixture_id": fr.fixture_id,
                    "status": fr.status,
                    "expected": dict(fr.expected),
                    "observed": dict(fr.observed),
                }
                for fr in self.fixture_results
            ],
            "reproducibility_digest": self.reproducibility_digest,
            "run_id": self.run_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EvaluationReport":
        """Deserialise from a JSON-parsed dict.

        Pure structural deserialisation. Validates the
        schema version and required fields. Does NOT call
        sensitive-content auditing — see
        `sanitisation_audit.audit_report_sensitive_content`.
        Historical artefacts that happen to contain a
        long hex string in `metric_id` MUST remain
        parseable here.

        Raises `ValueError` on schema mismatch. Raises
        `RawTextPayloadError` when `expected` / `observed`
        is a raw string at the top level.
        """
        schema_version = data.get("schema_version")
        if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(
                f"schema_version MUST be one of "
                f"{sorted(SUPPORTED_SCHEMA_VERSIONS)}; got "
                f"{schema_version!r}"
            )

        started_at = _parse_datetime(data["started_at"])
        ended_at = _parse_datetime(data["ended_at"])

        metrics = {
            metric_id: MetricValue(
                metric_id=mv["metric_id"],
                value=float(mv["value"]),
                unit=mv["unit"],
                direction=mv["direction"],
            )
            for metric_id, mv in data["metrics"].items()
        }

        threshold_decisions = tuple(
            ThresholdDecision(
                metric_id=td["metric_id"],
                thresholded=bool(td["thresholded"]),
                decision=td["decision"],
                observed=float(td["observed"]),
                threshold=(
                    None
                    if td["threshold"] is None
                    else float(td["threshold"])
                ),
            )
            for td in data["threshold_decisions"]
        )

        fixture_results = tuple(
            FixtureResult(
                fixture_id=fr["fixture_id"],
                status=fr["status"],
                expected=_parse_mapping(fr["expected"], "expected"),
                observed=_parse_mapping(fr["observed"], "observed"),
            )
            for fr in data["fixture_results"]
        )

        return cls(
            schema_version=schema_version,
            dataset_id=data["dataset_id"],
            dataset_version=data["dataset_version"],
            planner_version=data["planner_version"],
            execution_profile=data["execution_profile"],
            started_at=started_at,
            ended_at=ended_at,
            metrics=metrics,
            threshold_decisions=threshold_decisions,
            fixture_results=fixture_results,
            reproducibility_digest=data["reproducibility_digest"],
            run_id=data["run_id"],
        )


def _parse_mapping(value: Any, path: str) -> Mapping[str, Any]:
    """Parse a top-level expected/observed Mapping.

    Enforces the D18 anti-raw-text rule at the top level.
    """
    if isinstance(value, str):
        raise RawTextPayloadError(
            f"{path!r} MUST be a Mapping, not a raw string "
            f"(per D18 anti-raw-text rule)",
            path=path,
        )
    if not isinstance(value, Mapping):
        raise RawTextPayloadError(
            f"{path!r} MUST be a Mapping; got {type(value).__name__}",
            path=path,
        )
    return dict(value)


def _parse_datetime(value: Any) -> datetime:
    """Parse an ISO 8601 string to a timezone-aware datetime.

    Accepts the `+00:00` UTC offset. Naive datetimes are
    rejected (D18 requires UTC).
    """
    if not isinstance(value, str):
        raise ValueError(f"datetime MUST be a string; got {type(value)}")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as e:
        raise ValueError(f"invalid ISO 8601 datetime {value!r}: {e}")
    if dt.tzinfo is None:
        raise ValueError(
            f"datetime MUST be timezone-aware; got naive {value!r}"
        )
    return dt


def to_json(report: EvaluationReport) -> str:
    """Serialise a report to a JSON string.

    Convenience wrapper over `to_dict` + json.dumps. Uses
    `sort_keys=True` for deterministic output (relevant for
    digest computation in P3.2.E).
    """
    return dumps(report.to_dict(), sort_keys=True)


def from_json(text: str) -> EvaluationReport:
    """Deserialise a report from a JSON string.

    Convenience wrapper over json.loads + `from_dict`.
    """
    data = loads(text)
    if not isinstance(data, dict):
        raise ValueError(
            f"report MUST be a JSON object; got {type(data).__name__}"
        )
    return EvaluationReport.from_dict(data)


__all__ = [
    "REPORT_SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "RawTextPayloadError",
    "MetricValue",
    "ThresholdDecision",
    "FixtureResult",
    "EvaluationReport",
    "to_json",
    "from_json",
]