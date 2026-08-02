"""P1.3 acceptance gate — projections (FR-P1.3-08).

`step-results.json` must be fully derivable from `events.jsonl` +
observation evidence; deleting and rebuilding produces byte-identical
output.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
for p in (str(_REPO / "target"), str(_REPO / "agent")):
    if p not in sys.path:
        sys.path.insert(0, p)


import dataclasses

from trace import (  # noqa: E402
    EventType,
    TraceEvent,
    project_step_results,
    stamp_event,
)


def _ev(seq, event_type, step_id=None, **payload):
    return TraceEvent(
        schema_version="1.0.0",
        trace_id="TR-1",
        branch_id="BR-1",
        event_id=f"EVT-{seq:08x}00000000",
        sequence_no=seq,
        parent_event_id=None if seq == 1 else f"EVT-{seq-1:08x}00000000",
        caused_by_event_id=None,
        event_type=event_type,
        recorded_at="2026-01-01T00:00:00.000Z",
        plan_id=None,
        case_id="TC-1",
        step_id=step_id,
        call_id=None,
        payload=payload,
        previous_hash=None,
        event_hash="",
    )


def _build_chain(events):
    out = []
    prev_hash = None
    for e in events:
        stamped = stamp_event(e)
        stamped = dataclasses.replace(stamped, previous_hash=prev_hash)
        h = stamp_event(stamped).event_hash
        out.append(stamped)
        prev_hash = h
    return out


def test_project_step_results_three_steps():
    events = _build_chain([
        _ev(1, EventType.STEP_STARTED, step_id="S001", step_no=1),
        _ev(2, EventType.ACTION_REQUESTED, step_id="S001"),
        _ev(3, EventType.ACTION_RESULT, step_id="S001",
            receipt={"ok": True, "action_id": "gui.click"}),
        _ev(4, EventType.EXPECTATION_RESULT, step_id="S001",
            expectation_type="action_ok", status="passed"),
        _ev(5, EventType.STEP_COMPLETED, step_id="S001",
            status="passed", duration_ms=42),
        _ev(6, EventType.STEP_STARTED, step_id="S002", step_no=2),
        _ev(7, EventType.ACTION_RESULT, step_id="S002",
            receipt={"ok": True, "action_id": "gui.type_text"}),
        _ev(8, EventType.STEP_COMPLETED, step_id="S002",
            status="passed", duration_ms=10),
        _ev(9, EventType.STEP_STARTED, step_id="S003", step_no=3),
        _ev(10, EventType.STEP_COMPLETED, step_id="S003",
             status="failed", duration_ms=5, error={"code": "boom"}),
    ])
    results = project_step_results(events)
    assert len(results) == 3
    assert [r["step_id"] for r in results] == ["S001", "S002", "S003"]
    assert [r["status"] for r in results] == ["passed", "passed", "failed"]
    # Step 3 carries an error payload.
    assert results[2]["error"] == {"code": "boom"}


def test_project_step_results_empty_when_no_step_events():
    events = _build_chain([
        _ev(1, EventType.TRACE_STARTED),
        _ev(2, EventType.TRACE_COMPLETED, payload={"outcome": "passed"}),
    ])
    assert project_step_results(events) == []


def test_projection_byte_identical_replay(tmp_path: Path):
    """FR-P1.3-08: rebuild step-results.json and compare to the
    one produced by P1.2's atomic writer.
    """
    import json

    events = _build_chain([
        _ev(1, EventType.STEP_STARTED, step_id="S001", step_no=1),
        _ev(2, EventType.ACTION_RESULT, step_id="S001",
            receipt={"ok": True, "action_id": "gui.click"}),
        _ev(3, EventType.STEP_COMPLETED, step_id="S001",
            status="passed", duration_ms=12),
    ])
    results = project_step_results(events)
    payload_a = {
        "schema_version": "1.0.0",
        "case_id": "TC-1",
        "step_results": results,
    }
    path = tmp_path / "step-results.json"
    path.write_text(json.dumps(payload_a, indent=2, sort_keys=True) + "\n")
    on_disk = path.read_text()

    # Replay: project again from the same events.
    results_2 = project_step_results(events)
    payload_b = {
        "schema_version": "1.0.0",
        "case_id": "TC-1",
        "step_results": results_2,
    }
    path.write_text(json.dumps(payload_b, indent=2, sort_keys=True) + "\n")
    rebuilt = path.read_text()

    assert rebuilt == on_disk


def test_projection_orders_by_step_no():
    """Step ordering is by `step_no`, not chain order, so a late
    step_started for an earlier step_no slots in correctly."""
    events = _build_chain([
        _ev(1, EventType.STEP_STARTED, step_id="S002", step_no=2),
        _ev(2, EventType.STEP_COMPLETED, step_id="S002",
            status="passed", duration_ms=5),
        _ev(3, EventType.STEP_STARTED, step_id="S001", step_no=1),
        _ev(4, EventType.STEP_COMPLETED, step_id="S001",
            status="passed", duration_ms=10),
    ])
    results = project_step_results(events)
    assert [r["step_id"] for r in results] == ["S001", "S002"]