"""P2.2 contract tests — enums, dataclasses, registry, state machine.

Each test pins a specific property of the public contract layer
(Commit A). These tests are intentionally **import-only**: they
do not exercise the planner (``plan_recovery``); planner tests
live in ``test_planner.py``.

Coverage:

    * RecoverySeverity ordering (Q2)
    * RestoreStrategy coverage by RESTORE_SEVERITY (no orphan)
    * ReplanState legal / illegal transitions (Round 2 Minor 3)
    * RecoveryResult invariants (Round 2 Minor 2 parent_branch_id)
    * RecoveryBudget field defaults (Q3)
    * InverseRegistry duplicate detection
"""

from __future__ import annotations

import pytest

from agent.execution.recovery import (
    IllegalReplanTransition,
    RecoveryBudget,
    RecoveryResult,
    RecoverySeverity,
    RecoveryStatus,
    ReplanState,
    RestoreStrategy,
    advance_replan_state,
    severity_of,
)
from agent.execution.recovery_inverse import (
    DuplicateInverseError,
    InverseRegistry,
    SOPInverseAction,
)


# ---------------------------------------------------------------------------
# RecoverySeverity ordering (Q2)
# ---------------------------------------------------------------------------


def test_recovery_severity_ordering_is_strict():
    """Q2 — the IntEnum values must satisfy:

        NONE < CONTROL < PAGE < WINDOW < SESSION < APPLICATION.

    Any future addition that violates monotonic increase is a
    contract break.
    """
    sev = RecoverySeverity
    expected = [
        sev.NONE,
        sev.CONTROL,
        sev.PAGE,
        sev.WINDOW,
        sev.SESSION,
        sev.APPLICATION,
    ]
    assert expected == sorted(expected, key=lambda s: s.value)
    # And the gaps must leave room for future insertions (per the
    # design doc: 10-unit spacing).
    assert sev.APPLICATION.value - sev.NONE.value == 50
    assert all(
        b.value - a.value == 10
        for a, b in zip(expected, expected[1:])
    )


def test_restore_severity_covers_every_strategy():
    """No ``RestoreStrategy`` member may be missing from the
    severity mapping. The planner calls ``severity_of()`` for
    every plan and an un-mapped strategy would crash with
    ``KeyError`` — this test catches that class of regression
    at the contract layer.
    """
    for strategy in RestoreStrategy:
        sev = severity_of(strategy)
        assert isinstance(sev, RecoverySeverity)


# ---------------------------------------------------------------------------
# ReplanState state machine (Round 2 Minor 3)
# ---------------------------------------------------------------------------


def test_replan_state_legal_progression():
    """The legal sequence is NOT_REQUESTED -> AVAILABLE ->
    CONSUMED. Anything else is illegal.
    """
    assert advance_replan_state(
        ReplanState.NOT_REQUESTED, ReplanState.AVAILABLE
    ) is ReplanState.AVAILABLE
    assert advance_replan_state(
        ReplanState.AVAILABLE, ReplanState.CONSUMED
    ) is ReplanState.CONSUMED


@pytest.mark.parametrize(
    "src, dst",
    [
        (ReplanState.NOT_REQUESTED, ReplanState.CONSUMED),  # skip AVAILABLE
        (ReplanState.CONSUMED, ReplanState.AVAILABLE),     # re-open
        (ReplanState.CONSUMED, ReplanState.NOT_REQUESTED), # rewind
        (ReplanState.AVAILABLE, ReplanState.NOT_REQUESTED), # rewind
        # Self-transitions are also illegal; the design treats
        # the state machine as one-way and forward-only.
        (ReplanState.NOT_REQUESTED, ReplanState.NOT_REQUESTED),
        (ReplanState.AVAILABLE, ReplanState.AVAILABLE),
        (ReplanState.CONSUMED, ReplanState.CONSUMED),
    ],
)
def test_replan_state_illegal_transitions_raise(src, dst):
    """Round 2 Minor 3 — illegal transitions raise
    ``IllegalReplanTransition`` which the executor surfaces as
    the ``recovery_loop_detected`` terminal code.
    """
    with pytest.raises(IllegalReplanTransition):
        advance_replan_state(src, dst)


# ---------------------------------------------------------------------------
# RecoveryResult invariants (Round 2 Minor 2)
# ---------------------------------------------------------------------------


def test_recovery_result_forbids_error_code_on_success():
    """SUCCESS is the only status that forbids an error_code —
    the rule prevents the executor from accidentally attaching
    a stale code to a successful recovery.
    """
    with pytest.raises(ValueError, match="SUCCESS forbids error_code"):
        RecoveryResult(
            status=RecoveryStatus.SUCCESS,
            strategy=RestoreStrategy.REDRIVE,
            attempts=1,
            branch_id="BR-002",
            parent_branch_id="BR-001",
            error_code="restore_attempts_exhausted",
        )


