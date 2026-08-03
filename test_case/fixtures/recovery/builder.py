"""Recovery test fixtures — builders for FailureContext / CheckpointDecision / InverseRegistry.

Mirrors :mod:`test_case.fixtures.transitions.builder` (P2.1) but
for the P2.2 recovery surface. Tests construct inputs without
copy-pasting dataclass construction every time.

Usage::

    from test_case.fixtures.recovery.builder import (
        make_decision, make_failure, make_inverse, make_registry,
    )

    decision = make_decision(
        kind=CheckpointKind.APPLICATION_STATE,
        restorable=True,
        restore_strategy="process_restart",
        rationale=("sop:edrclient.main",),
    )
    failure = make_failure(replan_state=ReplanState.NOT_REQUESTED)
    registry = make_registry(("edrclient.main", ("edrclient.relaunch",)))

    plan = plan_recovery(failure, decision, RecoveryBudget(), registry)
    assert plan.strategy is RestoreStrategy.PROCESS_RESTART

These builders return plain dataclass instances, not mocks. The
planner under test does no I/O so the inputs are pure data.
"""

from __future__ import annotations

from typing import Iterable

from agent.execution.checkpoints import (
    CheckpointDecision,
    CheckpointKind,
)
from agent.execution.recovery import (
    FailureContext,
    ReplanState,
)
from agent.execution.recovery_inverse import (
    InverseRegistry,
    SOPInverseAction,
)


_DEFAULT_STEP_ID = "step-fixture"
_DEFAULT_BRANCH_ID = "BR-fixture"
_DEFAULT_FAILURE_KIND = "fixture_failure"
_DEFAULT_SOP_ID = "fixture.sop"


def make_decision(
    *,
    kind: CheckpointKind = CheckpointKind.LOGICAL,
    restorable: bool = True,
    restore_strategy: str | None = "redrive_prior_steps",
    rationale: tuple[str, ...] = ("step:checkpoint_before",),
) -> CheckpointDecision:
    """Build a P2.1 ``CheckpointDecision``.

    Defaults mirror the common "logical checkpoint, redrive"
    scenario. Override ``restore_strategy`` to test strategy
    mapping in the planner.
    """
    return CheckpointDecision(
        kind=kind,
        restorable=restorable,
        restore_strategy=restore_strategy,
        rationale=rationale,
    )


def make_failure(
    *,
    step_id: str = _DEFAULT_STEP_ID,
    branch_id: str = _DEFAULT_BRANCH_ID,
    failure_kind: str = _DEFAULT_FAILURE_KIND,
    error_code: str | None = None,
    detail: str | None = None,
    snapshot_id_before: str | None = None,
    snapshot_id_after: str | None = None,
    replan_state: ReplanState = ReplanState.NOT_REQUESTED,
) -> FailureContext:
    """Build a ``FailureContext`` with sensible defaults.

    Defaults model a "step just failed, no replan requested yet"
    scenario — the most common starting state for the planner.
    """
    return FailureContext(
        step_id=step_id,
        branch_id=branch_id,
        failure_kind=failure_kind,
        error_code=error_code,
        detail=detail,
        snapshot_id_before=snapshot_id_before,
        snapshot_id_after=snapshot_id_after,
        replan_state=replan_state,
    )


def make_inverse(
    *,
    sop_id: str = _DEFAULT_SOP_ID,
    inverse_action_ids: tuple[str, ...] = ("sop.reload",),
    expected_after_sop_id: str | None = None,
    timeout_s: float = 30.0,
    tags: frozenset[str] = frozenset(),
) -> SOPInverseAction:
    """Build an ``SOPInverseAction`` with sensible defaults."""
    return SOPInverseAction(
        sop_id=sop_id,
        inverse_action_ids=inverse_action_ids,
        expected_after_sop_id=expected_after_sop_id,
        timeout_s=timeout_s,
        tags=tags,
    )


def make_registry(
    entries: Iterable[tuple[str, tuple[str, ...]]] = (),
) -> InverseRegistry:
    """Build an ``InverseRegistry`` populated from ``entries``.

    Each entry is ``(sop_id, inverse_action_ids)``. Tests that
    need a single inverse can do::

        reg = make_registry([("edrclient.main", ("edrclient.relaunch",))])

    Tests that need an empty registry can call with no args.
    """
    reg = InverseRegistry()
    for sop_id, action_ids in entries:
        reg.register(
            SOPInverseAction(sop_id=sop_id, inverse_action_ids=action_ids)
        )
    return reg