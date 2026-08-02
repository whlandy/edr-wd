"""
state_machine.py — Per-step state machine (architecture §11).

States are listed in the P1.2 requirements doc:

    CREATED → VALIDATING → OBSERVING → PREPARING_STEP →
    CAPTURING_BEFORE (policy-dependent) → EXECUTING →
    OBSERVING_AFTER → CAPTURING_AFTER → ASSERTING →
    RECORDING → (loop or RECOVERING or FINALIZING) →
    COMPLETED | ABORTED.

This module exposes:

    * `StepState` enum.
    * `TRANSITIONS`: the legal forward-only transition map. Anything not in
      the map is rejected by `validate_transition()`.
    * `is_terminal(state)`: True for COMPLETED / ABORTED.
    * `next_state(state, action)`: returns the next state given a tagged
      event. Raises `IllegalTransition` if the move is not allowed.

Recovery and replan states (RECOVERING, REPLAN_PROPOSED) are stubs that
exist as enum members so P2.2 can hook in without renaming; their
behaviour in P1.2 is "no move past FINALIZING".

P1.2 reviews emphasised: "no undocumented transitions". This module is
the single source of truth for those rules — everything else either
calls `validate_transition()` or is broken by design.
"""

from __future__ import annotations

from enum import Enum


class StepState(str, Enum):
    CREATED = "created"
    VALIDATING = "validating"
    OBSERVING = "observing"
    PREPARING_STEP = "preparing_step"
    CAPTURING_BEFORE = "capturing_before"
    EXECUTING = "executing"
    OBSERVING_AFTER = "observing_after"
    CAPTURING_AFTER = "capturing_after"
    ASSERTING = "asserting"
    RECORDING = "recording"
    # Hooks reserved for P2.2; not used in P1.2 runs.
    RECOVERING = "recovering"
    REPLAN_PROPOSED = "replan_proposed"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    ABORTED = "aborted"


_TERMINAL_STATES: frozenset[StepState] = frozenset(
    {StepState.COMPLETED, StepState.ABORTED}
)


# Legal forward transitions.
#
# `OBSERVING` -> `OBSERVING` is allowed because the per-step algorithm
# permits a refresh retry when the first observation does not match
# the step's expectations (architecture §11 step 11 -> 11 fallback).
#
# `OBSERVING_AFTER` -> `EXECUTING` is the documented retry path: a
# mutating step that did not produce the expected transition may be
# retried. This is bounded by `ExecutorConfig.retry_max`.
TRANSITIONS: dict[StepState, frozenset[StepState]] = {
    StepState.CREATED: frozenset({StepState.VALIDATING}),
    StepState.VALIDATING: frozenset({
        StepState.OBSERVING,
        StepState.ABORTED,
    }),
    StepState.OBSERVING: frozenset({
        StepState.OBSERVING,
        StepState.PREPARING_STEP,
        StepState.ABORTED,
    }),
    StepState.PREPARING_STEP: frozenset({
        StepState.CAPTURING_BEFORE,
        StepState.EXECUTING,  # capture is policy-dependent; skip if off
        StepState.ABORTED,
    }),
    StepState.CAPTURING_BEFORE: frozenset({
        StepState.EXECUTING,
        StepState.ABORTED,
    }),
    StepState.EXECUTING: frozenset({
        StepState.OBSERVING_AFTER,
        StepState.ABORTED,
    }),
    StepState.OBSERVING_AFTER: frozenset({
        StepState.OBSERVING_AFTER,  # refresh retry
        StepState.CAPTURING_AFTER, # screenshot policy on
        StepState.ASSERTING,        # screenshot policy off
        StepState.EXECUTING,        # bounded retry
        StepState.ABORTED,
    }),
    StepState.CAPTURING_AFTER: frozenset({
        StepState.ASSERTING,
        StepState.ABORTED,
    }),
    StepState.ASSERTING: frozenset({
        StepState.RECORDING,
        StepState.ABORTED,
    }),
    StepState.RECORDING: frozenset({
        StepState.FINALIZING,
        StepState.ABORTED,
    }),
    StepState.RECOVERING: frozenset({
        StepState.ASSERTING,
        StepState.REPLAN_PROPOSED,
        StepState.ABORTED,
    }),
    StepState.REPLAN_PROPOSED: frozenset({
        StepState.FINALIZING,
        StepState.ABORTED,
    }),
    StepState.FINALIZING: frozenset({
        StepState.COMPLETED,
        StepState.ABORTED,
    }),
    StepState.COMPLETED: frozenset(),
    StepState.ABORTED: frozenset(),
}


class IllegalTransition(Exception):
    """Raised when the executor tries an undocumented move."""

    def __init__(self, current: StepState, attempted: StepState) -> None:
        super().__init__(
            f"illegal transition: {current.value} -> {attempted.value}"
        )
        self.current = current
        self.attempted = attempted


def is_terminal(state: StepState) -> bool:
    return state in _TERMINAL_STATES


def allowed_next(state: StepState) -> frozenset[StepState]:
    return TRANSITIONS[state]


def validate_transition(current: StepState, attempted: StepState) -> None:
    if attempted not in TRANSITIONS[current]:
        raise IllegalTransition(current, attempted)


def next_state(current: StepState, attempted: StepState) -> StepState:
    validate_transition(current, attempted)
    return attempted


__all__ = [
    "StepState",
    "TRANSITIONS",
    "IllegalTransition",
    "is_terminal",
    "allowed_next",
    "validate_transition",
    "next_state",
]