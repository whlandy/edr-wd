"""
P0-1 acceptance — PlannedAction + DispatcherAdapter bridge
(Review P0 #2: ActionReceipt must never be fabricated as a command; the
adapter is the only place that calls the real dispatcher.)
"""

from __future__ import annotations

import pytest


from action_dispatcher.receipts import CODE_OK, ActionReceipt
from scroll.actions import DispatcherAdapter, PlannedAction


# ---------------------------------------------------------------------------
# PlannedAction carries the real dispatch() kwargs
# ---------------------------------------------------------------------------


def test_planned_action_produces_dispatch_kwargs():
    act = PlannedAction(
        action_id="gui.click",
        action_code="A020",
        args={"x": 10, "y": 20},
        target_ref={"snapshot_id": "s1", "target_id": "T0001"},
    )
    kw = act.dispatch_kwargs()
    assert kw["action_code"] == "A020"
    assert kw["args"] == {"x": 10, "y": 20}
    assert kw["target_ref"] == {"snapshot_id": "s1", "target_id": "T0001"}


def test_planned_action_defaults_to_empty_action_code_and_none_target():
    act = PlannedAction(action_id="pointer.scroll", args={"clicks": -3})
    kw = act.dispatch_kwargs()
    assert kw["action_code"] is None
    assert kw["target_ref"] is None


def test_planned_action_is_frozen():
    act = PlannedAction(action_id="x", args={})
    with pytest.raises(Exception):
        act.action_id = "y"  # type: ignore[misc]


def test_planned_action_is_keyword_only_guards_positional_misbinding():
    """Constructing `PlannedAction(\"gui.click\", \"A020\", {})` would silently
    bind \"A020\" to `args` — kw_only forbids that footgun entirely."""
    with pytest.raises(TypeError):
        PlannedAction("gui.click", "A020", {})  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Adapter bridges to a real-style dispatch(action_id, *, ...) -> ActionReceipt
# ---------------------------------------------------------------------------


def _recorded_dispatcher():
    calls = []

    def dispatch(action_id, *, action_code=None, args=None, target_ref=None):
        calls.append(
            (action_id, action_code, dict(args or {}), dict(target_ref or {}))
        )
        return ActionReceipt.from_ok(
            action_id=action_id, action_code=action_code,
            request_id=None, result=args,
        )

    return dispatch, calls


def test_adapter_calls_dispatcher_with_dispatch_shape():
    dispatch, calls = _recorded_dispatcher()
    adapter = DispatcherAdapter(dispatch=dispatch)
    act = PlannedAction(action_id="gui.click", action_code="A020",
                        args={"x": 1}, target_ref={"target_id": "T1"})
    receipt = adapter.execute(act)
    assert isinstance(receipt, ActionReceipt)
    assert receipt.ok is True
    assert receipt.code == CODE_OK
    assert calls == [("gui.click", "A020", {"x": 1}, {"target_id": "T1"})]


def test_adapter_executes_only_the_planned_action():
    """The adapter is mechanical: it sends exactly the PlannedAction's fields,
    never fabricates a success on its own."""
    dispatch, calls = _recorded_dispatcher()
    adapter = DispatcherAdapter(dispatch=dispatch)
    assert adapter.execute(PlannedAction(action_id="a", args={"k": "v"})).ok is True
    assert calls == [("a", None, {"k": "v"}, {})]


def test_adapter_execute_all_preserves_order_and_returns_receipts():
    dispatch, calls = _recorded_dispatcher()
    adapter = DispatcherAdapter(dispatch=dispatch)
    plan = [
        PlannedAction(action_id="pointer.scroll", args={"clicks": -3}),
        PlannedAction(action_id="gui.click", args={"x": 0}, action_code="A020"),
    ]
    receipts = adapter.execute_all(plan)
    assert len(receipts) == 2
    assert all(isinstance(r, ActionReceipt) for r in receipts)
    assert all(r.ok for r in receipts)
    assert [c[0] for c in calls] == ["pointer.scroll", "gui.click"]


# ---------------------------------------------------------------------------
# Adapter passes dispatcher failures through untouched (never fakes success)
# ---------------------------------------------------------------------------


def test_adapter_passes_failed_receipt_through():
    def dispatch(action_id, **kwargs):
        return ActionReceipt.from_error(
            code="backend_error", action_id=action_id, message="boom",
        )

    adapter = DispatcherAdapter(dispatch=dispatch)
    receipt = adapter.execute(PlannedAction(action_id="gui.click", args={}))
    assert isinstance(receipt, ActionReceipt)
    assert receipt.ok is False
    assert receipt.code == "backend_error"
