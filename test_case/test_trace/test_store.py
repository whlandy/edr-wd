"""P1.3 acceptance gate — TraceStore (open/append/finalize/recovery)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
for p in (str(_REPO / "target"), str(_REPO / "agent")):
    if p not in sys.path:
        sys.path.insert(0, p)


from trace import (  # noqa: E402
    AlreadyFinalized,
    ChainCorrupted,
    EventType,
    TraceStore,
)


# ---------------------------------------------------------------------------
# Open / create
# ---------------------------------------------------------------------------


def test_open_creates_trace(tmp_path: Path):
    store = TraceStore(tmp_path)
    result = store.open(trace_id="TR-001", create=True)
    assert result.trace_id == "TR-001"
    assert not result.recovered
    # Bootstrap event: trace_started.
    assert (tmp_path / "TR-001" / "events.jsonl").exists()
    events = store.read_events()
    assert len(events) == 1
    assert events[0].event_type is EventType.TRACE_STARTED


def test_open_generates_trace_id_when_not_provided(tmp_path: Path):
    store = TraceStore(tmp_path)
    result = store.open(create=True)
    assert result.trace_id.startswith("TR-")


def test_open_existing_trace_does_not_double_bootstrap(tmp_path: Path):
    s1 = TraceStore(tmp_path)
    s1.open(trace_id="TR-002", create=True)
    s1.append_dict(EventType.STEP_STARTED, payload={"step_no": 1}, step_id="S1")
    # Reopen with a new store.
    s2 = TraceStore(tmp_path)
    r = s2.open(trace_id="TR-002", create=False)
    events = s2.read_events()
    # trace_started + step_started = 2.
    assert len(events) == 2


# ---------------------------------------------------------------------------
# Append + finalize
# ---------------------------------------------------------------------------


def test_append_chains_events(tmp_path: Path):
    store = TraceStore(tmp_path)
    store.open(trace_id="TR-003", create=True)
    # open() bootstrap inserts trace_started at seq=1; first user
    # event is seq=2.
    e1 = store.append_dict(EventType.STEP_STARTED, payload={"step_no": 1}, step_id="S1")
    e2 = store.append_dict(EventType.ACTION_RESULT, payload={"receipt": {"ok": True}}, step_id="S1")
    assert e1.sequence_no == 2
    assert e2.sequence_no == 3
    assert e2.previous_hash == e1.event_hash


def test_finalize_writes_terminal_event(tmp_path: Path):
    store = TraceStore(tmp_path)
    store.open(trace_id="TR-004", create=True)
    store.append_dict(EventType.STEP_COMPLETED, payload={"status": "passed"}, step_id="S1")
    final = store.finalize(EventType.TRACE_COMPLETED, payload={"outcome": "passed"})
    assert final.event_type is EventType.TRACE_COMPLETED
    events = store.read_events()
    assert events[-1].event_type is EventType.TRACE_COMPLETED


def test_finalize_rejects_non_terminal_event(tmp_path: Path):
    store = TraceStore(tmp_path)
    store.open(trace_id="TR-005", create=True)
    with pytest.raises(ValueError, match="terminal"):
        store.finalize(EventType.STEP_COMPLETED, payload={})


def test_append_after_finalize_raises(tmp_path: Path):
    store = TraceStore(tmp_path)
    store.open(trace_id="TR-006", create=True)
    store.finalize(EventType.TRACE_COMPLETED)
    with pytest.raises(AlreadyFinalized):
        store.append_dict(EventType.STEP_COMPLETED, payload={})


# ---------------------------------------------------------------------------
# Integrity
# ---------------------------------------------------------------------------


def test_verify_clean_chain(tmp_path: Path):
    store = TraceStore(tmp_path)
    store.open(trace_id="TR-007", create=True)
    for i in range(1, 4):
        store.append_dict(
            EventType.STEP_STARTED if i == 1 else EventType.STEP_COMPLETED,
            payload={"step_no": i},
            step_id=f"S{i}",
        )
    store.finalize(EventType.TRACE_COMPLETED)
    report = store.verify()
    assert report.ok


def test_verify_rejects_tamper_in_middle(tmp_path: Path):
    """Modify a stored event's event_hash; reopen fails (FR-P1.3-07)."""
    store = TraceStore(tmp_path)
    store.open(trace_id="TR-008", create=True)
    store.append_dict(EventType.STEP_STARTED, payload={"step_no": 1}, step_id="S1")
    store.append_dict(EventType.STEP_COMPLETED, payload={"status": "passed"}, step_id="S1")
    store.finalize(EventType.TRACE_COMPLETED)

    # Tamper with the JSONL on disk: replace the event_hash on
    # line 2 (step_started) with a different but structurally
    # valid sha256 hex.
    events_file = store.events_path
    text = events_file.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert len(lines) >= 3
    # The hash is somewhere in the middle of line 2.
    import re
    line2 = lines[1]
    m = re.search(r'"event_hash":\s*"sha256:[0-9a-f]{64}"', line2)
    assert m is not None, line2
    # Replace first 4 chars of the hash body.
    original = m.group(0)
    body_start = original.index("sha256:") + len("sha256:")
    new_hash = original[:body_start] + "DEAD" + original[body_start+4:]
    new_line2 = line2.replace(original, new_hash)
    lines[1] = new_line2
    events_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Re-open: chain corruption must raise.
    s2 = TraceStore(tmp_path)
    with pytest.raises(ChainCorrupted):
        s2.open(trace_id="TR-008", create=False)


