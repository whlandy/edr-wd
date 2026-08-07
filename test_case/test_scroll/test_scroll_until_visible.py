"""
T3 acceptance — `scroll_until_visible` target-driven bounded scroll
(docs/todo/scroll-and-paged-table-actions.md, "T3: scroll_until_visible").

Covers the composite's invariants:

  1. Probe BEFORE the first dispatch — a target already visible performs ZERO
     input and returns `TARGET_VISIBLE` with `moved=False`.
  2. The loop advances in ONE direction (down -> MOVE_NEXT / negative wheel +
     pagination next; up -> MOVE_PREV / positive wheel + pagination prev) until
     the target text appears, counting only moved-but-unmatched steps against
     `max_steps`.
  3. It stops immediately on NOT_DISPATCHED (empty plan / failed dispatch) and
     NO_SCROLL_EFFECT (verify=True, a step that moved nothing).
  4. Bounded exhaustion returns `TARGET_NOT_FOUND`, never an infinite spin.
  5. verify=False never returns moved=True (the found outcome stays honest).

The pure `find_target` probe and the `scroll_until_visible` driver are tested
through injected stub detector/controller/executor/observer/verifier so no live
backend is required.
"""

from __future__ import annotations

import pytest

from observations.models import Target
from observations.snapshot import build_snapshot
from scroll.controller import DEFAULT_WHEEL_CLICKS, NavigationMode, PageController
from scroll.detect import DetectionResult, PageDetector, PageStructure
from scroll.executor import ScrollExecutor
from scroll.results import Reason
from scroll.scroll_until_visible import DEFAULT_MAX_STEPS, find_target, scroll_until_visible
from scroll.actions import DispatcherAdapter
from action_dispatcher.receipts import ActionReceipt

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ctrl(id_, text, kind="control", ctype="ListItem"):
    """Build a snapshot dict (matches sibling test helpers); build_snapshot
    materialises a real Target with a T#### target_id for us."""
    return {
        "process_name": "X", "pid": 1, "native_window_id": "w",
        "title": id_, "kind": kind, "control_type": ctype,
        "automation_id": id_, "text": text,
    }


