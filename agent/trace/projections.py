"""
projections.py — Pure read-model projections (architecture §11).

The trace chain is the source of truth; `step-results.json` is
a *projection* over `events.jsonl` (FR-P1.3-08). Rebuilding it from
the chain must produce byte-identical output (the `Required Test`
`test_projection_byte_identical_replay`).

P1.3 implements the step-result projection only. Other projections
(`trace.md`, evidence aggregation, etc.) are P1.4 / P2.3 territory.
"""

from __future__ import annotations

from typing import Iterable, Mapping

from .events import EventType, TraceEvent


def project_step_results(
    events: Iterable[TraceEvent],
) -> list[dict[str, object]]:
    """Reduce `step_started` + `step_completed` + `action_result`
    events into a list of step-result dicts.

    Output ordering is `sequence_no` (the chain's natural order).
    The shape matches P1.2's `StepResult.to_dict()` so a deleted
    `step-results.json` can be rebuilt byte-identically.
    """
    # Index by step_id.
    by_step: dict[str, dict[str, object]] = {}

    for ev in events:
        if ev.event_type is EventType.STEP_STARTED:
            sid = ev.step_id or ""
            by_step[sid] = {
                "step_id": sid,
                "step_no": int(ev.payload.get("step_no", 0)),
                "status": "running",
                "started_at": ev.recorded_at,
                "ended_at": "",
                "duration_ms": 0,
                "action": None,
                "expectation_results": [],
                "error": None,
                "transition_expected": bool(
                    ev.payload.get("transition_expected", False)
                ),
            }
        elif ev.event_type is EventType.STEP_COMPLETED:
            sid = ev.step_id or ""
            entry = by_step.setdefault(sid, {"step_id": sid})
            entry["status"] = ev.payload.get("status", "unknown")
            entry["ended_at"] = ev.recorded_at
            entry["duration_ms"] = int(ev.payload.get("duration_ms", 0))
            if "error" in ev.payload:
                entry["error"] = ev.payload["error"]
        elif ev.event_type is EventType.ACTION_RESULT:
            sid = ev.step_id or ""
            entry = by_step.setdefault(sid, {"step_id": sid})
            entry["action"] = ev.payload.get("receipt")

    # Order by step_no ascending (events are append-order but step_no
    # is the canonical order).
    return sorted(by_step.values(), key=lambda s: int(s.get("step_no", 0)))


__all__ = ["project_step_results"]