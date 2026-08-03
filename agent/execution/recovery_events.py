"""recovery_events.py — Typed event payload contracts (P2.2 — Commit E, N2).

Round 2 review N2 (Commit D verdict) required freezing the
``RequestedEvent`` schema so downstream Commit F (TraceStore
wiring) and Commit G (projections) build on stable types.

This module owns:

    * :class:`RequestedEvent` — the envelope the executor emits.
      Carries an ``event_type`` discriminator and a typed
      ``payload`` (``RecoveryRequestedPayload`` or
      ``RecoveryResultPayload``).
    * :class:`RecoveryRequestedPayload` / :class:`RecoveryResultPayload`
      — frozen dataclasses representing the structured payload
      each event carries.

The envelope's ``.to_dict()`` returns the wire shape consumed by
``TraceStore.append_dict``. Commit E wires this via
:mod:`agent.execution.trace_adapter`.

Adding a new event type:

    1. Add a new payload dataclass below.
    2. Update the ``EventType`` dispatch in
       :class:`agent.execution.trace_adapter.TraceStoreAdapter`.
    3. Add a constructor in the executor or caller.

Do **not** build ad-hoc dict keys — frozen payload classes are
the contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping

from agent.execution.recovery import (
    RecoveryBudget,
    RecoveryErrorCode,
    RecoverySeverity,
    RecoveryStatus,
    RestoreStrategy,
)


if TYPE_CHECKING:
    # Forward refs for the typed payload union. ``branch_events``
    # defines these; importing at runtime would create a cycle.
    from agent.execution.branch_events import (
        BranchCreatedPayload,
        ReplanCreatedPayload,
    )


# ---------------------------------------------------------------------------
# Payload dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecoveryRequestedPayload:
    """Payload of a ``recovery_requested`` event.

    Attributes:
        step_id: The step the recovery is for.
        branch_id: The recovery branch (may be ``None`` when the
            planner returned ``BLOCKED`` and no branch was forked).
        parent_branch_id: The branch that triggered recovery.
        strategy: The :class:`RestoreStrategy` the executor chose.
        severity: The :class:`RecoverySeverity` of the strategy.
        attempts_so_far: Count of attempts consumed before this
            one (``0`` on first attempt).
        replans_so_far: Count of replans consumed before this one.

    Wire invariant: every field is JSON-serialisable.
    """

    step_id: str
    branch_id: str | None
    parent_branch_id: str | None
    strategy: RestoreStrategy
    severity: RecoverySeverity
    attempts_so_far: int
    replans_so_far: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "branch_id": self.branch_id,
            "parent_branch_id": self.parent_branch_id,
            "strategy": self.strategy.value,
            "severity": self.severity.value,
            "attempts_so_far": self.attempts_so_far,
            "replans_so_far": self.replans_so_far,
        }


@dataclass(frozen=True)
class RecoveryResultPayload:
    """Payload of a ``recovery_result`` event.

    Attributes:
        step_id: The step the recovery was for.
        branch_id: The recovery branch (``None`` for terminal
            ``BLOCKED`` outcomes where no branch was forked).
        parent_branch_id: The branch that triggered recovery.
        strategy: The :class:`RestoreStrategy` actually used.
        status: The :class:`RecoveryStatus` of the outcome.
        attempts: Count of attempts consumed by this cycle.
        elapsed_ms: Wall-clock duration of the dispatch (handler
            call only — pre/post flight are excluded).
        error_code: String form of :class:`RecoveryErrorCode`
            (``None`` on success).
        detail: Free-text detail (handler error message, terminal
            reason, etc.). Empty string when not applicable.
    """

    step_id: str
    branch_id: str | None
    parent_branch_id: str | None
    strategy: RestoreStrategy
    status: RecoveryStatus
    attempts: int
    elapsed_ms: int
    error_code: str | None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "branch_id": self.branch_id,
            "parent_branch_id": self.parent_branch_id,
            "strategy": self.strategy.value,
            "status": self.status.value,
            "attempts": self.attempts,
            "elapsed_ms": self.elapsed_ms,
            "error_code": self.error_code,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


# Event type discriminator strings. The mapping to the trace
# store's :class:`EventType` enum lives in
# :class:`agent.execution.trace_adapter.TraceStoreAdapter`.
EVENT_TYPE_RECOVERY_REQUESTED = "recovery_requested"
EVENT_TYPE_RECOVERY_RESULT = "recovery_result"


@dataclass(frozen=True)
class RequestedEvent:
    """A trace event the executor wants written.

    The envelope holds a typed payload (see P2.2 Commit E N2
    freeze for the recovery-cycle payloads, and Commit F for
    branch + replan payloads):

    * :class:`RecoveryRequestedPayload`
    * :class:`RecoveryResultPayload`
    * :class:`BranchCreatedPayload`
    * :class:`ReplanCreatedPayload`

    Wire serialization uses ``payload.to_dict()``.

    Construct via the helpers
    (:func:`make_recovery_requested_event`,
    :func:`make_recovery_result_event`,
    :func:`make_branch_created_event`,
    :func:`make_replan_created_event`)
    rather than direct dataclass construction so the
    event_type matches the payload class.

    Commit D shipped this as ``RequestedEvent(event_type: str,
    payload: Mapping[str, object])``. Commit E replaced the
    ``Mapping`` payload with a typed payload (N2 freeze).
    Commit F broadened the payload union to include the
    branch + replan events.
    """

    event_type: str
    payload: (
        RecoveryRequestedPayload
        | RecoveryResultPayload
        | "BranchCreatedPayload"
        | "ReplanCreatedPayload"
    )

    def to_dict(self) -> dict[str, Any]:
        return self.payload.to_dict()


# ---------------------------------------------------------------------------
# Constructors
# ---------------------------------------------------------------------------


def make_recovery_requested_event(
    *,
    step_id: str,
    branch_id: str | None,
    parent_branch_id: str | None,
    strategy: RestoreStrategy,
    severity: RecoverySeverity,
    attempts_so_far: int,
    replans_so_far: int,
) -> RequestedEvent:
    """Construct a ``recovery_requested`` envelope."""
    return RequestedEvent(
        event_type=EVENT_TYPE_RECOVERY_REQUESTED,
        payload=RecoveryRequestedPayload(
            step_id=step_id,
            branch_id=branch_id,
            parent_branch_id=parent_branch_id,
            strategy=strategy,
            severity=severity,
            attempts_so_far=attempts_so_far,
            replans_so_far=replans_so_far,
        ),
    )


def make_recovery_result_event(
    *,
    step_id: str,
    branch_id: str | None,
    parent_branch_id: str | None,
    strategy: RestoreStrategy,
    status: RecoveryStatus,
    attempts: int,
    elapsed_ms: int,
    error_code: str | None,
    detail: str = "",
) -> RequestedEvent:
    """Construct a ``recovery_result`` envelope."""
    return RequestedEvent(
        event_type=EVENT_TYPE_RECOVERY_RESULT,
        payload=RecoveryResultPayload(
            step_id=step_id,
            branch_id=branch_id,
            parent_branch_id=parent_branch_id,
            strategy=strategy,
            status=status,
            attempts=attempts,
            elapsed_ms=elapsed_ms,
            error_code=error_code,
            detail=detail,
        ),
    )


__all__ = [
    "RequestedEvent",
    "RecoveryRequestedPayload",
    "RecoveryResultPayload",
    "EVENT_TYPE_RECOVERY_REQUESTED",
    "EVENT_TYPE_RECOVERY_RESULT",
    "make_recovery_requested_event",
    "make_recovery_result_event",
]