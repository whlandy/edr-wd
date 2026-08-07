"""
T2 acceptance — `page_table` pagination-only composite
(docs/todo/scroll-and-paged-table-actions.md, "T2: page_table").

Covers two layers:

  1. `PageTableController` (pure planner): only emits semantic `gui.click`
     (A020); returns `[]` (→ NOT_DISPATCHED) for FLAT / INFINITE / TREE_LAZY,
     a disabled next/prev, or a missing enabled owner. Never produces a
     pointer primitive (A028/A029).
  2. `step_page_table` (executor + bounded verify): exactly one A020 click is
     dispatched for a PAGINATED/LOAD_MORE next and moved is reported only when
     verification observes a real content change; churn is never counted as
     movement.
"""

from __future__ import annotations

import copy

from action_dispatcher.receipts import ActionReceipt
from observations.models import Target
from observations.snapshot import build_snapshot
from scroll.actions import DispatcherAdapter
from scroll.detect import DetectedControl, DetectionResult, PageStructure
from scroll.executor import ScrollExecutor
from scroll.page_table import (
    PageTableController,
    PageTableVerifier,
    step_page_table,
)
from scroll.results import Reason, Strategy

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _btn(title, aid, text, target_id="T"):
    return {"process_name": "X", "pid": 1, "native_window_id": "w",
            "title": title, "kind": "control", "control_type": "Button",
            "automation_id": aid, "text": text}


def _win():
    return {"process_name": "X", "pid": 1, "native_window_id": "w",
            "title": "Main", "kind": "window"}


def _snap(targets):
    return build_snapshot(targets=targets, backend="x", host="h",
                          captured_at="2026-08-01T00:00:00Z")


def _dc(target_id="T0001", enabled=True):
    return DetectedControl(
        target=Target(target_id=target_id, kind="control", process_name="X",
                      pid=1, native_window_id="w", title="next",
                      control_type="Button", automation_id="nextPageButton"),
        enabled=enabled,
    )


def _prev(target_id="T0000", enabled=True):
    return DetectedControl(
        target=Target(target_id=target_id, kind="control", process_name="X",
                      pid=1, native_window_id="w", title="prev",
                      control_type="Button", automation_id="prevPageButton"),
        enabled=enabled,
    )


def _paginated(enabled=True, prev_enabled=True):
    kwargs = {"next_controls": (_dc(enabled=enabled),)}
    if prev_enabled is not None:
        kwargs["prev_controls"] = (_prev(enabled=prev_enabled),)
    return DetectionResult(structure=PageStructure.PAGINATED, confidence=0.95,
                           **kwargs)


def _load_more(enabled=True):
    return DetectionResult(structure=PageStructure.LOAD_MORE,
                           next_controls=(_dc(enabled=enabled),),
                           confidence=0.9)


def _struct(structure):
    return DetectionResult(structure=structure, confidence=0.8)


def _codes(plan):
    return [a.action_code for a in plan]


class _StubObserver:
    """snapshot() returns the fixed AFTER snapshot (simulated moved content)."""

    def __init__(self, after):
        self.after = after
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        return self.after


class _RealDispatcher:
    """Real-signature dispatcher (dispatch(action_id, *, ...) -> ActionReceipt)
    that records how it was called; the adapter/executor calls through to it."""

    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def __call__(self, action_id, *, action_code=None, args=None, target_ref=None):
        self.calls.append((action_id, action_code))
        if self.ok:
            return ActionReceipt.from_ok(
                action_id=action_id, action_code=action_code,
                request_id=None, result=args,
            )
        return ActionReceipt.from_error(
            code="backend_error", action_id=action_id, action_code=action_code,
            request_id=None, message="boom",
        )


def _executor(dispatch_ok=True):
    return ScrollExecutor(DispatcherAdapter(dispatch=_RealDispatcher(ok=dispatch_ok)))


# ---------------------------------------------------------------------------
# PageTableController — pure planner
# ---------------------------------------------------------------------------


