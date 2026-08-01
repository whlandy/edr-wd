"""
P0.3 acceptance gate — resolver 6-step pipeline (architecture §8.2,
FR-P0.3-03, -05, -06).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
_TARGET = _REPO / "target"

if str(_TARGET) not in sys.path:
    sys.path.insert(0, str(TARGET))


import observations as obs  # noqa: E402
from action_catalog import ACTIONS_V1  # noqa: E402


def _build_three_button_snapshot(active_window: str = "T0001") -> obs.ObservationSnapshot:
    targets = [
        {"process_name": "EDRClient.exe", "pid": 1234, "native_window_id": "w1",
         "title": "Main", "kind": "window", "rect": (0, 0, 800, 600)},
        {"process_name": "EDRClient.exe", "pid": 1234, "native_window_id": "w1",
         "title": "OK", "kind": "control", "control_type": "Button",
         "automation_id": "btn-ok", "rect": (100, 100, 200, 150)},
        {"process_name": "EDRClient.exe", "pid": 1234, "native_window_id": "w1",
         "title": "Cancel", "kind": "control", "control_type": "Button",
         "automation_id": "btn-cancel", "rect": (300, 100, 400, 150)},
    ]
    return obs.build_snapshot(targets=targets, backend="windows_pywinauto",
                              host="win26", captured_at="t",
                              active_window_target_id=active_window)


GUI_CLICK = next(a for a in ACTIONS_V1 if a.action_id == "gui.click")
CLICK_SCREEN = next(a for a in ACTIONS_V1
                    if a.action_id == "pointer.click_screen")
CLICK_WINDOW = next(a for a in ACTIONS_V1
                    if a.action_id == "pointer.click_window")


def _make_ref(snap, **kwargs) -> obs.ObservationRef:
    base = {
        "snapshot_id": snap.snapshot_id,
        "target_id": "",
        "expected_process_name": "EDRClient.exe",
        "selector_hint": {},
    }
    base.update(kwargs)
    return obs.ObservationRef(**base)


# ---------------------------------------------------------------------------
# Resolution by each pipeline strategy
# ---------------------------------------------------------------------------


def test_resolve_by_automation_id():
    snap = _build_three_button_snapshot()
    obs.mark_live(snap.snapshot_id)
    ref = _make_ref(snap, selector_hint={"automation_id": "btn-ok"})
    t = obs.resolve_target(ref, snap, GUI_CLICK)
    assert t.target_id == "T0002"
    assert t.automation_id == "btn-ok"


def test_resolve_by_target_id():
    snap = _build_three_button_snapshot()
    obs.mark_live(snap.snapshot_id)
    ref = _make_ref(snap, target_id="T0003")
    t = obs.resolve_target(ref, snap, GUI_CLICK)
    assert t.target_id == "T0003"


def test_resolve_by_fingerprint():
    snap = _build_three_button_snapshot()
    obs.mark_live(snap.snapshot_id)
    fp = snap.targets[1].fingerprint
    ref = _make_ref(snap, fingerprint=fp)
    t = obs.resolve_target(ref, snap, GUI_CLICK)
    assert t.target_id == "T0002"


def test_resolve_by_control_type_with_unique_match():
    """control_type matches 2 buttons; but selector_hint also pins
    `title=OK` → exactly one target."""
    snap = _build_three_button_snapshot()
    obs.mark_live(snap.snapshot_id)
    ref = _make_ref(snap, selector_hint={"control_type": "Button",
                                          "title": "OK"})
    t = obs.resolve_target(ref, snap, GUI_CLICK)
    assert t.target_id == "T0002"


# ---------------------------------------------------------------------------
# Typed-error pipeline (FR-P0.3-03, -04, -05, -06)
# ---------------------------------------------------------------------------


def test_resolver_raises_target_ambiguous():
    snap = _build_three_button_snapshot()
    obs.mark_live(snap.snapshot_id)
    ref = _make_ref(snap, selector_hint={"control_type": "Button"})
    with pytest.raises(obs.TargetResolutionError) as exc:
        obs.resolve_target(ref, snap, GUI_CLICK)
    assert exc.value.code == obs.CODE_TARGET_AMBIGUOUS
    assert exc.value.details["candidate_count"] == 2


def test_resolver_raises_ownership_mismatch():
    snap = _build_three_button_snapshot()
    obs.mark_live(snap.snapshot_id)
    ref = _make_ref(snap, expected_process_name="Wrong.exe")
    with pytest.raises(obs.TargetResolutionError) as exc:
        obs.resolve_target(ref, snap, GUI_CLICK)
    assert exc.value.code == obs.CODE_OWNERSHIP_MISMATCH
    assert exc.value.details["expected_process_name"] == "Wrong.exe"
    assert "EDRClient.exe" in exc.value.details["actual_process_name"]


def test_resolver_raises_target_stale_after_invalidate():
    snap = _build_three_button_snapshot()
    obs.mark_live(snap.snapshot_id)
    ref = _make_ref(snap, target_id="T0002")
    obs.invalidate_snapshot(snap.snapshot_id)
    with pytest.raises(obs.TargetResolutionError) as exc:
        obs.resolve_target(ref, snap, GUI_CLICK)
    assert exc.value.code == obs.CODE_TARGET_STALE


def test_resolver_raises_target_not_found():
    snap = _build_three_button_snapshot()
    obs.mark_live(snap.snapshot_id)
    ref = _make_ref(snap, target_id="T9999")
    with pytest.raises(obs.TargetResolutionError) as exc:
        obs.resolve_target(ref, snap, GUI_CLICK)
    assert exc.value.code == obs.CODE_TARGET_NOT_FOUND


def test_resolver_raises_fallback_not_allowed_for_gui_click():
    """gui.click is a semantic action; rect-proximity fallback is
    refused (architecture §8.2 step 6)."""
    snap = _build_three_button_snapshot()
    obs.mark_live(snap.snapshot_id)
    ref = _make_ref(snap, selector_hint={"rect": (110, 110, 130, 130)})
    with pytest.raises(obs.TargetResolutionError) as exc:
        obs.resolve_target(ref, snap, GUI_CLICK)
    assert exc.value.code == obs.CODE_FALLBACK_NOT_ALLOWED


def test_resolver_allows_rect_for_pointer_click_screen():
    snap = _build_three_button_snapshot()
    obs.mark_live(snap.snapshot_id)
    ref = _make_ref(snap, selector_hint={"rect": (110, 110, 130, 130)})
    t = obs.resolve_target(ref, snap, CLICK_SCREEN)
    assert t.target_id == "T0002"


def test_resolver_allows_rect_for_pointer_click_window():
    snap = _build_three_button_snapshot()
    obs.mark_live(snap.snapshot_id)
    ref = _make_ref(snap, selector_hint={"rect": (310, 110, 330, 130)})
    t = obs.resolve_target(ref, snap, CLICK_WINDOW)
    assert t.target_id == "T0003"


def test_resolver_text_contains_match():
    """text_contains falls in step 5 (architecture §8.2)."""
    targets = [
        {"process_name": "X", "pid": 1, "native_window_id": "w",
         "title": "W", "kind": "window"},
        {"process_name": "X", "pid": 1, "native_window_id": "w",
         "title": "Cancel", "kind": "control", "control_type": "Button",
         "text": "Cancel me"},
    ]
    snap = obs.build_snapshot(targets=targets, backend="x", host="h",
                              captured_at="t", active_window_target_id="T0001")
    obs.mark_live(snap.snapshot_id)
    ref = _make_ref(snap, expected_process_name="X",
                    selector_hint={"text_contains": "Cancel"})
    t = obs.resolve_target(ref, snap, GUI_CLICK)
    assert t.target_id == "T0002"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_resolver_rejects_malformed_ref_target_id():
    """An obviously malformed target_id surfaces as target_stale so
    the caller can re-discover the snapshot."""
    snap = _build_three_button_snapshot()
    obs.mark_live(snap.snapshot_id)
    ref = obs.ObservationRef(snapshot_id=snap.snapshot_id,
                             target_id="NOT_VALID",
                             expected_process_name="EDRClient.exe")
    with pytest.raises(obs.TargetResolutionError) as exc:
        obs.resolve_target(ref, snap, GUI_CLICK)
    assert exc.value.code == obs.CODE_TARGET_STALE


def test_resolver_returns_when_target_id_is_in_fingerprint():
    """If target_id fails to match (e.g. snapshot was invalidated and
    rebuilt), the resolver falls through to fingerprint."""
    # Build two snapshots with the same target structure but different
    # snapshot_id; the ref points to the FIRST snapshot, but
    # fingerprint matches a target in a logically-equivalent set. P0.3
    # does NOT do cross-snapshot matching — fingerprint match only
    # runs against the SAME snapshot's targets. Verify that here:
    snap = _build_three_button_snapshot()
    obs.mark_live(snap.snapshot_id)
    fp = snap.targets[1].fingerprint
    ref = obs.ObservationRef(snapshot_id=snap.snapshot_id, target_id="",
                             expected_process_name="EDRClient.exe",
                             fingerprint=fp)
    t = obs.resolve_target(ref, snap, GUI_CLICK)
    assert t.target_id == "T0002"