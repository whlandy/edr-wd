"""branch_events.py — typed payloads for branch + replan trace events (P2.2 — Commit F).

Round 2 review (#4: Trace events + branch ancestry) required
the recovery cycle to emit:

    * ``branch_created`` — when a recovery branch is forked
      from a checkpoint.
    * ``replan_created`` — when a replan is requested
      (strategy REOBSERVE_REPLAN succeeded; caller will
      create a new plan).

Commit E (N2) froze the ``RequestedEvent`` schema with a
typed payload. Commit F extends that pattern with two new
typed payloads — ``BranchCreatedPayload`` and
``ReplanCreatedPayload`` — and their constructor helpers.

Boundary:

    * This module does **not** import :class:`TraceStore`.
      Forwarding is the adapter's job
      (:mod:`agent.execution.trace_adapter`).
    * This module does **not** own branch ancestry tracking.
      Branch lineage is :mod:`agent.execution.branch_ancestry`'s
      job; this module just records the typed payload.
    * This module does **not** call ``resume_from_checkpoint()``.
      Branch creation is the caller's responsibility; this
      module just provides the typed payload.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.execution.recovery import (
    Branch,
    ReplanState,
    RestoreStrategy,
)


# ---------------------------------------------------------------------------
# Event-type strings
# ---------------------------------------------------------------------------


EVENT_TYPE_BRANCH_CREATED = "branch_created"
EVENT_TYPE_REPLAN_CREATED = "replan_created"


# ---------------------------------------------------------------------------
# BranchCreatedPayload
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BranchCreatedPayload:
    """Typed payload for a ``branch_created`` event.

    Attributes:
        branch_id: The new branch's id.
        forked_from_event_id: The trace event id at which
            this branch was forked.
        forked_from_checkpoint_id: The checkpoint id at
            which this branch was forked.
        parent_branch_id: The branch id of the parent
            (``None`` for the root branch).
        head_event_id: The first event id on the new
            branch (typically the ``trace_started`` event).
    """

    branch_id: str
    forked_from_event_id: str
    forked_from_checkpoint_id: str
    parent_branch_id: str | None
    head_event_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "branch_id": self.branch_id,
            "forked_from_event_id": self.forked_from_event_id,
            "forked_from_checkpoint_id": self.forked_from_checkpoint_id,
            "parent_branch_id": self.parent_branch_id,
            "head_event_id": self.head_event_id,
        }


def make_branch_created_event(branch: Branch) -> "RequestedEvent[BranchCreatedPayload]":
    """Build a :class:`RequestedEvent` from a :class:`Branch`.

    The event_type is ``EVENT_TYPE_BRANCH_CREATED`` (maps to
    :class:`EventType.BRANCH_CREATED`).
    """
    from agent.execution.recovery_events import RequestedEvent
    payload = BranchCreatedPayload(
        branch_id=branch.branch_id,
        forked_from_event_id=branch.forked_from_event_id,
        forked_from_checkpoint_id=branch.forked_from_checkpoint_id,
        parent_branch_id=branch.parent_branch_id,
        head_event_id=branch.head_event_id,
    )
    return RequestedEvent(
        event_type=EVENT_TYPE_BRANCH_CREATED,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# ReplanCreatedPayload
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplanCreatedPayload:
    """Typed payload for a ``replan_created`` event.

    Attributes:
        branch_id: The branch that consumed the replan.
        parent_branch_id: The branch that triggered the
            replan (typically the recovery branch).
        strategy: The restore strategy that initiated the
            replan (``REOBSERVE_REPLAN``).
        replan_state: The replan state machine position
            (``AVAILABLE`` when the replan is offered,
            ``CONSUMED`` after a plan is produced).
        step_id: The step id at which the replan was created.
        detail: Free-form rationale (planner's reason).
    """

    branch_id: str
    parent_branch_id: str | None
    strategy: RestoreStrategy
    replan_state: ReplanState
    step_id: str
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "branch_id": self.branch_id,
            "parent_branch_id": self.parent_branch_id,
            "strategy": self.strategy.value,
            "replan_state": self.replan_state.value,
            "step_id": self.step_id,
            "detail": self.detail,
        }


def make_replan_created_event(
    *,
    branch_id: str,
    parent_branch_id: str | None,
    strategy: RestoreStrategy,
    replan_state: ReplanState,
    step_id: str,
    detail: str = "",
) -> "RequestedEvent[ReplanCreatedPayload]":
    """Build a :class:`RequestedEvent` for a ``replan_created``.

    Typically called by the caller's replan handler after a
    successful REOBSERVE_REPLAN cycle completes.
    """
    from agent.execution.recovery_events import RequestedEvent
    payload = ReplanCreatedPayload(
        branch_id=branch_id,
        parent_branch_id=parent_branch_id,
        strategy=strategy,
        replan_state=replan_state,
        step_id=step_id,
        detail=detail,
    )
    return RequestedEvent(
        event_type=EVENT_TYPE_REPLAN_CREATED,
        payload=payload,
    )


__all__ = [
    "BranchCreatedPayload",
    "ReplanCreatedPayload",
    "EVENT_TYPE_BRANCH_CREATED",
    "EVENT_TYPE_REPLAN_CREATED",
    "make_branch_created_event",
    "make_replan_created_event",
]