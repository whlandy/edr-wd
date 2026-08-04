"""post_step.py — Planner-side replan trigger and bound (P3.1 Commit F).

Implements P3.1 design gate **D13** (replan trigger and
bound contract) and **FR-P3.1-07** (after a state-changing
step, the next request asks for a fresh snapshot; stale
target_refs are rejected; replan is bounded — no arbitrary
loops).

This module is the **planner-side coordinator** that answers
two questions:

  1. *Should the planner replan after a state-changing step?*
     → :func:`needs_replan`
  2. *Has the planner hit its replan budget?*
     → :func:`check_replan_budget` and :exc:`ReplanBudgetError`

Layer boundary (D13 / architecture §7.2):

  * This module does **NOT** re-execute the plan. The
    executor does. This module is pure policy.
  * This module does **NOT** mutate the plan or the
    snapshot. Both inputs are read-only.
  * This module does **NOT** dispatch to the LLM. It only
    returns a decision; the planner/executor is free to
    retry / replan / surface to the calling system.
  * The transition classifier (P2.1
    ``agent.execution.transitions``) is consulted when
    both ``snapshot_before`` and ``snapshot_after`` are
    supplied. Without ``snapshot_before``, condition 2
    (unexpected transition) is skipped — the caller chose
    not to provide the comparator snapshot.
"""

from __future__ import annotations

import datetime as _dt
import itertools
import threading
from dataclasses import dataclass
from typing import Any, Mapping

from target.protocol_models import ActionSequence, ActionStep, TargetRef
from target.observations import ObservationSnapshot


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------


CODE_REPLAN_BUDGET_EXHAUSTED: str = "replan_budget_exhausted"

DEFAULT_REPLAN_BUDGET: int = 3

REPLAN_REASON_STALE_TARGET_REF: str = "stale_target_ref"
REPLAN_REASON_UNEXPECTED_TRANSITION: str = "unexpected_transition"
REPLAN_REASON_STALE_SNAPSHOT: str = "stale_snapshot_id"
REPLAN_REASON_NO_PREVIOUS_STEP: str = "no_previous_step"
REPLAN_REASON_NO_REPLAN_NEEDED: str = "no_replan_needed"

ALL_REPLAN_REASONS: frozenset[str] = frozenset(
    {
        REPLAN_REASON_STALE_TARGET_REF,
        REPLAN_REASON_UNEXPECTED_TRANSITION,
        REPLAN_REASON_STALE_SNAPSHOT,
        REPLAN_REASON_NO_PREVIOUS_STEP,
        REPLAN_REASON_NO_REPLAN_NEEDED,
    }
)


# ---------------------------------------------------------------------------
# ReplanBudget (D13 implementation)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplanBudget:
    """Bounded replan policy.

    Per D13:

      * The bound value is implementation-defined but the
        planner MUST surface ``replan_count`` via the
        plan event.
      * When the bound is hit, the planner MUST emit
        ``code = "replan_budget_exhausted"`` and stop.

    The default ``max_attempts=3`` matches the example in
    D13; deployments may configure a larger budget without
    changing this module.
    """

    max_attempts: int = DEFAULT_REPLAN_BUDGET

    def __post_init__(self) -> None:
        if not isinstance(self.max_attempts, int):
            raise TypeError(
                f"max_attempts must be int, "
                f"got {type(self.max_attempts).__name__}"
            )
        if self.max_attempts < 1:
            raise ValueError(
                f"max_attempts must be >= 1, got {self.max_attempts}"
            )


# ---------------------------------------------------------------------------
# ReplanDecision (returned by needs_replan)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplanDecision:
    """Decision returned by :func:`needs_replan`.

    Fields:

      * ``needed`` — ``True`` iff a replan should be triggered.
      * ``reason`` — one of the stable :data:`ALL_REPLAN_REASONS`.
      * ``replan_id`` — sortable unique id for **this attempt**.
        Format ``<utc-timestamp-ms>:<random-suffix>``.
      * ``replan_count`` — total replan count **including this
        attempt** (1-indexed). ``0`` means "no replan yet"
        (initial attempt); values ``1..max_attempts`` are
        within budget; ``>max_attempts`` would have triggered
        :exc:`ReplanBudgetError` before this function returned.

    Implements the D13 hierarchy:

      * ``run_id`` (caller-supplied, out of scope here)
      * ``step_id``
        * ``replan_id`` (each attempt; monotonic within a step)
        * ``event_id`` (each event within the attempt — out of
          scope here)
    """

    needed: bool
    reason: str
    replan_id: str
    replan_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.needed, bool):
            raise TypeError(
                f"needed must be bool, got {type(self.needed).__name__}"
            )
        if self.reason not in ALL_REPLAN_REASONS:
            raise ValueError(
                f"reason {self.reason!r} not in ALL_REPLAN_REASONS"
            )
        if not isinstance(self.replan_id, str) or not self.replan_id:
            raise ValueError(
                f"replan_id must be non-empty str, got {self.replan_id!r}"
            )
        if (
            not isinstance(self.replan_count, int)
            or self.replan_count < 0
        ):
            raise ValueError(
                f"replan_count must be int >= 0, "
                f"got {self.replan_count}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "needed": self.needed,
            "reason": self.reason,
            "replan_id": self.replan_id,
            "replan_count": self.replan_count,
        }


