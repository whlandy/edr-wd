"""recovery.py — P2.2 recovery planner contracts (architecture §15.3).

This module is the **public contract layer** for P2.2. It contains:

    * Strategy / status / state enumerations
    * Frozen dataclasses for the planner's inputs and outputs
    * The two-layer ``RecoveryBudget`` (Round 1 design Q3)
    * The ``RecoverySeverity`` IntEnum + the ``RESTORE_SEVERITY``
      mapping (Round 1 design Q2; placed in a dict, NOT co-located
      on ``RestoreStrategy`` values, per Round 2 review Minor 1 —
      cleaner for trace serialization)

No recovery **logic** lives here. The pure ``plan_recovery()``
function is added in Commit B.

Design doc: ``docs/requirements/P2-recovery-planner.md`` §7.

Why split contracts vs logic:

    * Contracts can be unit-tested in isolation (import-only tests).
    * Logic tests can assume the contract surface is stable.
    * Trace code can depend on contracts without depending on
      planner internals.

Boundary with P2.1 (``agent.execution.checkpoints``):

    * ``CheckpointDecision`` is the input from P2.1.
    * This module never mutates it. The planner only consumes the
      ``kind``, ``restorable``, and ``restore_strategy`` fields.

Boundary with the executor:

    * The executor owns ``decide_checkpoint()`` orchestration
      (Q5: planner is pure; executor decides when to re-trigger).
    * This module emits ``RecoveryPlan`` and ``RecoveryResult``
      dataclasses only. It does not call ``decide_checkpoint``
      itself.
"""

from __future__ import annotations

from typing import Iterable

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any


# ---------------------------------------------------------------------------
# RestoreStrategy — plain str Enum for clean trace serialization
# ---------------------------------------------------------------------------


class RestoreStrategy(str, Enum):
    """P2.2 restore strategy enumeration.

    Plain ``str, Enum`` (Round 2 review Minor 1) so each value's
    ``.value`` is exactly the wire string used in trace events
    and ``step-results.json``. Severity is **not** co-located on
    the enum value — see ``RESTORE_SEVERITY`` below.

    Lifecycle:

        NONE              — no recovery semantics
        REDRIVE           — re-observe; re-validate remaining steps
                            (P2.1 ``LOGICAL`` checkpoint default)
        RECONNECT         — session lock re-established
                            (P2.1 ``SESSION`` checkpoint default)
        PROCESS_RESTART   — application-state inverse per SOP
                            (P2.1 ``APPLICATION_STATE``; P2.2-tested)
        REOBSERVE_REPLAN  — FR-08 one-shot replan
        BLOCKED           — ``restorable=False`` or budget exhausted
    """

    NONE = "none"
    REDRIVE = "redrive_prior_steps"
    RECONNECT = "reconnect_session"
    PROCESS_RESTART = "process_restart"
    REOBSERVE_REPLAN = "reobserve_replan"
    BLOCKED = "blocked"


# ---------------------------------------------------------------------------
# RecoverySeverity — IntEnum + separate mapping
# ---------------------------------------------------------------------------


class RecoverySeverity(IntEnum):
    """Recovery aggressiveness ordering (Round 1 design Q2).

    Higher value = more invasive strategy. Used as the tiebreaker
    for multi-strategy conflicts: the **most-restrictive** strategy
    wins (``max(proposed_severities)``).

    Future strategies may share a severity without being
    one-to-one (Round 2 review). Severity lives in ``RecoverySeverity``
    only; strategies are mapped via ``RESTORE_SEVERITY``.
    """

    NONE = 0
    CONTROL = 10
    PAGE = 20
    WINDOW = 30
    SESSION = 40
    APPLICATION = 50


# Strategy -> severity mapping (NOT co-located on the enum value).
# Future strategies can share severities without needing a new
# enum subclass — just add another mapping line.
RESTORE_SEVERITY: dict[RestoreStrategy, RecoverySeverity] = {
    RestoreStrategy.NONE: RecoverySeverity.NONE,
    RestoreStrategy.REDRIVE: RecoverySeverity.PAGE,
    RestoreStrategy.RECONNECT: RecoverySeverity.SESSION,
    RestoreStrategy.PROCESS_RESTART: RecoverySeverity.APPLICATION,
    RestoreStrategy.REOBSERVE_REPLAN: RecoverySeverity.WINDOW,
    RestoreStrategy.BLOCKED: RecoverySeverity.NONE,
}


