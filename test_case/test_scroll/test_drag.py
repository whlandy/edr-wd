"""
T4 acceptance — `drag_target` composite: one pointer.drag (A028) from a
resolved control to a release point, window/ownership + movement verified.
(docs/todo/scroll-and-paged-table-actions.md, "T4: drag_target".)

Covers the composite's invariants:

  1. `resolve_drag_target` resolves `target_ref` by target_id / automation_id /
     text / control_type, honouring `expected_process_name` ownership, and
     raises the right `DragTargetResolutionError` code on missing / ambiguous /
     ownership-mismatch.
  2. `grab_point` picks "center" or a "handle" leading-edge point along the
     dominant drag axis.
  3. `_compute_endpoints` accepts exactly one of (dx,dy) offset or absolute
     (x2,y2), and refuses a zero-vector no-op drag.
  4. `plan_drag` emits exactly ONE `pointer.drag` (A028) command — never a
     wheel (A029) or a semantic click (A020).
  5. `step_drag` dispatches A028 and, with verify=True, reports
     SCROLLBAR_DRAGGED (moved=True) only on a verified content change; a
     no-effect or unverified outcome is NO_SCROLL_EFFECT with moved=False;
     a resolution/dispatch failure is NOT_DISPATCHED.
  6. verify=False never returns moved=True.

All tests run against injected stub observer/executor/verifier — no live
backend required.
"""

from __future__ import annotations

import pytest

from observations.models import Target
from observations.snapshot import build_snapshot
from scroll.actions import DispatcherAdapter
from scroll.drag import (
    DragTargetResolutionError,
    center,
    grab_point,
    plan_drag,
    resolve_drag_target,
    step_drag,
)
from scroll.executor import ScrollExecutor
from scroll.results import Reason, Strategy
from action_dispatcher.receipts import ActionReceipt

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ctrl(id_, text, rect=(0, 0, 200, 40), ctype="Slider", proc="App.exe"):
    """A drag-able control. `automation_id` == id_ so resolution is stable."""
    return {
        "process_name": proc, "pid": 1, "native_window_id": "w-a",
        "title": id_, "kind": "control", "control_type": ctype,
        "automation_id": id_, "text": text, "rect": rect,
    }


def _win(proc="App.exe"):
    return {
        "process_name": proc, "pid": 1, "native_window_id": "w-a",
        "title": "Main", "kind": "window",
    }


def _snap(*targets):
    return build_snapshot(
        targets=list(targets), backend="x", host="h",
        captured_at="2026-08-01T00:00:00Z",
    )


class _StubObserver:
    """Returns a fixed sequence of snapshots, one per call."""

    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.calls = 0

    def snapshot(self):
        snap = self.snapshots[min(self.calls, len(self.snapshots) - 1)]
        self.calls += 1
        return snap


class _StubVerify:
    def __init__(self, changed=True):
        from scroll.verifier import PageChange

        self.value = PageChange.CHANGED if changed else PageChange.UNCHANGED

    def page_changed(self, before, after, threshold=0.0):
        return self.value


class _RealDispatcher:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def __call__(self, action_id, *, action_code=None, args=None, target_ref=None):
        self.calls.append((action_id, action_code, args, target_ref))
        if self.ok:
            return ActionReceipt.from_ok(
                action_id=action_id, action_code=action_code,
                request_id=None, result=args,
            )
        return ActionReceipt.from_error(
            code="backend_error", action_id=action_id, action_code=action_code,
            request_id=None, message="boom",
        )


def _executor(ok=True):
    real = _RealDispatcher(ok=ok)
    return ScrollExecutor(DispatcherAdapter(dispatch=real)), real


# ---------------------------------------------------------------------------
# resolve_drag_target — pure resolution
# ---------------------------------------------------------------------------


def test_resolve_by_automation_id():
    snap = _snap(_win(), _ctrl("thumb", "音量"))
    t = resolve_drag_target(snap, {"automation_id": "thumb"})
    assert isinstance(t, Target)
    assert t.automation_id == "thumb"


def test_resolve_by_target_id():
    snap = _snap(_win(), _ctrl("thumb", "音量"))
    t = resolve_drag_target(snap, {"automation_id": "thumb"})
    assert resolve_drag_target(snap, {"target_id": t.target_id}).target_id == t.target_id


