"""
__init__.py — Public surface of the agent/execution package (P1.2).

Layering:

    target/protocol_models    — TestCase / AtomicTestStep / Expectation
    target/action_dispatcher  — dispatch() + ActionReceipt (P1.1)
    agent/execution           — AtomicExecutor + models + state machine
                                  + expectations + step_results (P1.2)

P1.3 will add a parallel `agent/trace` package that consumes
`StepResult` as projection input. P1.4 will hook screenshots into the
CAPTURING_BEFORE / CAPTURING_AFTER states and flip
`ExecutorConfig.visual_evidence_available = True`.
"""

from .checkpoints import (
    CheckpointDecision,
    CheckpointKind,
    VALID_CHECKPOINT_KINDS,
    decide_checkpoint,
)
from .executor import AtomicExecutor, BackendUnavailable, IllegalTransition, ObservationProvider
from .expectations import (
    EXPECTATION_REGISTRY,
    EVALUATORS_NOT_AVAILABLE,
    ExpectationNotAvailable,
)
from .models import (
    CaseOutcome,
    CaseRunResult,
    ExecutorConfig,
    ExpectationResult,
    OnErrorPolicy,
    STEP_STATUS_PRECEDENCE,
    StepResult,
    StepStatus,
    coerce_on_error,
    worst_status,
)
from .sop_index import (
    SopIndex,
    SopIndexEntry,
    SopIndexLoadError,
    load_sop_index,
)
from .state_machine import (
    TRANSITIONS,
    StepState,
    allowed_next,
    is_terminal,
    next_state,
    validate_transition,
)
from .recovery import (
    IllegalReplanTransition,
    RecoveryBudget,
    RecoveryErrorCode,
    RecoveryPlan,
    RecoveryResult,
    RecoverySeverity,
    RecoveryStatus,
    ReplanState,
    RestoreResult,
    RestoreStrategy,
    advance_replan_state,
    resolve_strategy,
    severity_of,
    strategies_for_severity,
)
from .recovery_inverse import (
    DuplicateInverseError,
    InverseRegistry,
    SOPInverseAction,
)
from .recovery_planner import plan_recovery
from .recovery_executor import (
    ExecutionOutcome,
    RecoveryExecutor,
    RequestedEvent,
    RestoreHandler,
)
from .step_results import load_step_results, write_atomic
from .transitions import (
    TransitionKind,
    TransitionResult,
    VALID_TRANSITION_KINDS,
    classify_transition,
)


__all__ = [
    # executor
    "AtomicExecutor",
    "ObservationProvider",
    "BackendUnavailable",
    # state machine
    "StepState",
    "TRANSITIONS",
    "is_terminal",
    "allowed_next",
    "next_state",
    "validate_transition",
    # models
    "StepStatus",
    "CaseOutcome",
    "OnErrorPolicy",
    "STEP_STATUS_PRECEDENCE",
    "worst_status",
    "coerce_on_error",
    "ExecutorConfig",
    "StepResult",
    "CaseRunResult",
    "ExpectationResult",
    # expectations
    "EXPECTATION_REGISTRY",
    "EVALUATORS_NOT_AVAILABLE",
    "ExpectationNotAvailable",
    # step_results
    "write_atomic",
    "load_step_results",
    "IllegalTransition",
    # transitions (P2.1)
    "TransitionKind",
    "VALID_TRANSITION_KINDS",
    "TransitionResult",
    "classify_transition",
    # checkpoints (P2.1)
    "CheckpointKind",
    "VALID_CHECKPOINT_KINDS",
    "CheckpointDecision",
    "decide_checkpoint",
    # sop_index (P2.1)
    "SopIndex",
    "SopIndexEntry",
    "SopIndexLoadError",
    "load_sop_index",
    # recovery contracts (P2.2 — Commit A)
    "RestoreStrategy",
    "RecoverySeverity",
    "RecoveryStatus",
    "ReplanState",
    "RecoveryErrorCode",
    "RESTORE_SEVERITY",
    "severity_of",
    "advance_replan_state",
    "IllegalReplanTransition",
    "RecoveryBudget",
    "RestoreResult",
    "RecoveryResult",
    "FailureContext",
    "RecoveryPlan",
    "Branch",
    # recovery composition helpers (P2.2 — Round 3 patch)
    "strategies_for_severity",
    "resolve_strategy",
    # recovery inverse registry (P2.2 — Commit A)
    "SOPInverseAction",
    "InverseRegistry",
    "DuplicateInverseError",
    # recovery planner (P2.2 — Commit B)
    "plan_recovery",
    # recovery executor (P2.2 — Commit D)
    "RecoveryExecutor",
    "ExecutionOutcome",
    "RequestedEvent",
    "RestoreHandler",
]