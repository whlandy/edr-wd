"""
thresholds.py — P3.2.D threshold declarations (D20).

Implements P3.2 design gate contract **D20** (Threshold
policy):

    Threshold declarations are a STATIC POLICY layer that
    tells the CI gate (P3.2.F) how to interpret metric
    values. The declaration's `state` is one of:

      * THRESHOLDED — metric participates in the gate.
        A pass/fail decision is computed from the
        observed value, the declared threshold, and
        the metric's direction (higher/lower is better).

      * UNGATED — metric is recorded but does NOT
        participate in the gate. The decision is
        `ungated`, never `pass` or `fail`.

      * MISSING DECLARATION — the metric is registered
        (in P3.2.C) but has no entry here. Per D20,
        missing declaration is an automatic FAIL — this
        prevents silent regressions on newly added
        metrics.

Layer boundary (per round 2 review of P3.2.B / P3.2.C):

    * thresholds.py is a POLICY layer. It does NOT
      know about reports.py's schema fields, sanitisation
      kinds, or runner mechanics.
    * thresholds.py does NOT register metrics. Metric
      lifecycle is owned by P3.2.C.
    * thresholds.py does NOT raise on missing declaration.
      The missing-declaration → fail mapping is the
      CI gate's (P3.2.F) job. We expose `None` from
      `get()` so the gate can decide.
    * The threshold evaluator below is a PURE FUNCTION:
      given (declaration, observed, direction) it returns
      a `ThresholdDecision`. The runner (P3.2.E) and the
      CI gate (P3.2.F) consume this output.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import (
    FrozenSet,
    Mapping,
    Optional,
)

from agent.eval.reports import ThresholdDecision


# ---------------------------------------------------------------------------
# D20 state taxonomy
# ---------------------------------------------------------------------------

# Three-state threshold policy (round 2 fix to D20).
# The round 1 "missing threshold = fail" was too rigid
# and blocked the introduction of new metrics. Round 2
# introduced the three-state model:
#   thresholded  → gate participates
#   ungated      → recorded, not gated
#   missing      → fail (the CI gate enforces this)

THRESHOLD_STATE_THRESHOLDED: str = "thresholded"
THRESHOLD_STATE_UNGATED: str = "ungated"
# Note: MISSING is represented by `None` from
# `ThresholdRegistry.get()`, NOT by a string constant.
# This avoids an enum-style "ghost state" object.

ALL_THRESHOLD_STATES: FrozenSet[str] = frozenset(
    {
        THRESHOLD_STATE_THRESHOLDED,
        THRESHOLD_STATE_UNGATED,
    }
)

# Decision outcomes produced by the evaluator.
# Note that MISSING_DECLARATION → 'fail' is decided by
# the CI gate (P3.2.F), not by the evaluator.
THRESHOLD_DECISION_PASS: str = "pass"
THRESHOLD_DECISION_FAIL: str = "fail"
THRESHOLD_DECISION_UNGATED: str = "ungated"
THRESHOLD_DECISION_MISSING: str = "missing"  # marker used by the gate

ALL_THRESHOLD_DECISIONS: FrozenSet[str] = frozenset(
    {
        THRESHOLD_DECISION_PASS,
        THRESHOLD_DECISION_FAIL,
        THRESHOLD_DECISION_UNGATED,
        THRESHOLD_DECISION_MISSING,
    }
)

# Direction is shared with metrics.py. Re-declared here
# as strings so thresholds.py does not import private
# constants from metrics.py. The gate (P3.2.F) is
# responsible for cross-checking declaration direction
# against the registered metric's direction.
THRESHOLD_DIRECTION_HIGHER_IS_BETTER: str = "higher_is_better"
THRESHOLD_DIRECTION_LOWER_IS_BETTER: str = "lower_is_better"

ALL_THRESHOLD_DIRECTIONS: FrozenSet[str] = frozenset(
    {
        THRESHOLD_DIRECTION_HIGHER_IS_BETTER,
        THRESHOLD_DIRECTION_LOWER_IS_BETTER,
    }
)


# ---------------------------------------------------------------------------
# D20 declaration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThresholdDeclaration:
    """Static policy entry for a single metric.

    Per D20 round 2:

      * `state == THRESHOLDED`: threshold + direction are
        required. Pass/fail is computed by
        `evaluate_threshold`.
      * `state == UNGATED`: threshold / direction MUST
        be None. The decision is always `ungated`.
      * Missing declaration is represented by `None`
        from `ThresholdRegistry.get()` — there is no
        "missing declaration" object. The CI gate (P3.2.F)
        is responsible for converting `None` into a
        FAIL decision.

    The `comparison` is derived from `direction` (not
    stored separately):
      * higher_is_better → pass if observed >= threshold
      * lower_is_better  → pass if observed <= threshold
    """

    metric_id: str
    state: str
    threshold: Optional[float] = None
    direction: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.metric_id:
            raise ValueError("ThresholdDeclaration.metric_id MUST be non-empty")
        if self.state not in ALL_THRESHOLD_STATES:
            raise ValueError(
                f"ThresholdDeclaration.state MUST be one of "
                f"{sorted(ALL_THRESHOLD_STATES)}; got {self.state!r}"
            )
        if self.state == THRESHOLD_STATE_THRESHOLDED:
            if self.threshold is None:
                raise ValueError(
                    f"thresholded declaration for {self.metric_id!r} "
                    f"MUST have a threshold value"
                )
            if self.direction not in ALL_THRESHOLD_DIRECTIONS:
                raise ValueError(
                    f"thresholded declaration for {self.metric_id!r} "
                    f"MUST have direction ∈ "
                    f"{sorted(ALL_THRESHOLD_DIRECTIONS)}; "
                    f"got {self.direction!r}"
                )
        else:  # UNGATED
            if self.threshold is not None:
                raise ValueError(
                    f"ungated declaration for {self.metric_id!r} "
                    f"MUST NOT have a threshold value; "
                    f"got threshold={self.threshold}"
                )
            if self.direction is not None:
                raise ValueError(
                    f"ungated declaration for {self.metric_id!r} "
                    f"MUST NOT have a direction; "
                    f"got direction={self.direction!r}"
                )

    @property
    def fingerprint(self) -> str:
        """Stable SHA-256 hex of the semantically
        meaningful fields.

        Used by the registry to detect metric_id reuse
        with different policy (D20 mutation policy).
        """
        return _declaration_fingerprint(self)


def _declaration_fingerprint(decl: ThresholdDeclaration) -> str:
    payload = {
        "metric_id": decl.metric_id,
        "state": decl.state,
        "threshold": decl.threshold,
        "direction": decl.direction,
    }
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# ---------------------------------------------------------------------------
# D20 registry
# ---------------------------------------------------------------------------


class ThresholdRegistryError(ValueError):
    """Base error for threshold registry violations."""


class ThresholdReusedIdError(ThresholdRegistryError):
    """Raised when a metric_id is registered with a
    different declaration fingerprint. Per D20 the
    threshold policy for a given metric_id MUST be
    stable across the evaluation run."""

    def __init__(
        self,
        metric_id: str,
        existing_fingerprint: str,
        new_fingerprint: str,
    ) -> None:
        super().__init__(
            f"metric_id {metric_id!r} is already declared with a "
            f"different policy (existing fingerprint "
            f"{existing_fingerprint[:16]}..., new fingerprint "
            f"{new_fingerprint[:16]}...); per D20, threshold "
            f"declarations MUST NOT be reused with different "
            f"semantics"
        )
        self.metric_id = metric_id
        self.existing_fingerprint = existing_fingerprint
        self.new_fingerprint = new_fingerprint


@dataclass(frozen=True)
class _Registration:
    fingerprint: str
    declaration: ThresholdDeclaration


@dataclass(frozen=True)
class ThresholdRegistry:
    """Static threshold policy registry.

    Per D20, the registry holds the policy for the
    evaluation run. It is a frozen in-memory map keyed
    by metric_id. Missing entries (`get()` returns None)
    are NOT represented here — they are the CI gate's
    signal to FAIL (per D20 round 2: "missing declaration
    → fail").

    Layer boundary (per round 2 review):
      * Pure policy layer. No I/O, no runner mechanics.
      * Mutation policy: same metric_id + different
        fingerprint → reject.
      * Re-registration with same fingerprint → no-op
        (allows plugins to re-declare safely).
    """

    _entries: dict[str, _Registration] = field(default_factory=dict)

    def declare(self, declaration: ThresholdDeclaration) -> None:
        """Register a threshold declaration.

        Idempotent if the existing entry has the same
        fingerprint. Rejects if a different fingerprint
        is provided for an existing metric_id.
        """
        fp = declaration.fingerprint
        existing = self._entries.get(declaration.metric_id)
        if existing is not None:
            if existing.fingerprint != fp:
                raise ThresholdReusedIdError(
                    declaration.metric_id,
                    existing.fingerprint,
                    fp,
                )
            # Same fingerprint: no-op.
            return
        self._entries[declaration.metric_id] = _Registration(
            fingerprint=fp, declaration=declaration
        )

    def get(
        self, metric_id: str
    ) -> Optional[ThresholdDeclaration]:
        """Return the declaration for a metric_id, or
        None if missing.

        The caller (CI gate) is responsible for
        converting None into a FAIL decision per D20.
        """
        entry = self._entries.get(metric_id)
        if entry is None:
            return None
        return entry.declaration

    def has_declaration(self, metric_id: str) -> bool:
        return metric_id in self._entries

    def declared_metric_ids(self) -> FrozenSet[str]:
        """Return the set of metric_ids with a declaration
        (regardless of state)."""
        return frozenset(self._entries.keys())

    def thresholded_metric_ids(self) -> FrozenSet[str]:
        """Return the subset declared as THRESHOLDED."""
        return frozenset(
            decl.metric_id
            for decl in (
                entry.declaration for entry in self._entries.values()
            )
            if decl.state == THRESHOLD_STATE_THRESHOLDED
        )

    def ungated_metric_ids(self) -> FrozenSet[str]:
        """Return the subset declared as UNGATED."""
        return frozenset(
            decl.metric_id
            for decl in (
                entry.declaration for entry in self._entries.values()
            )
            if decl.state == THRESHOLD_STATE_UNGATED
        )

    def all_declarations(self) -> tuple[ThresholdDeclaration, ...]:
        return tuple(
            entry.declaration for entry in self._entries.values()
        )

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, metric_id: object) -> bool:
        return isinstance(metric_id, str) and metric_id in self._entries

    def __iter__(self):
        return iter(self._entries)


# ---------------------------------------------------------------------------
# D20 evaluator — pure function
# ---------------------------------------------------------------------------


def evaluate_threshold(
    metric_id: str,
    observed: float,
    declaration: Optional[ThresholdDeclaration],
) -> ThresholdDecision:
    """Pure function: compute a `ThresholdDecision` from
    a declaration and an observed value.

    The decision matrix:

      declaration.state == THRESHOLDED  → 'pass' or 'fail'
        higher_is_better: pass iff observed >= threshold
        lower_is_better:  pass iff observed <= threshold

      declaration.state == UNGATED      → 'ungated'

      declaration is None (MISSING)     → 'fail' (per D20
        round 2: missing declaration → fail. This decision
        is recorded here so the runner can produce a
        consistent ThresholdDecision object even when
        the registry does not cover the metric.)

    Boundary: the evaluator does NOT decide pass/fail
    across the entire evaluation. It produces one
    `ThresholdDecision` per metric. The CI gate (P3.2.F)
    aggregates these into an overall pass/fail.
    """
    if declaration is None:
        # Missing declaration → fail (D20 round 2).
        return ThresholdDecision(
            metric_id=metric_id,
            thresholded=False,
            decision=THRESHOLD_DECISION_FAIL,
            observed=observed,
            threshold=None,
        )
    if declaration.state == THRESHOLD_STATE_UNGATED:
        return ThresholdDecision(
            metric_id=metric_id,
            thresholded=False,
            decision=THRESHOLD_DECISION_UNGATED,
            observed=observed,
            threshold=None,
        )
    # THRESHOLDED
    assert declaration.threshold is not None
    assert declaration.direction in ALL_THRESHOLD_DIRECTIONS
    if declaration.direction == THRESHOLD_DIRECTION_HIGHER_IS_BETTER:
        passed = observed >= declaration.threshold
    else:  # lower_is_better
        passed = observed <= declaration.threshold
    return ThresholdDecision(
        metric_id=metric_id,
        thresholded=True,
        decision=(
            THRESHOLD_DECISION_PASS
            if passed
            else THRESHOLD_DECISION_FAIL
        ),
        observed=observed,
        threshold=declaration.threshold,
    )


def evaluate_all_thresholds(
    metric_values: Mapping[str, float],
    registry: ThresholdRegistry,
) -> tuple[ThresholdDecision, ...]:
    """Evaluate every metric in `metric_values` against
    the threshold registry.

    For each metric_id:

      * declared (any state) → use the declaration
      * not declared           → MISSING → fail decision
    """
    decisions: list[ThresholdDecision] = []
    for metric_id, observed in metric_values.items():
        declaration = registry.get(metric_id)
        decisions.append(
            evaluate_threshold(metric_id, observed, declaration)
        )
    return tuple(decisions)


def is_evaluation_passing(
    decisions: tuple[ThresholdDecision, ...],
) -> bool:
    """Aggregate pass/fail across a list of decisions.

    Per D20:
      * `pass` and `ungated` do NOT cause a fail.
      * `fail` (including missing-declaration → fail)
        causes the whole evaluation to fail.

    This is the simplest possible aggregation; the CI
    gate (P3.2.F) may add more sophisticated policies
    (e.g., critical vs informational metrics) on top.
    """
    for decision in decisions:
        if decision.decision == THRESHOLD_DECISION_FAIL:
            return False
    return True


def collect_failures(
    decisions: tuple[ThresholdDecision, ...],
) -> tuple[ThresholdDecision, ...]:
    """Return the subset of decisions that failed.

    Useful for surfacing actionable failure details in
    CI logs (per D21 sanitisation contract: no raw UI
    text in failure summaries).
    """
    return tuple(
        d for d in decisions if d.decision == THRESHOLD_DECISION_FAIL
    )


def collect_ungated(
    decisions: tuple[ThresholdDecision, ...],
) -> tuple[ThresholdDecision, ...]:
    """Return the subset of ungated decisions."""
    return tuple(
        d
        for d in decisions
        if d.decision == THRESHOLD_DECISION_UNGATED
    )


__all__ = [
    # State taxonomy.
    "THRESHOLD_STATE_THRESHOLDED",
    "THRESHOLD_STATE_UNGATED",
    "ALL_THRESHOLD_STATES",
    # Decision taxonomy.
    "THRESHOLD_DECISION_PASS",
    "THRESHOLD_DECISION_FAIL",
    "THRESHOLD_DECISION_UNGATED",
    "THRESHOLD_DECISION_MISSING",
    "ALL_THRESHOLD_DECISIONS",
    # Direction taxonomy.
    "THRESHOLD_DIRECTION_HIGHER_IS_BETTER",
    "THRESHOLD_DIRECTION_LOWER_IS_BETTER",
    "ALL_THRESHOLD_DIRECTIONS",
    # Declaration.
    "ThresholdDeclaration",
    # Registry.
    "ThresholdRegistry",
    "ThresholdRegistryError",
    "ThresholdReusedIdError",
    # Evaluator.
    "evaluate_threshold",
    "evaluate_all_thresholds",
    "is_evaluation_passing",
    "collect_failures",
    "collect_ungated",
]