# ---------------------------------------------------------------------------
# ReplanBudgetError
# ---------------------------------------------------------------------------


class ReplanBudgetError(Exception):
    """Raised when the replan budget is exhausted.

    Per D13, callers map this exception to the plan event:

      * ``code = "replan_budget_exhausted"``
      * ``replan_id`` = the most recent decision's replan_id
      * ``replan_count`` = the count that exceeded the budget

    The decision is **not** silently dropped — the calling
    system is responsible for surfacing it to the user /
    trace / events so downstream tooling can correlate.
    """

    def __init__(self, replan_count: int, budget: ReplanBudget) -> None:
        if not isinstance(replan_count, int):
            raise TypeError(
                f"replan_count must be int, "
                f"got {type(replan_count).__name__}"
            )
        self.replan_count = replan_count
        self.budget = budget
        super().__init__(
            f"replan budget exhausted: count={replan_count}, "
            f"max_attempts={budget.max_attempts}"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def generate_replan_id() -> str:
    """Generate a sortable unique replan id.

    Format::

        <utc-timestamp-ms>:<monotonic-counter>

    The timestamp prefix (``YYYYMMDDTHHMMSSfff`` truncated to
    milliseconds — 18 chars) ensures **lexicographic
    sortability** — replan ids created later in the same run
    sort after earlier ones. The monotonic counter (4-digit hex,
    process-global) guarantees uniqueness within a single
    millisecond where the timestamp alone would collide.

    This is **not a security token** — replan_id is a per-attempt
    trace handle, not an authentication secret.
    """
    # `%Y%m%dT%H%M%S%f` is 21 chars (microsecond precision);
    # truncate to milliseconds by removing last 3 digits.
    ts = _dt.datetime.now(_dt.timezone.utc).strftime(
        "%Y%m%dT%H%M%S%f"
    )[:-3]
    counter = format(_next_counter(), "04x")
    return f"{ts}:{counter}"


# Monotonic counter for replan_id uniqueness within a millisecond.
# Python's ``id()`` is unreliable because the GC recycles small
# object addresses immediately. A process-global counter is
# deterministic, sortable, and survives concurrent calls
# (a Lock is overkill here — `itertools.count` is GIL-safe).
_replan_counter = itertools.count(1)
_replan_counter_lock = threading.Lock()


def _next_counter() -> int:
    """Atomically increment the process-global replan counter."""
    with _replan_counter_lock:
        return next(_replan_counter)


def check_replan_budget(
    replan_count: int,
    budget: ReplanBudget,
) -> None:
    """Raise :exc:`ReplanBudgetError` if ``replan_count`` exceeds budget.

    Per D13, the semantics are:

      * ``replan_count == 0`` → no replan yet (initial attempt).
      * ``1 <= replan_count <= max_attempts`` → within budget.
      * ``replan_count > max_attempts`` → exhausted.

    Callers should invoke this **before** accepting a new
    replan attempt so the bound is enforced uniformly.
    """
    if replan_count > budget.max_attempts:
        raise ReplanBudgetError(replan_count, budget)


def _target_in_snapshot(
    target_ref: TargetRef,
    snapshot: ObservationSnapshot,
) -> bool:
    """True iff ``target_ref.target_id`` appears in the fresh snapshot.

    This is the canonical "stale target_ref" check (D13
    condition 1). It uses ``target_id`` equality only —
    fingerprints may differ (e.g. UI re-render); what matters
    is that the same logical target survived.
    """
    for t in snapshot.targets:
        if t.target_id == target_ref.target_id:
            return True
    return False


def _classify_transition_kind(
    snapshot_before: ObservationSnapshot,
    snapshot_after: ObservationSnapshot,
) -> str:
    """Return a stable transition kind string.

    Imports the P2.1 classifier lazily to avoid a hard
    dependency cycle (``agent.execution`` already imports
    from ``target.observations``; this module is imported
    by ``agent.planner``). When ``classify_transition`` is
    unavailable, falls back to ``unknown_material_change``
    so the planner's replan signal remains well-formed.
    """
    try:
        from agent.execution.transitions import classify_transition
    except Exception:
        return "unknown_material_change"
    result = classify_transition(snapshot_before, snapshot_after)
    return result.kind.value


# ---------------------------------------------------------------------------
# Main entry: needs_replan
# ---------------------------------------------------------------------------


def needs_replan(
    prev_plan: ActionSequence,
    snapshot_after: ObservationSnapshot,
    *,
    snapshot_before: ObservationSnapshot | None = None,
    snapshot_id: str | None = None,
    step_id: str | None = None,
    current_count: int = 0,
    budget: ReplanBudget | None = None,
) -> ReplanDecision:
    """Decide whether a replan is needed after a state-changing step.

    Implements D13 contract:

      Replan MUST be triggered when ANY of the following holds
      after a state-changing step:

      1. The previous step's ``target_ref`` is stale (no longer
         present in the fresh snapshot).
      2. The previous step's action succeeded but the snapshot
         shows an unexpected transition (transition classification
         ≠ what the step declared). Requires ``snapshot_before``
         to be supplied; without it, condition 2 is skipped.
      3. The planner is invoked with a stale ``snapshot_id`` —
         the supplied snapshot's id differs from the plan's
         expected snapshot id.

    The function enforces the bound *before* checking
    triggers: if ``current_count`` already exceeds
    ``budget.max_attempts``, :exc:`ReplanBudgetError` is
    raised. The caller is expected to catch and surface
    ``code = "replan_budget_exhausted"``.

    Args:
        prev_plan: the previously dispatched plan.
        snapshot_after: the fresh snapshot taken after the
            state-changing step.
        snapshot_before: optional comparator snapshot used
            for condition 2. If ``None``, condition 2 is
            skipped (the caller chose not to provide it).
        snapshot_id: the **expected** snapshot id. If the
            fresh snapshot's id differs, replan is triggered
            (condition 3). If ``None``, condition 3 is
            skipped.
        step_id: which step in ``prev_plan`` to evaluate.
            If ``None``, the last step is used.
        current_count: number of replans already attempted
            for this step. The returned decision increments
            this by 1 only when ``needed=True``.
        budget: the budget policy; default
            :class:`ReplanBudget` (``max_attempts=3``).

    Returns:
        :class:`ReplanDecision` with ``needed=True`` and a
        stable ``reason``, or ``needed=False`` with
        ``reason="no_replan_needed"``.

    Raises:
        ReplanBudgetError: if ``current_count`` already
            exceeds ``budget.max_attempts``.
        ValueError: if ``step_id`` is supplied but not found
            in ``prev_plan.steps``, or if ``prev_plan.steps``
            is empty AND ``step_id`` is None.
    """
    budget = budget or ReplanBudget()
    check_replan_budget(current_count, budget)

    # Pick the step to evaluate.
    target_step: ActionStep | None
    if step_id is not None:
        target_step = None
        for s in prev_plan.steps:
            if s.step_id == step_id:
                target_step = s
                break
        if target_step is None:
            raise ValueError(
                f"step_id {step_id!r} not found in prev_plan "
                f"(have {[s.step_id for s in prev_plan.steps]!r})"
            )
    else:
        if not prev_plan.steps:
            return ReplanDecision(
                needed=False,
                reason=REPLAN_REASON_NO_PREVIOUS_STEP,
                replan_id=generate_replan_id(),
                replan_count=current_count,
            )
        target_step = prev_plan.steps[-1]

    new_replan_id = generate_replan_id()

    # Condition 3: stale snapshot_id (caller supplied an expected id
    # and the fresh snapshot's id differs).
    if (
        snapshot_id is not None
        and snapshot_after.snapshot_id != snapshot_id
    ):
        return ReplanDecision(
            needed=True,
            reason=REPLAN_REASON_STALE_SNAPSHOT,
            replan_id=new_replan_id,
            replan_count=current_count + 1,
        )

    # Condition 1: stale target_ref (no longer in fresh snapshot).
    if target_step.target_ref is not None and not _target_in_snapshot(
        target_step.target_ref, snapshot_after
    ):
        return ReplanDecision(
            needed=True,
            reason=REPLAN_REASON_STALE_TARGET_REF,
            replan_id=new_replan_id,
            replan_count=current_count + 1,
        )

    # Condition 2: unexpected transition.
    # Requires both snapshots AND a declared transition on the step.
    if (
        snapshot_before is not None
        and target_step.transition is not None
        and target_step.transition.expected
    ):
        observed_kind = _classify_transition_kind(
            snapshot_before, snapshot_after
        )
        declared_kind = target_step.transition.kind
        if observed_kind != declared_kind:
            return ReplanDecision(
                needed=True,
                reason=REPLAN_REASON_UNEXPECTED_TRANSITION,
                replan_id=new_replan_id,
                replan_count=current_count + 1,
            )

    return ReplanDecision(
        needed=False,
        reason=REPLAN_REASON_NO_REPLAN_NEEDED,
        replan_id=new_replan_id,
        replan_count=current_count,
    )


__all__ = [
    # constants
    "CODE_REPLAN_BUDGET_EXHAUSTED",
    "DEFAULT_REPLAN_BUDGET",
    "REPLAN_REASON_STALE_TARGET_REF",
    "REPLAN_REASON_UNEXPECTED_TRANSITION",
    "REPLAN_REASON_STALE_SNAPSHOT",
    "REPLAN_REASON_NO_PREVIOUS_STEP",
    "REPLAN_REASON_NO_REPLAN_NEEDED",
    "ALL_REPLAN_REASONS",
    # dataclasses
    "ReplanBudget",
    "ReplanDecision",
    # exceptions
    "ReplanBudgetError",
    # helpers
    "generate_replan_id",
    "check_replan_budget",
    # main
    "needs_replan",
]