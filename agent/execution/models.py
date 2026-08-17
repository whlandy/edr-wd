"""
models.py — P1.2 executor data classes.

Layering:

    target/protocol_models  — TestCase / AtomicTestStep / Expectation (P0.2)
    target/action_dispatcher — ActionReceipt + dispatch() (P1.1)
    agent/execution         — StepResult / ExpectationResult / CaseRunResult
                              + ExecutorConfig (THIS FILE, P1.2)

This module owns only executor-local results and configuration.
P1.3 will consume StepResult/ExpectationResult as projection inputs.

Architecture:
    docs/architecture/01-action-trace-test-report-design.md
        §9.4 case + step outcomes
        §11 per-step algorithm
        FR-P1.2-* (docs/requirements/P1-execution-evidence-mvp.md)
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from target.action_dispatcher import ActionReceipt


# ---------------------------------------------------------------------------
# Status enums (architecture §9.4)
# ---------------------------------------------------------------------------


class StepStatus(str, Enum):
    """Per-step outcome. Precedence (high -> low): failed, blocked, skipped, passed.

    Architecture §9.4 lists `pending` and `running` too — those are transient
    and never appear on a stored StepResult (they live in the state machine,
    not in projections)."""

    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


# Precedence table for case-level aggregation.
STEP_STATUS_PRECEDENCE: tuple[StepStatus, ...] = (
    StepStatus.FAILED,
    StepStatus.BLOCKED,
    StepStatus.SKIPPED,
    StepStatus.PASSED,
)


def worst_status(*statuses: StepStatus) -> StepStatus:
    """Return the higher-precedence status among the inputs.

    With zero inputs (shouldn't happen for valid StepResult lists),
    fall back to PASSED defensively.
    """
    if not statuses:
        return StepStatus.PASSED
    candidates = list(statuses)
    for higher in STEP_STATUS_PRECEDENCE:
        if higher in candidates:
            return higher
    return StepStatus.PASSED


class CaseOutcome(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


# ---------------------------------------------------------------------------
# OnError policy enum (architecture §11, FR-P1.2-02, -05, -09)
# ---------------------------------------------------------------------------


class OnErrorPolicy(str, Enum):
    ABORT = "abort"
    RETRY = "retry"
    CAPTURE_AND_ABORT = "capture_and_abort"
    # P2.2 hook (no behavior in P1.2):
    REOBSERVE_REPLAN = "reobserve_replan"


# Map lowercase wire strings -> enum.
def coerce_on_error(value: Any) -> OnErrorPolicy:
    if isinstance(value, OnErrorPolicy):
        return value
    if isinstance(value, str):
        try:
            return OnErrorPolicy(value.lower())
        except ValueError as exc:
            raise ValueError(
                f"unsupported on_error policy {value!r}; "
                f"allowed: {[p.value for p in OnErrorPolicy]}"
            ) from exc
    raise TypeError(
        f"on_error must be str or OnErrorPolicy, got {type(value).__name__}"
    )


# ---------------------------------------------------------------------------
# Executor configuration (FR-P1.2-02: per-step bound + timeout)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutorConfig:
    """All knobs that govern executor behaviour.

    Defaults follow the requirements doc: `retry` bound = 2, per-step
    timeout = 30s. Per-case timeout = 120s."""

    retry_max: int = 2
    step_timeout_seconds: float = 30.0
    case_timeout_seconds: float = 120.0
    visual_evidence_available: bool = False  # FR-P1.2-07 — flips in P1.4

    def with_overrides(self, **overrides: Any) -> "ExecutorConfig":
        return dataclasses.replace(self, **overrides)


# ---------------------------------------------------------------------------
# ExpectationResult (architecture §9.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExpectationResult:
    expectation_type: str
    status: StepStatus  # always one of passed | failed (no blocked for evaluators)
    expected: Any = None
    actual: Any = None
    observation_id: str | None = None
    duration_ms: int = 0
    diagnostic: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.expectation_type,
            "status": self.status.value,
            "expected": self.expected,
            "actual": self.actual,
            "observation_id": self.observation_id,
            "duration_ms": self.duration_ms,
            "diagnostic": self.diagnostic,
        }


# ---------------------------------------------------------------------------
# StepResult (architecture §9.4, FR-P1.2-*)
# ---------------------------------------------------------------------------


@dataclass
class StepResult:
    """One executor-produced step outcome.

    P1.3 will add `evidence_refs`, `checkpoint_ref`, `branch_ref` (those
    are no-op stubs in P1.2 since P1.4 evidence and P2.1 checkpoints are
    deferred)."""

    step_id: str
    step_no: int
    status: StepStatus
    started_at: str  # ISO-8601
    ended_at: str  # ISO-8601
    duration_ms: int
    action: ActionReceipt | None = None
    expectation_results: list[ExpectationResult] = field(default_factory=list)
    error: dict[str, Any] | None = None  # stable code + message for blocked
    transition_expected: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "step_no": self.step_no,
            "status": self.status.value,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": self.duration_ms,
            "action": (
                dataclasses.asdict(self.action) if self.action is not None else None
            ),
            "expectation_results": [er.to_dict() for er in self.expectation_results],
            "error": self.error,
            "transition_expected": self.transition_expected,
        }


# ---------------------------------------------------------------------------
# CaseRunResult (architecture §9.4)
# ---------------------------------------------------------------------------


@dataclass
class CaseRunResult:
    case_id: str
    outcome: CaseOutcome
    started_at: str
    ended_at: str
    duration_ms: int
    step_results: list[StepResult] = field(default_factory=list)
    aborted: bool = False
    abort_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "outcome": self.outcome.value,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": self.duration_ms,
            "step_results": [s.to_dict() for s in self.step_results],
            "aborted": self.aborted,
            "abort_reason": self.abort_reason,
        }


__all__ = [
    "StepStatus",
    "CaseOutcome",
    "OnErrorPolicy",
    "STEP_STATUS_PRECEDENCE",
    "worst_status",
    "coerce_on_error",
    "ExecutorConfig",
    "ExpectationResult",
    "StepResult",
    "CaseRunResult",
]