def severity_of(strategy: RestoreStrategy) -> RecoverySeverity:
    """Look up the severity for a strategy.

    Always returns a value because ``RESTORE_SEVERITY`` covers
    every ``RestoreStrategy`` member. Provided as a function (not
    a dict membership check) so future strategies can compute
    severity dynamically without breaking callers.
    """
    return RESTORE_SEVERITY[strategy]


def strategies_for_severity(
    severity: RecoverySeverity,
) -> tuple[RestoreStrategy, ...]:
    """Round 3 A1 — reverse lookup.

    Return every :class:`RestoreStrategy` whose severity equals
    ``severity``, in :data:`RESTORE_SEVERITY` declaration order.

    Use case: the executor may surface "give me every strategy
    that could be classified at PAGE level" when deciding which
    restore strategy to retry after a partial failure.

    The result is deterministic (dict insertion order in modern
    CPython) so callers can rely on stable iteration.
    """
    return tuple(
        strategy
        for strategy, mapped_severity in RESTORE_SEVERITY.items()
        if mapped_severity == severity
    )


def resolve_strategy(
    strategies: Iterable[RestoreStrategy],
) -> RestoreStrategy:
    """Round 3 B2 — Q2 conflict resolver.

    Given a non-empty iterable of :class:`RestoreStrategy`
    candidates (typically emitted by multiple decision sources),
    return the **most-restrictive** strategy, where "restrictive"
    is defined by :class:`RecoverySeverity` ordering:

        APPLICATION > SESSION > WINDOW > PAGE > CONTROL > NONE

    BLOCKED is **not** a candidate: callers must filter BLOCKED
    out before calling. If any BLOCKED is present, raise
    :class:`ValueError` so the caller is forced to handle the
    terminal explicitly rather than silently ignoring it.

    Tie-breaking (same severity): the first registered strategy
    in :data:`RESTORE_SEVERITY` wins. This matches the design
    doc's "first registered source wins" rule (§5.2).

    The function is **pure**: same input -> same output,
    deterministic, no I/O.
    """
    seen: list[RestoreStrategy] = []
    for s in strategies:
        if s is RestoreStrategy.BLOCKED:
            raise ValueError(
                "resolve_strategy() received BLOCKED; "
                "filter BLOCKED before calling (it is terminal, "
                "not a candidate)."
            )
        seen.append(s)

    if not seen:
        raise ValueError(
            "resolve_strategy() requires at least one non-BLOCKED "
            "candidate; got an empty iterable."
        )

    # Sort by severity descending, then by RESTORE_SEVERITY
    # declaration order (Python 3.7+ dict preserves insertion
    # order, so enumerate gives us a stable tie-breaker).
    declaration_order = {s: i for i, s in enumerate(RESTORE_SEVERITY)}
    seen.sort(
        key=lambda s: (-severity_of(s).value, declaration_order[s])
    )
    return seen[0]


# ---------------------------------------------------------------------------
# RecoveryStatus — top-level outcome of a recovery cycle
# ---------------------------------------------------------------------------


class RecoveryStatus(str, Enum):
    """Top-level outcome of a recovery attempt (per step, per branch).

    Wire values are short and stable; downstream consumers
    (trace, report, dashboards) match on these strings.
    """

    SUCCESS = "success"      # restore + expectation check passed
    FAILED = "failed"        # restore ran but expectation check failed
    BLOCKED = "blocked"      # restorable=False or budget exhausted
    REPLANNED = "replanned"  # FR-08: emit replan_created, stop


# ---------------------------------------------------------------------------
# ReplanState — one-shot FR-08 enforcement
# ---------------------------------------------------------------------------


_LEGAL_REPLAN_TRANSITIONS: frozenset[tuple[str, str]] = frozenset({
    ("not_requested", "available"),
    ("available", "consumed"),
})


class ReplanState(str, Enum):
    """Per-step replan budget tracker (Q3 / FR-08).

    Legal sequence:

        NOT_REQUESTED -> AVAILABLE -> CONSUMED

    Forbidden (Round 2 review Minor 3 — enforced via
    :func:`advance_replan_state`):

        * ``CONSUMED -> AVAILABLE``   (cannot re-open)
        * ``NOT_REQUESTED -> CONSUMED`` (cannot skip AVAILABLE)

    Skipping the legal sequence surfaces as the terminal code
    ``recovery_loop_detected``.
    """

    NOT_REQUESTED = "not_requested"
    AVAILABLE = "available"
    CONSUMED = "consumed"


