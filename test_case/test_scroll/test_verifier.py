"""
PR3 acceptance — PageVerifier tri-state + ScrollCoordinator facade wiring
(docs/todo/scroll-and-paged-table-actions.md, enforceability item 3).
"""

from __future__ import annotations

import copy


from action_dispatcher.receipts import ActionReceipt
from observations.models import Target
from observations.snapshot import build_snapshot
from scroll.actions import DispatcherAdapter
from scroll.controller import NavigationMode, PageController
from scroll.detect import (
    DetectedControl,
    DetectionResult,
    PageChange,
    PageDetector,
    PageStructure,
)
from scroll.executor import ScrollExecutor
from scroll.policy import ScrollPolicy
from scroll.results import Reason
from scroll.verifier import ScrollCoordinator, PageVerifier


def _snap(targets):
    return build_snapshot(targets=targets, backend="x", host="h",
                          captured_at="2026-08-01T00:00:00Z")


def _win():
    return {"process_name": "X", "pid": 1, "native_window_id": "w",
            "title": "Main", "kind": "window"}


def _btn(title, aid, text):
    return {"process_name": "X", "pid": 1, "native_window_id": "w",
            "title": title, "kind": "control", "control_type": "Button",
            "automation_id": aid, "text": text}


# ---------------------------------------------------------------------------
# PageVerifier tri-state
# ---------------------------------------------------------------------------


def test_verifier_changed_on_different_digest():
    before = _snap([_win(), _btn("next", "nextPageButton", "下一页")])
    after_targets = copy.deepcopy([_win(), _btn("next", "nextPageButton", "下一页")])
    after_targets[1]["text"] = "第二页"
    after = _snap(after_targets)
    v = PageVerifier().page_changed(before, after)
    assert v is PageChange.CHANGED


def test_verifier_unchanged_on_identical():
    d = _snap([_win(), _btn("next", "nextPageButton", "下一页")])
    v = PageVerifier().page_changed(d, d)
    assert v is PageChange.UNCHANGED


def test_verifier_uncertain_on_virtualized_reuse():
    """Same digest + repeated identical rows (recycling) => UNCERTAIN."""
    before = _snap([_win(),
                    _btn("r1", "row", "value"), _btn("r2", "row", "value"),
                    _btn("r3", "row", "value")])
    after = _snap([_win(),
                   _btn("r1", "row", "value"), _btn("r2", "row", "value"),
                   _btn("r3", "row", "value")])
    v = PageVerifier().page_changed(before, after)
    # Same digest + repeated identical rows -> cannot tell a real shift.
    assert v is PageChange.UNCERTAIN


def test_verifier_not_uncertain_for_unique_rows():
    """Unique rows with equal digest => UNCHANGED, not UNCERTAIN."""
    before = _snap([_win(), _btn("n", "nextPageButton", "下一页")])
    after = _snap([_win(), _btn("n", "nextPageButton", "下一页")])
    assert PageVerifier().page_changed(before, after) is PageChange.UNCHANGED


# ---------------------------------------------------------------------------
# ScrollCoordinator.step — Detector -> Controller -> Verifier once
# ---------------------------------------------------------------------------


def _dc():
    return DetectedControl(
        target=Target(target_id="T0001", kind="control", process_name="X",
                      pid=1, native_window_id="w", title="next",
                      control_type="Button", automation_id="nextPageButton"),
        enabled=True,
    )


class _SpyDetector(PageDetector):
    """Overrides detect() to record calls (prove step does NOT re-detect)."""

    def __init__(self, result):
        super().__init__()
        self.result = result
        self.calls = []

    def detect(self, snapshot):
        self.calls.append(snapshot)
        return self.result


class _StubObserver:
    """snapshot() returns the fixed AFTER snapshot (simulating moved content)."""

    def __init__(self, after):
        self.after = after
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        return self.after


class _RealDispatcher:
    """A real-signature dispatcher (dispatch(action_id, *, ...) -> ActionReceipt)
    that records how it was called; the adapter/executor calls through to it."""

    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def __call__(self, action_id, *, action_code=None, args=None, target_ref=None):
        self.calls.append(
            (action_id, action_code, dict(args or {}), dict(target_ref or {}))
        )
        if self.ok:
            return ActionReceipt.from_ok(
                action_id=action_id, action_code=action_code,
                request_id=None, result=args,
            )
        return _err_receipt()


