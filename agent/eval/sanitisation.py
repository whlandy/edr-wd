"""
sanitisation.py — P3.2.D sanitisation kinds taxonomy (D21).

Implements P3.2 design gate contract **D21** (Sanitised
expected/observed payload taxonomy), deferred from
P3.2.B per round 2 review.

This module is INTENTIONALLY SEPARATE from
`agent/eval/reports.py` and from
`agent/eval/sanitisation_audit.py`:

    reports.py             — structural schema only
                            (no D21 taxonomy in P3.2.B round 2)
    sanitisation.py (here) — D21 kind taxonomy + builder
                            helpers (P3.2.D)
    sanitisation_audit.py  — pattern-based content audit
                            (P3.2.B round 2, separate)

Layer boundary (per round 2 review):

    * This module does NOT modify the report schema.
      `FixtureResult.expected` / `.observed` remain
      plain `Mapping[str, Any]`.
    * This module does NOT scan content. The audit
      layer (`sanitisation_audit.py`) owns pattern
      detection.
    * This module ONLY defines the kind taxonomy and
      builder/validator helpers. Callers (the runner
      P3.2.E, the CI gate P3.2.F) construct payloads
      via these builders to ensure kind validation
      and structured shape.

D21 contract items:

    * Structured expectation identifier — a fixed kind
      from `ALL_EXPECTATION_KINDS` plus a structured
      `value` payload (Mapping of primitives or
      structured values; no raw strings/screenshots/
      prompts).
    * Sanitised observed outcome summary — a fixed
      kind from `ALL_OBSERVED_KINDS` plus a structured
      `summary` payload.
    * kinds are the single source of truth for the
      taxonomy. Adding a new kind is a code change to
      the `ALL_*_KINDS` set and a code change to the
      consumers — there is no runtime extension.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import (
    Any,
    FrozenSet,
    Mapping,
    Optional,
)


# ---------------------------------------------------------------------------
# D21 kind taxonomy
# ---------------------------------------------------------------------------

# Expectation kinds — what the runner INTENDS the
# fixture to demonstrate.
EXPECTATION_KIND_THRESHOLD: str = "threshold"
"""The fixture is expected to meet a value threshold.
   The `value` payload MUST contain at minimum a
   `metric_id` (str) and `threshold` (float)."""

EXPECTATION_KIND_STAGE_REACHED: str = "stage_reached"
"""The fixture is expected to reach a particular stage
   in the execution. The `value` payload MUST contain
   `stage` (str)."""

EXPECTATION_KIND_MATCH_TARGET: str = "match_target"
"""The fixture is expected to match a specific target
   (e.g., a particular UI element). The `value` payload
   MUST contain `target_id` (str)."""

ALL_EXPECTATION_KINDS: FrozenSet[str] = frozenset(
    {
        EXPECTATION_KIND_THRESHOLD,
        EXPECTATION_KIND_STAGE_REACHED,
        EXPECTATION_KIND_MATCH_TARGET,
    }
)

# Observed kinds — what the runner OBSERVED.
OBSERVED_KIND_VALUE: str = "value"
"""A value was observed. The `summary` payload MUST
   contain `metric_id` (str) and `value` (float)."""

OBSERVED_KIND_MISMATCH: str = "mismatch"
"""An observed value did not match the expectation.
   The `summary` payload MUST contain `metric_id` (str),
   `expected` (float), `observed` (float)."""

OBSERVED_KIND_STAGE_REACHED: str = "stage_reached"
"""A stage was reached. The `summary` payload MUST
   contain `stage` (str)."""

OBSERVED_KIND_STAGE_MISSED: str = "stage_missed"
"""A stage was not reached. The `summary` payload MUST
   contain `stage` (str) and `reached_stage` (str) or
   similar context."""

OBSERVED_KIND_ABORTED: str = "aborted"
"""Execution was aborted before completion. The
   `summary` payload MUST contain `reason` (str)."""

OBSERVED_KIND_ERRORED: str = "errored"
"""Execution errored. The `summary` payload MUST
   contain `error_class` (str)."""

ALL_OBSERVED_KINDS: FrozenSet[str] = frozenset(
    {
        OBSERVED_KIND_VALUE,
        OBSERVED_KIND_MISMATCH,
        OBSERVED_KIND_STAGE_REACHED,
        OBSERVED_KIND_STAGE_MISSED,
        OBSERVED_KIND_ABORTED,
        OBSERVED_KIND_ERRORED,
    }
)


# ---------------------------------------------------------------------------
# D21 structured payload types
# ---------------------------------------------------------------------------


class SanitisationKindError(ValueError):
    """Raised when a kind is not in the allowed set."""


@dataclass(frozen=True)
class StructuredExpectation:
    """D21 structured expectation identifier.

    Replaces free-form `expected` payloads (raw strings).
    The `kind` MUST be in `ALL_EXPECTATION_KINDS`. The
    `value` payload is a `Mapping[str, Any]`; the kind
    defines the required keys (documented per kind above).
    """

    kind: str
    value: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.kind not in ALL_EXPECTATION_KINDS:
            raise SanitisationKindError(
                f"StructuredExpectation.kind MUST be one of "
                f"{sorted(ALL_EXPECTATION_KINDS)}; got {self.kind!r}"
            )
        if not isinstance(self.value, Mapping):
            # Reject raw strings, lists, etc.
            raise SanitisationKindError(
                f"StructuredExpectation.value MUST be a Mapping; "
                f"got {type(self.value).__name__}. Per D21, raw "
                f"strings are forbidden at the payload level."
            )

    def to_mapping(self) -> dict[str, Any]:
        """Convert to a plain dict suitable for use as
        `FixtureResult.expected`."""
        return {
            "_d21_kind": self.kind,
            "_d21_value": dict(self.value),
        }


@dataclass(frozen=True)
class SanitisedObserved:
    """D21 sanitised observed outcome summary.

    Replaces free-form `observed` payloads (raw strings).
    The `kind` MUST be in `ALL_OBSERVED_KINDS`. The
    `summary` payload is a `Mapping[str, Any]`; the
    kind defines the required keys (documented per
    kind above).
    """

    kind: str
    summary: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.kind not in ALL_OBSERVED_KINDS:
            raise SanitisationKindError(
                f"SanitisedObserved.kind MUST be one of "
                f"{sorted(ALL_OBSERVED_KINDS)}; got {self.kind!r}"
            )
        if not isinstance(self.summary, Mapping):
            raise SanitisationKindError(
                f"SanitisedObserved.summary MUST be a Mapping; "
                f"got {type(self.summary).__name__}. Per D21, raw "
                f"strings are forbidden at the payload level."
            )

    def to_mapping(self) -> dict[str, Any]:
        """Convert to a plain dict suitable for use as
        `FixtureResult.observed`."""
        return {
            "_d21_kind": self.kind,
            "_d21_summary": dict(self.summary),
        }


# ---------------------------------------------------------------------------
# D21 builders + extractors
# ---------------------------------------------------------------------------


def make_expectation(
    kind: str, value: Optional[Mapping[str, Any]] = None
) -> StructuredExpectation:
    """Builder: create a `StructuredExpectation` with
    validated kind.

    `value` defaults to an empty mapping if not provided.
    """
    return StructuredExpectation(
        kind=kind, value=value if value is not None else {}
    )


def make_observed(
    kind: str, summary: Optional[Mapping[str, Any]] = None
) -> SanitisedObserved:
    """Builder: create a `SanitisedObserved` with
    validated kind.

    `summary` defaults to an empty mapping if not
    provided.
    """
    return SanitisedObserved(
        kind=kind, summary=summary if summary is not None else {}
    )


def check_expectation_kind(kind: str) -> None:
    """Validate a kind string against `ALL_EXPECTATION_KINDS`.

    Useful when reading a kind from a raw `expected`
    Mapping (e.g., from JSON).
    """
    if kind not in ALL_EXPECTATION_KINDS:
        raise SanitisationKindError(
            f"expectation kind MUST be one of "
            f"{sorted(ALL_EXPECTATION_KINDS)}; got {kind!r}"
        )


def check_observed_kind(kind: str) -> None:
    """Validate a kind string against `ALL_OBSERVED_KINDS`."""
    if kind not in ALL_OBSERVED_KINDS:
        raise SanitisationKindError(
            f"observed kind MUST be one of "
            f"{sorted(ALL_OBSERVED_KINDS)}; got {kind!r}"
        )


# ---------------------------------------------------------------------------
# D21 taxonomy cross-checks (used by CI gate, runner, etc.)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _TaxonomyCheck:
    expectation_kinds_count: int = field(
        default_factory=lambda: len(ALL_EXPECTATION_KINDS)
    )
    observed_kinds_count: int = field(
        default_factory=lambda: len(ALL_OBSERVED_KINDS)
    )


def taxonomy_summary() -> dict[str, int]:
    """Return the count of kinds in each taxonomy. Used
    by tests and the CI gate to confirm the taxonomy is
    loaded correctly."""
    return {
        "expectation_kinds": len(ALL_EXPECTATION_KINDS),
        "observed_kinds": len(ALL_OBSERVED_KINDS),
    }


__all__ = [
    # Expectation kinds.
    "EXPECTATION_KIND_THRESHOLD",
    "EXPECTATION_KIND_STAGE_REACHED",
    "EXPECTATION_KIND_MATCH_TARGET",
    "ALL_EXPECTATION_KINDS",
    # Observed kinds.
    "OBSERVED_KIND_VALUE",
    "OBSERVED_KIND_MISMATCH",
    "OBSERVED_KIND_STAGE_REACHED",
    "OBSERVED_KIND_STAGE_MISSED",
    "OBSERVED_KIND_ABORTED",
    "OBSERVED_KIND_ERRORED",
    "ALL_OBSERVED_KINDS",
    # Structured types.
    "StructuredExpectation",
    "SanitisedObserved",
    "SanitisationKindError",
    # Builders.
    "make_expectation",
    "make_observed",
    "check_expectation_kind",
    "check_observed_kind",
    # Cross-checks.
    "taxonomy_summary",
]