def advance_replan_state(current: ReplanState, target: ReplanState) -> ReplanState:
    """Validate and perform a replan-state transition.

    Returns ``target`` if the transition is legal; raises
    :class:`IllegalReplanTransition` otherwise. Callers should
    treat the exception as the trigger for the
    ``recovery_loop_detected`` terminal code.
    """
    edge = (current.value, target.value)
    if edge not in _LEGAL_REPLAN_TRANSITIONS:
        raise IllegalReplanTransition(
            f"illegal replan transition {current.value!r} -> {target.value!r}; "
            f"only {sorted(_LEGAL_REPLAN_TRANSITIONS)} are allowed"
        )
    return target


class IllegalReplanTransition(ValueError):
    """Raised when a ``ReplanState`` transition violates FR-08.

    Caught by ``plan_recovery()`` and surfaced as the terminal
    code ``recovery_loop_detected``.
    """


# ---------------------------------------------------------------------------
# RecoveryBudget — two-layer (Round 1 design Q3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecoveryBudget:
    """Two-layer recovery budget.

    Layer 1 — ``max_attempts``: bounds retry storm within one
      recovery attempt sequence.
    Layer 2 — ``max_replans``:  enforces FR-08 ("at most one
      recovery + one replan per failing step").
    Layer 3 — ``deadline_ms``:  bounds total recovery time per
      step (cross-strategy, cross-replan).

    Defaults chosen for P2.2 smoke tests:

        * ``max_attempts = 3`` — generous enough for transient
          backends, tight enough to surface bad strategy early.
        * ``max_replans  = 1`` — direct mapping of FR-08.
        * ``deadline_ms  = 30000`` — 30 s ceiling per step.

    Mutating any field after construction raises ``FrozenInstanceError``
    (the dataclass is frozen). To get a new budget with a different
    value, use ``dataclasses.replace``.
    """

    max_attempts: int = 3
    max_replans: int = 1
    deadline_ms: int = 30_000

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(
                f"max_attempts must be >= 1, got {self.max_attempts}"
            )
        if self.max_replans < 0:
            raise ValueError(
                f"max_replans must be >= 0, got {self.max_replans}"
            )
        if self.deadline_ms < 1:
            raise ValueError(
                f"deadline_ms must be >= 1, got {self.deadline_ms}"
            )


# ---------------------------------------------------------------------------
# RecoveryErrorCode — terminal-code enumeration
# ---------------------------------------------------------------------------


class RecoveryErrorCode(str, Enum):
    """Stable terminal-code strings emitted by ``plan_recovery``.

    These are wire values; downstream consumers (trace, report,
    rerun scripts) match on these strings. Adding a new code
    requires updating ``docs/requirements/P2-recovery-planner.md``
    §5.3 terminal-code table.
    """

    RESTORE_NOT_AVAILABLE = "restore_not_available"
    RESTORE_LOCK_VERIFY_FAILED = "restore_lock_verify_failed"
    RESTORE_SOP_UNREGISTERED = "restore_sop_unregistered"
    RESTORE_EXPECTATION_FAILED = "restore_expectation_failed"
    RESTORE_ATTEMPTS_EXHAUSTED = "restore_attempts_exhausted"
    RESTORE_DEADLINE_EXCEEDED = "restore_deadline_exceeded"
    REPLAN_UNABLE_TO_REVALIDATE = "replan_unable_to_revalidate"
    RECOVERY_LOOP_DETECTED = "recovery_loop_detected"


# ---------------------------------------------------------------------------
# Result contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RestoreResult:
    """Lower-level outcome of a single restore execution.

    Emitted by ``execute_restore()`` (Commit B). The executor
    consumes this to decide whether to mark the step successful
    or to re-enter recovery.
    """

    ok: bool
    code: RecoveryErrorCode | None
    snapshot_id: str | None
    expectation_results: tuple[Any, ...] = ()