def _win():
    return {
        "process_name": "X", "pid": 1, "native_window_id": "w",
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


class _RealObserver:
    """snapshot() peeks the next queued snapshot without consuming (used for
    verifier-driven tests where we need a stable 'before' reference)."""

    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.calls = 0

    def snapshot(self):
        snap = self.snapshots[min(self.calls, len(self.snapshots) - 1)]
        self.calls += 1
        return snap


class _StubVerify:
    """page_changed returns CHANGED (moved) by default, or a fixed value."""

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


def _infinite_detector():
    """DetectionResult for an INFINITE list (wheel-scrollable)."""

    def detect(snapshot):
        return DetectionResult(structure=PageStructure.INFINITE, confidence=0.9)

    return _SimpleDetector(detect)


class _SimpleDetector:
    def __init__(self, fn):
        self._fn = fn

    def detect(self, snapshot):
        return self._fn(snapshot)


# ---------------------------------------------------------------------------
# find_target — pure probe
# ---------------------------------------------------------------------------


def test_find_target_matches_control_text_regex_case_insensitive():
    snap = _snap(_win(), _ctrl("r1", "Alerts"), _ctrl("r2", "安全设置"))
    assert find_target(snap, "alerts") is not None
    assert find_target(snap, "安全") is not None


def test_find_target_ignores_window_titles():
    snap = _snap(_win(), _ctrl("r1", "Alerts"))
    # A window titled "Main" plus a control "Alerts": probing for the window's
    # title must NOT match (only controls are probed).
    assert find_target(snap, "Main") is None
    assert find_target(snap, "Alerts") is not None


def test_find_target_none_when_not_present():
    snap = _snap(_win(), _ctrl("r1", "Alerts"))
    assert find_target(snap, "NOPE") is None


def test_find_target_invalid_regex_raises():
    with pytest.raises(ValueError):
        find_target(_snap(_win()), "[unclosed")


# ---------------------------------------------------------------------------
# scroll_until_visible — driver invariants
# ---------------------------------------------------------------------------


def test_target_already_visible_zero_dispatch():
    """Probe BEFORE the first dispatch: an already-visible target performs ZERO
    input and returns TARGET_VISIBLE, moved=False."""
    snap = _snap(_win(), _ctrl("T", "防护中心"))
    obs = _StubObserver([snap])
    ex, real = _executor()
    r = scroll_until_visible(
        target_text_re="防护",
        direction="down",
        detector=_infinite_detector(),
        executor=ex,
        observer=obs,
        verifier=_StubVerify(),
        max_steps=DEFAULT_MAX_STEPS,
    )
    assert r.reason is Reason.TARGET_VISIBLE
    assert r.dispatched is False and r.moved is False
    assert real.calls == []  # nothing was ever armed


def test_found_after_single_move_reports_success():
    """Target on page 2: exactly one advance (negative wheel for down), then
    success with moved=True because verification observed the change."""
    before = _snap(_win(), _ctrl("r1", "row1"))
    after = _snap(_win(), _ctrl("r1", "row1"), _ctrl("r2", "目标行"))
    obs = _StubObserver([before, after])  # 1st = before, 2nd = after
    ex, real = _executor()
    r = scroll_until_visible(
        target_text_re="目标行",
        direction="down",
        detector=_infinite_detector(),
        executor=ex,
        observer=obs,
        verifier=_StubVerify(changed=True),
        max_steps=5,
    )
    assert r.reason is Reason.TARGET_VISIBLE
    assert r.dispatched is True and r.moved is True
    assert r.success is True
    # down -> MOVE_NEXT -> INFINITE -> one A029 wheel with negative clicks.
    assert [(a, c, a_) for a, c, a_, _ in real.calls] == [
        ("pointer.scroll", "A029", {"clicks": DEFAULT_WHEEL_CLICKS})
    ]


def test_found_after_n_moves_counts_only_moved_unmatched_steps():
    """Target appears on page N+1: executes exactly N+1 advances (N
    moved-but-unmatched counted + 1 finder step), stopping as soon as probed."""

    def seq():
        yield _snap(_win(), _ctrl("r0", "row0"))          # before (initial)
        yield _snap(_win(), _ctrl("r0", "row0"), _ctrl("r1", "row1"))  # step1 moved, no target
        yield _snap(_win(), _ctrl("r0", "row0"), _ctrl("r1", "row1"),
                    _ctrl("r2", "row2"))                  # step2 moved, no target
        yield _snap(_win(), _ctrl("r0", "row0"), _ctrl("r1", "row1"),
                    _ctrl("r2", "row2"), _ctrl("r3", "目标"))  # step3 moved, target!

    ex, real = _executor()
    r = scroll_until_visible(
        target_text_re="目标",
        direction="down",
        detector=_infinite_detector(),
        executor=ex,
        observer=_StubObserver(list(seq())),
        verifier=_StubVerify(changed=True),
        max_steps=10,
    )
    assert r.reason is Reason.TARGET_VISIBLE
    assert r.moved is True
    assert len(real.calls) == 3  # exactly 3 advances, not more


def test_direction_up_uses_positive_wheel():
    """up -> MOVE_PREV -> INFINITE -> one A029 wheel with POSITIVE clicks."""
    before = _snap(_win(), _ctrl("r1", "row1"))
    after = _snap(_win(), _ctrl("r0", "row0"), _ctrl("r1", "row1"))
    obs = _StubObserver([before, after])
    ex, real = _executor()
    r = scroll_until_visible(
        target_text_re="row0",
        direction="up",
        detector=_infinite_detector(),
        executor=ex,
        observer=obs,
        verifier=_StubVerify(changed=True),
        max_steps=5,
    )
    assert r.reason is Reason.TARGET_VISIBLE
    assert r.moved is True
    assert [(a, c, a_) for a, c, a_, _ in real.calls] == [
        ("pointer.scroll", "A029", {"clicks": -DEFAULT_WHEEL_CLICKS})
    ]


def test_paginated_down_emits_a020_next_clicks():
    """down on a PAGINATED surface routes through the semantic next owner
    (A020 gui.click), not a wheel."""
    from scroll.detect import DetectedControl

    base = Target(
        target_id="T0001", kind="control", process_name="X", pid=1,
        native_window_id="w", title="next", control_type="Button",
        automation_id="nextPageButton", text="下一页",
    )
    dc = DetectedControl(target=base, enabled=True)

    def detect(snapshot):
        return DetectionResult(
            structure=PageStructure.PAGINATED, confidence=0.95,
            next_controls=(dc,),
        )

    before = _snap(_win(), _ctrl("next", "下一页", ctype="Button"))
    after = _snap(_win(), _ctrl("next", "下一页", ctype="Button"),
                  _ctrl("r2", "第二页"))
    obs = _StubObserver([before, after])
    ex, real = _executor()
    r = scroll_until_visible(
        target_text_re="第二页",
        direction="down",
        detector=_SimpleDetector(detect),
        executor=ex,
        observer=obs,
        verifier=_StubVerify(changed=True),
        max_steps=5,
    )
    assert r.reason is Reason.TARGET_VISIBLE
    assert r.moved is True
    assert [(a, c, a_) for a, c, a_, _ in real.calls] == [("gui.click", "A020", {})]


def test_never_found_terminates_within_max_steps():
    """Moved-but-unmatched forever -> TARGET_NOT_FOUND after exactly max_steps
    advances; never an infinite spin and never a fabricated success."""
    snapshot = _snap(_win(), _ctrl("r1", "row"))
    obs = _StubObserver([snapshot])  # flat: after == before (moved, no target)

    ex, real = _executor()
    r = scroll_until_visible(
        target_text_re="NOPE",
        direction="down",
        detector=_infinite_detector(),
        executor=ex,
        observer=obs,
        verifier=_StubVerify(changed=True),
        max_steps=4,
    )
    assert r.reason is Reason.TARGET_NOT_FOUND
    assert r.moved is False
    assert r.success is False
    # Verified-move steps keep counting: 4 moves, all unmatched.
    assert len(real.calls) == 4


def test_no_effect_stops_immediately():
    """verify=True and a step that moved nothing -> NO_SCROLL_EFFECT, stop."""
    snapshot = _snap(_win(), _ctrl("r1", "row"))
    obs = _StubObserver([snapshot, snapshot])  # content never changes
    ex, real = _executor()
    r = scroll_until_visible(
        target_text_re="NOPE",
        direction="down",
        detector=_infinite_detector(),
        executor=ex,
        observer=obs,
        verifier=_StubVerify(changed=False),
        max_steps=10,
    )
    assert r.reason is Reason.NO_SCROLL_EFFECT
    assert r.moved is False
    assert len(real.calls) == 1  # exactly one step, then stop


def test_not_dispatched_on_empty_plan():
    """A structure the controller cannot plan -> NOT_DISPATCHED, no dispatch."""
    from scroll.detect import DetectedControl, PageStructure

    class NoPlanDetector:
        def detect(self, snapshot):
            return DetectionResult(structure=PageStructure.TREE_LAZY, confidence=0.8)

    before = _snap(_win(), _ctrl("n", "node"))
    obs = _StubObserver([before])
    ex, real = _executor()
    r = scroll_until_visible(
        target_text_re="目标",
        direction="down",
        detector=NoPlanDetector(),
        executor=ex,
        observer=obs,
        verifier=_StubVerify(),
        max_steps=5,
    )
    assert r.reason is Reason.NOT_DISPATCHED
    assert r.dispatched is False and r.moved is False
    assert real.calls == []


def test_not_dispatched_on_dispatch_failure():
    before = _snap(_win(), _ctrl("r1", "row"))
    after = _snap(_win(), _ctrl("r1", "row"), _ctrl("r2", "目标"))
    obs = _StubObserver([before, after])
    ex, real = _executor(ok=False)
    r = scroll_until_visible(
        target_text_re="目标",
        direction="down",
        detector=_infinite_detector(),
        executor=ex,
        observer=obs,
        verifier=_StubVerify(changed=True),
        max_steps=5,
    )
    assert r.reason is Reason.NOT_DISPATCHED
    assert r.moved is False


def test_verify_false_never_reports_moved_true():
    """verify=False: found outcome keeps moved=False (unverified movement),
    even though the target was observed after a real dispatch."""
    before = _snap(_win(), _ctrl("r1", "row"))
    after = _snap(_win(), _ctrl("r1", "row"), _ctrl("r2", "目标行"))
    obs = _StubObserver([before, after])
    ex, real = _executor()
    r = scroll_until_visible(
        target_text_re="目标行",
        direction="down",
        detector=_infinite_detector(),
        executor=ex,
        observer=obs,
        verifier=_StubVerify(changed=True),
        verify=False,
        max_steps=5,
    )
    assert r.reason is Reason.TARGET_VISIBLE
    assert r.dispatched is True
    assert r.moved is False  # never moved=True under verify=False
    assert r.success is False


def test_verify_false_never_found_counts_all_steps_then_not_found():
    snapshot = _snap(_win(), _ctrl("r1", "row"))
    obs = _StubObserver([snapshot])
    ex, _ = _executor()
    r = scroll_until_visible(
        target_text_re="NOPE",
        direction="down",
        detector=_infinite_detector(),
        executor=ex,
        observer=obs,
        verifier=_StubVerify(changed=True),
        verify=False,
        max_steps=3,
    )
    assert r.reason is Reason.TARGET_NOT_FOUND
    assert r.moved is False
    assert len(ex.adapter._dispatch.calls) == 3


def test_invalid_args_raise():
    from scroll.actions import DispatcherAdapter

    def _mk():
        ex, _ = _executor()
        return dict(
            detector=_infinite_detector(),
            executor=ex,
            observer=_StubObserver([_snap(_win())]),
            verifier=_StubVerify(),
        )

    with pytest.raises(ValueError):
        scroll_until_visible(target_text_re="x", direction="sideways", **_mk())
    with pytest.raises(ValueError):
        scroll_until_visible(target_text_re="x", direction="down", max_steps=0, **_mk())
    with pytest.raises(ValueError):
        scroll_until_visible(target_text_re="", direction="down", **_mk())
