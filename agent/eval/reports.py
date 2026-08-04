"""
reports.py — P3.2.B evaluation result schema.

Implements P3.2 design gate contract **D18** (Evaluation
result schema contract):

    * Every report is parseable from JSON.
    * Required top-level fields:
      `dataset_id`, `dataset_version`, `planner_version`,
      `execution_profile`, `started_at`, `ended_at`,
      `metrics`, `threshold_decisions`,
      `reproducibility_digest`, `schema_version`.
    * Within a `(dataset_version, planner_version)` pair,
      schema evolves additively only.
    * Initial schema is `report_schema.v1`.
    * No observed screen text / args / selectors /
      confirmation tokens / LLM prompts in reports.

Public API:

    * REPORT_SCHEMA_VERSION — schema version constant.
    * SUPPORTED_SCHEMA_VERSIONS — set of supported versions.
    * MetricValue — single metric reading.
    * ThresholdDecision — single threshold decision.
    * FixtureResult — single fixture outcome.
    * StructuredExpectation — D21 expected-payload type.
    * SanitisedObserved — D21 observed-payload type.
    * EvaluationReport — full report.
    * SensitiveContentError — raised on schema-level content
      violation.
    * validate_no_sensitive_content(report_dict) — walks a
      serialised report and rejects sensitive patterns.

This module is D18-only. The exact metric formulas (D19),
threshold declarations (D20), digest scheme (D22), and CI
output shape (D21) are introduced by their respective
implementation reviews; the placeholder dataclasses here are
the minimum needed for the schema to round-trip.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
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
# Sensitive content detection (D18 + D21)
# ---------------------------------------------------------------------------

# Maximum length for any string field in a report. Fields
# longer than this are flagged as likely observed screen text
# or LLM prompt leakage.
_MAX_STRING_LENGTH = 256

# Pattern classes that MUST NOT appear in reports. These are
# deliberately conservative — false positives err on the side
# of rejecting the report rather than emitting sensitive
# content.
_FORBIDDEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    # DOM / XPath selectors.
    re.compile(r"//[A-Za-z]+(\[@[A-Za-z]+=)?"),
    re.compile(r"/html/body/"),
    re.compile(r"document\.querySelector\("),
    # Confirmation tokens: hex/base64 strings longer than 32
    # chars that look like a random token.
    re.compile(r"\b[a-f0-9]{40,}\b", re.IGNORECASE),
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),
    # LLM prompt leakage markers.
    re.compile(r"You are an AI", re.IGNORECASE),
    re.compile(r"^system:\s", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^assistant:\s", re.IGNORECASE | re.MULTILINE),
    re.compile(r"<\|.*?\|>"),
)


class SensitiveContentError(ValueError):
    """Raised when a serialised report contains content that
    D18 forbids (observed screen text, args, selectors,
    confirmation tokens, LLM prompts).

    The error message identifies the offending path and the
    matched pattern so audit logs are actionable.
    """

    def __init__(self, message: str, *, path: str) -> None:
        super().__init__(message)
        self.path = path


def _walk_strings(
    value: Any, path: str
) -> list[tuple[str, str]]:
    """Walk a (possibly nested) JSON-safe structure and
    collect every (path, string) pair.

    Strings inside `dict` values are walked; non-string types
    are skipped. The path is a `/`-separated index path (e.g.
    `metrics/parse_success_rate/value`).
    """
    found: list[tuple[str, str]] = []
    if isinstance(value, str):
        found.append((path, value))
    elif isinstance(value, Mapping):
        for k, v in value.items():
            found.extend(_walk_strings(v, f"{path}/{k}"))
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            found.extend(_walk_strings(v, f"{path}/{i}"))
    return found


def validate_no_sensitive_content(report_dict: Mapping[str, Any]) -> None:
    """Walk a serialised report and reject sensitive
    content.

    Raises `SensitiveContentError` if any string field:
      1. Exceeds `_MAX_STRING_LENGTH` characters, OR
      2. Matches any `_FORBIDDEN_PATTERNS` pattern.

    The check is structural — it walks the entire serialised
    report dict, not just the top-level fields. The
    `_forbidden_patterns` set is conservative; legitimate
    report data that happens to contain a long hex string
    SHOULD be encoded as a digest instead (per D21).
    """
    for path, string in _walk_strings(report_dict, ""):
        if len(string) > _MAX_STRING_LENGTH:
            raise SensitiveContentError(
                f"string at {path!r} exceeds "
                f"{_MAX_STRING_LENGTH} chars "
                f"(len={len(string)}); likely observed screen "
                f"text or LLM prompt leakage",
                path=path,
            )
        for pattern in _FORBIDDEN_PATTERNS:
            if pattern.search(string):
                raise SensitiveContentError(
                    f"string at {path!r} matches forbidden "
                    f"pattern {pattern.pattern!r}; likely "
                    f"selectors / confirmation tokens / LLM "
                    f"prompt leakage",
                    path=path,
                )


# ---------------------------------------------------------------------------
# D21 sanitised payload types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StructuredExpectation:
    """Structured expectation identifier (per D21).

    `kind` is one of the fixed kinds below. The payload is
    structured (dict), not a free-form string. This is the
    ONLY allowed shape for an `expected` field.
    """

    kind: str  # one of EXPECTATION_KINDS
    value: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.kind not in EXPECTATION_KINDS:
            raise ValueError(
                f"kind MUST be one of {sorted(EXPECTATION_KINDS)}; "
                f"got {self.kind!r}"
            )


@dataclass(frozen=True)
class SanitisedObserved:
    """Sanitised structured outcome summary (per D21).

    `kind` is one of the fixed kinds below. The summary is
    structured (dict), not a free-form string. The summary
    MUST NOT include screen text, args, selectors, or raw
    confirmation tokens — only structured markers and
    digests.
    """

    kind: str  # one of OBSERVED_KINDS
    summary: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.kind not in OBSERVED_KINDS:
            raise ValueError(
                f"kind MUST be one of {sorted(OBSERVED_KINDS)}; "
                f"got {self.kind!r}"
            )


# Kinds for StructuredExpectation (per D21 sanitisation).
EXPECTATION_KINDS: FrozenSet[str] = frozenset(
    {
        "threshold",  # {"value": 0.95}
        "stage_reached",  # {"name": "validation"}
        "match_target",  # {"category": "semantic_button"}
    }
)

# Kinds for SanitisedObserved (per D21 sanitisation).
OBSERVED_KINDS: FrozenSet[str] = frozenset(
    {
        "value",  # {"value": 0.92}
        "mismatch",  # {"category": "wrong_target", "digest": "..."}
        "stage_reached",  # {"name": "validation"}
        "stage_missed",  # {"name": "validation"}
        "aborted",  # {"reason": "timeout"}
        "errored",  # {"kind": "exception"}
    }
)


# ---------------------------------------------------------------------------
# Minimal placeholder payloads (extended in P3.2.C / P3.2.D)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricValue:
    """Single metric reading.

    Minimum shape needed for the report to round-trip. The
    full registry and formula shape arrive in P3.2.C (D19).
    """

    metric_id: str
    value: float
    unit: str  # "ratio", "count", "ms", "bytes"
    direction: str  # "higher_is_better" | "lower_is_better"

    def __post_init__(self) -> None:
        if self.direction not in {"higher_is_better", "lower_is_better"}:
            raise ValueError(
                f"direction MUST be 'higher_is_better' or "
                f"'lower_is_better'; got {self.direction!r}"
            )


@dataclass(frozen=True)
class ThresholdDecision:
    """Single threshold decision (per D20).

    Minimum shape needed for the report to round-trip. The
    full registry and declaration shape arrive in P3.2.D (D20).
    """

    metric_id: str
    thresholded: bool  # False = ungated (per D20 round 2)
    decision: str  # "pass" | "fail" | "ungated"
    observed: float
    threshold: float | None  # None when ungated

    def __post_init__(self) -> None:
        if self.decision not in {"pass", "fail", "ungated"}:
            raise ValueError(
                f"decision MUST be 'pass' | 'fail' | 'ungated'; "
                f"got {self.decision!r}"
            )
        if self.thresholded and self.threshold is None:
            raise ValueError(
                "thresholded=True requires threshold; use "
                "thresholded=False for ungated metrics"
            )
        if not self.thresholded and self.threshold is not None:
            raise ValueError(
                "thresholded=False (ungated) MUST NOT carry a "
                "threshold; set threshold=None"
            )


@dataclass(frozen=True)
class FixtureResult:
    """Per-fixture outcome.

    `expected` MUST be a `StructuredExpectation`; `observed`
    MUST be a `SanitisedObserved`. Free-form strings are
    forbidden by the schema.
    """

    fixture_id: str
    status: str  # "completed" | "aborted" | "errored"
    expected: StructuredExpectation
    observed: SanitisedObserved

    def __post_init__(self) -> None:
        if self.status not in {"completed", "aborted", "errored"}:
            raise ValueError(
                f"status MUST be 'completed' | 'aborted' | "
                f"'errored'; got {self.status!r}"
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
      `reproducibility_digest`, plus `fixture_results` for
      per-fixture outcomes and `run_id` for identification.

    `started_at` and `ended_at` MUST be timezone-aware
    UTC datetimes. JSON serialisation uses ISO 8601 with a
    `+00:00` UTC suffix.

    `metrics` is keyed by `metric_id`; values are
    `MetricValue`.

    `threshold_decisions` is a tuple of `ThresholdDecision`,
    one per metric (or a subset if some metrics are ungated
    and contribute no decision).

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
    reproducibility_digest: str
    fixture_results: tuple[FixtureResult, ...]
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
        if self.started_at.tzinfo is None:
            raise ValueError("started_at MUST be timezone-aware")
        if self.ended_at.tzinfo is None:
            raise ValueError("ended_at MUST be timezone-aware")
        if self.started_at.tzinfo != timezone.utc and (
            self.started_at.utcoffset() != timezone.utc.utcoffset(None)
        ):
            raise ValueError("started_at MUST be UTC")
        if self.ended_at.tzinfo != timezone.utc and (
            self.ended_at.utcoffset() != timezone.utc.utcoffset(None)
        ):
            raise ValueError("ended_at MUST be UTC")
        if self.ended_at < self.started_at:
            raise ValueError("ended_at MUST be >= started_at")
        if not self.run_id:
            raise ValueError("run_id MUST be non-empty")

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict.

        The serialised form is `validate_no_sensitive_content`
        -safe (sanitised types only), but the validator is
        still called to catch mistakes.

        Round-trip guarantee: `EvaluationReport.from_dict(
        report.to_dict()) == report`.
        """
        payload: dict[str, Any] = {
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
            "reproducibility_digest": self.reproducibility_digest,
            "run_id": self.run_id,
            "fixture_results": [
                {
                    "fixture_id": fr.fixture_id,
                    "status": fr.status,
                    "expected": {
                        "kind": fr.expected.kind,
                        "value": dict(fr.expected.value),
                    },
                    "observed": {
                        "kind": fr.observed.kind,
                        "summary": dict(fr.observed.summary),
                    },
                }
                for fr in self.fixture_results
            ],
        }
        validate_no_sensitive_content(payload)
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EvaluationReport":
        """Deserialise from a JSON-parsed dict.

        Validates the schema version, required fields, and
        sensitive content. Raises `ValueError` on schema
        mismatch; raises `SensitiveContentError` on forbidden
        patterns.
        """
        schema_version = data.get("schema_version")
        if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(
                f"schema_version MUST be one of "
                f"{sorted(SUPPORTED_SCHEMA_VERSIONS)}; got "
                f"{schema_version!r}"
            )
        validate_no_sensitive_content(data)

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
                expected=StructuredExpectation(
                    kind=fr["expected"]["kind"],
                    value=dict(fr["expected"]["value"]),
                ),
                observed=SanitisedObserved(
                    kind=fr["observed"]["kind"],
                    summary=dict(fr["observed"]["summary"]),
                ),
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
            reproducibility_digest=data["reproducibility_digest"],
            fixture_results=fixture_results,
            run_id=data["run_id"],
        )


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
    "MetricValue",
    "ThresholdDecision",
    "FixtureResult",
    "StructuredExpectation",
    "SanitisedObserved",
    "EXPECTATION_KINDS",
    "OBSERVED_KINDS",
    "EvaluationReport",
    "SensitiveContentError",
    "validate_no_sensitive_content",
    "to_json",
    "from_json",
]