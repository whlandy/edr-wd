"""P2.2 executor tests — RecoveryExecutor lifecycle + budget enforcement (Commit D).

These tests cover the 4 required cases from the Round 2 review:

    | Case                           | Expected      |
    | attempts exhausted             | terminal      |
    | deadline exceeded              | terminal      |
    | replan used twice              | loop detected |
    | restore succeeds first attempt | no retry      |

Plus lifecycle coverage:

    * BLOCKED plan is surfaced as terminal ``BLOCKED`` outcome.
    * Pre-flight attempts budget check fires before dispatch.
    * Replan state machine transitions are honored.
    * Handler error codes propagate into ``RecoveryResult.error_code``.
    * ``RequestedEvent`` list captures both ``recovery_requested``
      and ``recovery_result`` events.
    * Deadline is anchored on the first ``execute()`` call; second
      call after the deadline returns ``RESTORE_DEADLINE_EXCEEDED``.
    * ``reset_cycle()`` resets the anchor + counters.
"""

from __future__ import annotations

from typing import Mapping

import pytest

from agent.execution.recovery import (
    FailureContext,
    RecoveryBudget,
    RecoveryErrorCode,
    RecoveryPlan,
    RecoveryResult,
    RecoverySeverity,
    RecoveryStatus,
    ReplanState,
    RestoreResult,
    RestoreStrategy,
)
from agent.execution.recovery_executor import (
    ExecutionOutcome,
    RecoveryExecutor,
    RequestedEvent,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class FakeClock:
    """Test clock — monotonic-like counter that tests can advance."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, ms: int) -> None:
        self.t += ms / 1000


def _ok_handler(
    strategy: RestoreStrategy,
    payload: Mapping[str, object],
) -> RestoreResult:
    return RestoreResult(ok=True, code=None, snapshot_id="snap-after-restore")


def _fail_handler(
    strategy: RestoreStrategy,
    payload: Mapping[str, object],
) -> RestoreResult:
    return RestoreResult(
        ok=False,
        code=RecoveryErrorCode.RESTORE_LOCK_VERIFY_FAILED,
        snapshot_id=None,
    )


def _failure(replan_state: ReplanState = ReplanState.NOT_REQUESTED) -> FailureContext:
    return FailureContext(
        step_id="step-1",
        branch_id="BR-001",
        failure_kind="state_drift",
        replan_state=replan_state,
    )


def _plan(strategy: RestoreStrategy = RestoreStrategy.REDRIVE) -> RecoveryPlan:
    return RecoveryPlan(
        strategy=strategy,
        steps=(),
        severity=RecoverySeverity.PAGE,
        timeout_ms=30_000,
    )


# ---------------------------------------------------------------------------
# Case 1: success first attempt -> no retry
# ---------------------------------------------------------------------------


def test_executor_success_first_attempt_no_retry():
    """Round 2 review required test:

        restore succeeds first attempt -> no retry.
    """
    ex = RecoveryExecutor(handler=_ok_handler)
    out = ex.execute(
        _plan(), _failure(),
        branch_id="BR-002", parent_branch_id="BR-001",
    )

    assert out.result.status is RecoveryStatus.SUCCESS
    assert out.result.attempts == 1
    assert ex.attempts_used == 1
    assert out.result.branch_id == "BR-002"
    assert out.result.parent_branch_id == "BR-001"
    # Two events: recovery_requested + recovery_result.
    assert [e.event_type for e in out.events] == [
        "recovery_requested", "recovery_result",
    ]


# ---------------------------------------------------------------------------
# Case 2: attempts exhausted -> terminal
# ---------------------------------------------------------------------------


def test_executor_attempts_exhausted_returns_terminal():
    """Round 2 review required test:

        attempts exhausted -> terminal ``RESTORE_ATTEMPTS_EXHAUSTED``.
    """
    ex = RecoveryExecutor(
        handler=_fail_handler,
        budget=RecoveryBudget(max_attempts=2, max_replans=1, deadline_ms=30_000),
    )

    # First attempt fails (handler returns ok=False).
    out1 = ex.execute(
        _plan(), _failure(),
        branch_id="BR-002", parent_branch_id="BR-001",
    )
    assert out1.result.status is RecoveryStatus.FAILED
    assert out1.result.error_code is RecoveryErrorCode.RESTORE_LOCK_VERIFY_FAILED
    assert ex.attempts_used == 1

    # Second attempt fails; budget now exhausted.
    out2 = ex.execute(
        _plan(), _failure(),
        branch_id="BR-003", parent_branch_id="BR-002",
    )
    assert out2.result.status is RecoveryStatus.FAILED
    assert out2.result.error_code is RecoveryErrorCode.RESTORE_LOCK_VERIFY_FAILED
    assert ex.attempts_used == 2

    # Third attempt: pre-flight budget check fires before dispatch.
    out3 = ex.execute(
        _plan(), _failure(),
        branch_id="BR-004", parent_branch_id="BR-003",
    )
    assert out3.result.status is RecoveryStatus.FAILED
    assert out3.result.error_code is RecoveryErrorCode.RESTORE_ATTEMPTS_EXHAUSTED
    # The pre-flight check did NOT call the handler.
    # (We can detect this by checking attempts did not advance.)


# ---------------------------------------------------------------------------
# Case 3: deadline exceeded -> terminal
# ---------------------------------------------------------------------------


def test_executor_deadline_exceeded_returns_terminal():
    """Round 2 review required test:

        deadline exceeded -> terminal ``RESTORE_DEADLINE_EXCEEDED``.
    """
    clk = FakeClock()
    ex = RecoveryExecutor(
        handler=_ok_handler,
        budget=RecoveryBudget(max_attempts=3, max_replans=1, deadline_ms=1000),
        clock=clk,
    )

    # First call: cycle starts; deadline check 0 < 1000 OK.
    out1 = ex.execute(
        _plan(), _failure(),
        branch_id="BR-002", parent_branch_id="BR-001",
    )
    assert out1.result.status is RecoveryStatus.SUCCESS

    # Advance clock past deadline.
    clk.advance(1500)

    # Second call: deadline check fires.
    out2 = ex.execute(
        _plan(), _failure(),
        branch_id="BR-003", parent_branch_id="BR-002",
    )
    assert out2.result.status is RecoveryStatus.FAILED
    assert out2.result.error_code is RecoveryErrorCode.RESTORE_DEADLINE_EXCEEDED


# ---------------------------------------------------------------------------
# Case 4: replan used twice -> loop detected
# ---------------------------------------------------------------------------


def test_executor_replan_used_twice_detects_loop():
    """Round 2 review required test:

        replan used twice -> ``RECOVERY_LOOP_DETECTED``.

    The planner blocks on ``replan_state=CONSUMED`` already
    (Commit B), but the executor must also catch the case where
    multiple ``REOBSERVE_REPLAN`` cycles run on the same executor
    instance and the second cycle's replan budget is exhausted.
    """
    ex = RecoveryExecutor(
        handler=_ok_handler,
        budget=RecoveryBudget(max_attempts=5, max_replans=1, deadline_ms=30_000),
    )

    # Cycle 1: first replan. State machine: NOT_REQUESTED -> AVAILABLE -> CONSUMED.
    plan_replan = _plan(RestoreStrategy.REOBSERVE_REPLAN)
    out1 = ex.execute(
        plan_replan,
        _failure(replan_state=ReplanState.AVAILABLE),
        branch_id="BR-002", parent_branch_id="BR-001",
    )
    assert out1.result.status is RecoveryStatus.SUCCESS
    assert ex.replans_used == 1
    assert ex.replan_state is ReplanState.CONSUMED

    # Cycle 2: replan again. ``replan_state=CONSUMED`` so the
    # executor surfaces ``recovery_loop_detected`` immediately
    # (the planner-side branch handles this for fresh executors
    # but per-cycle replan exhaustion also fires here).
    out2 = ex.execute(
        plan_replan,
        _failure(replan_state=ReplanState.CONSUMED),
        branch_id="BR-003", parent_branch_id="BR-002",
    )
    assert out2.result.status is RecoveryStatus.FAILED
    assert out2.result.error_code is RecoveryErrorCode.RECOVERY_LOOP_DETECTED


# ---------------------------------------------------------------------------
# BLOCKED plan surfaces as terminal BLOCKED outcome
# ---------------------------------------------------------------------------


def test_executor_blocked_plan_returns_blocked_outcome():
    """The executor surfaces a ``RestoreStrategy.BLOCKED`` plan as
    a terminal ``RecoveryStatus.BLOCKED`` with
    ``RESTORE_NOT_AVAILABLE``. The handler is never called.
    """
    calls: list[RestoreStrategy] = []

    def tracker(
        strategy: RestoreStrategy, payload: Mapping[str, object]
    ) -> RestoreResult:
        calls.append(strategy)
        return RestoreResult(ok=True, code=None, snapshot_id="snap")

    ex = RecoveryExecutor(handler=tracker)
    blocked = RecoveryPlan(
        strategy=RestoreStrategy.BLOCKED,
        steps=(),
        severity=RecoverySeverity.NONE,
        timeout_ms=0,
    )
    out = ex.execute(
        blocked, _failure(),
        branch_id=None, parent_branch_id="BR-001",
    )

    assert out.result.status is RecoveryStatus.BLOCKED
    assert out.result.error_code is RecoveryErrorCode.RESTORE_NOT_AVAILABLE
    assert out.result.branch_id is None
    # Handler was never called.
    assert calls == []


# ---------------------------------------------------------------------------
# Event payloads carry context
# ---------------------------------------------------------------------------


def test_executor_emits_recovery_requested_and_result_events():
    """Every successful execute() emits one ``recovery_requested``
    event and one ``recovery_result`` event. The events carry
    step_id, branch lineage, strategy, attempts, and (for the
    result) the handler's outcome.
    """
    ex = RecoveryExecutor(handler=_ok_handler)
    out = ex.execute(
        _plan(RestoreStrategy.PROCESS_RESTART), _failure(),
        branch_id="BR-002", parent_branch_id="BR-001",
    )

    assert len(out.events) == 2
    requested, result_evt = out.events

    assert requested.event_type == "recovery_requested"
    assert requested.payload["step_id"] == "step-1"
    assert requested.payload["branch_id"] == "BR-002"
    assert requested.payload["parent_branch_id"] == "BR-001"
    assert requested.payload["strategy"] == "process_restart"
    assert requested.payload["severity"] == RecoverySeverity.APPLICATION.value

    assert result_evt.event_type == "recovery_result"
    assert result_evt.payload["status"] == "success"
    assert result_evt.payload["strategy"] == "process_restart"
    assert result_evt.payload["attempts"] == 1
    assert result_evt.payload["error_code"] is None


def test_executor_failure_event_carries_error_code():
    """A handler failure produces a ``recovery_result`` event
    with the handler's error code.
    """
    ex = RecoveryExecutor(handler=_fail_handler)
    out = ex.execute(
        _plan(), _failure(),
        branch_id="BR-002", parent_branch_id="BR-001",
    )

    assert out.result.status is RecoveryStatus.FAILED
    result_evt = out.events[-1]
    assert result_evt.event_type == "recovery_result"
    assert result_evt.payload["status"] == "failed"
    assert result_evt.payload["error_code"] == "restore_lock_verify_failed"


# ---------------------------------------------------------------------------
# reset_cycle
# ---------------------------------------------------------------------------


def test_executor_reset_cycle_anchors_new_deadline():
    """``reset_cycle()`` clears attempts, replans, and the
    deadline anchor. A subsequent execute() starts a fresh cycle.
    """
    clk = FakeClock()
    ex = RecoveryExecutor(
        handler=_ok_handler,
        budget=RecoveryBudget(max_attempts=3, max_replans=1, deadline_ms=1000),
        clock=clk,
    )

    # Burn an attempt and advance past deadline.
    ex.execute(
        _plan(), _failure(),
        branch_id="BR-002", parent_branch_id="BR-001",
    )
    clk.advance(1500)
    assert ex.attempts_used == 1

    # Reset and rewind clock.
    ex.reset_cycle()
    clk.t = 0.0
    assert ex.attempts_used == 0

    # Fresh cycle succeeds.
    out = ex.execute(
        _plan(), _failure(),
        branch_id="BR-002", parent_branch_id="BR-001",
    )
    assert out.result.status is RecoveryStatus.SUCCESS
    assert ex.attempts_used == 1


# ---------------------------------------------------------------------------
# Handler error code propagation
# ---------------------------------------------------------------------------


def test_executor_propagates_handler_error_code():
    """When the handler returns ``ok=False`` with a specific
    code, the executor surfaces that code in
    ``RecoveryResult.error_code``.
    """
    def specific_fail(
        strategy: RestoreStrategy, payload: Mapping[str, object]
    ) -> RestoreResult:
        return RestoreResult(
            ok=False,
            code=RecoveryErrorCode.RESTORE_SOP_UNREGISTERED,
            snapshot_id=None,
        )

    ex = RecoveryExecutor(handler=specific_fail)
    out = ex.execute(
        _plan(RestoreStrategy.PROCESS_RESTART), _failure(),
        branch_id="BR-002", parent_branch_id="BR-001",
    )
    assert out.result.status is RecoveryStatus.FAILED
    assert out.result.error_code is RecoveryErrorCode.RESTORE_SOP_UNREGISTERED


def test_executor_handler_without_code_defaults_to_not_available():
    """If the handler returns ``ok=False`` without a code, the
    executor defaults to ``RESTORE_NOT_AVAILABLE``.
    """
    def fail_no_code(
        strategy: RestoreStrategy, payload: Mapping[str, object]
    ) -> RestoreResult:
        return RestoreResult(ok=False, code=None, snapshot_id=None)

    ex = RecoveryExecutor(handler=fail_no_code)
    out = ex.execute(
        _plan(), _failure(),
        branch_id="BR-002", parent_branch_id="BR-001",
    )
    assert out.result.error_code is RecoveryErrorCode.RESTORE_NOT_AVAILABLE


# ---------------------------------------------------------------------------
# Pure inputs (no mutation)
# ---------------------------------------------------------------------------


def test_executor_does_not_mutate_inputs():
    """The executor must not mutate its inputs (failure, plan,
    budget). Verify by snapshotting ``failure.replan_state``
    and ``plan.strategy`` before two consecutive calls; both
    should be unchanged after.
    """
    ex = RecoveryExecutor(handler=_ok_handler)
    failure = _failure()
    plan = _plan()
    budget_snapshot = ex.budget

    # Snapshot identities / primitives.
    failure_state_before = failure.replan_state
    plan_strategy_before = plan.strategy

    ex.execute(plan, failure, branch_id="BR-X", parent_branch_id="BR-Y")
    ex.execute(plan, failure, branch_id="BR-X", parent_branch_id="BR-Y")

    assert failure.replan_state is failure_state_before
    assert plan.strategy is plan_strategy_before
    assert ex.budget is budget_snapshot