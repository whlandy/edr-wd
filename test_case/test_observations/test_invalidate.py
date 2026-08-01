"""
P0.3 acceptance gate — invalidate_snapshot registry (FR-P0.3-08).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
_TARGET = _REPO / "target"

if str(_TARGET) not in sys.path:
    sys.path.insert(0, str(_TARGET))


import observations as obs  # noqa: E402
from action_catalog import ACTIONS_V1  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_registry():
    """Reset the global snapshot registry between tests."""
    obs.invalidate.reset_for_tests()
    yield
    obs.invalidate.reset_for_tests()


GUI_CLICK = next(a for a in ACTIONS_V1 if a.action_id == "gui.click")


def _make_snapshot() -> obs.ObservationSnapshot:
    targets = [
        {"process_name": "X", "pid": 1, "native_window_id": "w",
         "title": "Main", "kind": "window"},
        {"process_name": "X", "pid": 1, "native_window_id": "w",
         "title": "OK", "kind": "control", "control_type": "Button"},
    ]
    return obs.build_snapshot(targets=targets, backend="x",
                              host="h", captured_at="t",
                              active_window_target_id="T0001")


def test_unknown_snapshot_id_is_not_live():
    snap = _make_snapshot()
    # Without mark_live, is_live returns False.
    assert not obs.is_live(snap.snapshot_id)


def test_mark_live_makes_snapshot_resolvable():
    snap = _make_snapshot()
    obs.mark_live(snap.snapshot_id)
    assert obs.is_live(snap.snapshot_id)
    ref = obs.ObservationRef(snapshot_id=snap.snapshot_id, target_id="T0002",
                             expected_process_name="X")
    t = obs.resolve_target(ref, snap, GUI_CLICK)
    assert t.target_id == "T0002"


def test_invalidate_snapshot_blocks_subsequent_resolution():
    snap = _make_snapshot()
    obs.mark_live(snap.snapshot_id)
    obs.invalidate_snapshot(snap.snapshot_id)
    assert not obs.is_live(snap.snapshot_id)
    ref = obs.ObservationRef(snapshot_id=snap.snapshot_id, target_id="T0002",
                             expected_process_name="X")
    from observations.resolver import TargetResolutionError
    with pytest.raises(TargetResolutionError) as exc:
        obs.resolve_target(ref, snap, GUI_CLICK)
    assert exc.value.code == obs.CODE_TARGET_STALE


def test_forget_drops_entry():
    snap = _make_snapshot()
    obs.mark_live(snap.snapshot_id)
    obs.forget(snap.snapshot_id)
    assert not obs.is_live(snap.snapshot_id)


def test_mark_invalid_does_not_remove_entry():
    """`mark_invalid` records False so that subsequent `is_live`
    lookups return False (rather than KeyError); `forget` removes
    the entry entirely."""
    snap = _make_snapshot()
    obs.mark_live(snap.snapshot_id)
    obs.mark_invalid(snap.snapshot_id)
    # No KeyError; is_live returns False.
    assert not obs.is_live(snap.snapshot_id)