def _err_receipt():
    return ActionReceipt.from_error(code="backend_error", action_id="gui.click",
                                    action_code="A020", request_id=None,
                                    message="boom")


def _navigator(before, after, result=None, dispatch_ok=True):
    if result is None:
        result = DetectionResult(
            structure=PageStructure.PAGINATED,
            next_controls=(_dc(),),
            confidence=0.95,
        )
    detector = _SpyDetector(result)
    observer = _StubObserver(after)
    real = _RealDispatcher(ok=dispatch_ok)
    executor = ScrollExecutor(DispatcherAdapter(dispatch=real))
    nav = ScrollCoordinator(detector, PageController(), PageVerifier(),
                           observer, executor)
    nav.set_detection(result)  # machine's CLASSIFY/OWNERSHIP ran before step
    return nav, detector, observer, real


# -- step does not re-detect; verify is mandatory; order is respected --------


def test_step_does_not_redetect():
    before = _snap([_win(), _btn("next", "nextPageButton", "下一页")])
    after = _snap([_win(), _btn("next", "nextPageButton", "第二页")])
    nav, detector, _, _ = _navigator(before, after)
    nav.step(NavigationMode.MOVE_NEXT, before, ScrollPolicy())
    assert detector.calls == []  # detect NOT invoked from inside step


def test_step_armed_receipt_verified_and_success():
    before = _snap([_win(), _btn("next", "nextPageButton", "下一页")])
    after = _snap([_win(), _btn("next", "nextPageButton", "第二页")])
    nav, _, observer, real = _navigator(before, after)
    r = nav.step(NavigationMode.MOVE_NEXT, before, ScrollPolicy())
    assert r.success and r.moved
    assert r.reason is Reason.NEXT_PAGE
    assert real.calls  # controller plan reached the real dispatcher
    assert real.calls[0][0] == "gui.click"  # dispatched the actual action_id
    # verify (observer.snapshot) ran after dispatch (verify is not optional)
    assert observer.calls == 1


def test_step_no_change_returns_no_effect():
    before = _snap([_win(), _btn("next", "nextPageButton", "下一页")])
    after = _snap([_win(), _btn("next", "nextPageButton", "下一页")])  # same
    nav, _, _, _ = _navigator(before, after)
    r = nav.step(NavigationMode.MOVE_NEXT, before, ScrollPolicy())
    assert not r.moved
    assert r.reason is Reason.NO_SCROLL_EFFECT


def test_step_no_cached_detection_not_dispatched():
    before = _snap([_win(), _btn("next", "nextPageButton", "下一页")])
    nav = ScrollCoordinator(_SpyDetector(None), PageController(), PageVerifier(),
                           _StubObserver(before))
    # No set_detection() call => machine did not run CLASSIFY/OWNERSHIP.
    r = nav.step(NavigationMode.MOVE_NEXT, before, ScrollPolicy())
    assert r.reason is Reason.NOT_DISPATCHED


def test_step_dispatch_failure_not_dispatched_never_moved():
    before = _snap([_win(), _btn("next", "nextPageButton", "下一页")])
    after = _snap([_win(), _btn("next", "nextPageButton", "第二页")])
    nav, _, _, real = _navigator(before, after, dispatch_ok=False)
    r = nav.step(NavigationMode.MOVE_NEXT, before, ScrollPolicy())
    assert r.reason is Reason.NOT_DISPATCHED
    assert not r.moved
    # The controller planned 1 command; dispatch returned ok=False so nothing
    # was armed — a never-armed step must NOT report moved.
    assert len(real.calls) == 1


def test_direction_mapping_prev_uses_prev_code_path():
    """step(NavigationMode.MOVE_PREV) goes through the dedicated prev planner (which for
    a paginated list without a prev control plans [] -> NOT_DISPATCHED, never
    clicking the next control — Review P0 "prev executes next")."""
    before = _snap([_win(), _btn("next", "nextPageButton", "下一页")])
    after = _snap([_win(), _btn("next", "nextPageButton", "第二页")])
    nav, _, _, real = _navigator(before, after)
    r = nav.step(NavigationMode.MOVE_PREV, before, ScrollPolicy())
    assert r.reason is Reason.NOT_DISPATCHED
    assert real.calls == []  # must NOT have dispatched the next/prev click


# ---------------------------------------------------------------------------
# f5: region-scoped verification
# ---------------------------------------------------------------------------


def _btn_with_rect(title, aid, text, rect):
    d = _btn(title, aid, text)
    d["rect"] = rect
    return d


