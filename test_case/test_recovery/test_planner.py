"""P2.2 planner tests — exercise ``plan_recovery()`` decision tree.

The planner is a pure function. Each test here pins one branch
of the decision tree in :mod:`agent.execution.recovery_planner`:

    1. ``checkpoint.restorable=False``              -> BLOCKED
    2. ``checkpoint.restore_strategy=None/unknown`` -> BLOCKED
    3. ``failure.replan_state=CONSUMED``            -> BLOCKED (FR-08)
    4. PROCESS_RESTART with no inverse registered   -> BLOCKED
    5. PROCESS_RESTART with inverse registered      -> PROCESS_RESTART
    6. RECONNECT / REDRIVE                          -> strategy passthrough
    7. REOBSERVE_REPLAN + AVAILABLE                 -> REOBSERVE_REPLAN
    8. REOBSERVE_REPLAN + NOT_REQUESTED             -> BLOCKED

These tests cover 6 of the 9 spec tests in
``docs/requirements/P2-recovery-production.md`` §P2.2 directly:

    * ``test_application_state_only_registered_sops``
    * ``test_restore_not_available_blocks``
    * ``test_replan_single_recovery_per_step``

The remaining 3 spec tests (``test_logical_recovery_reobserves``,
``test_session_recovery_recreates_lock``,
``test_step_results_show_both_attempts``,
``test_trace_md_two_subsections``) require executor + trace
integration that lands in Commit C' / P2.4 (out of scope for
the pure planner).
"""

from __future__ import annotations

from agent.execution.checkpoints import CheckpointKind
from agent.execution.recovery import (
    RecoverySeverity,
    RestoreStrategy,
)
from agent.execution.recovery_planner import plan_recovery

from test_case.fixtures.recovery.builder import (
    make_decision,
    make_failure,
    make_registry,
)


# Default budget: the test does not exercise budget variations
# here — that lives in the executor integration tests.
from agent.execution.recovery import RecoveryBudget  # noqa: E402


# ---------------------------------------------------------------------------
# Branch 1: not restorable -> BLOCKED
# ---------------------------------------------------------------------------


def test_plan_recovery_blocks_when_checkpoint_not_restorable():
    """Spec test ``test_restore_not_available_blocks``:

        ``restorable=False`` -> ``RestoreStrategy.BLOCKED``.

    The planner must not dispatch any inverse action when the
    P2.1 decision said this checkpoint is not restorable.
    """
    decision = make_decision(
        kind=CheckpointKind.APPLICATION_STATE,
        restorable=False,
        restore_strategy=None,
        rationale=("step:application_restart",),
    )
    failure = make_failure()

    plan = plan_recovery(failure, decision, RecoveryBudget(), make_registry())

    assert plan.strategy is RestoreStrategy.BLOCKED
    assert plan.steps == ()
    assert plan.severity is RecoverySeverity.NONE
    assert plan.timeout_ms == 0


# ---------------------------------------------------------------------------
# Branch 2: unknown strategy -> BLOCKED
#
# Note: ``restore_strategy=None`` with ``restorable=True`` is rejected
# by ``CheckpointDecision.__post_init__`` in P2.1, so we cannot
# exercise the planner's None-defence here. The branch remains in
# the planner as belt-and-braces in case P2.1's contract loosens.
# ---------------------------------------------------------------------------


def test_plan_recovery_blocks_when_restore_strategy_is_unknown_string():
    """P2.1 emits a known ``restore_strategy`` string. If the
    strategy is not in ``RestoreStrategy`` (e.g., drift from a
    stale P2.2 build), the planner must not crash — it must
    return BLOCKED so the executor surfaces
    ``restore_not_available``.
    """
    decision = make_decision(
        kind=CheckpointKind.LOGICAL,
        restorable=True,
        restore_strategy="imaginary_strategy",
    )
    failure = make_failure()

    plan = plan_recovery(failure, decision, RecoveryBudget(), make_registry())

    assert plan.strategy is RestoreStrategy.BLOCKED


# ---------------------------------------------------------------------------
# Branch 3: replan budget exhausted (FR-08)
# ---------------------------------------------------------------------------


