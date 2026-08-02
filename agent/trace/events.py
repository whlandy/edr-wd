"""
events.py — Typed event envelopes (architecture §12.1, §12.2).

Every line in `events.jsonl` is one complete JSON object whose
shape is `TraceEvent` below. P1.3 ships the V1 event type list in
full; no event type is omitted (architecture §12.2 enumerates 24
types, plus `trace_recovered` from P1.3 review-of-crash-tail).

The envelope shape:

    {
      "schema_version": "1.0.0",
      "trace_id": "TR-...",
      "branch_id": "BR-...",
      "event_id": "EVT-...",
      "sequence_no": 14,
      "parent_event_id": "EVT-..." | None,
      "caused_by_event_id": "EVT-..." | None,
      "event_type": "<one of EventType>",
      "recorded_at": "2026-08-01T10:00:01.123Z",
      "plan_id": "PLAN-..." | None,
      "case_id": "TC-..." | None,
      "step_id": "S003" | None,
      "call_id": "CALL-..." | None,
      "payload": {...},
      "previous_hash": "sha256:..." | None,
      "event_hash": "sha256:..."
    }

`event_hash` is computed over canonical bytes of the event dict
WITHOUT `event_hash` itself (FR-P1.3-02). The hash chain is the
canonical-bytes sha256 chain, which P0.2's `canonical_json`
already gives us deterministically.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


SCHEMA_VERSION = "1.0.0"


class EventType(str, Enum):
    """Full V1 event-type list (architecture §12.2 + trace_recovered).

    Adding a new event type is a breaking change for any consumer
    that enumerates `EventType`. P1.3 ships the full list per spec.
    """

    TRACE_STARTED = "trace_started"
    ENVIRONMENT_RECORDED = "environment_recorded"
    PLAN_CREATED = "plan_created"
    PLAN_VALIDATED = "plan_validated"
    PLAN_REJECTED = "plan_rejected"
    OBSERVATION_RECORDED = "observation_recorded"
    STEP_STARTED = "step_started"
    CHECKPOINT_CREATED = "checkpoint_created"
    ACTION_REQUESTED = "action_requested"
    ACTION_STARTED = "action_started"
    ACTION_RESULT = "action_result"
    TRANSITION_DETECTED = "transition_detected"
    UNEXPECTED_TRANSITION = "unexpected_transition"
    SCREENSHOT_CAPTURED = "screenshot_captured"
    SCREENSHOT_PERSISTED = "screenshot_persisted"
    EXPECTATION_RESULT = "expectation_result"
    STEP_COMPLETED = "step_completed"
    RECOVERY_REQUESTED = "recovery_requested"
    BRANCH_CREATED = "branch_created"
    RECOVERY_RESULT = "recovery_result"
    REPLAN_CREATED = "replan_created"
    CLEANUP_STARTED = "cleanup_started"
    CLEANUP_COMPLETED = "cleanup_completed"
    TRACE_COMPLETED = "trace_completed"
    TRACE_ABORTED = "trace_aborted"
    TRACE_RECOVERED = "trace_recovered"


# Terminal event types (FR-P1.3-09). These close a trace.
TERMINAL_EVENT_TYPES: frozenset[EventType] = frozenset({
    EventType.TRACE_COMPLETED,
    EventType.TRACE_ABORTED,
})


@dataclass(frozen=True)
class TraceEvent:
    """One event in the trace. Frozen so the hash chain is tamper-evident.

    `payload` is a free-form dict; per-event-type payload schemas
    are versioned by `schema_version` and validated at consumer
    side. The chain itself only constrains the envelope.
    """

    schema_version: str
    trace_id: str
    branch_id: str
    event_id: str
    sequence_no: int
    parent_event_id: str | None
    caused_by_event_id: str | None
    event_type: EventType
    recorded_at: str
    plan_id: str | None
    case_id: str | None
    step_id: str | None
    call_id: str | None
    payload: Mapping[str, Any]
    previous_hash: str | None
    event_hash: str

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict (event_hash included)."""
        return {
            "schema_version": self.schema_version,
            "trace_id": self.trace_id,
            "branch_id": self.branch_id,
            "event_id": self.event_id,
            "sequence_no": self.sequence_no,
            "parent_event_id": self.parent_event_id,
            "caused_by_event_id": self.caused_by_event_id,
            "event_type": self.event_type.value,
            "recorded_at": self.recorded_at,
            "plan_id": self.plan_id,
            "case_id": self.case_id,
            "step_id": self.step_id,
            "call_id": self.call_id,
            "payload": dict(self.payload),
            "previous_hash": self.previous_hash,
            "event_hash": self.event_hash,
        }

    def to_hashable_dict(self) -> dict[str, Any]:
        """Serialise without `event_hash` (for chain hashing).

        The chain hash is computed over the canonical bytes of
        this dict; `event_hash` is added after the hash is set,
        breaking the recursion.
        """
        d = self.to_dict()
        d.pop("event_hash", None)
        return d


_ALLOWED = frozenset({
    "schema_version", "trace_id", "branch_id", "event_id",
    "sequence_no", "parent_event_id", "caused_by_event_id",
    "event_type", "recorded_at", "plan_id", "case_id", "step_id",
    "call_id", "payload", "previous_hash", "event_hash",
})


def from_dict(data: Mapping[str, Any]) -> TraceEvent:
    """Reconstruct a TraceEvent from a JSON-loaded dict.

    Validates the envelope field set so a tampered JSON line fails
    open (FR-P1.3-07) before the chain check even runs.
    """
    extra = set(data.keys()) - _ALLOWED
    if extra:
        raise ValueError(f"TraceEvent has unexpected fields: {sorted(extra)}")
    return TraceEvent(
        schema_version=data["schema_version"],
        trace_id=data["trace_id"],
        branch_id=data["branch_id"],
        event_id=data["event_id"],
        sequence_no=int(data["sequence_no"]),
        parent_event_id=data.get("parent_event_id"),
        caused_by_event_id=data.get("caused_by_event_id"),
        event_type=EventType(data["event_type"]),
        recorded_at=data["recorded_at"],
        plan_id=data.get("plan_id"),
        case_id=data.get("case_id"),
        step_id=data.get("step_id"),
        call_id=data.get("call_id"),
        payload=dict(data.get("payload") or {}),
        previous_hash=data.get("previous_hash"),
        event_hash=data["event_hash"],
    )


__all__ = [
    "SCHEMA_VERSION",
    "EventType",
    "TERMINAL_EVENT_TYPES",
    "TraceEvent",
    "from_dict",
]