# ---------------------------------------------------------------------------
# Crash-tail recovery
# ---------------------------------------------------------------------------


def test_incomplete_final_line_recovery(tmp_path: Path):
    """Append a partial trailing line; reopen, recover, verify (FR-P1.3-06)."""
    store = TraceStore(tmp_path)
    store.open(trace_id="TR-009", create=True)
    store.append_dict(EventType.STEP_STARTED, payload={"step_no": 1}, step_id="S1")
    store.append_dict(EventType.STEP_COMPLETED, payload={"status": "passed"}, step_id="S1")

    # Append a partial line (simulating crash mid-write).
    events_file = store.events_path
    with events_file.open("ab") as fh:
        fh.write(b'{"incomplete": "tra')  # no newline, no closing

    # Reopen: should detect incomplete tail and recover.
    s2 = TraceStore(tmp_path)
    r = s2.open(trace_id="TR-009", create=False)
    assert r.recovered is True
    assert any("incomplete final line" in i for i in r.issues)

    # The chain now contains trace_recovered as the last event.
    events = s2.read_events()
    assert events[-1].event_type is EventType.TRACE_RECOVERED

    # Verify the recovered chain.
    report = s2.verify()
    assert report.ok


def test_two_stores_verify_same_chain(tmp_path: Path):
    """Two TraceStore instances opening the same directory verify
    the same chain (acceptance #2)."""
    s1 = TraceStore(tmp_path)
    s1.open(trace_id="TR-010", create=True)
    s1.append_dict(EventType.STEP_STARTED, payload={"step_no": 1}, step_id="S1")
    s1.finalize(EventType.TRACE_COMPLETED)

    s2 = TraceStore(tmp_path)
    s2.open(trace_id="TR-010", create=False)
    r1 = s1.verify()
    r2 = s2.verify()
    assert r1.ok and r2.ok
    # Same number of valid events (trace_started + step_started
    # + trace_completed = 3).
    assert len(s1.read_events()) == len(s2.read_events()) == 3


# ---------------------------------------------------------------------------
# Corruption earlier in the file
# ---------------------------------------------------------------------------


def test_corruption_in_earlier_line_rejected(tmp_path: Path):
    """Edit a historical event's hash; reopen fails (FR-P1.3-07)."""
    store = TraceStore(tmp_path)
    store.open(trace_id="TR-011", create=True)
    store.append_dict(EventType.STEP_STARTED, payload={"step_no": 1}, step_id="S1")
    store.append_dict(EventType.STEP_COMPLETED, payload={"status": "passed"}, step_id="S1")

    events_file = store.events_path
    text = events_file.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert len(lines) >= 3
    import re
    line2 = lines[1]
    m = re.search(r'"event_hash":\s*"sha256:[0-9a-f]{64}"', line2)
    assert m is not None, line2
    original = m.group(0)
    body_start = original.index("sha256:") + len("sha256:")
    new_hash = original[:body_start] + "DEAD" + original[body_start+4:]
    new_line2 = line2.replace(original, new_hash)
    lines[1] = new_line2
    events_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    s2 = TraceStore(tmp_path)
    with pytest.raises(ChainCorrupted):
        s2.open(trace_id="TR-011", create=False)