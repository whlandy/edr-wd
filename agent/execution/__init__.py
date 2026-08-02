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
from .state_machine import (
    TRANSITIONS,
    StepState,
    allowed_next,
    is_terminal,
    next_state,
    validate_transition,
)
from .step_results import load_step_results, write_atomic


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
]