def test_resolve_by_text_and_control_type():
    snap = _snap(_win(), _ctrl("t1", "亮度", ctype="Slider"))
    assert resolve_drag_target(snap, {"text": "亮度"}).automation_id == "t1"
    assert resolve_drag_target(snap, {"control_type": "slider"}).automation_id == "t1"


def test_resolve_ownership_gate():
    snap = _snap(_win(), _ctrl("thumb", "音量", proc="Mismatched.exe"))
    with pytest.raises(DragTargetResolutionError) as ei:
        resolve_drag_target(snap, {"automation_id": "thumb"}, expected_process_name="App.exe")
    assert ei.value.code == "ownership_mismatch"


def test_resolve_missing_raises_not_found():
    snap = _snap(_win(), _ctrl("thumb", "音量"))
    with pytest.raises(DragTargetResolutionError) as ei:
        resolve_drag_target(snap, {"automation_id": "nope"})
    assert ei.value.code == "target_not_found"


def test_resolve_ambiguous_raises():
    snap = _snap(_win(), _ctrl("a", "x"), _ctrl("b", "x"))
    with pytest.raises(DragTargetResolutionError) as ei:
        resolve_drag_target(snap, {"text": "x"})
    assert ei.value.code == "target_ambiguous"


def test_resolve_does_not_drag_windows():
    snap = _snap(_win())
    with pytest.raises(DragTargetResolutionError):
        resolve_drag_target(snap, {"title": "Main"})


# ---------------------------------------------------------------------------
# grab_point / center — pure geometry
# ---------------------------------------------------------------------------


def test_center():
    assert center((10, 20, 210, 260)) == (110, 140)


def test_grab_center():
    assert grab_point((0, 0, 200, 100), "center", 20, 0) == (100, 50)


