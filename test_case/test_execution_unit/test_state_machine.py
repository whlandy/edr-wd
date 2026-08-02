"""P1.2 acceptance gate — state machine transitions."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
for p in (str(_REPO / "target"), str(_REPO / "agent")):
    if p not in sys.path:
        sys.path.insert(0, p)


from execution import (  # noqa: E402
    StepState,
    IllegalTransition,
    is_terminal,
    next_state,
    validate_transition,
)


def test_initial_state_is_created():
    assert StepState.CREATED.value == "created"


def test_terminal_states_are_completed_and_aborted():
    assert is_terminal(StepState.COMPLETED)
    assert is_terminal(StepState.ABORTED)
    assert not is_terminal(StepState.EXECUTING)
    assert not is_terminal(StepState.OBSERVING)


def test_happy_path_transitions():
    """CREATED -> VALIDATING -> OBSERVING -> ... -> COMPLETED."""
    path = [
        StepState.VALIDATING,
        StepState.OBSERVING,
        StepState.PREPARING_STEP,
        StepState.CAPTURING_BEFORE,
        StepState.EXECUTING,
        StepState.OBSERVING_AFTER,
        StepState.CAPTURING_AFTER,
        StepState.ASSERTING,
        StepState.RECORDING,
        StepState.FINALIZING,
        StepState.COMPLETED,
    ]
    state = StepState.CREATED
    for target in path:
        state = next_state(state, target)
    assert state is StepState.COMPLETED


def test_abort_from_every_active_state():
    """Any state at or past VALIDATING may abort.

    CREATED -> ABORTED is illegal by design (you must at least enter
    VALIDATING first; a CREATED step that never started is just
    discarded, not "aborted").
    """
    abortable = [
        StepState.VALIDATING,
        StepState.OBSERVING,
        StepState.PREPARING_STEP,
        StepState.CAPTURING_BEFORE,
        StepState.EXECUTING,
        StepState.OBSERVING_AFTER,
        StepState.CAPTURING_AFTER,
        StepState.ASSERTING,
        StepState.RECORDING,
        StepState.RECOVERING,
        StepState.REPLAN_PROPOSED,
        StepState.FINALIZING,
    ]
    for s in abortable:
        assert next_state(s, StepState.ABORTED) is StepState.ABORTED


def test_illegal_transition_raises():
    # Cannot jump from CREATED to EXECUTING.
    with pytest.raises(IllegalTransition):
        validate_transition(StepState.CREATED, StepState.EXECUTING)
    # Cannot leave a terminal state.
    with pytest.raises(IllegalTransition):
        next_state(StepState.COMPLETED, StepState.ABORTED)


def test_observing_self_loop_allowed():
    """First observation may fail to resolve; refresh path is legal."""
    state = next_state(StepState.OBSERVING, StepState.OBSERVING)
    assert state is StepState.OBSERVING


def test_observing_after_to_executing_is_retry_path():
    """Retry path from OBSERVING_AFTER -> EXECUTING is documented."""
    state = next_state(StepState.OBSERVING_AFTER, StepState.EXECUTING)
    assert state is StepState.EXECUTING


def test_without_evidence_skips_capture_transition():
    """When the step declares no evidence (no before/after capture),
    the executor must be able to move directly from OBSERVING_AFTER
    to ASSERTING — bypassing CAPTURING_BEFORE / CAPTURING_AFTER.

    This is the documented "policy off" skip-capture path; without
    this transition the executor would have no legal move to
    ASSERTING when evidence.screenshot is unset.
    """
    # Both skip-capture paths must be legal:
    #   PREPARING_STEP -> EXECUTING         (skip CAPTURING_BEFORE)
    #   OBSERVING_AFTER -> ASSERTING        (skip CAPTURING_AFTER)
    assert next_state(StepState.PREPARING_STEP, StepState.EXECUTING) is StepState.EXECUTING
    assert next_state(StepState.OBSERVING_AFTER, StepState.ASSERTING) is StepState.ASSERTING

    # And the with-capture paths remain legal:
    assert next_state(StepState.PREPARING_STEP, StepState.CAPTURING_BEFORE) is StepState.CAPTURING_BEFORE
    assert next_state(StepState.CAPTURING_BEFORE, StepState.EXECUTING) is StepState.EXECUTING
    assert next_state(StepState.OBSERVING_AFTER, StepState.CAPTURING_AFTER) is StepState.CAPTURING_AFTER
    assert next_state(StepState.CAPTURING_AFTER, StepState.ASSERTING) is StepState.ASSERTING


def test_recovering_state_can_replan():
    """P2.2 hook: RECOVERING may transition to REPLAN_PROPOSED."""
    state = next_state(StepState.RECOVERING, StepState.REPLAN_PROPOSED)
    assert state is StepState.REPLAN_PROPOSED


def test_all_transitions_listed_in_spec():
    """Every state must have at least one legal successor (besides
    terminals which legitimately have none)."""
    for s in StepState:
        allowed = [t for t in StepState if t in []]
        if is_terminal(s):
            continue
        successors = [t for t in StepState if s.value != t.value]
        # Cheap check: at least one outgoing edge.
        found = False
        for t in StepState:
            try:
                validate_transition(s, t)
                found = True
                break
            except IllegalTransition:
                pass
        assert found, f"dead-end state {s.value} has no successors"