@dataclass(frozen=True)
class RecoveryResult:
    """Final outcome of a recovery cycle (per step, per branch).

    Always carries:

        * ``status``     — high-level outcome (see ``RecoveryStatus``)
        * ``strategy``   — the ``RestoreStrategy`` actually used
        * ``attempts``   — number of attempts consumed (>= 0)
        * ``branch_id``  — the new branch's id; ``None`` when
                           recovery did not fork a new branch
                           (``status == BLOCKED`` with
                           ``restorable=False``)
        * ``parent_branch_id`` — lineage pointer back to the
                           branch that triggered recovery
                           (Round 2 review Minor 2 — without this
                           field trace analysis requires searching
                           events to reconstruct the tree)
        * ``error_code`` — structured terminal code when
                           ``status != SUCCESS``; ``None`` on success
    """

    status: RecoveryStatus
    strategy: RestoreStrategy
    attempts: int
    branch_id: str | None
    parent_branch_id: str | None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.attempts < 0:
            raise ValueError(
                f"attempts must be >= 0, got {self.attempts}"
            )
        if self.status is RecoveryStatus.SUCCESS and self.error_code is not None:
            raise ValueError(
                "status=SUCCESS forbids error_code; "
                f"got error_code={self.error_code!r}"
            )
        if (
            self.status is not RecoveryStatus.SUCCESS
            and self.error_code is None
        ):
            raise ValueError(
                f"status={self.status.value!r} requires error_code"
            )
        if self.status is RecoveryStatus.BLOCKED and self.branch_id is not None:
            # BLOCKED with no new branch is the common case
            # (restorable=False or budget exhausted). A branch
            # may legitimately be created to log the BLOCKED
            # outcome, but the dataclass forbids that for now
            # to keep the contract narrow.
            raise ValueError(
                "status=BLOCKED forbids branch_id; "
                f"got branch_id={self.branch_id!r}"
            )


# ---------------------------------------------------------------------------
# Plan contracts — input (FailureContext) + output (RecoveryPlan)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FailureContext:
    """What failed, where, why.

    Pure data; consumed by ``plan_recovery()``. The executor
    builds this from the failing step's ``StepResult`` and the
    P2.1 ``CheckpointDecision``.
    """

    step_id: str
    branch_id: str                            # the branch that failed
    failure_kind: str                         # stable failure tag
    error_code: str | None = None             # machine-friendly code
    detail: str | None = None                 # human-readable
    snapshot_id_before: str | None = None
    snapshot_id_after: str | None = None
    replan_state: ReplanState = ReplanState.NOT_REQUESTED


@dataclass(frozen=True)
class RecoveryPlan:
    """Output of ``plan_recovery()`` (Commit B).

    Pure: input -> output. No I/O. No trace mutation. No
    ``decide_checkpoint()`` call. Executor owns the orchestration.

    For now (Commit A), the dataclass is empty by intent — the
    planner in Commit B will fill it. Fields:

        * ``strategy``  — the ``RestoreStrategy`` to execute
        * ``steps``     — ordered list of restore-action ids
                          (looked up from the SOP inverse registry)
        * ``severity``  — the recovered severity used for the
                          tiebreaker
        * ``timeout_ms``— total wall-clock budget for execution
    """

    strategy: RestoreStrategy
    steps: tuple[str, ...] = field(default_factory=tuple)
    severity: RecoverySeverity = RecoverySeverity.NONE
    timeout_ms: int = 0


# ---------------------------------------------------------------------------
# Branch lineage
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Branch:
    """A recovery branch — lineage of a recovery attempt.

    Created by ``resume_from_checkpoint()`` (Commit B) and emitted
    as a ``branch_created`` trace event.
    """

    branch_id: str
    forked_from_event_id: str
    forked_from_checkpoint_id: str
    parent_branch_id: str | None
    head_event_id: str


__all__ = [
    # Enums
    "RestoreStrategy",
    "RecoverySeverity",
    "RecoveryStatus",
    "ReplanState",
    "RecoveryErrorCode",
    # Mapping
    "RESTORE_SEVERITY",
    "severity_of",
    # State transitions
    "advance_replan_state",
    "IllegalReplanTransition",
    # Budget
    "RecoveryBudget",
    # Result + plan + failure contracts
    "RestoreResult",
    "RecoveryResult",
    "FailureContext",
    "RecoveryPlan",
    "Branch",
]