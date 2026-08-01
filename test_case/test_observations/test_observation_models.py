"""
P0.3 acceptance gate — strict models + round-trip + snapshot
construction (architecture §8.1, FR-P0.3-01).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
_TARGET = _REPO / "target"
_FIX = _REPO / "test_case" / "fixtures" / "observation"

if str(_TARGET) not in sys.path:
    sys.path.insert(0, str(_TARGET))


import observations as obs  # noqa: E402
from protocol_models.models import ProtocolModelError  # noqa: E402


V1_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Strict mode (mirror of P0.2 acceptance gate)
# ---------------------------------------------------------------------------


def test_observation_snapshot_strict_rejects_unknown_field():
    payload = {
        "schema_version": V1_VERSION,
        "snapshot_id": "s",
        "captured_at": "t",
        "backend": "windows_pywinauto",
        "host": "h",
        "active_window": None,
        "targets": [],
        "tree_digest": "d",
        "rogue": 1,
    }
    with pytest.raises(ProtocolModelError) as exc:
        obs.ObservationSnapshot.from_dict(payload)
    assert exc.value.code == "unknown_field"


def test_target_strict_rejects_unknown_field():
    payload = {
        "target_id": "T0001",
        "kind": "window",
        "process_name": "x",
        "pid": 1,
        "native_window_id": "w",
        "title": "t",
        "ghost": 1,
    }
    with pytest.raises(ProtocolModelError) as exc:
        obs.Target.from_dict(payload)
    assert exc.value.code == "unknown_field"


def test_target_strict_rejects_invalid_kind():
    payload = {
        "target_id": "T0001",
        "kind": "ghost_kind",
        "process_name": "x",
        "pid": 1,
        "native_window_id": "w",
        "title": "t",
    }
    with pytest.raises(ProtocolModelError) as exc:
        obs.Target.from_dict(payload)
    assert exc.value.code == "type_error"


def test_target_rejects_malformed_target_id():
    with pytest.raises(ProtocolModelError):
        obs.Target(target_id="NOT_T_FMT", kind="window", process_name="x",
                   pid=1, native_window_id="w", title="t")
    with pytest.raises(ProtocolModelError):
        obs.Target(target_id="T12", kind="window", process_name="x",
                   pid=1, native_window_id="w", title="t")  # too short


def test_target_rejects_bad_rect():
    with pytest.raises(ProtocolModelError):
        obs.Target(target_id="T0001", kind="control", process_name="x",
                   pid=1, native_window_id="w", title="t",
                   rect=(1, 2, 3))  # not a 4-tuple


def test_observation_ref_target_id_may_be_empty():
    """P0.3 ObservationRef allows target_id="" (unknown state)."""
    ref = obs.ObservationRef(snapshot_id="s", target_id="")
    assert ref.target_id == ""


def test_observation_ref_strict_rejects_unknown_field():
    with pytest.raises(ProtocolModelError) as exc:
        obs.ObservationRef.from_dict({"snapshot_id": "s", "surprise": 1})
    assert exc.value.code == "unknown_field"


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_observation_snapshot_round_trip():
    payload = json.loads(
        (_FIX / "windows_uia_minimal.json").read_text()
    )
    snap = obs.ObservationSnapshot.from_dict(payload)
    assert snap.snapshot_id == payload["snapshot_id"]
    assert snap.backend == "windows_pywinauto"
    assert len(snap.targets) == 3
    assert snap.tree_digest == payload["tree_digest"]
    assert snap.active_window.target_id == "T0001"


def test_macos_ax_snapshot_round_trip():
    payload = json.loads((_FIX / "macos_ax_minimal.json").read_text())
    snap = obs.ObservationSnapshot.from_dict(payload)
    assert snap.backend == "macos_accessibility"
    assert len(snap.targets) == 2
    assert snap.targets[1].control_type == "AXButton"


# ---------------------------------------------------------------------------
# Deterministic assignment
# ---------------------------------------------------------------------------


def test_assignment_is_deterministic_across_calls():
    """Same input list (in the same order) produces the same
    target_ids and the same tree_digest (FR-P0.3-01).

    Note: P0.3 follows the "stable" rule (architecture §8.1):
    input order affects target_id ordering when stable sort keys
    are equal (same process_name + pid + native_window_id).
    Content-only equality without order is not a P0.3 acceptance
    gate; it belongs to the fingerprint (a separate content-
    addressed digest). See the next test for that distinction.
    """
    base = [
        {"process_name": "EDRClient.exe", "pid": 1234,
         "native_window_id": "w1", "title": "Main",
         "kind": "window"},
        {"process_name": "EDRClient.exe", "pid": 1234,
         "native_window_id": "w1", "title": "OK",
         "kind": "control", "control_type": "Button",
         "automation_id": "btn-ok"},
        {"process_name": "EDRClient.exe", "pid": 1234,
         "native_window_id": "w1", "title": "Cancel",
         "kind": "control", "control_type": "Button",
         "automation_id": "btn-cancel"},
    ]
    snap_a = obs.build_snapshot(
        targets=list(base), backend="x", host="h", captured_at="t",
    )
    snap_b = obs.build_snapshot(
        targets=list(base), backend="x", host="h", captured_at="t",
    )
    a_ids = [t.target_id for t in snap_a.targets]
    b_ids = [t.target_id for t in snap_b.targets]
    assert a_ids == b_ids == ["T0001", "T0002", "T0003"]
    assert snap_a.tree_digest == snap_b.tree_digest


def test_assignment_returns_typed_assignment_result():
    """Blocker 1 (P0.3 review #1): assign_target_ids must return a
    typed dataclass, not a bare tuple. This guards against future
    regressions where the API silently drifts back to (list, str)."""
    base = [
        {"process_name": "X", "pid": 1, "native_window_id": "w",
         "title": "T", "kind": "window"},
    ]
    from observations.assignment import (
        AssignmentResult, assign_target_ids,
    )
    result = assign_target_ids(base)
    assert isinstance(result, AssignmentResult)
    assert isinstance(result.targets, tuple)
    assert isinstance(result.tree_digest, str)
    assert result.tree_digest.startswith("sha256:")
    assert len(result.targets) == 1
    assert result.targets[0]["target_id"] == "T0001"


def test_input_order_changes_target_id_assignment():
    """Stable sort: when (process_name, pid, native_window_id)
    collide, the input order determines the target_id ordinal.
    Fingerprints and tree_digest are still deterministic across
    orderings (content-addressed); only the target_id ordinals
    shift."""
    base = [
        {"process_name": "X", "pid": 1, "native_window_id": "w",
         "title": "A", "kind": "window"},
        {"process_name": "X", "pid": 1, "native_window_id": "w",
         "title": "B", "kind": "control"},
    ]
    snap_a = obs.build_snapshot(targets=list(base), backend="x",
                                host="h", captured_at="t")
    snap_b = obs.build_snapshot(targets=list(reversed(base)),
                                backend="x", host="h", captured_at="t")
    # Titles preserved in respective positions; the assignment
    # uses input order as the stable tie-breaker.
    assert snap_a.targets[0].title == "A"
    assert snap_b.targets[0].title == "B"
    # Fingerprints (per-target) are content-addressed — must match
    # even though the ordinals differ.
    fp_a = {t.title: t.fingerprint for t in snap_a.targets}
    fp_b = {t.title: t.fingerprint for t in snap_b.targets}
    assert fp_a == fp_b


def test_assignment_uses_stable_sort_key():
    """Sort by (process_name, pid, native_window_id, native_index)."""
    t1 = {"process_name": "B.exe", "pid": 1, "native_window_id": "w1",
          "title": "B", "kind": "window"}
    t2 = {"process_name": "A.exe", "pid": 1, "native_window_id": "w1",
          "title": "A", "kind": "window"}
    snap = obs.build_snapshot(targets=[t1, t2], backend="x", host="h",
                              captured_at="t")
    # A.exe < B.exe → A first
    assert snap.targets[0].process_name == "A.exe"
    assert snap.targets[0].target_id == "T0001"
    assert snap.targets[1].process_name == "B.exe"
    assert snap.targets[1].target_id == "T0002"


def test_active_window_target_id_must_exist():
    with pytest.raises(ProtocolModelError):
        obs.build_snapshot(
            targets=[{"process_name": "x", "pid": 1, "native_window_id": "w",
                      "title": "t", "kind": "window"}],
            backend="x", host="h", captured_at="t",
            active_window_target_id="T9999",
        )


def test_build_snapshot_requires_at_least_one_target():
    with pytest.raises(ProtocolModelError):
        obs.build_snapshot(targets=[], backend="x", host="h", captured_at="t")