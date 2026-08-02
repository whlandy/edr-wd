"""P1.3 acceptance gate — event envelope + integrity chain."""

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
    IntegrityReport,
    SCHEMA_VERSION,
    TERMINAL_EVENT_TYPES,
    TraceEvent,
    canonical_event_hash,
    from_dict,
    load_events,
    stamp_event,
    verify_chain,
)


# ---------------------------------------------------------------------------
# EventType catalogue
# ---------------------------------------------------------------------------


def test_event_type_catalog_complete():
    """Architecture §12.2 defines 25 lifecycle events; P1.3 adds
    trace_recovered for crash-tail repair (FR-P1.3-06).

    Total V1 event types: 26 = 25 + 1."""
    expected = {
        "trace_started", "environment_recorded", "plan_created",
        "plan_validated", "plan_rejected", "observation_recorded",
        "step_started", "checkpoint_created", "action_requested",
        "action_started", "action_result", "transition_detected",
        "unexpected_transition", "screenshot_captured",
        "screenshot_persisted", "expectation_result", "step_completed",
        "recovery_requested", "branch_created", "recovery_result",
        "replan_created", "cleanup_started", "cleanup_completed",
        "trace_completed", "trace_aborted", "trace_recovered",
    }
    assert {e.value for e in EventType} == expected


def test_terminal_event_types():
    assert EventType.TRACE_COMPLETED in TERMINAL_EVENT_TYPES
    assert EventType.TRACE_ABORTED in TERMINAL_EVENT_TYPES
    assert EventType.STEP_STARTED not in TERMINAL_EVENT_TYPES


# ---------------------------------------------------------------------------
# Chain construction helper
# ---------------------------------------------------------------------------


def _ev(seq: int, event_type: EventType = EventType.STEP_STARTED,
        **payload) -> TraceEvent:
    """Build an event WITHOUT a hash; callers stamp + chain."""
    return TraceEvent(
        schema_version=SCHEMA_VERSION,
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
        step_id="S1",
        call_id=None,
        payload=payload,
        previous_hash=None,
        event_hash="",
    )


def _build_chain(events: list[TraceEvent]) -> list[TraceEvent]:
    """Stamp each event and chain `previous_hash` correctly."""
    out: list[TraceEvent] = []
    prev_hash: str | None = None
    for e in events:
        stamped = stamp_event(e)
        # Re-stamp with the right previous_hash.
        stamped = dataclasses.replace(stamped, previous_hash=prev_hash)
        final_hash = canonical_event_hash(stamped)
        stamped = dataclasses.replace(stamped, event_hash=final_hash)
        out.append(stamped)
        prev_hash = final_hash
    return out


# ---------------------------------------------------------------------------
# Canonical hash + chain
# ---------------------------------------------------------------------------


def test_canonical_hash_determinism():
    """Same inputs -> same hash (FR-P1.3-02)."""
    e1 = _ev(1, payload={"step_no": 1})
    e2 = _ev(1, payload={"step_no": 1})
    h1 = canonical_event_hash(e1)
    h2 = canonical_event_hash(e2)
    assert h1 == h2
    assert h1.startswith("sha256:")


def test_chain_continuity():
    """A correctly-chained sequence verifies cleanly."""
    chain = _build_chain([_ev(1), _ev(2), _ev(3)])
    report = verify_chain(chain)
    assert report.ok
    assert report.issues == ()


def test_tamper_detection_remove_event():
    """Removing a middle event fails verification (FR-P1.3-04)."""
    chain = _build_chain([_ev(1), _ev(2), _ev(3), _ev(4)])
    chain.pop(1)
    report = verify_chain(chain)
    assert not report.ok
    assert any(i.code == "bad_previous_hash" for i in report.issues)


def test_tamper_detection_reorder():
    """Reordering two events fails verification (FR-P1.3-04)."""
    chain = _build_chain([_ev(1), _ev(2), _ev(3)])
    chain[1], chain[2] = chain[2], chain[1]
    report = verify_chain(chain)
    assert not report.ok


def test_tamper_detection_insert():
    """Inserting an event mid-stream fails verification (FR-P1.3-05)."""
    chain = _build_chain([_ev(1), _ev(2), _ev(3)])
    # Re-stamp a forged event so it slots in between seq=2 and seq=3.
    forged = _ev(99, payload={"forged": True})
    forged = dataclasses.replace(
        stamp_event(forged),
        previous_hash=chain[1].event_hash,
        sequence_no=3,
    )
    forged = dataclasses.replace(
        forged, event_hash=canonical_event_hash(forged),
    )
    chain.insert(2, forged)
    report = verify_chain(chain)
    assert not report.ok


def test_first_event_must_have_no_previous_hash():
    chain = _build_chain([_ev(1)])
    report = verify_chain(chain)
    assert report.ok


def test_first_event_with_previous_hash_rejected():
    """Chain rule: first event has previous_hash=None (FR-P1.3-04)."""
    chain = _build_chain([_ev(1)])
    # Patch the first event to claim a previous_hash.
    e = chain[0]
    e = dataclasses.replace(
        e,
        previous_hash="sha256:deadbeef",
    )
    e = dataclasses.replace(e, event_hash=canonical_event_hash(e))
    chain = [e]
    report = verify_chain(chain)
    assert not report.ok
    assert any(i.code == "bad_previous_hash" for i in report.issues)


def test_bad_hash_rejected():
    """Manually corrupting an event's hash is detected."""
    chain = _build_chain([_ev(1), _ev(2)])
    # Tamper with the second event's stored hash.
    e = chain[1]
    e = dataclasses.replace(e, event_hash="sha256:0000000000000000")
    chain[1] = e
    report = verify_chain(chain)
    assert not report.ok
    assert any(i.code == "bad_hash" for i in report.issues)


# ---------------------------------------------------------------------------
# from_dict round-trip
# ---------------------------------------------------------------------------


def test_from_dict_round_trip():
    chain = _build_chain([_ev(1, payload={"step_no": 1, "tag": "x"})])
    d = chain[0].to_dict()
    e2 = from_dict(d)
    assert chain[0] == e2


def test_from_dict_rejects_extra_field():
    chain = _build_chain([_ev(1, payload={"x": 1})])
    d = chain[0].to_dict()
    d["unknown_field"] = "bad"
    with pytest.raises(ValueError, match="unexpected fields"):
        from_dict(d)


def _json(v):
    import json
    return json.dumps(v, ensure_ascii=False)


def test_load_events_parses_jsonl():
    chain = _build_chain([_ev(1), _ev(2), _ev(3)])
    jsonl = "\n".join(
        # Use the same canonical bytes shape the store writes.
        # We use to_dict() + json.dumps for the test since the
        # store uses canonical_bytes which is equivalent.
        "{" + ",".join(
            f'"{k}":{_json(v)}' for k, v in ev.to_dict().items()
        ) + "}"
        for ev in chain
    )
    parsed = load_events(jsonl)
    assert len(parsed) == 3


def test_load_events_rejects_garbage():
    with pytest.raises(ValueError, match="not valid JSON"):
        load_events('not-json\n')


def test_load_events_rejects_non_trace_event_dict():
    """A valid JSON object that isn't a TraceEvent is rejected with
    an envelope-shape error."""
    with pytest.raises(ValueError, match="unexpected fields"):
        load_events('{"ok": true}\n')