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
    verify_chain,
)
from .ids import (
    PREFIX,
    generate_branch_id,
    generate_call_id,
    generate_event_id,
    generate_plan_id,
    generate_trace_id,
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
    # integrity
    "canonical_event_hash",
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
]