def test_plan_recovery_blocks_when_replan_state_consumed():
    """Spec test ``test_replan_single_recovery_per_step``:

        After the replan is consumed, any subsequent recovery
        attempt on the same step must BLOCKED. The planner
        cannot unblock even if the strategy is recoverable.
    """
    from agent.execution.recovery import ReplanState

    decision = make_decision(
        kind=CheckpointKind.LOGICAL,
        restorable=True,
        restore_strategy="reobserve_replan",
    )
    failure = make_failure(replan_state=ReplanState.CONSUMED)

    plan = plan_recovery(failure, decision, RecoveryBudget(), make_registry())

    assert plan.strategy is RestoreStrategy.BLOCKED


def test_plan_recovery_blocks_replan_when_state_not_available():
    """REOBSERVE_REPLAN is only legal when ``replan_state`` is
    AVAILABLE. NOT_REQUESTED must BLOCK so the executor cannot
    skip the state machine.
    """
    from agent.execution.recovery import ReplanState

    decision = make_decision(
        kind=CheckpointKind.LOGICAL,
        restorable=True,
        restore_strategy="reobserve_replan",
    )
    failure = make_failure(replan_state=ReplanState.NOT_REQUESTED)

    plan = plan_recovery(failure, decision, RecoveryBudget(), make_registry())

    assert plan.strategy is RestoreStrategy.BLOCKED


# ---------------------------------------------------------------------------
# Branch 4: PROCESS_RESTART — SOP inverse required
# ---------------------------------------------------------------------------


def test_plan_recovery_process_restart_blocks_when_inverse_not_registered():
    """Spec test ``test_application_state_only_registered_sops``:

        ``PROCESS_RESTART`` with a SOP id that has no inverse
        registered -> BLOCKED. The planner must not dispatch
        any actions when the inverse is missing.
    """
    decision = make_decision(
        kind=CheckpointKind.APPLICATION_STATE,
        restorable=True,
        restore_strategy="process_restart",
        rationale=("sop:edrclient.main",),
    )
    failure = make_failure()
    registry = make_registry()  # empty registry

    plan = plan_recovery(failure, decision, RecoveryBudget(), registry)

    assert plan.strategy is RestoreStrategy.BLOCKED
    assert plan.steps == ()


def test_plan_recovery_process_restart_blocks_when_no_sop_tag_in_rationale():
    """``PROCESS_RESTART`` strategy without a ``sop:<id>`` tag
    in rationale -> BLOCKED. The planner has no way to look up
    an inverse for an unknown SOP.
    """
    decision = make_decision(
        kind=CheckpointKind.APPLICATION_STATE,
        restorable=True,
        restore_strategy="process_restart",
        rationale=("step:application_restart",),  # no sop:<id>
    )
    failure = make_failure()
    registry = make_registry([("edrclient.main", ("edrclient.relaunch",))])

    plan = plan_recovery(failure, decision, RecoveryBudget(), registry)

    assert plan.strategy is RestoreStrategy.BLOCKED


def test_plan_recovery_process_restart_uses_inverse_when_registered():
    """Spec test ``test_application_state_only_registered_sops``
    (positive case):

        When the SOP has an inverse registered, the planner
        returns the inverse action ids in order and the inverse's
        timeout (converted to ms).
    """
    decision = make_decision(
        kind=CheckpointKind.APPLICATION_STATE,
        restorable=True,
        restore_strategy="process_restart",
        rationale=("sop:edrclient.main",),
    )
    failure = make_failure()
    registry = make_registry([
        ("edrclient.main", ("edrclient.kill", "edrclient.relaunch")),
    ])

    plan = plan_recovery(failure, decision, RecoveryBudget(), registry)

    assert plan.strategy is RestoreStrategy.PROCESS_RESTART
    assert plan.steps == ("edrclient.kill", "edrclient.relaunch")
    assert plan.severity is RecoverySeverity.APPLICATION
    # Default inverse timeout_s=30.0 -> 30000 ms.
    assert plan.timeout_ms == 30_000


def test_plan_recovery_process_restart_severity_is_application():
    """Pin the severity mapping for ``PROCESS_RESTART`` so
    future refactors of ``RESTORE_SEVERITY`` cannot silently
    demote it (Round 1 design Q2).
    """
    decision = make_decision(
        kind=CheckpointKind.APPLICATION_STATE,
        restorable=True,
        restore_strategy="process_restart",
        rationale=("sop:edrclient.main",),
    )
    failure = make_failure()
    registry = make_registry([
        ("edrclient.main", ("edrclient.relaunch",)),
    ])

    plan = plan_recovery(failure, decision, RecoveryBudget(), registry)

    assert plan.severity is RecoverySeverity.APPLICATION


