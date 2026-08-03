"""recovery_planner.py — P2.2 pure recovery planner (Commit B).

The single public function is :func:`plan_recovery`. It is **pure**:

    * No I/O.
    * No filesystem access.
    * No backend calls.
    * No ``decide_checkpoint()`` invocation.
    * No trace mutation.

This module exists separately from :mod:`agent.execution.recovery`
to keep the contract layer import-clean (Commit A tests can import
the dataclasses without pulling in any planner logic) and so
the planner's logic surface is one focused file.

Design doc: ``docs/requirements/P2-recovery-planner.md`` §4.3
(purity), §5 (decisions), §7 (interface).

Round 1 design decisions baked into the planner:

    Q1 — ``decide_checkpoint()`` is NEVER called from inside this
         module. The executor owns that decision.

    Q2 — Multi-strategy conflict resolution uses
         ``RecoverySeverity`` ordering; the most-restrictive
         strategy wins. The current planner consumes a single
         strategy from P2.1's ``CheckpointDecision.restore_strategy``
         (P2.1 already does the source merging); we only enforce
         the severity tiebreaker when the caller supplies multiple
         strategies via the optional ``proposed_strategies`` field
         on :class:`FailureContext`. (See ``FailureContext``
         extension note in §Notes.)

    Q3 — Two-layer ``RecoveryBudget`` enforcement. The planner
         consumes the budget but does not own a timer; the
         executor times the execution and surfaces
         ``restore_deadline_exceeded`` when the wall clock
         exceeds ``budget.deadline_ms``.

    Q4 — ``Target.native_index`` is not consumed here. (The
         classifier may use it; the planner doesn't care.)

    Q5 — The planner is pure. See top-of-file docstring.
"""

from __future__ import annotations

from typing import Iterable

from agent.execution.checkpoints import (
    CheckpointDecision,
    CheckpointKind,
)
from agent.execution.recovery import (
    FailureContext,
    RecoveryBudget,
    RecoveryPlan,
    RecoverySeverity,
    ReplanState,
    RestoreStrategy,
    severity_of,
)
from agent.execution.recovery_inverse import InverseRegistry


# ---------------------------------------------------------------------------
# SOP id extraction from P2.1 rationale tags
# ---------------------------------------------------------------------------


def _extract_sop_id(rationale: Iterable[str]) -> str | None:
    """Extract ``sop:<id>`` from a P2.1 ``CheckpointDecision.rationale``.

    Rationale tags follow the ``"<source>:<detail>"`` convention used
    by ``agent.execution.checkpoints``. The SOP hint rule in
    checkpoints.py emits ``"sop:<sop_id>"``; this helper pulls the
    id out.

    Returns ``None`` if no ``sop:`` tag is present. Multiple sop
    tags in one rationale are an internal P2.1 contract violation
    that we do not defend against here (caller would already be in
    a bad state).
    """
    for tag in rationale:
        if tag.startswith("sop:"):
            return tag[len("sop:"):]
    return None


# ---------------------------------------------------------------------------
# Pure planner
# ---------------------------------------------------------------------------