def test_controller_paginated_next_emits_exactly_one_a020():
    plan = PageTableController().plan(_paginated(), "next")
    assert len(plan) == 1
    assert _codes(plan) == ["A020"]
    assert plan[0].action_id == "gui.click"
    assert plan[0].target_ref == {"target_id": "T0001"}


def test_controller_load_more_emits_exactly_one_a020():
    plan = PageTableController().plan(_load_more(), "next")
    assert len(plan) == 1
    assert _codes(plan) == ["A020"]


def test_controller_paginated_prev_emits_one_a020_on_prev_control():
    plan = PageTableController().plan(_paginated(), "prev")
    assert len(plan) == 1
    assert _codes(plan) == ["A020"]
    assert plan[0].target_ref == {"target_id": "T0000"}


def test_controller_paginated_first_emits_one_a020_on_prev_control():
    plan = PageTableController().plan(_paginated(), "first")
    assert len(plan) == 1
    assert _codes(plan) == ["A020"]
    assert plan[0].target_ref == {"target_id": "T0000"}


def test_controller_flat_emits_no_pointer_primitive():
    """FLAT → empty plan (NOT_DISPATCHED), and NEVER a wheel/drag fallback."""
    plan = PageTableController().plan(_struct(PageStructure.FLAT), "next")
    assert plan == []


def test_controller_infinite_emits_no_pointer_primitive():
    plan = PageTableController().plan(_struct(PageStructure.INFINITE), "next")
    assert plan == []


def test_controller_tree_lazy_emits_no_pointer_primitive():
    plan = PageTableController().plan(_struct(PageStructure.TREE_LAZY), "next")
    assert plan == []


def test_controller_disabled_next_emits_nothing():
    plan = PageTableController().plan(_paginated(enabled=False), "next")
    assert plan == []


def test_controller_disabled_prev_emits_nothing():
    plan = PageTableController().plan(_paginated(prev_enabled=False), "prev")
    assert plan == []


def test_controller_prev_without_prev_control_already_at_first_page():
    """PAGINATED with a next but no prev control → already on page 1, so
    prev/first plan nothing (NOT_DISPATCHED) rather than clicking next."""
    r = DetectionResult(structure=PageStructure.PAGINATED,
                        next_controls=(_dc(),), confidence=0.95)
    assert PageTableController().plan(r, "prev") == []
    assert PageTableController().plan(r, "first") == []


def test_controller_unknown_direction_raises():
    import pytest

    with pytest.raises(ValueError):
        PageTableController().plan(_paginated(), "sideways")


# ---------------------------------------------------------------------------
# step_page_table — dispatcher + verify
# ---------------------------------------------------------------------------


def test_step_paginated_next_one_click_and_moved_when_verified():
    before = _snap([_win(), _btn("next", "nextPageButton", "下一页")])
    after = _snap([_win(), _btn("next", "nextPageButton", "第二页")])
    obs = _StubObserver(after)
    ex = _executor()
    r = step_page_table(_paginated(), "next", before=before, executor=ex,
                        observer=obs)
    assert r.success and r.moved
    assert r.reason is Reason.NEXT_PAGE
    assert r.strategy_used is Strategy.PAGINATION
    # exactly one A020 semantic click — no wheel/drag fallback.
    assert [(a, c) for a, c in ex.adapter._dispatch.calls] == [("gui.click", "A020")]


def test_step_load_more_one_click_and_moved_when_verified():
    before = _snap([_win(), _btn("more", "loadMoreButton", "加载更多")])
    # loaded an extra row → content digest changes → verifiable movement.
    after = _snap([_win(),
                   _btn("more", "loadMoreButton", "加载更多"),
                   _btn("r9", "row", "loaded-row")])
    real = _RealDispatcher()
    ex = ScrollExecutor(DispatcherAdapter(dispatch=real))
    r = step_page_table(_load_more(), "next", before=before, executor=ex,
                        observer=_StubObserver(after))
    assert r.success and r.moved
    assert r.reason is Reason.NEXT_PAGE
    assert r.dispatched is True
    # exactly one A020 semantic click, never a wheel/drag fallback.
    assert [(a, c) for a, c in real.calls] == [("gui.click", "A020")]