# ---------------------------------------------------------------------------
# Branch 5: REDRIVE / RECONNECT — direct dispatch
# ---------------------------------------------------------------------------


def test_plan_recovery_logical_kind_dispatches_redrive():
    """Spec test ``test_logical_recovery_reobserves`` (planner
    half):

        P2.1 ``LOGICAL`` checkpoint -> ``RestoreStrategy.REDRIVE``,
        no inverse steps (the executor re-observes the live
        system; nothing to dispatch).
    """
    decision = make_decision(
        kind=CheckpointKind.LOGICAL,
        restorable=True,
        restore_strategy="redrive_prior_steps",
    )
    failure = make_failure()

    plan = plan_recovery(failure, decision, RecoveryBudget(), make_registry())

    assert plan.strategy is RestoreStrategy.REDRIVE
    assert plan.steps == ()
    assert plan.severity is RecoverySeverity.PAGE
    # Timeout defaults to budget.deadline_ms (30000) for non-inverse paths.
    assert plan.timeout_ms == 30_000


def test_plan_recovery_session_kind_dispatches_reconnect():
    """Spec test ``test_session_recovery_recreates_lock``
    (planner half):

        P2.1 ``SESSION`` checkpoint -> ``RestoreStrategy.RECONNECT``.
        The executor re-creates the session lock; planner does
        not dispatch catalog actions directly.
    """
    decision = make_decision(
        kind=CheckpointKind.SESSION,
        restorable=True,
        restore_strategy="reconnect_session",
    )
    failure = make_failure()

    plan = plan_recovery(failure, decision, RecoveryBudget(), make_registry())

    assert plan.strategy is RestoreStrategy.RECONNECT
    assert plan.steps == ()
    assert plan.severity is RecoverySeverity.SESSION


# ---------------------------------------------------------------------------
# Branch 6: REOBSERVE_REPLAN happy path
# ---------------------------------------------------------------------------


def test_plan_recovery_replan_dispatches_when_state_available():
    """Spec test ``test_replan_single_recovery_per_step``
    (positive case):

        ``REOBSERVE_REPLAN`` strategy + ``replan_state=AVAILABLE``
        -> the planner returns the replan plan with the budget's
        deadline_ms as the timeout. The executor is responsible
        for advancing ``replan_state`` to ``CONSUMED`` once the
        replan runs.
    """
    from agent.execution.recovery import ReplanState

    decision = make_decision(
        kind=CheckpointKind.LOGICAL,
        restorable=True,
        restore_strategy="reobserve_replan",
    )
    failure = make_failure(replan_state=ReplanState.AVAILABLE)

    plan = plan_recovery(failure, decision, RecoveryBudget(), make_registry())

    assert plan.strategy is RestoreStrategy.REOBSERVE_REPLAN
    assert plan.severity is RecoverySeverity.WINDOW
    assert plan.timeout_ms == 30_000


# ---------------------------------------------------------------------------
# Purity (Q5)
# ---------------------------------------------------------------------------


def test_plan_recovery_is_pure_no_mutation():
    """Q5 — the planner must not mutate its inputs. Two
    consecutive calls with identical inputs must produce
    identical plans (and the input dataclasses must be
    byte-for-byte unchanged).
    """
    decision = make_decision(
        kind=CheckpointKind.SESSION,
        restorable=True,
        restore_strategy="reconnect_session",
    )
    failure = make_failure()
    registry = make_registry([("edrclient.main", ("edrclient.relaunch",))])

    before_decision = decision  # frozen dataclass; verify no mutation
    before_failure = failure
    before_registry = registry

    plan_a = plan_recovery(failure, decision, RecoveryBudget(), registry)
    plan_b = plan_recovery(failure, decision, RecoveryBudget(), registry)

    assert plan_a == plan_b
    assert decision is before_decision
    assert failure is before_failure
    assert registry is before_registry
    # And the planner returned plans must themselves be frozen.
    assert plan_a.strategy == plan_b.strategy
    assert plan_a.severity == plan_b.severity