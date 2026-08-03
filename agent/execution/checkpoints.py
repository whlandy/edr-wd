"""checkpoints.py — P2.1 checkpoint decision pipeline (architecture §15.2).

Given a step, the current snapshot, the catalog, the SOP index, and
an optional pre-classified transition, :func:`decide_checkpoint`
returns a :class:`CheckpointDecision` describing whether and what
kind of checkpoint to take before executing the step.

P2.1 implements ``logical`` and ``session`` checkpoints. The
``application_state`` and ``environment_snapshot`` kinds are
declared in the policy matrix but are always emitted with
``restorable=False`` until P2.2 lands tested restore strategies
(P2.1 doc line 64-65).

Decision inputs merged (per P2.1 doc line 45-49):

    1. Catalog ``transition_policy``     (ActionSpec.transition_policy)
    2. Step ``transition`` declaration   (Transition dataclass)
    3. Known SOP transition metadata     (SopIndexEntry.transition_kind)
    4. Selector semantics               (submit/close/delete mapping)
    5. Current application/session state (snapshot — consulted only
       when ``transition_result`` is provided)

Matrix (architecture §15.2 + FR-P2.1-04, -07, -08, -09):

    +-------------------+----------------------+----------------+--------+
    | catalog_policy    | step.transition      | selector        | kind   |
    +===================+======================+================+========+
    | never             | step.kind=application_restart | *     | appl.  |
    +-------------------+----------------------+----------------+--------+
    | never             | *                    | *              | none   |
    +-------------------+----------------------+----------------+--------+
    | required_checkpoint | *                  | *              | logical |
    |                   |                      | submit/close   | session|
    +-------------------+----------------------+----------------+--------+
    | expected/possible | step.checkpoint_before=True | *      | logical |
    +-------------------+----------------------+----------------+--------+
    | expected          | step.expected=True   | *              | logical |
    +-------------------+----------------------+----------------+--------+
    | *                 | transition_result.kind=window_owner_change | session |
    +-------------------+----------------------+----------------+--------+
    | *                 | transition_result.kind=unknown_material | none   |
    +-------------------+----------------------+----------------+--------+
    | *                 | sop transition hint  | *              | per kind|
    +-------------------+----------------------+----------------+--------+
    | otherwise         |                      |                | none   |
    +-------------------+----------------------+----------------+--------+

Priority order (highest first):

    1. step.application_restart          → APPLICATION_STATE
    2. catalog:never                     → NONE
    3. transition:unknown_material       → NONE (caller halts)
    4. catalog:required_checkpoint       → LOGICAL / SESSION
    5. transition:window_owner_change    → SESSION
    6. step.checkpoint_before            → LOGICAL / SESSION
    7. step.expected                     → LOGICAL / SESSION
    8. sop:<kind>                        → per kind
    9. default                           → NONE

`restorable` follows the kind:

    none                -> restorable=False
    logical             -> restorable=True,  strategy="redrive_prior_steps"
    session             -> restorable=True,  strategy="reconnect_session"
    application_state   -> restorable=False (P2.1)
    environment_snapshot-> restorable=False (P2.1)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from target.action_catalog.models import ActionSpec
from target.observations.models import ObservationSnapshot
from target.protocol_models.models import AtomicTestStep

from agent.execution.sop_index import SopIndex, SopIndexEntry
from agent.execution.transitions import TransitionKind, TransitionResult


# ---------------------------------------------------------------------------
# CheckpointKind
# ---------------------------------------------------------------------------


class CheckpointKind(str, Enum):
    """V1 checkpoint kind enumeration.

    `application_state` and `environment_snapshot` are emitted with
    `restorable=False` until tested restore strategies land in P2.2.
    """

    NONE = "none"
    LOGICAL = "logical"
    SESSION = "session"
    APPLICATION_STATE = "application_state"
    ENVIRONMENT_SNAPSHOT = "environment_snapshot"


VALID_CHECKPOINT_KINDS: frozenset[CheckpointKind] = frozenset(CheckpointKind)


# Selector semantics that escalate a logical checkpoint to session.
_SESSION_SELECTOR_SEMANTICS: frozenset[str] = frozenset({
    "submit", "close", "delete",
})


# Restore strategies — public constants for callers to match on.
RESTORE_STRATEGY_NONE = "none"
RESTORE_STRATEGY_REDRIVE = "redrive_prior_steps"
RESTORE_STRATEGY_RECONNECT = "reconnect_session"
RESTORE_STRATEGY_PROCESS_RESTART = "process_restart"  # P2.2 (not implemented yet)
RESTORE_STRATEGY_SYSTEM_SNAPSHOT = "system_snapshot"   # P2.2 (not implemented yet)


# ---------------------------------------------------------------------------
# CheckpointDecision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckpointDecision:
    """The result of `decide_checkpoint`.

    `rationale` is an ordered tuple of short, machine-friendly tags
    identifying which inputs drove the decision. The first tag is
    always the highest-priority input that won:

        ("catalog:required_checkpoint", "selector:submit")
        ("step:checkpoint_before",)
        ("transition:window_owner_change",)
        ("catalog:never",)
    """

    kind: CheckpointKind
    restorable: bool
    restore_strategy: str | None
    rationale: tuple[str, ...] = field(default_factory=tuple)

    _ALLOWED: frozenset[str] = frozenset({
        "kind", "restorable", "restore_strategy", "rationale",
    })

    def __post_init__(self) -> None:
        if self.kind not in VALID_CHECKPOINT_KINDS:
            raise ValueError(
                f"kind must be one of {sorted(k.value for k in VALID_CHECKPOINT_KINDS)}, "
                f"got {self.kind!r}"
            )
        if not isinstance(self.restorable, bool):
            raise ValueError(
                f"restorable must be a bool, got {type(self.restorable).__name__}"
            )
        if not isinstance(self.rationale, tuple):
            raise ValueError(
                f"rationale must be a tuple, got {type(self.rationale).__name__}"
            )
        # Contract: restorable=True implies a strategy; restorable=False
        # implies no strategy (except NONE which has no strategy either).
        if self.restorable and self.restore_strategy is None:
            raise ValueError(
                "restorable=True requires a restore_strategy"
            )
        if not self.restorable and self.restore_strategy is not None:
            raise ValueError(
                "restorable=False forbids a restore_strategy"
            )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build(kind: CheckpointKind, rationale: tuple[str, ...]) -> CheckpointDecision:
    """Materialize a decision with the right restorable + strategy."""
    if kind is CheckpointKind.NONE:
        return CheckpointDecision(
            kind=kind,
            restorable=False,
            restore_strategy=None,
            rationale=rationale,
        )
    if kind is CheckpointKind.LOGICAL:
        return CheckpointDecision(
            kind=kind,
            restorable=True,
            restore_strategy=RESTORE_STRATEGY_REDRIVE,
            rationale=rationale,
        )
    if kind is CheckpointKind.SESSION:
        return CheckpointDecision(
            kind=kind,
            restorable=True,
            restore_strategy=RESTORE_STRATEGY_RECONNECT,
            rationale=rationale,
        )
    # application_state / environment_snapshot — P2.1 policy only.
    return CheckpointDecision(
        kind=kind,
        restorable=False,
        restore_strategy=None,
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Decision rules — explicit precedence list (Issue 5, code review).
#
# Each rule inspects the inputs and returns either a CheckpointDecision
# or None. The list below is iterated in order; the first rule that
# returns a non-None decision wins. Adding a new precedence rule means
# appending one rule here, not editing nested if/elif branches.
# ---------------------------------------------------------------------------


def _rule_step_application_restart(
    spec: ActionSpec | None,
    step_transition: Any,
    sop_entry: SopIndexEntry | None,
    transition_result: TransitionResult | None,
    selector_semantics: str,
) -> CheckpointDecision | None:
    """Step-level application_restart hint wins BEFORE catalog policy.

    A restart is structurally incompatible with a "never checkpoint"
    policy (catalog metadata can never override an application restart
    signal), so this rule is first.
    """
    if step_transition is None:
        return None
    if step_transition.kind != TransitionKind.APPLICATION_RESTART.value:
        return None
    return _build(
        CheckpointKind.APPLICATION_STATE,
        ("step:application_restart",),
    )


def _rule_catalog_never(
    spec: ActionSpec | None,
    step_transition: Any,
    sop_entry: SopIndexEntry | None,
    transition_result: TransitionResult | None,
    selector_semantics: str,
) -> CheckpointDecision | None:
    """FR-P2.1-09: catalog "never" wins unconditionally."""
    catalog_policy = _catalog_policy(spec)
    if catalog_policy != "never":
        return None
    return CheckpointDecision(
        kind=CheckpointKind.NONE,
        restorable=False,
        restore_strategy=None,
        rationale=("catalog:never",),
    )


def _rule_transition_unknown(
    spec: ActionSpec | None,
    step_transition: Any,
    sop_entry: SopIndexEntry | None,
    transition_result: TransitionResult | None,
    selector_semantics: str,
) -> CheckpointDecision | None:
    """Unexpected transition from previous step.

    ``UNKNOWN_MATERIAL_CHANGE`` means the previous step's outcome is
    opaque; the executor is expected to halt (P2.1 doc line 56-57).
    Returns NONE so the caller does not waste a checkpoint before
    halting.
    """
    if transition_result is None:
        return None
    if transition_result.kind is not TransitionKind.UNKNOWN_MATERIAL_CHANGE:
        return None
    return CheckpointDecision(
        kind=CheckpointKind.NONE,
        restorable=False,
        restore_strategy=None,
        rationale=("transition:unexpected_material_change",),
    )


def _rule_catalog_required_checkpoint(
    spec: ActionSpec | None,
    step_transition: Any,
    sop_entry: SopIndexEntry | None,
    transition_result: TransitionResult | None,
    selector_semantics: str,
) -> CheckpointDecision | None:
    """FR-P2.1-07: catalog required_checkpoint overrides step."""
    catalog_policy = _catalog_policy(spec)
    if catalog_policy != "required_checkpoint":
        return None
    if selector_semantics in _SESSION_SELECTOR_SEMANTICS:
        return _build(
            CheckpointKind.SESSION,
            (
                "catalog:required_checkpoint",
                f"selector:{selector_semantics}",
            ),
        )
    return _build(
        CheckpointKind.LOGICAL,
        ("catalog:required_checkpoint",),
    )


def _rule_transition_window_owner_change(
    spec: ActionSpec | None,
    step_transition: Any,
    sop_entry: SopIndexEntry | None,
    transition_result: TransitionResult | None,
    selector_semantics: str,
) -> CheckpointDecision | None:
    """Previous step caused a WINDOW_OWNER_CHANGE → session."""
    if transition_result is None:
        return None
    if transition_result.kind is not TransitionKind.WINDOW_OWNER_CHANGE:
        return None
    return _build(
        CheckpointKind.SESSION,
        ("transition:window_owner_change",),
    )


def _rule_step_checkpoint_before(
    spec: ActionSpec | None,
    step_transition: Any,
    sop_entry: SopIndexEntry | None,
    transition_result: TransitionResult | None,
    selector_semantics: str,
) -> CheckpointDecision | None:
    """Step.transition.checkpoint_before=True → logical/session."""
    if step_transition is None or not step_transition.checkpoint_before:
        return None
    if selector_semantics in _SESSION_SELECTOR_SEMANTICS:
        return _build(
            CheckpointKind.SESSION,
            (
                "step:checkpoint_before",
                f"selector:{selector_semantics}",
            ),
        )
    return _build(
        CheckpointKind.LOGICAL,
        ("step:checkpoint_before",),
    )


def _rule_step_expected(
    spec: ActionSpec | None,
    step_transition: Any,
    sop_entry: SopIndexEntry | None,
    transition_result: TransitionResult | None,
    selector_semantics: str,
) -> CheckpointDecision | None:
    """Step.transition.expected=True → logical/session (low priority)."""
    if step_transition is None or not step_transition.expected:
        return None
    catalog_policy = _catalog_policy(spec)
    if catalog_policy not in ("expected", "possible"):
        return None
    if selector_semantics in _SESSION_SELECTOR_SEMANTICS:
        return _build(
            CheckpointKind.SESSION,
            (
                "step:expected",
                f"selector:{selector_semantics}",
            ),
        )
    return _build(
        CheckpointKind.LOGICAL,
        ("step:expected",),
    )


def _rule_sop_transition_hint(
    spec: ActionSpec | None,
    step_transition: Any,
    sop_entry: SopIndexEntry | None,
    transition_result: TransitionResult | None,
    selector_semantics: str,
) -> CheckpointDecision | None:
    """FR-P2.1-06: SOP transition metadata hint (lowest priority)."""
    if sop_entry is None or sop_entry.transition_kind is None:
        return None
    catalog_policy = _catalog_policy(spec)
    if catalog_policy not in ("expected", "possible"):
        return None
    kind = sop_entry.transition_kind
    if kind is TransitionKind.PAGE_NAVIGATION:
        return _build(
            CheckpointKind.LOGICAL,
            ("sop:page_navigation",),
        )
    if kind is TransitionKind.WINDOW_OPEN:
        return _build(
            CheckpointKind.SESSION,
            ("sop:window_open",),
        )
    if kind is TransitionKind.MODAL_OPEN:
        return _build(
            CheckpointKind.SESSION,
            ("sop:modal_open",),
        )
    if kind is TransitionKind.APPLICATION_RESTART:
        return _build(
            CheckpointKind.APPLICATION_STATE,
            ("sop:application_restart",),
        )
    # Other sop transition kinds do not produce a checkpoint
    # by themselves; let later rules take over.
    return None


# Rule list — explicit precedence (Issue 5). Insert a rule to add a
# new precedence level. NEVER remove a rule without bumping a
# ``__rev__`` constant if the wire schema is involved.
_DECISION_RULES: tuple = (
    _rule_step_application_restart,
    _rule_catalog_never,
    _rule_transition_unknown,
    _rule_catalog_required_checkpoint,
    _rule_transition_window_owner_change,
    _rule_step_checkpoint_before,
    _rule_step_expected,
    _rule_sop_transition_hint,
)


def _catalog_policy(spec: ActionSpec | None) -> str:
    """The catalog transition policy for a spec (or fallback default).

    Defaulting to ``"possible"`` when no spec is found keeps the
    ``possible`` branches of later rules active even when the catalog
    is unavailable (FR-P2.1 review: ``catalog=None`` still allows
    SOP and step hints to drive the decision).
    """
    if spec is None:
        return "possible"
    return spec.transition_policy


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def decide_checkpoint(
    step: AtomicTestStep,
    snapshot: ObservationSnapshot,
    catalog: Mapping[str, ActionSpec] | None,
    sop_index: SopIndex,
    *,
    sop_id: str | None = None,
    selector_semantics: str = "none",
    transition_result: TransitionResult | None = None,
) -> CheckpointDecision:
    """Decide whether and what kind of checkpoint to take for `step`.

    Pre-execution hook: the executor calls this **before** dispatching
    `step` so the checkpoint can be created in the trace.

    Parameters
    ----------
    step:
        The atomic step being entered.
    snapshot:
        The current ``ObservationSnapshot`` (captured BEFORE the step
        runs). The snapshot itself does not drive the decision; it is
        accepted for API symmetry with future `transition_result`
        consumers and to keep the function pure over its inputs.
    catalog:
        Mapping of ``action_id → ActionSpec``. May be ``None`` when no
        catalog metadata is available — the function then falls back
        to ``possible`` and ``never`` policies only.
    sop_id:
        Optional SOP id for fetching transition metadata from
        ``sop_index``. ``None`` means no SOP context.
    selector_semantics:
        One of ``"none" | "submit" | "close" | "delete"``. Pushed by
        the dispatcher based on the action category. Defaults to
        ``"none"``.
    transition_result:
        Optional pre-classified ``TransitionResult`` from the
        PREVIOUS step. Used to escalate to ``session`` when the
        previous step caused a ``window_owner_change`` (FR-P2.1-04).

    Returns
    -------
    CheckpointDecision
        Frozen result with `kind`, `restorable`, `restore_strategy`,
        and `rationale` (ordered tuple of input tags that drove the
        decision).

    Decision rules (Issue 5 — explicit precedence list):

        1. ``step:application_restart``            → APPLICATION_STATE
        2. ``catalog:never``                       → NONE
        3. ``transition:unexpected_material_change``→ NONE
        4. ``catalog:required_checkpoint``          → LOGICAL/SESSION
        5. ``transition:window_owner_change``       → SESSION
        6. ``step:checkpoint_before``               → LOGICAL/SESSION
        7. ``step:expected``                       → LOGICAL/SESSION
        8. ``sop:<kind>``                          → per-kind
        9. default                                 → NONE
    """
    # Resolve inputs once and pass them to every rule. This keeps the
    # rule signatures uniform so a new rule can be added without
    # rewiring call sites.
    spec = catalog.get(step.action_id) if catalog else None
    sop_entry = sop_index.get(sop_id) if sop_id else None
    step_transition = step.transition

    for rule in _DECISION_RULES:
        decision = rule(
            spec=spec,
            step_transition=step_transition,
            sop_entry=sop_entry,
            transition_result=transition_result,
            selector_semantics=selector_semantics,
        )
        if decision is not None:
            return decision

    # Default — no rule matched.
    return CheckpointDecision(
        kind=CheckpointKind.NONE,
        restorable=False,
        restore_strategy=None,
        rationale=(),
    )


__all__ = [
    "CheckpointKind",
    "VALID_CHECKPOINT_KINDS",
    "CheckpointDecision",
    "decide_checkpoint",
    "RESTORE_STRATEGY_NONE",
    "RESTORE_STRATEGY_REDRIVE",
    "RESTORE_STRATEGY_RECONNECT",
    "RESTORE_STRATEGY_PROCESS_RESTART",
    "RESTORE_STRATEGY_SYSTEM_SNAPSHOT",
]