def test_step_flat_no_dispatch_not_dispatched():
    before = _snap([_win(), _btn("box", "searchBox", "type")])
    real = _RealDispatcher()
    ex = ScrollExecutor(DispatcherAdapter(dispatch=real))
    r = step_page_table(_struct(PageStructure.FLAT), "next", before=before,
                        executor=ex, observer=_StubObserver(before))
    assert r.reason is Reason.NOT_DISPATCHED
    assert not r.moved and not r.dispatched
    assert real.calls == []  # no pointer primitive ever emitted


def test_step_infinite_no_dispatch_not_dispatched():
    before = _snap([_win(), _btn("l", "virtualList", "row")])
    real = _RealDispatcher()
    ex = ScrollExecutor(DispatcherAdapter(dispatch=real))
    r = step_page_table(_struct(PageStructure.INFINITE), "next", before=before,
                        executor=ex, observer=_StubObserver(before))
    assert r.reason is Reason.NOT_DISPATCHED
    assert real.calls == []


def test_step_tree_lazy_no_dispatch_not_dispatched():
    before = _snap([_win(), _btn("n", "node", "root")])
    real = _RealDispatcher()
    ex = ScrollExecutor(DispatcherAdapter(dispatch=real))
    r = step_page_table(_struct(PageStructure.TREE_LAZY), "next", before=before,
                        executor=ex, observer=_StubObserver(before))
    assert r.reason is Reason.NOT_DISPATCHED
    assert real.calls == []


def test_step_disabled_next_not_dispatched():
    before = _snap([_win(), _btn("next", "nextPageButton", "下一页")])
    real = _RealDispatcher()
    ex = ScrollExecutor(DispatcherAdapter(dispatch=real))
    r = step_page_table(_paginated(enabled=False), "next", before=before,
                        executor=ex, observer=_StubObserver(before))
    assert r.reason is Reason.NOT_DISPATCHED
    assert real.calls == []  # disabled → nothing armed


def test_step_verification_churn_does_not_count_as_movement():
    """Dispatched but content never changes (churn/transients that never
    stabilise) → NO_SCROLL_EFFECT, never moved=True."""
    before = _snap([_win(), _btn("next", "nextPageButton", "下一页")])
    # after identical → observer never reports a change.
    r = step_page_table(_paginated(), "next", before=before, executor=_executor(),
                        observer=_StubObserver(before),
                        max_attempts=3)
    assert r.dispatched is True
    assert r.moved is False
    assert r.reason is Reason.NO_SCROLL_EFFECT
    assert r.success is False


def test_step_dispatch_failure_not_dispatched():
    before = _snap([_win(), _btn("next", "nextPageButton", "下一页")])
    after = _snap([_win(), _btn("next", "nextPageButton", "第二页")])
    real = _RealDispatcher(ok=False)
    ex = ScrollExecutor(DispatcherAdapter(dispatch=real))
    r = step_page_table(_paginated(), "next", before=before, executor=ex,
                        observer=_StubObserver(after))
    assert r.reason is Reason.NOT_DISPATCHED
    assert not r.moved


def test_step_verify_false_never_returns_moved():
    """verify=False: we never observed movement, so it must not report
    moved=True — returns the honest dispatched-but-unverified NO_SCROLL_EFFECT."""
    before = _snap([_win(), _btn("next", "nextPageButton", "下一页")])
    real = _RealDispatcher()
    ex = ScrollExecutor(DispatcherAdapter(dispatch=real))
    r = step_page_table(_paginated(), "next", before=before, executor=ex,
                        observer=_StubObserver(before), verify=False)
    assert r.dispatched is True
    assert r.moved is False
    assert r.success is False
    assert r.reason is Reason.NO_SCROLL_EFFECT


def test_step_verifier_requires_observer():
    """PageTableVerifier without an observer fails loudly rather than guessing
    whether content moved (verify=True with no observer is a wiring bug)."""
    import pytest

    before = _snap([_win(), _btn("next", "nextPageButton", "下一页")])
    with pytest.raises(ValueError):
        step_page_table(_paginated(), "next", before=before, executor=_executor(),
                        observer=None, max_attempts=1)