def test_recovery_result_requires_error_code_on_failure():
    """Non-SUCCESS statuses must carry a non-None error_code.
    This makes the dataclass self-validating: the executor can
    rely on every non-SUCCESS ``RecoveryResult`` having a
    parseable code.
    """
    for status in (
        RecoveryStatus.FAILED,
        RecoveryStatus.BLOCKED,
        RecoveryStatus.REPLANNED,
    ):
        with pytest.raises(ValueError, match=f"status={status.value!r}"):
            RecoveryResult(
                status=status,
                strategy=RestoreStrategy.REDRIVE,
                attempts=2,
                branch_id="BR-002",
                parent_branch_id="BR-001",
                error_code=None,
            )


def test_recovery_result_blocked_forbids_branch_id():
    """BLOCKED is the "we did not fork a new branch" status —
    the dataclass forbids ``branch_id`` here to keep the
    lineage tree honest (a blocked recovery has no downstream
    events that need a branch id).
    """
    with pytest.raises(ValueError, match="BLOCKED forbids branch_id"):
        RecoveryResult(
            status=RecoveryStatus.BLOCKED,
            strategy=RestoreStrategy.BLOCKED,
            attempts=0,
            branch_id="BR-002",
            parent_branch_id="BR-001",
            error_code="restore_not_available",
        )


def test_recovery_result_parent_branch_id_carries_lineage():
    """Round 2 Minor 2 — the ``parent_branch_id`` field is the
    lineage pointer. A successful recovery's parent is the
    branch that triggered it; the field is non-null whenever
    ``branch_id`` is non-null.
    """
    result = RecoveryResult(
        status=RecoveryStatus.SUCCESS,
        strategy=RestoreStrategy.REDRIVE,
        attempts=1,
        branch_id="BR-002",
        parent_branch_id="BR-001",
    )
    assert result.parent_branch_id == "BR-001"
    # A no-branch recovery (e.g., the original failed branch
    # itself with no recovery action) has None for both.
    no_fork = RecoveryResult(
        status=RecoveryStatus.FAILED,
        strategy=RestoreStrategy.REDRIVE,
        attempts=1,
        branch_id=None,
        parent_branch_id="BR-001",
        error_code="restore_attempts_exhausted",
    )
    assert no_fork.branch_id is None
    assert no_fork.parent_branch_id == "BR-001"


# ---------------------------------------------------------------------------
# RecoveryBudget defaults (Q3)
# ---------------------------------------------------------------------------


def test_recovery_budget_defaults_match_design():
    """Q3 defaults locked in the design doc:

        max_attempts = 3
        max_replans  = 1   (FR-08 mapping)
        deadline_ms  = 30000
    """
    b = RecoveryBudget()
    assert b.max_attempts == 3
    assert b.max_replans == 1
    assert b.deadline_ms == 30_000


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"max_attempts": -1},
        {"max_replans": -1},
        {"deadline_ms": 0},
        {"deadline_ms": -100},
    ],
)
def test_recovery_budget_rejects_invalid_values(kwargs):
    """Defensive validation: the budget is frozen and consumed
    by the executor; catching bad values here prevents the
    executor from silently running a recovery with a nonsense
    budget.
    """
    with pytest.raises(ValueError):
        RecoveryBudget(**kwargs)


# ---------------------------------------------------------------------------
# InverseRegistry duplicates
# ---------------------------------------------------------------------------


def test_inverse_registry_rejects_duplicate_sop_id():
    """Two inverses for the same sop_id would create an
    ambiguity in the planner's lookup. The registry rejects
    with a first-vs-second path-style message (carried over
    from P2.1 ``SopIndex``'s hardening).
    """
    reg = InverseRegistry()
    reg.register(
        SOPInverseAction(
            sop_id="edrclient.main",
            inverse_action_ids=("edrclient.relaunch",),
        )
    )
    with pytest.raises(DuplicateInverseError, match="duplicate inverse"):
        reg.register(
            SOPInverseAction(
                sop_id="edrclient.main",
                inverse_action_ids=("edrclient.kill_and_restart",),
            )
        )


def test_inverse_registry_membership_and_get():
    """``has`` and ``get`` agree, and ``get`` returns ``None``
    on absence (not ``KeyError``) so the planner can probe
    without try/except.
    """
    reg = InverseRegistry()
    reg.register(
        SOPInverseAction(sop_id="edrclient.main", inverse_action_ids=())
    )
    assert reg.has("edrclient.main") is True
    assert reg.has("nonexistent") is False
    assert reg.get("edrclient.main") is not None
    assert reg.get("nonexistent") is None
    assert "edrclient.main" in reg
    assert "nonexistent" not in reg