def plan_recovery(
    failure: FailureContext,
    checkpoint: CheckpointDecision,
    budget: RecoveryBudget,
    inverse_registry: InverseRegistry,
) -> RecoveryPlan:
    """Plan a recovery attempt for one failing step.

    Pure function. See module docstring.

    Inputs:

        * ``failure`` — what failed, where, why.
        * ``checkpoint`` — the P2.1 ``CheckpointDecision`` consumed
                          but never mutated.
        * ``budget`` — the two-layer ``RecoveryBudget``; the
                       planner reads it but does not enforce timing.
        * ``inverse_registry`` — SOP inverse lookup table. Used
                                 only when the strategy is
                                 ``PROCESS_RESTART``.

    Output:

        * ``RecoveryPlan(strategy, steps, severity, timeout_ms)``.
          When the plan is BLOCKED, ``strategy`` is
          ``RestoreStrategy.BLOCKED``, ``steps`` is empty, and
          ``severity`` is ``RecoverySeverity.NONE``. The caller
          (executor) reads the plan and decides what
          ``RecoveryErrorCode`` to surface — the planner does
          not encode the code (the design doc's
          ``RecoveryPlan`` shape has no ``error_code`` field;
          the executor derives it from ``plan.strategy`` plus
          the original ``FailureContext``).

    Blocking decision tree (deterministic):

        1. ``checkpoint.restorable`` is ``False``
           → ``BLOCKED``
        2. ``checkpoint.restore_strategy`` is ``None`` or unknown
           → ``BLOCKED``
        3. ``failure.replan_state`` is ``CONSUMED``
           → ``BLOCKED`` (FR-08: replan budget exhausted)
        4. Strategy is ``PROCESS_RESTART`` and the inverse
           registry has no entry for the SOP driving the
           checkpoint
           → ``BLOCKED``
        5. Strategy is ``REOBSERVE_REPLAN`` but
           ``failure.replan_state`` is not ``AVAILABLE``
           → ``BLOCKED``
        6. Otherwise → ``RecoveryPlan(strategy, ...)``

    The order matters: earlier branches catch problems before
    later branches probe deeper state.
    """
    # --- 1. Restorable? -----------------------------------------------
    if not checkpoint.restorable:
        return _blocked_plan()

    # --- 2. Strategy present? -----------------------------------------
    raw_strategy = checkpoint.restore_strategy
    if raw_strategy is None:
        return _blocked_plan()
    try:
        strategy = RestoreStrategy(raw_strategy)
    except ValueError:
        # P2.1 emitted an unknown strategy string. Treat as BLOCKED
        # so the executor can surface ``restore_not_available``;
        # we never want to dispatch an unknown action.
        return _blocked_plan()

    # --- 3. Replan budget exhausted? ---------------------------------
    if failure.replan_state is ReplanState.CONSUMED:
        return _blocked_plan()

    # --- 4. PROCESS_RESTART path — need SOP inverse ------------------
    if strategy is RestoreStrategy.PROCESS_RESTART:
        sop_id = _extract_sop_id(checkpoint.rationale)
        inverse = (
            inverse_registry.get(sop_id) if sop_id is not None else None
        )
        if inverse is None:
            return _blocked_plan()
        return RecoveryPlan(
            strategy=strategy,
            steps=inverse.inverse_action_ids,
            severity=severity_of(strategy),
            timeout_ms=int(inverse.timeout_s * 1000),
        )

    # --- 5. REOBSERVE_REPLAN path — must be AVAILABLE ---------------
    if strategy is RestoreStrategy.REOBSERVE_REPLAN:
        if failure.replan_state is not ReplanState.AVAILABLE:
            return _blocked_plan()
        return RecoveryPlan(
            strategy=strategy,
            steps=(),
            severity=severity_of(strategy),
            timeout_ms=budget.deadline_ms,
        )

    # --- 6. REDRIVE / RECONNECT — straight execute -------------------
    return RecoveryPlan(
        strategy=strategy,
        steps=(),
        severity=severity_of(strategy),
        timeout_ms=budget.deadline_ms,
    )


def _blocked_plan() -> RecoveryPlan:
    """The single canonical BLOCKED plan.

    Centralized so callers can compare ``plan == _blocked_plan()``
    without re-constructing the dataclass in tests.
    """
    return RecoveryPlan(
        strategy=RestoreStrategy.BLOCKED,
        steps=(),
        severity=RecoverySeverity.NONE,
        timeout_ms=0,
    )


# ---------------------------------------------------------------------------
# Notes (informational, not enforced at runtime)
# ---------------------------------------------------------------------------

# 1. Multi-strategy conflict resolution (Q2)
#
#    P2.1 ``decide_checkpoint`` already merges multiple sources
#    (catalog + step + transition + SOP) into a single
#    ``restore_strategy`` string. By the time ``plan_recovery``
#    runs, the conflict is already resolved. The
#    ``RecoverySeverity`` ordering is therefore enforced at the
#    *boundary* between P2.1 and P2.2, not inside this planner.
#
#    If a future executor path emits a multi-strategy
#    ``FailureContext`` (e.g., a custom dispatcher producing
#    several candidate plans), the planner would compute
#    ``max(severity_of(s) for s in proposed_strategies)`` and
#    dispatch the highest-severity one. This is captured in the
#    design doc but not exercised in the V1 surface; the current
#    ``plan_recovery`` accepts a single strategy only.
#
# 2. Native index (Q4)
#
#    Not consumed here. The planner does not look at
#    ``Target.native_index``; it works entirely on
#    ``CheckpointDecision`` + ``FailureContext`` data.
#
# 3. Executor owns decide_checkpoint orchestration (Q1/Q5)
#
#    After this planner returns a plan for ``PROCESS_RESTART``,
#    the executor runs the inverse actions, observes the new
#    snapshot, and **then** calls ``decide_checkpoint()`` again
#    to verify the kind flipped back to ``none``. That re-call
#    lives in the executor (Commit C territory); this module
#    has no such call.


__all__ = [
    "plan_recovery",
]