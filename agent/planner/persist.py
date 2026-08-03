"""
persist.py — Plan event persistence (P3.1 Commit D).

P3.1 design gate contract D16:

  * Persisted plan events (`plan_created`, `plan_validated`,
    `plan_rejected`, `replan_created`) MUST run their payload
    through `RedactionRegistry.apply_text` BEFORE persistence.
  * The audit that P2.4 acceptance #4-5 requires (zero secret
    matches across a trace directory) MUST continue to hold
    after P3.1 lands.
  * Persisted plan events MUST be in the trace chain (P1.3)
    and participate in the integrity chain — not in a side
    channel.
  * **Event schema versioning**: planner-emitted events MUST
    carry an explicit `schema_version` field. Initial values:
    - `plan_created.v1`
    - `plan_validated.v1`
    - `plan_rejected.v1`
    - `replan_created.v1` (carries `replan_id` from D13)

This module ships:

  * `PlanEventType` — enum-like constants for the four V1
    event types.
  * `PlanEvent` — frozen dataclass carrying the wire shape
    (one event). Includes `schema_version` per D16.
  * `build_plan_event(...)` — pure factory that takes a
    payload + context and produces a sanitised `PlanEvent`.
  * `audit_persisted_plans(trace_dir, registry)` — convenience
    wrapper that runs the registry's `audit_artifacts` and
    narrows the result to planner events.

The actual write to `events.jsonl` (the trace chain) is the
caller's responsibility. This module is **pure**: no I/O.
The persist module produces a `PlanEvent` whose `to_dict()`
output is the JSON line that the trace store will append.

Layer-boundary notes:

  * Sanitisation happens at this boundary (Layer 1 per D2).
  * Markdown / prompt escape is a different layer (D15) and
    lives elsewhere.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Mapping

from agent.redaction import RedactionRegistry, default_registry


__all__ = [
    "PlanEventType",
    "PlanEvent",
    "build_plan_event",
    "audit_persisted_plans",
    "PLANNER_EVENT_SCHEMA_VERSION",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


PLANNER_EVENT_SCHEMA_VERSION = "1"

# Event-type to schema-version mapping (D16). A version bump is
# a major break per architecture §7.2; future consumers MUST
# reject unknown versions.
_PLANNER_EVENT_SCHEMA_VERSIONS: dict[str, str] = {
    "plan_created":   "plan_created.v1",
    "plan_validated": "plan_validated.v1",
    "plan_rejected":  "plan_rejected.v1",
    "replan_created": "replan_created.v1",
}


class PlanEventType:
    PLAN_CREATED   = "plan_created"
    PLAN_VALIDATED = "plan_validated"
    PLAN_REJECTED  = "plan_rejected"
    REPLAN_CREATED = "replan_created"


PLAN_EVENT_TYPES: frozenset[str] = frozenset(_PLANNER_EVENT_SCHEMA_VERSIONS)


# ---------------------------------------------------------------------------
# PlanEvent
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanEvent:
    """One persisted planner event (D16).

    `schema_version` is required; `payload` is the sanitised
    dict (Layer 1 applied via `RedactionRegistry.apply_text`).
    The trace store consumes `to_dict()` directly as the
    `events.jsonl` JSON line.
    """

    event_type: str
    schema_version: str
    plan_id: str
    snapshot_id: str
    payload: Mapping[str, Any]
    replan_id: str = ""
    replan_count: int = 0
    validation_errors: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if self.event_type not in PLAN_EVENT_TYPES:
            raise ValueError(
                f"unknown event_type {self.event_type!r}; "
                f"expected one of {sorted(PLAN_EVENT_TYPES)}"
            )
        expected = _PLANNER_EVENT_SCHEMA_VERSIONS[self.event_type]
        if self.schema_version != expected:
            raise ValueError(
                f"schema_version {self.schema_version!r} does not "
                f"match the locked version for "
                f"{self.event_type!r}: expected {expected!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialise for the trace store.

        `replan_id` / `replan_count` are gated on the event type:
        only `replan_created` carries them (D16 schema shape).
        """
        out: dict[str, Any] = {
            "event_type": self.event_type,
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "snapshot_id": self.snapshot_id,
            "payload": dict(self.payload),
        }
        if self.event_type == PlanEventType.REPLAN_CREATED:
            if self.replan_id:
                out["replan_id"] = self.replan_id
            if self.replan_count:
                out["replan_count"] = self.replan_count
        if self.validation_errors:
            out["validation_errors"] = [
                dict(e) for e in self.validation_errors
            ]
        return out


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_plan_event(
    event_type: str,
    *,
    plan_id: str,
    snapshot_id: str,
    payload: Mapping[str, Any],
    replan_id: str = "",
    replan_count: int = 0,
    validation_errors: Sequence[Mapping[str, Any]] = (),
    registry: RedactionRegistry | None = None,
) -> PlanEvent:
    """Build a sanitised PlanEvent (D16).

    The payload is sanitised via `RedactionRegistry.apply_text`
    (D16 + D7) before the event is constructed. Numeric
    fields, keys, and other scalars pass through unchanged.

    Raises ValueError if `event_type` is not one of the four V1
    types, or if `schema_version` does not match the locked
    version for the event_type.
    """
    if event_type not in PLAN_EVENT_TYPES:
        raise ValueError(
            f"unknown event_type {event_type!r}; expected one of "
            f"{sorted(PLAN_EVENT_TYPES)}"
        )

    reg = registry or default_registry()
    sanitised_payload, _fired = reg.apply_to_payload(payload)

    return PlanEvent(
        event_type=event_type,
        schema_version=_PLANNER_EVENT_SCHEMA_VERSIONS[event_type],
        plan_id=plan_id,
        snapshot_id=snapshot_id,
        payload=sanitised_payload,
        replan_id=replan_id,
        replan_count=replan_count,
        validation_errors=tuple(validation_errors),
    )


# ---------------------------------------------------------------------------
# Audit convenience
# ---------------------------------------------------------------------------


def audit_persisted_plans(
    trace_dir,
    registry: RedactionRegistry | None = None,
) -> "RedactionAuditReport":
    """Run the registry's audit on `trace_dir`.

    Returns the `RedactionAuditReport`. The caller is expected
    to assert `report.ok` is True (no secret matches found
    across the trace directory).

    The audit walks the entire trace directory (per D16: the
    events MUST be in the trace chain, so all P3.1 events end
    up under `trace_dir/.../events.jsonl`).
    """
    from pathlib import Path
    reg = registry or default_registry()
    return reg.audit_artifacts(Path(trace_dir))