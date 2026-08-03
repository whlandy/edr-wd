"""recovery_executor.py — P2.2 recovery executor lifecycle + budget enforcement (Commit D).

This module owns:

    * The :class:`RecoveryExecutor` class — lifecycle + budget
      enforcement for one recovery attempt.
    * The budget tracker that counts attempts, replans, and wall
      clock against :class:`RecoveryBudget`.
    * Dispatch of restore strategies. The executor delegates the
      **side-effecting** restore action to injected callbacks
      (``REDRIVE_HANDLER`` / ``RECONNECT_HANDLER`` etc.) so the
      class itself remains testable without backend wiring.

The executor does **not** mutate the trace store directly. It
returns a :class:`RecoveryResult` plus a list of *requested*
trace event dicts. Commit E wires those dicts to
``TraceStore.append``. This separation keeps Commit D free of
trace-store coupling and lets the round-trip be unit-tested
without filesystem I/O.

Design doc: ``docs/requirements/P2-recovery-planner.md`` §4.3
(planner purity), §5.3 (budget), §6 (carry-over).

Round 2 budget tests covered here:

    | Case                           | Expected      |
    | attempts exhausted             | terminal      |
    | deadline exceeded              | terminal      |
    | replan used twice              | loop detected |
    | restore succeeds first attempt | no retry      |
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Mapping

from agent.execution.recovery import (
    FailureContext,
    RecoveryBudget,
    RecoveryErrorCode,
    RecoveryResult,
    RecoverySeverity,
    RecoveryStatus,
    ReplanState,
    RestoreResult,
    RestoreStrategy,
    advance_replan_state,
    severity_of,
)
from agent.execution.recovery_events import (
    RequestedEvent,
    make_recovery_requested_event,
    make_recovery_result_event,
)


# ---------------------------------------------------------------------------
# Restore-handler protocol
# ---------------------------------------------------------------------------


RestoreHandler = Callable[
    [RestoreStrategy, Mapping[str, object]],
    RestoreResult,
]
"""Protocol for an injected side-effecting restore handler.

Commit D ships **no** default handler — the executor requires
callers to inject one. This is the DI seam that Commit E will
fill with concrete catalog-action dispatchers.

The handler must be a pure function of ``(strategy, payload)``
returning a :class:`RestoreResult`:

    * ``RestoreResult.ok=True``  — the restore succeeded.
    * ``RestoreResult.ok=False`` — the restore failed; ``code``
      carries the :class:`RecoveryErrorCode`.