def test_verifier_region_scoped_change_inside_region_is_changed():
    before = _snap([
        _win(),
        _btn_with_rect("next", "nextPageButton", "下一页", (10, 10, 110, 40)),
        _btn_with_rect("row", "row1", "value A", (0, 100, 200, 120)),
    ])
    after_targets = copy.deepcopy([
        _win(),
        _btn_with_rect("next", "nextPageButton", "下一页", (10, 10, 110, 40)),
        _btn_with_rect("row", "row1", "value B", (0, 100, 200, 120)),
    ])
    after = _snap(after_targets)
    v = PageVerifier().page_changed(before, after, region=(0, 90, 220, 130))
    assert v is PageChange.CHANGED


def test_verifier_region_scoped_outside_change_is_unchanged():
    """f5: unrelated window churn OUTSIDE the region must NOT count as a move."""
    before = _snap([
        _win(),
        _btn_with_rect("next", "nextPageButton", "下一页", (10, 10, 110, 40)),
        _btn_with_rect("row", "row1", "value A", (0, 100, 200, 120)),
    ])
    after_targets = copy.deepcopy([
        _win(),
        _btn_with_rect("next", "nextPageButton", "下一页", (10, 10, 110, 40)),
        _btn_with_rect("row", "row1", "value B", (0, 100, 200, 120)),
    ])
    after = _snap(after_targets)
    # Region covers only the next button area; the row change is outside it.
    v = PageVerifier().page_changed(before, after, region=(10, 10, 110, 40))
    assert v is PageChange.UNCHANGED


# ---------------------------------------------------------------------------
# f5: bounded polling/backoff in step
# ---------------------------------------------------------------------------


class _PollingObserver:
    """snapshot() returns `unchanged` for the first `lag` polls, then `changed`."""

    def __init__(self, unchanged, changed, lag=1):
        self.unchanged = unchanged
        self.changed = changed
        self.lag = lag
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        if self.calls <= self.lag:
            return self.unchanged
        return self.changed


def test_step_polls_until_content_change_within_bound():
    """f5: a single dispatch is followed by BOUNDED polling — the verify that
    first reports CHANGED (after the content stabilises) yields success."""
    before = _snap([_win(), _btn("next", "nextPageButton", "下一页")])
    unchanged = _snap([_win(), _btn("next", "nextPageButton", "下一页")])
    changed = _snap([_win(), _btn("next", "nextPageButton", "第二页")])
    result = DetectionResult(
        structure=PageStructure.PAGINATED,
        next_controls=(_dc(),),
        confidence=0.95,
    )
    detector = _SpyDetector(result)
    observer = _PollingObserver(unchanged, changed, lag=1)
    real = _RealDispatcher(ok=True)
    nav = ScrollCoordinator(
        detector, PageController(), PageVerifier(), observer,
        ScrollExecutor(DispatcherAdapter(dispatch=real)),
    )
    nav.set_detection(result)
    policy = ScrollPolicy(max_attempts=3, backoff_initial_ms=0)
    r = nav.step(NavigationMode.MOVE_NEXT, before, policy)
    assert r.success and r.moved
    assert r.reason is Reason.NEXT_PAGE
    assert observer.calls == 2  # one immediate + one poll


def test_step_polls_are_bounded_exhausted_to_no_effect():
    """f5: if content never stabilises within `max_attempts` polls, the step
    returns NO_SCROLL_EFFECT (never a fabricated success)."""
    before = _snap([_win(), _btn("next", "nextPageButton", "下一页")])
    same = _snap([_win(), _btn("next", "nextPageButton", "下一页")])
    result = DetectionResult(
        structure=PageStructure.PAGINATED,
        next_controls=(_dc(),),
        confidence=0.95,
    )
    detector = _SpyDetector(result)
    observer = _PollingObserver(same, same, lag=999)  # never changes
    real = _RealDispatcher(ok=True)
    nav = ScrollCoordinator(
        detector, PageController(), PageVerifier(), observer,
        ScrollExecutor(DispatcherAdapter(dispatch=real)),
    )
    nav.set_detection(result)
    policy = ScrollPolicy(max_attempts=3, backoff_initial_ms=0)
    r = nav.step(NavigationMode.MOVE_NEXT, before, policy)
    assert not r.moved
    assert r.reason is Reason.NO_SCROLL_EFFECT
    assert observer.calls == 3  # bounded: exactly max_attempts polls, no more

