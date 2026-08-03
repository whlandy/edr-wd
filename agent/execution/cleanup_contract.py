"""
cleanup_contract.py — P2.4.B stable interface contract for cleanup result
propagation to case-attempt-manifest.json.

This module is the **frozen interface contract** between:

  * the execution runner (which runs cleanup steps), and
  * the manifest writer (which persists case-attempt-manifest.json).

Real cleanup execution (P2.5) implements against this contract, NOT
against the mock. Once P2.4.B lands, the contract is frozen; P2.5
replaces the mock implementation without changing the contract.

Contract (P2.4 design §5.5):

  * Input: `TestCase` (with `cleanup` + `cleanup_outcome_critical`).
  * Output: case-attempt-manifest.json extension with
      `cleanup_status` ∈ {`passed`, `failed`, `unknown`} and
      `cleanup_outcome_critical: bool`.
  * Contract guarantees:
      - `cleanup_status="unknown"` iff cleanup was not executed.
      - `cleanup_status="passed"` iff cleanup ran and all steps
        succeeded.
      - `cleanup_status="failed"` iff cleanup ran and at least one
        step failed.
      - `cleanup_outcome_critical` is read from `TestCase` verbatim.

The renderer reads both fields verbatim (D11) — no inference. The
manifest.json on disk is never mutated after this point (D17: cascade
is read-time only).

Out of scope (P2.4):
  * Live cleanup step execution (deferred to P2.5 — real executor
    must implement this contract).

P2.4.B ships:
  * `CleanupExecutionResult` dataclass.
  * `MockCleanupRunner` (deterministic, configurable).
  * `apply_cleanup_to_manifest()` propagation function.
  * `CleanupSkippedError` for skipped-before-cleanup semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent.trace.manifest import (
    CLEANUP_STATUS_FAILED,
    CLEANUP_STATUS_PASSED,
    CLEANUP_STATUS_UNKNOWN,
    ManifestRecord,
)

if TYPE_CHECKING:
    from target.protocol_models.models import TestCase


__all__ = [
    "CleanupExecutionResult",
    "CleanupSkippedError",
    "MockCleanupRunner",
    "apply_cleanup_to_manifest",
]


class CleanupSkippedError(Exception):
    """Raised when cleanup was not executed (case skipped/aborted)."""


@dataclass(frozen=True)
class CleanupExecutionResult:
    """The runner's runtime view of cleanup execution.

    The runner produces one of these per case attempt, regardless of
    whether cleanup actually ran. The contract guarantees:

      * `status="unknown"` iff cleanup was not executed (no cleanup
        steps, or runner aborted before cleanup).
      * `status="passed"` iff cleanup ran and all steps succeeded.
      * `status="failed"` iff cleanup ran and at least one step failed.

    `outcome_critical` is a verbatim copy of `TestCase.cleanup_outcome_critical`.
    The runner MUST NOT infer or modify this flag (D11: cleanup outcome
    data ownership).
    """

    status: str  # "passed" | "failed" | "unknown"
    outcome_critical: bool
    steps_run: int

    def __post_init__(self) -> None:
        if self.status not in (
            CLEANUP_STATUS_PASSED,
            CLEANUP_STATUS_FAILED,
            CLEANUP_STATUS_UNKNOWN,
        ):
            raise ValueError(
                f"status must be one of "
                f"{{'passed', 'failed', 'unknown'}}, got {self.status!r}"
            )
        if not isinstance(self.outcome_critical, bool):
            raise TypeError(
                f"outcome_critical must be a bool, "
                f"got {type(self.outcome_critical).__name__}"
            )
        if not isinstance(self.steps_run, int) or self.steps_run < 0:
            raise ValueError(
                f"steps_run must be a non-negative int, got {self.steps_run!r}"
            )


# Pre-defined scenarios for MockCleanupRunner.
_SCENARIO_PASS = "pass"
_SCENARIO_FAIL = "fail"
_SCENARIO_UNKNOWN = "unknown"


class MockCleanupRunner:
    """Deterministic mock implementation of the cleanup runner contract.

    P2.4.B ships this mock. P2.5 replaces it with a real cleanup
    executor that implements the same contract — same input/output,
    same guarantees.

    Modes (constructor `simulate`):

      * `"pass"` (default): cleanup ran successfully → status="passed".
      * `"fail"`: cleanup ran with at least one failure →
        status="failed".
      * `"unknown"`: cleanup was not executed → status="unknown".

    Contract behaviors:

      * If `testcase.cleanup` is empty, return status="unknown"
        regardless of `simulate` (no steps → no execution).
      * `outcome_critical` is read from `testcase.cleanup_outcome_critical`
        verbatim — the mock MUST NOT infer it.
      * `steps_run` reports how many cleanup steps actually executed.
    """

    def __init__(self, simulate: str = _SCENARIO_PASS) -> None:
        if simulate not in (_SCENARIO_PASS, _SCENARIO_FAIL, _SCENARIO_UNKNOWN):
            raise ValueError(
                f"simulate must be one of "
                f"{{'pass', 'fail', 'unknown'}}, got {simulate!r}"
            )
        self._simulate = simulate

    def run(self, testcase: "TestCase") -> CleanupExecutionResult:
        """Execute cleanup per the contract and return the result.

        Contract behaviors implemented here:

          1. If `testcase.cleanup` is empty → status="unknown",
             steps_run=0 (no steps to run).
          2. Else if `simulate="unknown"` → status="unknown",
             steps_run=0 (mock skips execution — same effect).
          3. Else → execute per `simulate` and report steps_run
             = len(testcase.cleanup).
          4. `outcome_critical` is read from
             `testcase.cleanup_outcome_critical` verbatim — never
             inferred or modified (D11).
        """
        outcome_critical = testcase.cleanup_outcome_critical

        if not testcase.cleanup:
            return CleanupExecutionResult(
                status=CLEANUP_STATUS_UNKNOWN,
                outcome_critical=outcome_critical,
                steps_run=0,
            )
        if self._simulate == _SCENARIO_UNKNOWN:
            return CleanupExecutionResult(
                status=CLEANUP_STATUS_UNKNOWN,
                outcome_critical=outcome_critical,
                steps_run=0,
            )
        if self._simulate == _SCENARIO_FAIL:
            return CleanupExecutionResult(
                status=CLEANUP_STATUS_FAILED,
                outcome_critical=outcome_critical,
                steps_run=len(testcase.cleanup),
            )
        # Default: pass.
        return CleanupExecutionResult(
            status=CLEANUP_STATUS_PASSED,
            outcome_critical=outcome_critical,
            steps_run=len(testcase.cleanup),
        )


def apply_cleanup_to_manifest(
    manifest: ManifestRecord,
    cleanup_result: CleanupExecutionResult,
) -> ManifestRecord:
    """Apply a cleanup result to a manifest, returning a new manifest.

    The runner builds a ManifestRecord from the case steps, then
    calls this function to layer in the cleanup fields. Returns a new
    ManifestRecord (the input is not mutated, since ManifestRecord is
    a frozen dataclass).

    Contract:
      * The returned manifest has:
          - cleanup_status = cleanup_result.status
          - cleanup_outcome_critical = cleanup_result.outcome_critical
      * Other fields are preserved verbatim (terminal_status,
        evidence_counts, etc.).
      * `cleanup_status="unknown"` is preserved (renderer treats as
        "do not cascade" per D13).
    """
    # ManifestRecord is frozen — `dataclasses.replace` returns a new
    # instance without mutating the original. This keeps the
    # contract's "manifest.json unchanged on disk" guarantee (D17)
    # while still layering cleanup fields into the in-memory view
    # that the manifest writer serializes.
    from dataclasses import replace as _dc_replace

    return _dc_replace(
        manifest,
        cleanup_status=cleanup_result.status,
        cleanup_outcome_critical=cleanup_result.outcome_critical,
    )