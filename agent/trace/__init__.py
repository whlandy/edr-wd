"""
__init__.py — Public surface of agent/trace (P1.3).

Layering:

    target/protocol_models  — canonical_json + dataclass wire models
    target/action_dispatcher — ActionReceipt + dispatch()
    target/observations     — snapshots / target identity
    agent/execution          — AtomicExecutor + StepResult (P1.2)
    agent/trace              — TraceEvent + TraceStore + integrity +
                                projections + manifest (THIS PACKAGE)

P1.3 owns only the agent-side trace machinery; no executor state
changes are required for P1.3 (the P1.3 spec lists
"agent/execution/executor.py rewired to produce events" as a
deliverable but only P1.4 evidence gating requires actual
rewiring; the P1.3 trace store can already consume manually
emitted events).
"""

from .events import (
    SCHEMA_VERSION,
    TERMINAL_EVENT_TYPES,
    EventType,
    TraceEvent,
    from_dict,
)
from .integrity import (
    IntegrityIssue,
    IntegrityReport,
    canonical_event_hash,
    load_events,
    stamp_event,
    verify_chain,
)
from .ids import (
    PREFIX,
    generate_branch_id,
    generate_call_id,
    generate_event_id,
    generate_plan_id,
    generate_trace_id,
    reset_for_tests,
)
from .manifest import (
    SCHEMA_VERSION as MANIFEST_SCHEMA_VERSION,
    ManifestRecord,
)
from .projections import project_step_results
from .store import (
    AlreadyFinalized,
    ChainCorrupted,
    OpenResult,
    TraceError,
    TraceStore,
)
from .evidence import (
    EvidenceError,
    EvidenceIntegrityError,
    EvidencePathError,
    EvidenceRecord,
    ImageProvider,
    ImageRule,
    Rectangle,
    TextRule,
    persist_screenshot,
    verify_evidence_on_disk,
    VALID_ROLES,
)
from .capture_policy import (
    CaptureContext,
    CapturePlan,
    resolve_capture_plan,
)
from .markdown import (
    RenderContext,
    render_case_trace,
    write_case_trace_atomic,
)
from .redaction import (
    apply_image_redaction,
    apply_text_redaction,
)
from .runs import (
    MANIFEST_SCHEMA_VERSION as RUN_MANIFEST_SCHEMA_VERSION,
    SUPPORTED_MANIFEST_SCHEMA_VERSIONS as RUN_SUPPORTED_MANIFEST_SCHEMA_VERSIONS,
    UnsupportedManifestSchemaError,
    RunState,
    InvalidStateTransitionError,
    sanitize_identifier,
    ALLOWED_METRIC_KEYS,
    MetricKeyForbiddenError,
    MetricValueTypeError,
    MetricRecord,
    make_attempt_id,
    CaseAttemptRef,
    Manifest as RunManifest,
    now_utc_iso,
    atomic_write_json,
    atomic_write_text,
    RunContext,
)
from .render_report import (
    AttemptSplit,
    ManifestCorruptError,
    ManifestMissingError,
    ReconciliationError,
    ReportStatus,
    Totals,
    compute_aggregate_status,
    compute_attempt_split,
    reconcile_totals,
    render_report,
    write_run_report,
)


__all__ = [
    # events
    "SCHEMA_VERSION",
    "EventType",
    "TraceEvent",
    "TERMINAL_EVENT_TYPES",
    "from_dict",
    # ids
    "PREFIX",
    "generate_event_id",
    "generate_trace_id",
    "generate_branch_id",
    "generate_plan_id",
    "generate_call_id",
    "reset_for_tests",
    # integrity
    "canonical_event_hash",
    "stamp_event",
    "load_events",
    "verify_chain",
    "IntegrityIssue",
    "IntegrityReport",
    # manifest
    "MANIFEST_SCHEMA_VERSION",
    "ManifestRecord",
    # projections
    "project_step_results",
    # store
    "TraceStore",
    "OpenResult",
    "TraceError",
    "ChainCorrupted",
    "AlreadyFinalized",
    # P2.3 runs (lifecycle owner)
    "RUN_MANIFEST_SCHEMA_VERSION",
    "RUN_SUPPORTED_MANIFEST_SCHEMA_VERSIONS",
    "UnsupportedManifestSchemaError",
    "RunState",
    "InvalidStateTransitionError",
    "sanitize_identifier",
    "ALLOWED_METRIC_KEYS",
    "MetricKeyForbiddenError",
    "MetricValueTypeError",
    "MetricRecord",
    "make_attempt_id",
    "CaseAttemptRef",
    "RunManifest",
    "now_utc_iso",
    "atomic_write_json",
    "atomic_write_text",
    "RunContext",
    # P2.3 render_report
    "AttemptSplit",
    "ManifestCorruptError",
    "ManifestMissingError",
    "ReconciliationError",
    "ReportStatus",
    "Totals",
    "compute_aggregate_status",
    "compute_attempt_split",
    "reconcile_totals",
    "render_report",
    "write_run_report",
]