def test_grab_handle_horizontal_right():
    # dominant horizontal drag to the right -> leading (left) quarter.
    assert grab_point((0, 0, 200, 100), "handle", 30, 0) == (50, 100 // 2)


def test_grab_handle_horizontal_left():
    assert grab_point((0, 0, 200, 100), "handle", -30, 0) == (200 - 50, 50)


def test_grab_handle_vertical_down():
    assert grab_point((0, 0, 200, 100), "handle", 0, 20) == (100, 100 // 4)


def test_grab_handle_vertical_up():
    assert grab_point((0, 0, 200, 100), "handle", 0, -20) == (100, 100 - 100 // 4)


def test_grab_unknown_mode_raises():
    with pytest.raises(ValueError):
        grab_point((0, 0, 10, 10), "edge", 0, 5)


# ---------------------------------------------------------------------------
# plan_drag — single A028 command
# ---------------------------------------------------------------------------


def test_plan_drag_emits_only_a028():
    snap = _snap(_win(), _ctrl("thumb", "音量"))
    t = resolve_drag_target(snap, {"automation_id": "thumb"})
    action = plan_drag(t, start=(50, 20), end=(150, 20))
    assert action.action_id == "pointer.drag"
    assert action.action_code == "A028"
    assert action.args["start"] == {"x": 50, "y": 20}
    assert action.args["end"] == {"x": 150, "y": 20}
    # never a wheel (A029) or a semantic click (A020)
    assert action.action_id not in ("pointer.scroll", "gui.click")


# ---------------------------------------------------------------------------
# step_drag — dispatch + verify invariants
# ---------------------------------------------------------------------------


def test_drag_verified_moved_reports_success():
    before = _snap(_win(), _ctrl("thumb", "音量", rect=(0, 0, 200, 40)))
    after = _snap(_win(), _ctrl("thumb", "音量", rect=(80, 0, 280, 40)))
    obs = _StubObserver([before, after])
    ex, real = _executor()

    r = step_drag(
        target_ref={"automation_id": "thumb"},
        before=before,
        dx=80, dy=0, grab="handle",
        observer=obs, verifier=_StubVerify(changed=True), executor=ex,
    )
    assert r.reason is Reason.SCROLLBAR_DRAGGED
    assert r.strategy_used is Strategy.SCROLLBAR_DRAG
    assert r.moved is True and r.dispatched is True
    # exactly one A028 was armed
    assert [(a, c) for a, c, _, _ in real.calls] == [("pointer.drag", "A028")]


def test_drag_no_effect_reports_no_effect():
    before = _snap(_win(), _ctrl("thumb", "音量", rect=(0, 0, 200, 40)))
    obs = _StubObserver([before, before])  # content never changes
    ex, real = _executor()

    r = step_drag(
        target_ref={"automation_id": "thumb"},
        before=before,
        dx=80, dy=0,
        observer=obs, verifier=_StubVerify(changed=False), executor=ex,
    )
    assert r.reason is Reason.NO_SCROLL_EFFECT
    assert r.moved is False
    assert r.success is False
    assert len(real.calls) == 1  # dispatched once, then honest no-effect


def test_drag_verify_false_never_moved_true():
    before = _snap(_win(), _ctrl("thumb", "音量", rect=(0, 0, 200, 40)))
    ex, real = _executor()

    r = step_drag(
        target_ref={"automation_id": "thumb"},
        before=before,
        dx=80, dy=0,
        verify=False, executor=ex,
    )
    assert r.moved is False
    assert r.success is False
    assert r.reason is Reason.NO_SCROLL_EFFECT
    assert [(a, c) for a, c, _, _ in real.calls] == [("pointer.drag", "A028")]


def test_drag_absolute_endpoint():
    before = _snap(_win(), _ctrl("thumb", "音量", rect=(0, 0, 200, 40)))
    after = _snap(_win(), _ctrl("thumb", "音量", rect=(150, 0, 350, 40)))
    obs = _StubObserver([before, after])
    ex, real = _executor()

    r = step_drag(
        target_ref={"automation_id": "thumb"},
        before=before,
        x2=150, y2=20, grab="center",
        observer=obs, verifier=_StubVerify(changed=True), executor=ex,
    )
    assert r.reason is Reason.SCROLLBAR_DRAGGED
    _, _, args, _ = real.calls[0]
    assert args["start"] == {"x": 100, "y": 20}
    assert args["end"] == {"x": 150, "y": 20}


def test_drag_resolution_failure_not_dispatched():
    before = _snap(_win(), _ctrl("thumb", "音量"))
    ex, real = _executor()

    r = step_drag(
        target_ref={"automation_id": "nope"},
        before=before,
        dx=80, dy=0,
        verify=False, executor=ex,
    )
    assert r.reason is Reason.NOT_DISPATCHED
    assert r.moved is False
    assert real.calls == []  # nothing armed on resolution failure


def test_drag_dispatch_failure_not_dispatched():
    before = _snap(_win(), _ctrl("thumb", "音量", rect=(0, 0, 200, 40)))
    ex, real = _executor(ok=False)

    r = step_drag(
        target_ref={"automation_id": "thumb"},
        before=before,
        dx=80, dy=0,
        verify=False, executor=ex,
    )
    assert r.reason is Reason.NOT_DISPATCHED
    assert r.moved is False


def test_drag_zero_vector_refused():
    before = _snap(_win(), _ctrl("thumb", "音量", rect=(0, 0, 200, 40)))
    ex, _ = _executor()
    with pytest.raises(ValueError):
        step_drag(
            target_ref={"automation_id": "thumb"},
            before=before,
            dx=0, dy=0,
            verify=False, executor=ex,
        )


def test_drag_both_endpoint_forms_refused():
    before = _snap(_win(), _ctrl("thumb", "音量", rect=(0, 0, 200, 40)))
    ex, _ = _executor()
    with pytest.raises(ValueError):
        step_drag(
            target_ref={"automation_id": "thumb"},
            before=before,
            dx=10, dy=0, x2=150, y2=20,
            verify=False, executor=ex,
        )


def test_drag_verify_true_without_observer_raises():
    before = _snap(_win(), _ctrl("thumb", "音量", rect=(0, 0, 200, 40)))
    ex, _ = _executor()
    with pytest.raises(ValueError):
        step_drag(
            target_ref={"automation_id": "thumb"},
            before=before,
            dx=10, dy=0,
            verify=True,  # observer is None -> wiring bug
            executor=ex,
        )