The handler must not raise; it must catch its own errors and
return ``RestoreResult(ok=False, code=...)``.
"""


# ---------------------------------------------------------------------------
# Outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionOutcome:
    """The complete result of one ``RecoveryExecutor.execute`` call.

    Holds:

        * :class:`RecoveryResult` — the wire-shaped outcome.
        * :class:`list` ``[RequestedEvent]`` — events to write
          to the trace store (Commit E's job).
        * ``replan_state_after`` — the :class:`ReplanState` to
          pass back to the planner for the next attempt (if any).
          ``None`` if recovery terminated.

    The dataclass is frozen so callers can hand it across threads
    or persist it without aliasing risk.
    """

    result: RecoveryResult
    events: tuple[RequestedEvent, ...]
    replan_state_after: ReplanState | None


# ---------------------------------------------------------------------------
# Budget tracker
# ---------------------------------------------------------------------------


@dataclass
class _BudgetState:
    """Mutable internal state for one executor run.

    Lives on the :class:`RecoveryExecutor` instance for the
    duration of one ``execute()`` call and is reset on the next.
    """

    attempts: int = 0
    replans: int = 0
    deadline_exceeded: bool = False
    loop_detected: bool = False
    replan_state: ReplanState = ReplanState.NOT_REQUESTED
    cycle_started_perf: float | None = None


# ---------------------------------------------------------------------------
# RecoveryExecutor
# ---------------------------------------------------------------------------


class RecoveryExecutor:
    """Lifecycle + budget enforcement for P2.2 recovery.

    The executor consumes a :class:`RecoveryPlan` produced by
    :func:`agent.execution.recovery_planner.plan_recovery` and
    dispatches the restore action via an injected
    :class:`RestoreHandler`. It enforces the two-layer
    :class:`RecoveryBudget`:

        * ``max_attempts`` — per-step cap on restore invocations.
        * ``max_replans``  — per-step cap on replan consume events.
        * ``deadline_ms``  — wall-clock cap on the whole cycle.

    Constructor parameters:

        * ``handler`` — :class:`RestoreHandler` (DI seam).
        * ``budget`` — :class:`RecoveryBudget` (defaults to
                        ``RecoveryBudget()``).

    The executor is **stateful** in the sense that budget state
    accumulates across calls to ``execute()``. Callers who want
    per-step isolation should construct a fresh executor per
    step (the AtomicExecutor's natural integration point).
    """

    def __init__(
        self,
        *,
        handler: RestoreHandler,
        budget: RecoveryBudget | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._handler = handler
        self._budget = budget or RecoveryBudget()
        self._clock = clock  # injectable for tests
        self._state = _BudgetState()

    # -----------------------------------------------------------------
    # Public read-only accessors (used by tests + the caller to
    # observe post-execution state without mutating it)
    # -----------------------------------------------------------------

    @property
    def attempts_used(self) -> int:
        return self._state.attempts

    @property
    def replans_used(self) -> int:
        return self._state.replans

    @property
    def budget(self) -> RecoveryBudget:
        return self._budget

    @property
    def replan_state(self) -> ReplanState:
        return self._state.replan_state

    def reset_cycle(self) -> None:
        """Reset the executor's cycle state (anchor clock, attempts, replans).

        Useful for callers that want per-step isolation. The
        injected handler and budget are preserved.
        """
        self._state = _BudgetState()

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------

    def execute(
        self,
        plan: RecoveryPlan,
        failure: FailureContext,
        *,
        branch_id: str | None,
        parent_branch_id: str | None,
    ) -> ExecutionOutcome:
        """Execute one recovery cycle for ``failure``.

        Returns an :class:`ExecutionOutcome` whose ``result`` is
        one of:

            * ``RecoveryStatus.SUCCESS`` — handler returned
              success and the budget allowed the attempt.
            * ``RecoveryStatus.FAILED`` — handler returned
              failure, the budget is exhausted, or the deadline
              tripped. ``result.error_code`` is set.
            * ``RecoveryStatus.BLOCKED`` — the planner emitted
              ``BLOCKED``. The executor surfaces a terminal code
              from :class:`RecoveryErrorCode`.
            * ``RecoveryStatus.REPLANNED`` — strategy was
              ``REOBSERVE_REPLAN`` and the replan is now
              available. The caller should re-plan and
              re-execute; ``replan_state_after`` carries the
              next :class:`ReplanState`.

        Side effects: none. The executor does not mutate the
        failure / plan / budget inputs. It does not write to the
        trace store; it produces :class:`RequestedEvent` objects
        that Commit E forwards.

        Deadline semantics: the deadline is anchored on the
        **first** ``execute()`` call on this executor. Subsequent
        calls share the same anchor so the wall-clock cap
        applies to the whole recovery cycle, not per call.
        Callers who need per-step isolation should construct a
        fresh executor per step (or call :meth:`reset_cycle`).
        """
        events: list[RequestedEvent] = []

        # Anchor the cycle's deadline clock on the first call.
        if self._state.cycle_started_perf is None:
            self._state.cycle_started_perf = self._clock()

        # --- Pre-flight: BLOCKED strategy is terminal ---
        if plan.strategy is RestoreStrategy.BLOCKED:
            return self._terminal_blocked(
                failure, plan, branch_id, parent_branch_id, events
            )

        # --- Pre-flight: deadline check (anchored) ---
        elapsed_ms = int(
            (self._clock() - self._state.cycle_started_perf) * 1000
        )
        if elapsed_ms > self._budget.deadline_ms:
            return self._terminal(
                failure=failure,
                plan=plan,
                branch_id=branch_id,
                parent_branch_id=parent_branch_id,
                events=events,
                status=RecoveryStatus.FAILED,
                code=RecoveryErrorCode.RESTORE_DEADLINE_EXCEEDED,
                detail=(
                    f"elapsed_ms={elapsed_ms} "
                    f"> deadline_ms={self._budget.deadline_ms}"
                ),
            )

        # --- Pre-flight: budget exhaustion check ---
        if self._state.attempts >= self._budget.max_attempts:
            return self._terminal(
                failure=failure,
                plan=plan,
                branch_id=branch_id,
                parent_branch_id=parent_branch_id,
                events=events,
                status=RecoveryStatus.FAILED,
                code=RecoveryErrorCode.RESTORE_ATTEMPTS_EXHAUSTED,
                detail=(
                    f"attempts={self._state.attempts} "
                    f">= max_attempts={self._budget.max_attempts}"
                ),
            )

        # --- Replan loop detection ---
        # If the caller asks for REOBSERVE_REPLAN but the replan
        # has already been consumed in a previous cycle, this is
        # a loop. Surface ``recovery_loop_detected`` and terminate.
        if (
            plan.strategy is RestoreStrategy.REOBSERVE_REPLAN
            and self._state.replan_state is ReplanState.CONSUMED
        ):
            return self._terminal(
                failure=failure,
                plan=plan,
                branch_id=branch_id,
                parent_branch_id=parent_branch_id,
                events=events,
                status=RecoveryStatus.FAILED,
                code=RecoveryErrorCode.RECOVERY_LOOP_DETECTED,
                detail=(
                    f"replan_state=CONSUMED; "
                    f"max_replans={self._budget.max_replans}"
                ),
            )

        # --- Replan state transition: NOT_REQUESTED -> AVAILABLE ---
        # The planner saw ``failure.replan_state`` and emitted a
        # REOBSERVE_REPLAN plan; mirror that by advancing our
        # internal state from NOT_REQUESTED -> AVAILABLE before
        # dispatch. After successful dispatch, we'll transition
        # AVAILABLE -> CONSUMED (in the success path).
        if plan.strategy is RestoreStrategy.REOBSERVE_REPLAN:
            try:
                self._state.replan_state = advance_replan_state(
                    self._state.replan_state, ReplanState.AVAILABLE
                )
            except ValueError:
                # Should not happen — we just guarded CONSUMED.
                pass

        # --- Construct the RECOVERY_REQUESTED event ---
        events.append(make_recovery_requested_event(
            step_id=failure.step_id,
            branch_id=branch_id,
            parent_branch_id=parent_branch_id,
            strategy=plan.strategy,
            severity=severity_of(plan.strategy),
            attempts_so_far=self._state.attempts,
            replans_so_far=self._state.replans,
        ))

        # --- Dispatch ---
        attempt_started_perf = self._clock()
        restore_result = self._handler(
            plan.strategy,
            {
                "failure": failure,
                "plan": plan,
                "branch_id": branch_id,
            },
        )
        attempt_elapsed_ms = int(
            (self._clock() - attempt_started_perf) * 1000
        )

        # --- Consume an attempt ---
        self._state.attempts += 1

        # --- Build the RECOVERY_RESULT event (handler outcome) ---
        result_status = (
            RecoveryStatus.SUCCESS if restore_result.ok
            else RecoveryStatus.FAILED
        )
        result_error_code = (
            restore_result.code.value
            if restore_result.code is not None
            else None
        )
        events.append(make_recovery_result_event(
            step_id=failure.step_id,
            branch_id=branch_id,
            parent_branch_id=parent_branch_id,
            strategy=plan.strategy,
            status=result_status,
            attempts=self._state.attempts,
            elapsed_ms=attempt_elapsed_ms,
            error_code=result_error_code,
            detail="",
        ))

        # --- Path A: handler reported failure ---
        if not restore_result.ok:
            code = (
                restore_result.code
                if restore_result.code is not None
                else RecoveryErrorCode.RESTORE_NOT_AVAILABLE
            )
            return self._terminal(
                failure=failure,
                plan=plan,
                branch_id=branch_id,
                parent_branch_id=parent_branch_id,
                events=events,
                status=RecoveryStatus.FAILED,
                code=code,
                detail=(
                    f"handler returned ok=False "
                    f"(strategy={plan.strategy.value}, "
                    f"code={code.value})"
                ),
            )

        # --- Path B: success ---
        # If the strategy was REOBSERVE_REPLAN, we have now
        # CONSUMED the replan. Transition the state machine
        # AVAILABLE -> CONSUMED (the AVAILABLE transition was
        # performed in pre-flight before dispatch).
        if plan.strategy is RestoreStrategy.REOBSERVE_REPLAN:
            self._consume_replan()

        return ExecutionOutcome(
            result=RecoveryResult(
                status=RecoveryStatus.SUCCESS,
                strategy=plan.strategy,
                attempts=self._state.attempts,
                branch_id=branch_id,
                parent_branch_id=parent_branch_id,
            ),
            events=tuple(events),
            replan_state_after=(
                self._state.replan_state
                if plan.strategy is RestoreStrategy.REOBSERVE_REPLAN
                else None
            ),
        )

    # -----------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------

    def _consume_replan(self) -> None:
        """Advance the replan state machine AVAILABLE -> CONSUMED
        and check the ``max_replans`` budget.

        If the budget is exceeded, sets ``loop_detected`` so the
        next ``execute()`` call surfaces ``recovery_loop_detected``.
        """
        try:
            self._state.replan_state = advance_replan_state(
                self._state.replan_state, ReplanState.CONSUMED
            )
            self._state.replans += 1
        except ValueError:
            # Illegal transition (already CONSUMED or weird state).
            self._state.loop_detected = True

        if self._state.replans > self._budget.max_replans:
            self._state.loop_detected = True

    def _terminal(
        self,
        *,
        failure: FailureContext,
        plan: RecoveryPlan,
        branch_id: str | None,
        parent_branch_id: str | None,
        events: list[RequestedEvent],
        status: RecoveryStatus,
        code: RecoveryErrorCode,
        detail: str,
    ) -> ExecutionOutcome:
        """Build a terminal outcome (FAILED / BLOCKED).

        Helper centralizes the RecoveryResult construction so
        tests can compare against a single canonical terminal
        shape.
        """
        events.append(make_recovery_result_event(
            step_id=failure.step_id,
            branch_id=branch_id,
            parent_branch_id=parent_branch_id,
            strategy=plan.strategy,
            status=status,
            attempts=self._state.attempts,
            elapsed_ms=0,
            error_code=code.value,
            detail=detail,
        ))
        return ExecutionOutcome(
            result=RecoveryResult(
                status=status,
                strategy=plan.strategy,
                attempts=self._state.attempts,
                branch_id=(
                    branch_id
                    if status is RecoveryStatus.SUCCESS
                    else None
                ),
                parent_branch_id=parent_branch_id,
                error_code=code,
            ),
            events=tuple(events),
            replan_state_after=None,
        )

    def _terminal_blocked(
        self,
        failure: FailureContext,
        plan: RecoveryPlan,
        branch_id: str | None,
        parent_branch_id: str | None,
        events: list[RequestedEvent],
    ) -> ExecutionOutcome:
        """Specialized terminal for ``RestoreStrategy.BLOCKED``.

        Maps to :class:`RecoveryStatus.BLOCKED` with the
        ``restore_not_available`` error code (the canonical
        "no plan produced" sentinel).
        """
        return self._terminal(
            failure=failure,
            plan=plan,
            branch_id=branch_id,
            parent_branch_id=parent_branch_id,
            events=events,
            status=RecoveryStatus.BLOCKED,
            code=RecoveryErrorCode.RESTORE_NOT_AVAILABLE,
            detail=(
                f"planner returned BLOCKED "
                f"(step_id={failure.step_id}, "
                f"failure_kind={failure.failure_kind!r})"
            ),
        )


__all__ = [
    "RecoveryExecutor",
    "ExecutionOutcome",
    "RestoreHandler",
]