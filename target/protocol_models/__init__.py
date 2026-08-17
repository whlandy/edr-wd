"""
__init__.py — Public surface of the protocol_models package (P0.2).

Re-exports the stable API used by target/server.py and by tests.

Layering (parallel to action_catalog):

    target/protocol_models/
        enums.py         — expectation / on_error / transition / status enums
        ids.py           — sortable stdlib-only ID generators
        canonical_json.py— deterministic JSON + sha256 helper
        models.py        — strict frozen dataclasses (ActionSequence, ...)
        expectations.py  — typed expectation registry (P1.2 implements evaluators)
        validator.py     — schema / catalog / DAG validation
        dry_run.py       — pure static dry-run (no backend calls)

P0.2 introduces this package. P0.3 will consume TargetRef from here
when the observation / resolver module lands. P1.1 dispatcher and
P1.2 atomic executor reuse the validator + dry_run + expectations
modules; new code goes under `agent/execution/` (P1.2 territory).
"""

from __future__ import annotations

from .canonical_json import canonical_bytes, canonical_sha256
from .dry_run import DryRunResult, dry_run, dry_run_case
from .enums import (
    EXPECTATION_ACTION_OK,
    EXPECTATION_ACTIVE_WINDOW_OWNER,
    EXPECTATION_CONTROL_ABSENT,
    EXPECTATION_CONTROL_EXISTS,
    EXPECTATION_CONTROL_TEXT_CONTAINS,
    EXPECTATION_CONTROL_TEXT_EQUALS,
    EXPECTATION_CONTROL_VALUE_EQUALS,
    EXPECTATION_CONTROL_CHECKED_EQUALS,
    EXPECTATION_CONTROL_ENABLED_EQUALS,
    EXPECTATION_VISUAL_EVIDENCE_CAPTURED,
    EXPECTATION_WINDOW_CLOSED,
    EXPECTATION_WINDOW_OPEN,
    EXPECTATION_WINDOW_TEXT_CONTAINS,
    VALID_EVIDENCE_SCREENSHOT,
    VALID_EXPECTATION_TYPES,
    VALID_ON_ERROR,
    VALID_STEP_STATUSES,
    VALID_CASE_OUTCOMES,
    VALID_TARGET_KINDS,
    VALID_TRANSITION_KINDS,
)
from .expectations import (
    EXPECTATION_TYPE_REGISTRY,
    ExpectationTypeSpec,
    iter_expectation_types,
    validate_expectation_type,
)
from .ids import (
    SCOPE_PLAN,
    SCOPE_STEP,
    SCOPE_REQ,
    SCOPE_CP,
    SCOPE_EVT,
    SCOPE_BRANCH,
    SCOPE_SNAP,
    SCOPE_EVID,
    SCOPE_TRACE,
    VALID_SCOPES,
    _id_parts,
    new_branch_id,
    new_checkpoint_id,
    new_event_id,
    new_evidence_id,
    new_plan_id,
    new_request_id,
    new_snapshot_id,
    new_step_id,
    new_trace_id,
)
from .models import (
    PROTOCOL_VERSION,
    ActionReceipt,
    ActionSequence,
    ActionStep,
    AtomicTestStep,
    ErrorEnvelope,
    EvidenceSpec,
    Expectation,
    ProtocolModelError,
    TargetRef,
    TestCase,
    Transition,
    model_field_names,
)
from .validator import (
    ARCHITECTURE_P0_2_VALIDATION_CODES,
    CODE_ACTION_CODE_MISMATCH,
    CODE_BACKEND_DISABLED,
    CODE_CATALOG_DIGEST_MISMATCH,
    CODE_DEPENDENCY_CYCLE,
    CODE_DUPLICATE_STEP_ID,
    CODE_INVALID_ARGS,
    CODE_INVALID_TARGET_REF,
    CODE_MISSING_DEPENDENCY,
    CODE_SCHEMA_VERSION_MISMATCH,
    CODE_UNKNOWN_ACTION_CODE,
    CODE_UNKNOWN_ACTION_ID,
    CODE_UNKNOWN_EXPECTATION_TYPE,
    CODE_UNKNOWN_FIELD,
    ValidationError,
    re_raise_protocol_model_error,
    validate_case,
    validate_plan,
)


__all__ = [
    # Version
    "PROTOCOL_VERSION",
    # Models
    "ActionSequence",
    "ActionStep",
    "ActionReceipt",
    "AtomicTestStep",
    "ErrorEnvelope",
    "EvidenceSpec",
    "Expectation",
    "ProtocolModelError",
    "TargetRef",
    "TestCase",
    "Transition",
    "model_field_names",
    # Validator
    "ValidationError",
    "validate_plan",
    "validate_case",
    "re_raise_protocol_model_error",
    "ARCHITECTURE_P0_2_VALIDATION_CODES",
    "CODE_UNKNOWN_ACTION_ID",
    "CODE_UNKNOWN_ACTION_CODE",
    "CODE_ACTION_CODE_MISMATCH",
    "CODE_SCHEMA_VERSION_MISMATCH",
    "CODE_CATALOG_DIGEST_MISMATCH",
    "CODE_DUPLICATE_STEP_ID",
    "CODE_MISSING_DEPENDENCY",
    "CODE_DEPENDENCY_CYCLE",
    "CODE_BACKEND_DISABLED",
    "CODE_INVALID_ARGS",
    "CODE_UNKNOWN_EXPECTATION_TYPE",
    "CODE_UNKNOWN_FIELD",
    "CODE_INVALID_TARGET_REF",
    # Dry-run
    "DryRunResult",
    "dry_run",
    "dry_run_case",
    # Canonical JSON
    "canonical_bytes",
    "canonical_sha256",
    # IDs
    "SCOPE_PLAN", "SCOPE_STEP", "SCOPE_REQ", "SCOPE_CP",
    "SCOPE_EVT", "SCOPE_BRANCH", "SCOPE_SNAP", "SCOPE_EVID",
    "SCOPE_TRACE", "VALID_SCOPES",
    "new_plan_id", "new_step_id", "new_request_id",
    "new_checkpoint_id", "new_event_id", "new_branch_id",
    "new_snapshot_id", "new_evidence_id", "new_trace_id",
    # Expectation registry
    "ExpectationTypeSpec",
    "EXPECTATION_TYPE_REGISTRY",
    "validate_expectation_type",
    "iter_expectation_types",
    # Enums
    "EXPECTATION_ACTION_OK",
    "EXPECTATION_ACTIVE_WINDOW_OWNER",
    "EXPECTATION_CONTROL_ABSENT",
    "EXPECTATION_CONTROL_EXISTS",
    "EXPECTATION_CONTROL_TEXT_CONTAINS",
    "EXPECTATION_CONTROL_TEXT_EQUALS",
    "EXPECTATION_CONTROL_VALUE_EQUALS",
    "EXPECTATION_CONTROL_CHECKED_EQUALS",
    "EXPECTATION_CONTROL_ENABLED_EQUALS",
    "EXPECTATION_VISUAL_EVIDENCE_CAPTURED",
    "EXPECTATION_WINDOW_CLOSED",
    "EXPECTATION_WINDOW_OPEN",
    "EXPECTATION_WINDOW_TEXT_CONTAINS",
    "VALID_EVIDENCE_SCREENSHOT",
    "VALID_EXPECTATION_TYPES",
    "VALID_ON_ERROR",
    "VALID_STEP_STATUSES",
    "VALID_CASE_OUTCOMES",
    "VALID_TARGET_KINDS",
    "VALID_TRANSITION_KINDS",
]
